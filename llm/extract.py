"""Tek atışlık çıkarım — agent değil. Model hesap yapmaz, karar vermez,
oran uydurmaz, sayfada gezinmez. Tek işi elindeki HTML'den yapılandırılmış
veri çıkarmak (bkz. tasarım dokümanı §06).

Yalnızca Collector.parse() istisna attığında veya sıfır kayıt döndürdüğünde
çağrılır. Normal akışta tek bir token harcanmaz.

TOKEN DİSİPLİNİ (kullanıcı isteği: "takılırsa token harcamasın"):
  1. Fallback varsayılan olarak KAPALI (LLM_FALLBACK_ENABLED).
  2. Girdi önce script/style temizlenip TABLO/SAYI içeren bölgeye daraltılır,
     sonra sert bir karakter sınırına kırpılır.
  3. Yanıt max_tokens ile sınırlı.
  4. Tekrar deneme YOK (provider'da max_retries=0).
  5. Koşu başına çağrı sayısı sınırlı — bir toplayıcı sürekli kırılsa bile
     fallback koşu başına bir kez denenir, sonra susar.
  6. Çıkan kayıtlar yine sanity_check'ten geçer; model bir sayı uydurursa
     veritabanına YAZILMAZ.
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar

from pydantic import BaseModel, create_model

from llm import settings
from llm.provider import get_client

logger = logging.getLogger(__name__)

PROMPT = """Aşağıdaki HTML parçasından, verilen JSON şemasına uyan kayıtları çıkar.
Hesap yapma, oran uydurma, sayfada olmayan bilgi ekleme — yalnızca sayfada
gördüğünü yapılandır. Emin olmadığın kaydı hiç üretme.
Yalnızca JSON döndür, açıklama yazma.

HTML:
{html}
"""

_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_WS_RE = re.compile(r"[ \t]*\n[ \t\n]*")
# Oran/tutar tabloları neredeyse her zaman bu işaretlerin yakınında olur.
_SIGNAL_RE = re.compile(r"%|\bgün\b|\bvade\b|\boran\b|\bfaiz\b|\bkar\s*pay", re.IGNORECASE)


class LlmBudgetExceeded(RuntimeError):
    """Bu koşuda izin verilen LLM çağrısı sayısı doldu."""


class LlmDisabled(RuntimeError):
    """Fallback açık değil — .env'de LLM_FALLBACK_ENABLED=1 gerekiyor."""


# Sayaç ContextVar: modül globali OLAMAZ. Panelin "Şimdi tazele" düğmesi
# uzun ömürlü Streamlit sürecinde koşuyor ve Streamlit her tarayıcı
# oturumunu AYNI SÜREÇTE ayrı bir iş parçacığında çalıştırıyor. Global
# sayaçla iki ayrı ziyaretçinin koşusu birbirinin bütçesini tüketir, biri
# hiç çağrı yapamadan "sınıra ulaşıldı" hatası alırdı.
_calls_made: ContextVar[int] = ContextVar("llm_calls_made", default=0)


#: Bu koşuda denenmiş veritabanı kimlikleri — aynı anahtarı iki kez
#: denememek ve düşme döngüsünü sınırlamak için.
_tried_credentials: ContextVar[frozenset] = ContextVar(
    "llm_tried_credentials", default=frozenset()
)


def reset_budget() -> None:
    """Her koşunun BAŞINDA çağrılır (worker, scheduler ve panel)."""
    _calls_made.set(0)
    _tried_credentials.set(frozenset())


def calls_made() -> int:
    return _calls_made.get()


