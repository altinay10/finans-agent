"""Panellerin ortak biçimlendirme yardımcıları.

Buradaki tek kural: veritabanındaki her zaman damgası UTC'dir (naive
saklanır, `datetime.now(timezone.utc)` ile yazılır), kullanıcıya gösterilen
her zaman damgası ise İstanbul saatidir. Bu dönüşüm tek yerde yapılır ki
paneller arasında tutarsızlık olmasın — aksi halde "11:09'da çekildi" yazan
bir panel, saat 14:09'da bakan kullanıcıya veriyi üç saat bayat gösterir.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ISTANBUL = ZoneInfo("Europe/Istanbul")


def as_utc(ts: datetime) -> datetime:
    """Naive saklanan zaman damgasını UTC olarak etiketler."""
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def to_istanbul(ts: datetime) -> datetime:
    return as_utc(ts).astimezone(ISTANBUL)


def format_local(ts: datetime) -> str:
    return to_istanbul(ts).strftime("%d.%m.%Y %H:%M")


def age_hours(ts: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return (now - as_utc(ts)).total_seconds() / 3600


def fetched_caption(timestamps, *, label: str = "Veri çekilme zamanı") -> str:
    """Tabloların altına düşen 'ne zaman çekildi' notu.

    Birden fazla satır varsa en yenisini gösterir ve kaç saat önce olduğunu
    yazar; tazelik bilgisi olmadan bir oran tablosu kendi başına yanıltıcıdır.
    """
    stamps = [t for t in timestamps if t is not None]
    if not stamps:
        return f"{label}: bilinmiyor"
    newest = max(stamps)
    hours = age_hours(newest)
    if hours < 1:
        ago = f"{hours * 60:.0f} dakika önce"
    elif hours < 48:
        ago = f"{hours:.1f} saat önce"
    else:
        ago = f"{hours / 24:.1f} gün önce"
    return f"{label}: **{format_local(newest)}** ({ago}, Europe/Istanbul)"


# --- kurum etiketleri ------------------------------------------------------
#
# TEK KAYNAK. Daha önce bu sözlük fx.py, deposit.py ve loan.py'de ayrı ayrı
# duruyordu; yeni kurum eklenince biri güncellenmeyi unutuyor ve panelde
# "AKBANK" gibi ham kod görünüyordu.
INSTITUTION_LABELS = {
    "TCMB": "TCMB (resmi referans)",
    "AKBANK": "Akbank",
    "EMLAKKATILIM": "Emlak Katılım",
    "ENPARA": "Enpara (QNB)",
    "GARANTIBBVA": "Garanti BBVA",
    "HALKBANK": "Halkbank",
    "ISBANK": "Türkiye İş Bankası",
    "KUVEYTTURK": "Kuveyt Türk",
    "TEB": "CepteTEB",
    "VAKIFBANK": "VakıfBank",
    "YAPIKREDI": "Yapı Kredi",
    "ZIRAAT": "Ziraat Bankası",
}


def institution_label(code: str) -> str:
    """Bilinmeyen kod gelirse kodun kendisini döndür — panel patlamasın."""
    return INSTITUTION_LABELS.get(code, code)
