"""Panellerin ortak biçimlendirme yardımcıları.

Buradaki tek kural: veritabanındaki her zaman damgası UTC'dir (naive
saklanır, `datetime.now(timezone.utc)` ile yazılır), kullanıcıya gösterilen
her zaman damgası ise İstanbul saatidir. Bu dönüşüm tek yerde yapılır ki
paneller arasında tutarsızlık olmasın — aksi halde "11:09'da çekildi" yazan
bir panel, saat 14:09'da bakan kullanıcıya veriyi üç saat bayat gösterir.
"""
from __future__ import annotations

import re
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


_YAML_LABELS: dict[str, str] | None = None


def _registry_labels() -> dict[str, str]:
    """Kurum adları ENVANTERDEN okunur; yukarıdaki sözlük yalnızca yedek.

    NEDEN: elle tutulan kopya kaydı. `config/sources.yaml` 19 kurum
    tanımlıyor, buradaki sözlükte 12 tanesi vardı — DenizBank, ING, QNB,
    Odeabank, Fibabanka, Anadolubank ve Burgan panelde ham kod olarak
    görünüyordu ("DENIZBANK"). Sözlüğün üstündeki yorum tam da bunu
    uyarıyor: "yeni kurum eklenince biri güncellenmeyi unutuyor". Çözüm
    ikinci bir kopya tutmamak; kurum eklemek artık tek bir YAML satırı.
    """
    global _YAML_LABELS
    if _YAML_LABELS is None:
        try:
            from config.loader import load_sources

            _YAML_LABELS = {
                kayit["code"]: kayit["name"]
                for kayit in (load_sources().get("institutions") or [])
                if kayit.get("code") and kayit.get("name")
            }
        except Exception:  # noqa: BLE001 - envanter okunamazsa sabit liste yeter
            _YAML_LABELS = {}
    return _YAML_LABELS


def institution_label(code: str) -> str:
    """Bilinmeyen kod gelirse kodun kendisini döndür — panel patlamasın."""
    if code in INSTITUTION_LABELS:
        return INSTITUTION_LABELS[code]
    return _registry_labels().get(code, code)


# --- LLM çağrısı: HANGİ VERİ çekiliyordu ------------------------------------
#
# Tabloda tek başına "loan_rates_llm" yazması yetmiyordu: o ad toplayıcıyı
# söyler, ÇEKİLEN VERİYİ söylemez. Aynı toplayıcı 12 farklı banka sayfasına
# gidiyor ve her satır ayrı bir çağrı. Hangi bankanın hangi kredisi olduğu
# `trigger_source` alanında ZATEN kayıtlıydı (ör. "denizbank_tasit"), yalnızca
# ham anahtar olarak duruyordu.

#: Kredi türü kodları -> okunabilir karşılık. collectors/loan_rates_llm.py
#: ile aynı sözlük; oradaki kaynak, buradaki gösterim.
LOAN_TYPE_LABELS = {"personal": "ihtiyaç", "housing": "konut", "vehicle": "taşıt"}

#: Onarım yedeği (generic fallback) çağrılarının açıklaması. Bu çağrılarda
#: `trigger_source` yok: toplayıcının KENDİ ayrıştırıcısı kırıldığı için
#: devreye giriyorlar, tek bir kaynağa ait değiller.
FALLBACK_DESCRIPTIONS = {
    "fx_banks": "Banka döviz kurları",
    "fx_tcmb": "TCMB resmi referans kuru",
    "deposit_rates": "Mevduat faiz oranları",
    "loan_rates": "Kredi oranları (uç noktalı bankalar)",
    "fund_prices": "Fon fiyat serileri",
    "profit_shares": "Emlak Katılım kâr paylaşım oranları",
    "profit_shares_kt": "Kuveyt Türk kâr paylaşım oranları",
    "participation_rates": "Emlak Katılım yıllık kâr payı",
    "participation_rates_kt": "Kuveyt Türk yıllık kâr payı",
    "loan_rates_llm": "Agent — kredi oranı",
}


def _agent_pages() -> dict[str, dict]:
    """`sources.yaml`'daki agent sayfaları. Sonuç süreç ömrü boyunca aynı."""
    global _AGENT_PAGES
    if _AGENT_PAGES is None:
        try:
            from config.loader import load_sources

            _AGENT_PAGES = load_sources().get("loan_llm_endpoints") or {}
        except Exception:  # noqa: BLE001 - envanter okunamazsa ham anahtar gösterilir
            _AGENT_PAGES = {}
    return _AGENT_PAGES


_AGENT_PAGES: dict[str, dict] | None = None


def llm_call_description(collector: str | None, trigger_source: str | None) -> str:
    """"Bu çağrı tam olarak hangi veriyi çekiyordu?"

    Agent çağrılarında banka + kredi türü ("DenizBank taşıt kredisi"),
    onarım yedeğinde ise kırılan toplayıcının ne topladığı yazılır.
    """
    if trigger_source:
        kayit = _agent_pages().get(trigger_source) or {}
        kod = kayit.get("institution")
        tur = LOAN_TYPE_LABELS.get(kayit.get("loan_type_hint") or "personal", "ihtiyaç")
        if kod:
            return f"{institution_label(kod)} {tur} kredisi"
        # Envanterde yoksa ham anahtarı göster — uydurmaktansa dürüst.
        return f"{trigger_source} ({tur} kredisi)"
    if collector:
        aciklama = FALLBACK_DESCRIPTIONS.get(collector)
        if aciklama:
            return f"{aciklama} — ayrıştırıcı kırıldı, LLM yedeği"
        return collector
    return "—"


# --- para tutarı biçimleme --------------------------------------------------
#
# Panelin tablolarında tutarlar zaten `{:,.0f}` ile yazılıyor (2,500,000).
# Anapara kutusu ise ham `2500000` gösteriyordu; aynı sayfada iki ayrı
# gösterim vardı ve yedi haneli tutarda basamak saymadan okumak zordu.
# Ayırıcı bilinçli olarak virgül: panelin geri kalanıyla aynı olsun diye.


def format_amount(value: int) -> str:
    """2500000 -> '2,500,000'. Kutuda ve metinlerde aynı gösterim."""
    return f"{value:,}"


def parse_amount(raw: str) -> int | None:
    """Kullanıcının yazdığı tutarı tam sayıya çevirir; rakam yoksa None.

    Ayırıcıyı biz koyuyoruz ama kullanıcı kendi alışkanlığıyla yazıyor:
    '2,500,000' da '2.500.000' da '2 500 000' da gelir. Bu yüzden rakam
    dışındaki her şey atılıyor. Anapara tam sayı bir alan (adım 50.000),
    ondalık ayırıcı ayrımı yapmaya gerek yok — '.' her zaman basamak
    ayırıcısıdır. Eksi işareti de eleniyor; kutunun eski hâlindeki
    min_value=0 sınırı böylece korunuyor.
    """
    digits = re.sub(r"\D", "", raw or "")
    return int(digits) if digits else None