def _retry_with_next_credential(exc: Exception) -> bool:
    """Hata anahtarın kendisindeyse bir sonraki kayıtlı anahtara geçer.

    Dönen True: bağlam yeni anahtara ayarlandı, çağıran bir kez daha
    denemeli. False: ya hata anahtarla ilgili değil, ya da denenecek başka
    anahtar kalmadı.

    Bütçe sayacı BİLEREK geri alınmıyor: kimlik hatası token harcamaz ama
    sayacı geri almak, bozuk bir anahtar zinciriyle sınırsız deneme
    döngüsü açardı.
    """
    from llm import credentials

    if not credentials.is_credential_error(exc):
        return False
    kullanilan = settings.current_credential_id()
    if kullanilan is None:
        # Oturum anahtarı ya da .env anahtarı — düşecek bir zincir yok.
        return False
    credentials.mark_failed(kullanilan, str(exc))
    denenen = set(_tried_credentials.get()) | {kullanilan}
    _tried_credentials.set(frozenset(denenen))
    sonraki = credentials.first_untried(denenen)
    if sonraki is None:
        logger.warning("kimlik #%s başarısız ve denenmemiş yedek anahtar yok", kullanilan)
        return False
    logger.warning(
        "kimlik #%s kullanılamadı (%s) — #%s ile yeniden deneniyor",
        kullanilan, str(exc)[:120], sonraki.id,
    )
    # KİMLİK HATASI TOKEN HARCAMAZ, bu yüzden bütçe sayacı geri alınıyor;
    # aksi halde bütçesi 1 olan onarım fallback'i yedek anahtarı hiç
    # deneyemeden "sınıra ulaşıldı" derdi. Döngü riski yok: her anahtar
    # `denenen` kümesi sayesinde en fazla bir kez denenir.
    _calls_made.set(max(0, _calls_made.get() - 1))
    # Bağlamı KALICI olarak değiştiriyoruz (context manager değil): çağıran
    # aynı koşu içinde tekrar deneyecek ve koşu bitince süreç bağlamı zaten
    # yeni bir koşuya geçecek.
    settings._api_key_override.set(sonraki.api_key)
    settings._base_url_override.set(sonraki.base_url)
    settings._model_override.set(sonraki.model)
    settings._extra_body_override.set(sonraki.extra_body)
    settings._credential_id.set(sonraki.id)
    return True


def _mark_current_credential_ok() -> None:
    from llm import credentials

    kullanilan = settings.current_credential_id()
    if kullanilan is not None:
        credentials.mark_ok(kullanilan)


