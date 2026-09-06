"""Sır olmayan, panelden değiştirilebilen ayarlar.

Şimdilik tek iş: token birim fiyatları. `.env` yerine veritabanı çünkü
konteynerde `.env` dosyası YOK (`.dockerignore` onu imaja sokmuyor) ve
panel ile zamanlayıcı ayrı konteynerler; ikisinin ortak gördüğü tek kalıcı
yer `finans-data` volume'ündeki veritabanı.

Fiyatın `.env`'deki karşılığı (LLM_PRICE_INPUT_PER_1M / _OUTPUT_PER_1M)
KALDIRILMADI: burada bir kayıt yoksa ona düşülür. Böylece sunucuyu
`.env`'le kuran mevcut kurulumlar bozulmuyor.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from store.clock import utc_now
from store.db import SessionLocal
from store.models import AppSetting

logger = logging.getLogger(__name__)

PRICE_INPUT = "llm_price_input_per_1m"
PRICE_OUTPUT = "llm_price_output_per_1m"


def get(key: str) -> str | None:
    with SessionLocal() as session:
        row = session.get(AppSetting, key)
        return row.value if row else None


def set_value(key: str, value: str | None) -> None:
    with SessionLocal() as session:
        row = session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=value, updated_at=utc_now())
            session.add(row)
        else:
            row.value = value
            row.updated_at = utc_now()
        session.commit()


def _as_float(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def price_rates() -> tuple[float | None, float | None]:
    """(girdi, çıktı) USD / 1.000.000 token.

    Veritabanında kayıt varsa O geçerlidir; yoksa `.env`'deki değere
    düşülür. İkisi de yoksa None — "bilinmiyor" ile "sıfır" AYRI tutuluyor,
    0 yazmak "bedava" demek olurdu.

    Tablo henüz yaratılmamışsa (çok eski bir veritabanı, migrate koşmadan)
    burada patlamak bütün LLM kaydını düşürürdü; sessizce `.env`'e düşmek
    doğru davranış.
    """
    from llm import settings as llm_settings

    try:
        girdi = _as_float(get(PRICE_INPUT))
        cikti = _as_float(get(PRICE_OUTPUT))
    except Exception as exc:  # noqa: BLE001 - fiyat okunamıyorsa .env geçerli
        logger.debug("fiyat ayarı okunamadı, .env kullanılıyor: %s", exc)
        girdi = cikti = None
    if girdi is None:
        girdi = llm_settings.LLM_PRICE_INPUT_PER_1M
    if cikti is None:
        cikti = llm_settings.LLM_PRICE_OUTPUT_PER_1M
    return girdi, cikti


def set_price_rates(girdi: float | None, cikti: float | None) -> None:
    set_value(PRICE_INPUT, "" if girdi is None else repr(float(girdi)))
    set_value(PRICE_OUTPUT, "" if cikti is None else repr(float(cikti)))
