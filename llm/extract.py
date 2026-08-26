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


_calls_made = 0


def reset_budget() -> None:
    """Her worker koşusunun başında çağrılır."""
    global _calls_made
    _calls_made = 0


def calls_made() -> int:
    return _calls_made


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
) -> list[BaseModel]:
    """HTML'den yapılandırılmış kayıt çıkarır ve HER SONUCU kalıcı olarak kaydeder.

    Çağrının yapılmadığı durumlar da (`disabled`, `budget_exceeded`) yazılır:
    "fallback neden devreye girmedi" sorusunun cevabı ve sıfır token
    harcandığının kanıtı olur.
    """
    global _calls_made

    from store.observability import record_llm_call  # gecikmeli: llm -> store bağı

    if not settings.LLM_FALLBACK_ENABLED:
        record_llm_call(
            trigger_error=trigger_error, trigger_source=trigger_source,
            model=settings.LLM_MODEL, status="disabled", collector=collector, run_id=run_id,
            error="LLM_FALLBACK_ENABLED=0",
        )
        raise LlmDisabled(
            "LLM fallback kapalı. Açmak için .env: LLM_FALLBACK_ENABLED=1 "
            "(ve LLM_API_KEY dolu olmalı)."
        )
    # `budget` verilmezse onarım fallback'inin dar sınırı geçerlidir.
    limit = settings.LLM_MAX_CALLS_PER_RUN if budget is None else budget
    if _calls_made >= limit:
        record_llm_call(
            trigger_error=trigger_error, trigger_source=trigger_source,
            model=settings.LLM_MODEL, status="budget_exceeded", collector=collector,
            run_id=run_id,
            error=f"koşu başına sınır: {limit}",
        )
        raise LlmBudgetExceeded(
            f"Bu koşuda LLM çağrı sınırına ulaşıldı ({limit}). "
            "Sınır .env'de LLM_MAX_CALLS_PER_RUN ile değiştirilir."
        )

    trimmed = _condense(html, settings.LLM_MAX_INPUT_CHARS)
    wrapper = create_model("Rows", rows=(list[schema], ...))

    _calls_made += 1
    started = time.monotonic()
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=settings.LLM_MODEL,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": wrapper.model_json_schema()},
            },
            messages=[{"role": "user", "content": (prompt or PROMPT).format(html=trimmed)}],
            temperature=0,
            max_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 - başarısız çağrı da kaydedilmeli
        record_llm_call(
            trigger_error=trigger_error, trigger_source=trigger_source,
            model=settings.LLM_MODEL, status="failed", collector=collector, run_id=run_id,
            input_chars=len(trimmed), error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise

    usage = getattr(resp, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    completion_tokens = getattr(usage, "completion_tokens", None) if usage else None

    try:
        content = resp.choices[0].message.content or ""
        parsed = wrapper.model_validate(json.loads(content))
        rows = list(parsed.rows)
    except Exception as exc:  # noqa: BLE001 - token yandı ama sonuç kullanılamadı
        record_llm_call(
            trigger_error=trigger_error, trigger_source=trigger_source,
            model=settings.LLM_MODEL, status="failed", collector=collector, run_id=run_id,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            input_chars=len(trimmed), error=f"yanıt ayrıştırılamadı: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise

    record_llm_call(
        trigger_error=trigger_error, trigger_source=trigger_source,
        model=settings.LLM_MODEL, status="ok", collector=collector, run_id=run_id,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        input_chars=len(trimmed), rows_recovered=len(rows),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    logger.warning(
        "LLM fallback kullanıldı: collector=%s model=%s girdi=%s çıktı=%s token, %s kayıt",
        collector, settings.LLM_MODEL, prompt_tokens, completion_tokens, len(rows),
    )
    return rows
