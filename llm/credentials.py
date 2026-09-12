"""Panelden kaydedilen LLM kimlik bilgileri — kaydet, sırayla dene, düşür.

ÜÇ KURAL:

  1. KAYDETMEDEN ÖNCE TEST. Çalışmayan bir anahtarı kaydetmek, sistemi
     "anahtar var ama hiçbir şey çalışmıyor" durumuna sokar ve bu durumun
     teşhisi zordur: panel anahtarı gösterir, koşular sessizce başarısız
     olur. Test tek atışlık ve en küçük istek (max_tokens=1).

  2. EN SON GİRİLEN KULLANILIR. Kullanıcı yeni anahtar giriyorsa eskisi
     bitmiş demektir; en yeniyi tercih etmek beklenen davranış.

  3. KULLANILAMAYAN ANAHTAR BİR ÖNCEKİNE DÜŞER. Kota dolması ya da
     anahtarın iptali koşu ANINDA ortaya çıkar; o an elde başka anahtar
     varken koşuyu düşürmek gereksiz veri kaybı olurdu. Yalnızca KİMLİK
     hatalarında düşülür (401/403/429): zaman aşımı ya da 500 sağlayıcının
     geçici arızasıdır, anahtarı suçlamak yanlış olur ve sağlam bir
     anahtarı boşuna 'failed' işaretlerdi.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from store.clock import utc_now
from store.db import SessionLocal
from store.models import LlmCredential

logger = logging.getLogger(__name__)

#: Anahtarın KENDİSİNİN sorunlu olduğunu gösteren HTTP kodları. Bunlarda
#: bir sonraki anahtara düşülür; diğer hatalarda düşülmez.
KIMLIK_HATA_KODLARI = frozenset({401, 403, 429})

#: `.env`'deki anahtarın birleşik zincirdeki sabit id'si. Veritabanı
#: satırları 1'den başlayıp autoincrement olduğu için 0 hiç çakışmaz.
#: `mark_failed(0, ...)` / `mark_ok(0, ...)` zaten var olmayan bir satırı
#: arayıp sessizce hiçbir şey yapmaz (bkz. aşağıdaki iki fonksiyon) — bu
#: bilinçli: `.env`'in kimlik hatası veritabanına YAZILMAZ, çünkü onu
#: düzeltmenin tek yolu dosyayı değiştirip konteyneri yeniden başlatmak;
#: kalıcı bir 'failed' damgası kullanıcı düzeltse bile hiç silinmezdi.
ENV_CREDENTIAL_ID = 0


def hash_code(code: str) -> str:
    """Silme kodunun sunucuda saklanan biçimi.

    KOD DEĞİL ÖZET saklanıyor: veritabanını okuyabilen biri (ya da bir
    yedek dosyası) kodların kendisini ele geçirmesin. Tuz yok ve gerekli
    değil — kod zaten sunucunun ürettiği, tahmin edilemez bir rastgele
    dizge; sözlük saldırısına açık bir kullanıcı parolası değil.
    """
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def new_code() -> str:
    """Yeni silme kodu — okunabilir ama tahmin edilemez.

    `token_urlsafe(9)` 12 karakterlik, ~72 bitlik bir dizge veriyor.
    Kullanıcı bunu bir yere not edecek, o yüzden kısa; ama kaba kuvvetle
    denenemeyecek kadar da geniş.
    """
    return secrets.token_urlsafe(9)


@dataclass(frozen=True)
class Credential:
    id: int | None
    api_key: str
    base_url: str
    model: str
    extra_body: dict
    status: str = "ok"
    last_error: str | None = None
    created_at: datetime | None = None
    #: Sahibi belli mi? Kodun kendisi ASLA buraya konmuyor.
    owner_hash: str | None = None

    @property
    def korumali(self) -> bool:
        """Silmek için kod gerekiyor mu? (Eski satırlarda gerekmiyor.)"""
        return bool(self.owner_hash)

    @property
    def masked(self) -> str:
        if not self.api_key:
            return "yok"
        return f"…{self.api_key[-4:]}" if len(self.api_key) > 4 else "…"


def _to_credential(row: LlmCredential) -> Credential:
    try:
        extra = json.loads(row.extra_body) if row.extra_body else {}
        if not isinstance(extra, dict):
            extra = {}
    except ValueError:
        logger.warning("kimlik #%s: extra_body ayrıştırılamadı, boş kabul edildi", row.id)
        extra = {}
    return Credential(
        id=row.id,
        api_key=row.api_key,
        base_url=row.base_url,
        model=row.model,
        extra_body=extra,
        status=row.status,
        last_error=row.last_error,
        created_at=row.created_at,
        owner_hash=row.owner_hash,
    )


def env_credential() -> Credential | None:
    """`.env`'deki anahtar, birleşik zincirde SABİT id=0 ile temsil edilir.

    `llm.settings` modül globallerinden okunuyor (`LLM_API_KEY` vb.),
    çünkü bu, panelden hiç dokunulmamış, sunucu sahibinin doğrudan
    yapılandırdığı anahtardır — `LLM_API_KEY` içeriye ContextVar
    kapsamasız, süreç geneli bir sabittir.
    """
    from llm import settings

    if not settings.LLM_API_KEY:
        return None
    return Credential(
        id=ENV_CREDENTIAL_ID,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        model=settings.LLM_MODEL,
        extra_body=dict(settings.LLM_EXTRA_BODY),
        status="ok",
    )


def chain() -> list[Credential]:
    """Panelden kaydedilen anahtarlar — EN YENİ ÖNCE. `.env` DAHİL DEĞİL.

    Bu liste yalnızca YÖNETİM ekranı için: "Kayıtlı anahtarlar" bölümü
    kullanıcının panelden eklediği satırları listeler/siler. `.env`'deki
    anahtar panelden eklenmedi, silinemez, bu yüzden burada görünmüyor —
    onu `effective_chain()`'de ayrıca en başa ekleniyor.

    'failed' işaretliler listenin SONUNA atılır, atılmaz: anahtarın kotası
    ertesi gün yenilenebilir ve tek kullanılabilir anahtar oysa onu kalıcı
    olarak dışlamak sistemi gereksizce yarım bırakırdı.
    """
    try:
        with SessionLocal() as session:
            rows = session.execute(
                select(LlmCredential).order_by(LlmCredential.id.desc())
            ).scalars().all()
    except Exception as exc:  # noqa: BLE001 - tablo yoksa .env'e düşülür
        logger.debug("kimlik tablosu okunamadı: %s", exc)
        return []
    hepsi = [_to_credential(r) for r in rows]
    calisan = [c for c in hepsi if c.status != "failed"]
    bozuk = [c for c in hepsi if c.status == "failed"]
    return calisan + bozuk


def effective_chain() -> list[Credential]:
    """GERÇEKTE denenecek TAM sıra: `.env` ÖNCE, sonra panelden kaydedilenler.

    NEDEN `.env` ÖNCE (kullanıcı kararı, 2026-09-06): `.env` sunucu
    sahibinin kendi elleriyle girdiği, dışarıdan kimsenin dokunamadığı
    anahtardır. Panelden "sürekli kullan" ile kaydedilen bir anahtar bunu
    SESSİZCE geride bırakırsa, paneli görebilen biri (compose bugün
    yalnızca 127.0.0.1'e bağlıyor ama bu değişebilir) sahibinin planlı
    koşularını kendi anahtarına yönlendirmiş olurdu. `.env` boşsa panelden
    kaydedilenler devreye girer; bu davranış değişmedi.

    `.env`'in kendisi kimlik hatasıyla düşerse (bkz. llm/extract.py)
    zincirdeki BİR SONRAKİ (panelden kaydedilen en yeni) anahtar denenir —
    yani "en son kaydedilen kullanılsın, olmazsa bir öncekine düşsün"
    isteği hâlâ geçerli, yalnızca `.env` artık o zincirin BAŞINDA.
    """
    onde = env_credential()
    return ([onde] if onde else []) + chain()


def active() -> Credential | None:
    """Şu an FİİLEN kullanılacak anahtar (yoksa None)."""
    zincir = effective_chain()
    return zincir[0] if zincir else None


def first_untried(tried: set[int]) -> Credential | None:
    """Zincirde HENÜZ DENENMEMİŞ ilk anahtar — `.env` dahil TÜM zincirde.

    NEDEN "sonraki" DEĞİL DE "denenmemiş": bir anahtar 'failed'
    işaretlendiği anda zincirin SONUNA kayıyor. Konuma göre "sonraki"yi
    seçen bir mantık bu yeniden sıralama yüzünden aday atlıyordu — üç
    anahtarlı bir zincirde ilki düştüğünde ikinciyi hiç denemeden
    "yedek yok" diyordu. Denenmişleri kümede tutmak bunu yapısal olarak
    imkânsız kılıyor ve döngüyü de sınırlıyor: her anahtar en fazla bir kez.

    `effective_chain()` KULLANILIYOR, `chain()` DEĞİL: `.env` kimlik
    hatasıyla düşerse bir sonraki aday panelden kaydedilen en yeni anahtar
    olmalı, yalnızca DB satırları arasında aranmamalı.
    """
    for cred in effective_chain():
        if cred.id is not None and cred.id not in tried:
            return cred
    return None


def is_credential_error(exc: Exception) -> bool:
    """Bu hata ANAHTARIN sorunu mu, sağlayıcının geçici arızası mı?"""
    kod = getattr(exc, "status_code", None)
    if kod is None:
        kod = getattr(getattr(exc, "response", None), "status_code", None)
    if kod in KIMLIK_HATA_KODLARI:
        return True
    # Kod okunamayan istemci sürümleri için metin yedeği. Dar tutuldu:
    # geniş bir desen sağlayıcının 500'ünü anahtar hatası sanardı.
    metin = str(exc).lower()
    return any(
        parca in metin
        for parca in ("api key", "unauthorized", "invalid_api_key", "quota", "insufficient")
    )


def mark_failed(credential_id: int | None, error: str) -> None:
    if credential_id is None:
        return
    try:
        with SessionLocal() as session:
            row = session.get(LlmCredential, credential_id)
            if row is None:
                return
            row.status = "failed"
            row.last_error = error[:500]
            row.last_failed_at = utc_now()
            session.commit()
    except Exception as exc:  # noqa: BLE001 - işaretleyememek koşuyu düşürmemeli
        logger.warning("kimlik #%s 'failed' işaretlenemedi: %s", credential_id, exc)


def mark_ok(credential_id: int | None) -> None:
    if credential_id is None:
        return
    try:
        with SessionLocal() as session:
            row = session.get(LlmCredential, credential_id)
            if row is None:
                return
            row.status = "ok"
            row.last_error = None
            row.last_ok_at = utc_now()
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("kimlik #%s 'ok' işaretlenemedi: %s", credential_id, exc)


def save(
    api_key: str,
    *,
    base_url: str,
    model: str,
    extra_body: dict | None = None,
) -> tuple[Credential, str]:
    """Anahtarı kaydeder. ÇAĞIRAN ÖNCE TEST ETMİŞ OLMALI (bkz. test_credential).

    Dönen: (kayıt, SİLME KODU). Kod ÇAĞRIYA BİR KEZ dönüyor ve bir daha
    hiçbir yerden okunamıyor — veritabanında yalnızca özeti var. Paneli
    açabilen herkesin her anahtarı silebilmesi, kötü niyetli birinin
    agent'ı tamamen durdurmasına yetiyordu (kullanıcı bildirimi,
    2026-09-12); kodu yalnızca kaydeden kişi görüyor.
    """
    if not api_key or not api_key.strip():
        raise ValueError("boş anahtar kaydedilemez")
    govde = json.dumps(extra_body) if extra_body else None
    kod = new_code()
    with SessionLocal() as session:
        row = LlmCredential(
            api_key=api_key.strip(),
            base_url=base_url.strip(),
            model=model.strip(),
            extra_body=govde,
            status="ok",
            last_ok_at=utc_now(),
            created_at=utc_now(),
            owner_hash=hash_code(kod),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        cred = _to_credential(row)
    _aynayi_tazele({cred.id: kod})
    return cred, kod


class NotAuthorized(Exception):
    """Silme kodu yanlış ya da eksik."""


def delete(credential_id: int, code: str | None = None) -> None:
    """Anahtarı siler. Sahibi belliyse DOĞRU KOD şart.

    `owner_hash` boş olan satırlar (bu koruma eklenmeden önce kaydedilmiş
    olanlar) kodsuz silinebiliyor — aksi halde onları kimse silemezdi ve
    panelde kalıcı olarak takılı kalırlardı.

    KODU KAYBEDENİN ÇIKIŞ YOLU: kod `data/llm_credentials.json` aynasında
    düz metin duruyor; sunucuya erişebilen kişi oradan okuyabilir
    (bkz. llm/credentials_file.py).
    """
    with SessionLocal() as session:
        row = session.get(LlmCredential, credential_id)
        if row is None:
            return
        if row.owner_hash:
            if not code or not secrets.compare_digest(hash_code(code), row.owner_hash):
                raise NotAuthorized(
                    "Bu anahtarı yalnızca ekleyen kişi silebilir; silme kodu gerekiyor."
                )
        session.delete(row)
        session.commit()
    _aynayi_tazele()


def _aynayi_tazele(yeni_kodlar: dict[int, str] | None = None) -> None:
    """JSON aynasını veritabanının güncel hâline göre yeniden yazar.

    Hata yutuluyor: ayna yazılamadı diye kaydetme/silme işlemi düşmemeli,
    kaynak doğru veritabanı (bkz. llm/credentials_file.py).
    """
    from llm import credentials_file

    try:
        credentials_file.write(chain(), yeni_kodlar)
    except Exception as exc:  # noqa: BLE001
        logger.warning("anahtar aynası tazelenemedi: %s", exc)


def listele() -> list[Credential]:
    """Panelde gösterim için — anahtarın kendisi ASLA tam dönmez."""
    return chain()


def test_credential(
    api_key: str, *, base_url: str, model: str, extra_body: dict | None = None
) -> tuple[bool, str | None]:
    """Anahtar GERÇEKTEN çalışıyor mu — en küçük istekle canlı deneme.

    max_tokens=1: amaç yanıtın içeriği değil, sağlayıcının anahtarı kabul
    edip etmediği. Bu, kaydedilen her anahtarın çalıştığının kanıtı.
    """
    from openai import OpenAI

    from llm import settings

    try:
        client = OpenAI(
            base_url=base_url.strip(),
            api_key=api_key.strip(),
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
        client.chat.completions.create(
            model=model.strip(),
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
            extra_body=extra_body or None,
        )
        return True, None
    except Exception as exc:  # noqa: BLE001 - test her hatayı bildirmeli
        return False, str(exc)
