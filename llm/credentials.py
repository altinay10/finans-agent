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

import json
import logging
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
    )


def chain() -> list[Credential]:
    """Denenecek anahtarlar — EN YENİ ÖNCE.

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


def active() -> Credential | None:
    """Şu an kullanılacak anahtar (yoksa None -> .env'e düşülür)."""
    zincir = chain()
    return zincir[0] if zincir else None


def first_untried(tried: set[int]) -> Credential | None:
    """Zincirde HENÜZ DENENMEMİŞ ilk anahtar.

    NEDEN "sonraki" DEĞİL DE "denenmemiş": bir anahtar 'failed'
    işaretlendiği anda zincirin SONUNA kayıyor. Konuma göre "sonraki"yi
    seçen bir mantık bu yeniden sıralama yüzünden aday atlıyordu — üç
    anahtarlı bir zincirde ilki düştüğünde ikinciyi hiç denemeden
    "yedek yok" diyordu. Denenmişleri kümede tutmak bunu yapısal olarak
    imkânsız kılıyor ve döngüyü de sınırlıyor: her anahtar en fazla bir kez.
    """
    for cred in chain():
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
) -> Credential:
    """Anahtarı kaydeder. ÇAĞIRAN ÖNCE TEST ETMİŞ OLMALI (bkz. test_credential)."""
    if not api_key or not api_key.strip():
        raise ValueError("boş anahtar kaydedilemez")
    govde = json.dumps(extra_body) if extra_body else None
    with SessionLocal() as session:
        row = LlmCredential(
            api_key=api_key.strip(),
            base_url=base_url.strip(),
            model=model.strip(),
            extra_body=govde,
            status="ok",
            last_ok_at=utc_now(),
            created_at=utc_now(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _to_credential(row)


def delete(credential_id: int) -> None:
    with SessionLocal() as session:
        row = session.get(LlmCredential, credential_id)
        if row is not None:
            session.delete(row)
            session.commit()


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
