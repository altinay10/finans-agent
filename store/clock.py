"""Tek saat kaynağı — toplayıcıların ve sorguların zaman kavramı burada.

İKİ AYRI ZAMAN VAR, karıştırılmamalı:

* `utc_now()`  — bir olayın NE ZAMAN olduğu (fetched_at, quoted_at). Makineden
  bağımsız olsun diye her zaman UTC.
* `istanbul_today()` — oranın HANGİ BANKA GÜNÜNE ait olduğu (valid_date).
  Bu bir takvim günüdür ve Türkiye'nin takvim günüdür.

Eskiden `valid_date` için `date.today()` kullanılıyordu; bu makinenin yerel
saatine bağlıydı. Geliştirme makinesi zaten Europe/Istanbul olduğu için sorun
görünmüyordu, ama Docker konteyneri varsayılan olarak UTC çalışır: Türkiye'de
gece yarısı ile 03:00 arasında `date.today()` bir GÜN GERİ döner. Sonuç:
o saatlerde çekilen bütün oranlar dünün tarihiyle yazılır, "bugünün eski
satırlarını temizle" mantığı yanlış günü hedefler ve panel dünkü oranı
bugünkü sanır. Konteynerin TZ'si de ayarlanıyor ama asıl güvence burası.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ISTANBUL = ZoneInfo("Europe/Istanbul")


def utc_now() -> datetime:
    """Olay zamanı — fetched_at / quoted_at için."""
    return datetime.now(timezone.utc)


def istanbul_today() -> date:
    """Banka günü — valid_date için. Makinenin saat diliminden bağımsız."""
    return datetime.now(ISTANBUL).date()
