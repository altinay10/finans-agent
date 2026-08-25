"""Katılım bankalarının KÂR PAYLAŞIM ORANLARI — faiz oranı değil.

Kaynaklar: Emlak Katılım ve Kuveyt Türk.

Neden ayrı bir toplayıcı: katılım bankalarının yayınladığı sayılar yıllık getiri değil, bankanın elde ettiği
kârın müşteriye düşen yüzdesidir. "%92" bir getiri vaadi değil, bir bölüşme
oranıdır. Bunu deposit_rates tablosuna annual_rate diye yazmak paneli
%92 faiz hesaplamaya iter — tamamen uydurma bir sayı üretir.

Bu yüzden veri kendi tablosunda (profit_share_ratios) tutulur, hesaplamaya
hiç girmez ve panelde ayrı bir blokta, ne olduğu açıkça yazılarak gösterilir.

Bonus: aynı sayfa STOPAJ oranlarını da yayınlıyor ve bunlar config/taxes.yaml
ile bağımsız olarak örtüşüyor (TL: 17,5 / 17,5 / 17,5 / 17,5 / 15 / 10 ve
USD-EUR: 25). Yani bu kaynak aynı zamanda vergi tablomuzun ikinci teyidi.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime

import httpx

from collectors import http
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from collectors.base import Collector, ParseError, SanityCheckError
from store.clock import istanbul_today, utc_now
from store.db import SessionLocal
from store.models import ProfitShareRatio

logger = logging.getLogger(__name__)

URL = "https://www.emlakkatilim.com.tr/tr/bireysel/hesaplar/katilma-hesaplari/katilma-hesabi"
HEADERS = {"User-Agent": "finans-agent/0.1", "Accept": "text/html"}

# Tablo başlığındaki para birimi adı -> ISO kodu.
CURRENCY_BY_TITLE = {
    "Türk Lirası": "TRY",
    "Dolar": "USD",
    "Euro": "EUR",
    "Altın": "XAU",
    "Gümüş": "XAG",
}

# Bankanın vade etiketi -> kıyas için gün. Katılım bankaları vadeyi ay/yıl
# olarak yazıyor; mevduat tarafıyla aynı ölçeğe getirmek için normalize edilir.
TERM_DAYS_BY_LABEL = {
    "1 Günlük": 1,
    "31 Günlük": 31,
    "3 Aylık": 92,
    "6 Aylık": 182,
    "Yıllık": 365,
    "1 Yıldan Uzun": 366,
}


class ProfitShareRecord(BaseModel):
    institution: str
    currency: str
    term_label: str
    term_days: int
    amount_min: float
    amount_max: float | None
    share_ratio: float
    withholding_rate: float | None = None

    @model_validator(mode="after")
    def _bounds(self):
        # Paylaşım oranı bir yüzdedir: 0-1 arası olmalı. 1'i aşıyorsa
        # neredeyse kesin bir ayrıştırma hatasıdır (ör. 92'yi 92.0 yerine
        # 9200 okumak).
        if not (0 < self.share_ratio <= 1):
            raise ValueError(f"paylaşım oranı bant dışında: {self.share_ratio}")
        if self.term_days <= 0:
            raise ValueError("term_days pozitif olmalı")
        if self.amount_max is not None and self.amount_max < self.amount_min:
            raise ValueError("amount_max < amount_min")
        return self


_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.DOTALL)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL)
_TITLE_RE = re.compile(r"(Türk Lirası|Dolar|Euro|Altın|Gümüş)\s+Kar Paylaşım Oranları")


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " ").strip()


def _amount(raw: str) -> float | None:
    """Tutar hücresi: nokta BİNLİK ayırıcı, virgül ondalık ("24.999" -> 24999)."""
    cleaned = raw.replace(".", "").replace(",", ".").strip().lstrip("+")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _percent(raw: str) -> float | None:
    """Yüzde hücresi: nokta ONDALIK ayırıcı ("17.50%" -> 17.5).

    Aynı sayfada iki farklı sayı biçimi var: tutarlarda nokta binlik ayırıcı
    ("24.999" = yirmi dört bin), stopaj yüzdesinde nokta ondalık ayırıcı
    ("17.50%" = yüzde on yedi buçuk). Tek bir ayrıştırıcı kullanmak stopajı
    17,5 yerine 1750 okur — bu yüzden ikisi ayrı.
    """
    cleaned = raw.replace("%", "").strip()
    if "," in cleaned:  # TR biçimi: nokta binlik, virgül ondalık
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


class EmlakKatilimProfitShareCollector(Collector):
    """Emlak Katılım — bkz. modül docstring'i."""

    name = "profit_shares"
    # config/sources.yaml -> profit_share_endpoints.emlakkatilim
    default_source = "emlakkatilim"
    schema = ProfitShareRecord
    institution = "EMLAKKATILIM"

    def fetch(self) -> bytes:
        resp = http.get(URL, headers=HEADERS, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    def parse(self, raw: bytes) -> list[ProfitShareRecord]:
        page = raw.decode("utf-8", errors="replace")
        records: list[ProfitShareRecord] = []

        for table in _TABLE_RE.findall(page):
            rows = [[_text(c) for c in _CELL_RE.findall(r)] for r in _ROW_RE.findall(table)]
            # Sayfa her satıra BOŞ bir ilk hücre koyuyor (stil amaçlı). Bunu
            # atmazsak "tutar" sütunu olarak boş string okunur ve tüm satırlar
            # sessizce atlanır.
            rows = [r[1:] if r and not r[0] else r for r in rows]
            rows = [r for r in rows if any(c for c in r)]
            if not rows:
                continue

            flat = " ".join(" ".join(r) for r in rows[:2])
            title = _TITLE_RE.search(flat)
            if not title:
                continue
            currency = CURRENCY_BY_TITLE[title.group(1)]

            # Vade başlıkları: "Minimum Bakiye" ve "Maksimum Bakiye"den sonraki sütunlar.
            header = next((r for r in rows if any("Minimum Bakiye" in c for c in r)), None)
            if header is None:
                continue
            start = next(i for i, c in enumerate(header) if "Maksimum Bakiye" in c) + 1
            term_labels = header[start:]

            # Stopaj satırı ayrı tutulur, kademe satırı değildir.
            withholdings: dict[str, float] = {}
            stoppage = next((r for r in rows if r and "Stopaj" in r[0]), None)
            if stoppage:
                # Stopaj satırında etiketten sonra BOŞ dolgu hücreleri var
                # (canlı örnek: ['Stopaj Oranı', '', '', '17.50%', ...]).
                # Boşları atıp kalanları vade başlıklarıyla hizala; aksi halde
                # stopaj iki sütun kayar ve yanlış vadeye yazılır.
                values = [c for c in stoppage[1:] if c.strip()]
                for label, cell in zip(term_labels, values):
                    value = _percent(cell)
                    if value is not None:
                        withholdings[label] = value / 100

            for row in rows:
                if not row or "Stopaj" in row[0] or "Bakiye" in row[0]:
                    continue
                amount_min = _amount(row[0])
                if amount_min is None:
                    continue
                # En üst kademe "+1.250.000" biçiminde ve maksimum hücresi BOŞ
                # bırakılıyor — sütunlar kaymıyor, sadece değer yok.
                amount_max = _amount(row[1]) if len(row) > 1 else None
                rate_cells = row[2:]

                for label, cell in zip(term_labels, rate_cells):
                    ratio = _percent(cell)
                    if ratio is None:
                        continue  # "-" : bu vade bu kademede açık değil
                    term_days = TERM_DAYS_BY_LABEL.get(label)
                    if term_days is None:
                        continue
                    records.append(
                        ProfitShareRecord(
                            institution=self.institution,
                            currency=currency,
                            term_label=label,
                            term_days=term_days,
                            amount_min=amount_min,
                            amount_max=amount_max,
                            share_ratio=ratio / 100,
                            withholding_rate=withholdings.get(label),
                        )
                    )

        if not records:
            raise ParseError("Kâr paylaşım oranı tablosu okunamadı")
        return records

    def sanity_check(self, records: list[ProfitShareRecord]) -> None:
        currencies = {r.currency for r in records}
        if "TRY" not in currencies:
            raise SanityCheckError("TL kâr paylaşım tablosu çıkmadı — sayfa değişmiş olabilir")

    def persist(self, records: list[ProfitShareRecord], run_id: int) -> None:
        now = utc_now()
        today = istanbul_today()
        with SessionLocal() as session:
            fresh = {(r.currency, r.term_days, float(r.amount_min)) for r in records}
            existing = session.execute(
                select(ProfitShareRatio).where(
                    ProfitShareRatio.institution == self.institution,
                    ProfitShareRatio.valid_date == today,
                )
            ).scalars().all()
            for row in existing:
                if (row.currency, row.term_days, float(row.amount_min)) not in fresh:
                    session.delete(row)
            session.flush()

            for r in records:
                current = session.execute(
                    select(ProfitShareRatio).where(
                        ProfitShareRatio.institution == r.institution,
                        ProfitShareRatio.currency == r.currency,
                        ProfitShareRatio.term_days == r.term_days,
                        ProfitShareRatio.amount_min == r.amount_min,
                        ProfitShareRatio.valid_date == today,
                    )
                ).scalar_one_or_none()
                if current:
                    current.share_ratio = r.share_ratio
                    current.withholding_rate = r.withholding_rate
                    current.fetched_at = now
                    continue
                session.add(
                    ProfitShareRatio(
                        institution=r.institution,
                        currency=r.currency,
                        term_label=r.term_label,
                        term_days=r.term_days,
                        amount_min=r.amount_min,
                        amount_max=r.amount_max,
                        share_ratio=r.share_ratio,
                        withholding_rate=r.withholding_rate,
                        valid_date=today,
                        fetched_at=now,
                    )
                )
            session.commit()


# ---------------------------------------------------------------- Kuveyt Türk --

KUVEYTTURK_URL = (
    "https://www.kuveytturk.com.tr/kendim-icin/hesaplar/katilma-hesaplari/katilma-hesabi"
)

# Kuveyt Türk vadeyi ARALIK olarak yazıyor: "3 Aylık (32-91 Gün)". Aralığın
# başlangıcı alınır (mevduat parser'larıyla aynı politika: bir kovada garanti
# edilen en kısa vade odur).
_KT_TERM_RE = re.compile(r"\((\d+)\s*-\s*(\d+)\s*Gün\)")
_KT_SIMPLE_TERM_RE = re.compile(r"^(\d+)\s*-\s*(\d+)\s*Gün$")

# "87-13" = müşteriye %87, bankaya %13.
_KT_SPLIT_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


class KuveytTurkProfitShareCollector(Collector):
    """Kuveyt Türk kâr paylaşım oranları.

    Emlak Katılım'dan iki farkı var:

    1. Oran "87-13" biçiminde yazılıyor — müşteri payı / banka payı. Sadece
       ilk sayı bizim `share_ratio`'muz; ikinciyi almak oranı tersine çevirir.
    2. Vade sütun başlığı bir ARALIK içeriyor ("3 Aylık (32-91 Gün)");
       aralığın başlangıç günü kullanılır.

    Sayfada dört tablo var: TL standart, TL günlük kazançlı, döviz standart,
    döviz günlük kazançlı. Döviz tablosu USD ve EUR için AYNI oranı veriyor
    ("USD ve EURO ..."), o yüzden iki para birimi olarak da yazılıyor.
    """

    name = "profit_shares_kt"
    # config/sources.yaml -> profit_share_endpoints.kuveytturk
    default_source = "kuveytturk"
    schema = ProfitShareRecord
    institution = "KUVEYTTURK"

    def fetch(self) -> bytes:
        resp = http.get(KUVEYTTURK_URL, headers=HEADERS, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def _term_days(label: str) -> int | None:
        m = _KT_TERM_RE.search(label) or _KT_SIMPLE_TERM_RE.match(label.strip())
        return int(m.group(1)) if m else None

    def parse(self, raw: bytes) -> list[ProfitShareRecord]:
        page = raw.decode("utf-8", errors="replace")
        records: list[ProfitShareRecord] = []

        for table in _TABLE_RE.findall(page):
            rows = [[_text(c) for c in _CELL_RE.findall(r)] for r in _ROW_RE.findall(table)]
            rows = [r for r in rows if any(c for c in r)]
            if not rows:
                continue
            heading = " ".join(rows[0])
            if "Kar Paylaşım Oranları" not in heading:
                continue
            # "USD ve EURO ..." başlığı iki para birimini birlikte kapsıyor.
            currencies = ("USD", "EUR") if "USD" in heading or "EURO" in heading else ("TRY",)

            header = next((r for r in rows if any("Alt Bakiye" in c for c in r)), None)
            if header is None:
                continue
            start = next(i for i, c in enumerate(header) if "Alt Bakiye" in c) + 1
            term_labels = header[start:]

            withholdings: dict[str, float] = {}
            stoppage = next((r for r in rows if r and "Stopaj" in r[0]), None)
            if stoppage:
                values = [c for c in stoppage[1:] if c.strip()]
                for label, cell in zip(term_labels, values):
                    value = _percent(cell)
                    if value is not None:
                        withholdings[label] = value / 100

            for row in rows:
                if not row or "Stopaj" in row[0] or "Bakiye" in row[0] or len(row) <= start:
                    continue
                # row = [segment adı, açılış bakiyesi, alt bakiye, oranlar...]
                amount_min = _amount(row[1]) if len(row) > 1 else None
                if amount_min is None:
                    continue
                for label, cell in zip(term_labels, row[start:]):
                    split = _KT_SPLIT_RE.match(cell.strip())
                    if not split:
                        continue
                    term_days = self._term_days(label)
                    if term_days is None:
                        continue
                    # İlk sayı MÜŞTERİ payı; ikinciyi almak oranı ters çevirir.
                    customer_share = float(split.group(1))
                    for currency in currencies:
                        records.append(
                            ProfitShareRecord(
                                institution=self.institution,
                                currency=currency,
                                term_label=label,
                                term_days=term_days,
                                amount_min=amount_min,
                                amount_max=None,  # segmentler üst sınır yayınlamıyor
                                share_ratio=customer_share / 100,
                                withholding_rate=withholdings.get(label),
                            )
                        )

        if not records:
            raise ParseError("Kuveyt Türk kâr paylaşım tablosu okunamadı")
        return records

    def sanity_check(self, records: list[ProfitShareRecord]) -> None:
        if "TRY" not in {r.currency for r in records}:
            raise SanityCheckError("TL tablosu çıkmadı — sayfa değişmiş olabilir")

    persist = EmlakKatilimProfitShareCollector.persist


if __name__ == "__main__":
    print(EmlakKatilimProfitShareCollector().run())
    print(KuveytTurkProfitShareCollector().run())