def _condense(html: str, max_chars: int) -> str:
    """script/style/yorum at, sonra SİNYALLİ bölgeye daralt.

    Ham HTML'in ilk N karakterini almak neredeyse her zaman <head> ve menüyü
    gönderir — yani tokenin tamamı çöpe gider. Bunun yerine oran işaretlerinin
    (%, gün, vade, oran, faiz) geçtiği ilk yerden itibaren kırpılır.
    """
    cleaned = _COMMENT_RE.sub("", _SCRIPT_RE.sub("", html))
    cleaned = _WS_RE.sub("\n", cleaned).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    match = _SIGNAL_RE.search(cleaned)
    start = max(0, match.start() - max_chars // 4) if match else 0
    return cleaned[start : start + max_chars]


def extract(
    html: str,
    schema: type[BaseModel],
    *,
    collector: str | None = None,
    run_id: int | None = None,
    trigger_error: str | None = None,
    trigger_source: str | None = None,
    budget: int | None = None,
    prompt: str | None = None,
    _credential_retry: bool = False,
) -> list[BaseModel]:
    """HTML'den yapılandırılmış kayıt çıkarır ve HER SONUCU kalıcı olarak kaydeder.

    Çağrının yapılmadığı durumlar da (`disabled`, `budget_exceeded`) yazılır:
    "fallback neden devreye girmedi" sorusunun cevabı ve sıfır token
    harcandığının kanıtı olur.
    """
    from store.observability import record_llm_call  # gecikmeli: llm -> store bağı

    if not settings.fallback_enabled():
        record_llm_call(
            trigger_error=trigger_error, trigger_source=trigger_source,
            model=settings.current_model(), status="disabled", collector=collector, run_id=run_id,
            error="LLM_FALLBACK_ENABLED=0",
        )
        raise LlmDisabled(
            "LLM fallback kapalı. Açmak için .env: LLM_FALLBACK_ENABLED=1 "
            "(ve LLM_API_KEY dolu olmalı)."
        )
    # `budget` verilmezse onarım fallback'inin dar sınırı geçerlidir.
    limit = settings.LLM_MAX_CALLS_PER_RUN if budget is None else budget
    yapilan = _calls_made.get()
    if yapilan >= limit:
        record_llm_call(
            trigger_error=trigger_error, trigger_source=trigger_source,
            model=settings.current_model(), status="budget_exceeded", collector=collector,
            run_id=run_id,
            error=f"koşu başına sınır: {limit}",
        )
        raise LlmBudgetExceeded(
            f"Bu koşuda LLM çağrı sınırına ulaşıldı ({limit}). "
            "Sınır .env'de LLM_MAX_CALLS_PER_RUN ile değiştirilir."
        )

    trimmed = _condense(html, settings.LLM_MAX_INPUT_CHARS)
    wrapper = create_model("Rows", rows=(list[schema], ...))

    _calls_made.set(yapilan + 1)
    started = time.monotonic()
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=settings.current_model(),
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": wrapper.model_json_schema()},
            },
            messages=[{"role": "user", "content": (prompt or PROMPT).format(html=trimmed)}],
            temperature=0,
            max_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
            # Sağlayıcıya özel alanlar (ör. Qwen'de enable_thinking=False).
            # Boşsa hiçbir şey eklenmez — Gemini'de olduğu gibi.
            extra_body=settings.current_extra_body() or None,
        )
    except Exception as exc:  # noqa: BLE001 - başarısız çağrı da kaydedilmeli
        record_llm_call(
            trigger_error=trigger_error, trigger_source=trigger_source,
            model=settings.current_model(), status="failed", collector=collector, run_id=run_id,
            input_chars=len(trimmed), error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        # ANAHTAR DÜŞÜRME: anahtarın kotası dolduysa ya da iptal edildiyse
        # elde başka anahtar varken koşuyu düşürmek gereksiz veri kaybı.
        # YALNIZCA kimlik hatalarında (401/403/429) düşülür — zaman aşımı ya
        # da 500 sağlayıcının geçici arızasıdır ve sağlam bir anahtarı
        # boşuna 'failed' işaretlemek onu zincirin sonuna atardı.
        if _retry_with_next_credential(exc):
            return extract(
                html, schema, collector=collector, run_id=run_id,
                trigger_error=trigger_error, trigger_source=trigger_source,
                budget=budget, prompt=prompt, _credential_retry=True,
            )
        raise

    usage = getattr(resp, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    completion_tokens = getattr(usage, "completion_tokens", None) if usage else None

    try:
        content = resp.choices[0].message.content or ""
        ham = json.loads(content)
        # SATIR BAZINDA DOĞRULAMA — tek bozuk satır diğerlerini götürmesin.
        #
        # Eskiden tüm liste `wrapper.model_validate` ile bir kerede
        # doğrulanıyordu: bir satır şema kontrolüne takılınca (ör. bant
        # dışı oran) İSTİSNA yükseliyor ve GEÇERLİ satırlar da dahil olmak
        # üzere yanıtın tamamı çöpe gidiyordu — harcanan token'la birlikte.
        #
        # 2026-09-05'te canlıda görüldü: DenizBank taşıt sayfasında model
        # 5 satır döndürdü, 4'ü kredi/değer oranını (%70, %50, %30) aylık
        # faiz sanmıştı ve bant kontrolü onları haklı olarak reddetti.
        # Ama 5. satır geçerliydi; hepsi birden atıldığı için DenizBank'ın
        # taşıt kredisi tablodan tamamen kayboldu.
        #
        # Elemenin kendisi doğru; yanlış olan geçerli satırları da elemek.
        # Zeminleme ve erişilebilirlik kontrolleri bunun ARDINDAN yine
        # çalışıyor, yani gevşeme yok.
        satirlar = ham.get("rows") if isinstance(ham, dict) else ham
        if not isinstance(satirlar, list):
            raise ValueError(f"beklenen liste değil: {type(satirlar).__name__}")
        rows, elenen = [], []
        for ham_satir in satirlar:
            try:
                rows.append(schema.model_validate(ham_satir))
            except Exception as satir_hata:  # noqa: BLE001
                elenen.append(str(satir_hata).split("\n")[0][:120])
        if satirlar and not rows:
            raise ValueError(
                f"{len(satirlar)} satırın hiçbiri şemaya uymadı: {'; '.join(elenen[:3])}"
            )
        if elenen:
            logger.warning(
                "%s/%s: %s satır şemaya uymadı, elendi (%s geçerli): %s",
                collector, trigger_source, len(elenen), len(rows), "; ".join(elenen[:3]),
            )
    except Exception as exc:  # noqa: BLE001 - token yandı ama sonuç kullanılamadı
        record_llm_call(
            trigger_error=trigger_error, trigger_source=trigger_source,
            model=settings.current_model(), status="failed", collector=collector, run_id=run_id,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            input_chars=len(trimmed), error=f"yanıt ayrıştırılamadı: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise

    record_llm_call(
        trigger_error=trigger_error, trigger_source=trigger_source,
        model=settings.current_model(), status="ok", collector=collector, run_id=run_id,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        input_chars=len(trimmed), rows_recovered=len(rows),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    # Çalışan anahtar 'failed' damgası taşıyorsa (kotası yenilenmiş olabilir)
    # temizlensin; aksi halde zincirin sonunda kalmaya devam ederdi.
    _mark_current_credential_ok()
    logger.warning(
        "LLM fallback kullanıldı: collector=%s model=%s girdi=%s çıktı=%s token, %s kayıt",
        collector, settings.current_model(), prompt_tokens, completion_tokens, len(rows),
    )
    return rows
