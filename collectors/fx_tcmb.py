"""TCMB resmi kur XML'i — referans/çapa kaynağı. Anahtarsız, günlük.

Uç nokta: tcmb.gov.tr/kurlar/today.xml
"""
from __future__ import annotations

from datetime import datetime, timezone
from xml.etree import ElementTree

import httpx

from collectors import http
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from collectors.base import Collector, ParseError, SanityCheckError
from store.db import SessionLocal
from store.models import FxQuote

URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
CURRENCIES = ("USD", "EUR")
# Kaba çapa bandı — büyük ölçek hatalarını (kaçırılan sıfır, ters çevrilmiş
# birim) yakalamak için. Aralığı zaman zaman elle güncelle.
SANITY_BAND = {"USD": (10.0, 200.0), "EUR": (10.0, 220.0)}
MAX_DAILY_JUMP = 0.20  # %20


class TcmbFxRecord(BaseModel):
    currency: str
    buy: float
    sell: float
    quoted_at: datetime

    @model_validator(mode="after")
    def _buy_lt_sell(self):
        if self.buy <= 0 or self.sell <= 0:
            raise ValueError(f"{self.currency}: alış/satış pozitif olmalı")
        if self.buy > self.sell:
            raise ValueError(f"{self.currency}: alış ({self.buy}) satıştan ({self.sell}) büyük olamaz")
        return self


class TcmbCollector(Collector):
    name = "fx_tcmb"
    schema = TcmbFxRecord
    # config/sources.yaml -> fx_endpoints.tcmb
    default_source = "tcmb"

    def fetch(self) -> bytes:
        resp = http.get(URL, timeout=15, headers={"User-Agent": "finans-agent/0.1"})
        resp.raise_for_status()
        return resp.content

    def parse(self, raw: bytes) -> list[TcmbFxRecord]:
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise ParseError(str(exc)) from exc

        date_str = root.get("Tarih")
        if not date_str:
            raise ParseError("Tarih_Date kökünde 'Tarih' özniteliği yok")
        quoted_at = datetime.strptime(date_str, "%d.%m.%Y").replace(tzinfo=timezone.utc)

        records: list[TcmbFxRecord] = []
        for currency_el in root.findall("Currency"):
            code = currency_el.get("Kod") or currency_el.get("CurrencyCode")
            if code not in CURRENCIES:
                continue
            buy_text = currency_el.findtext("ForexBuying") or currency_el.findtext("BanknoteBuying")
            sell_text = currency_el.findtext("ForexSelling") or currency_el.findtext("BanknoteSelling")
            if not buy_text or not sell_text:
                continue
            records.append(
                TcmbFxRecord(
                    currency=code, buy=float(buy_text), sell=float(sell_text), quoted_at=quoted_at
                )
            )

        if not records:
            raise ParseError("USD/EUR kaydı bulunamadı — TCMB XML şeması değişmiş olabilir")
        return records

    def sanity_check(self, records: list[TcmbFxRecord]) -> None:
        with SessionLocal() as session:
            for r in records:
                low, high = SANITY_BAND.get(r.currency, (0, float("inf")))
                if not (low <= r.buy <= high and low <= r.sell <= high):
                    raise SanityCheckError(f"{r.currency}: {r.buy}/{r.sell} bant dışında ({low}-{high})")

                prev = session.execute(
                    select(FxQuote)
                    .where(FxQuote.institution == "TCMB", FxQuote.currency == r.currency)
                    .order_by(FxQuote.quoted_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if prev is not None:
                    change = abs(r.sell - float(prev.sell)) / float(prev.sell)
                    if change > MAX_DAILY_JUMP:
                        raise SanityCheckError(
                            f"{r.currency}: günlük değişim %{change*100:.1f}, sınır %{MAX_DAILY_JUMP*100:.0f}"
                        )

    def persist(self, records: list[TcmbFxRecord], run_id: int) -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal() as session:
            for r in records:
                exists = session.execute(
                    select(FxQuote).where(
                        FxQuote.institution == "TCMB",
                        FxQuote.currency == r.currency,
                        FxQuote.quoted_at == r.quoted_at,
                    )
                ).scalar_one_or_none()
                if exists:
                    # Aynı kotasyon anı yeniden görüldü. Satırı silmiyoruz ama
                    # fetched_at'i tazeliyoruz: bu alan "bu değeri kaynakta EN SON
                    # ne zaman gördük" demek ve panelde "veri çekilme zamanı"
                    # olarak gösteriliyor. Güncellenmezse, kaynağı beş dakika önce
                    # kontrol etmiş olmamıza rağmen veri saatler öncesinden
                    # kalmış gibi görünür. buy/sell de yazılıyor ki kaynak aynı
                    # zaman damgasıyla bir düzeltme yayınlarsa yakalansın.
                    exists.buy = r.buy
                    exists.sell = r.sell
                    exists.fetched_at = now
                    exists.run_id = run_id
                    continue
                session.add(
                    FxQuote(
                        institution="TCMB",
                        currency=r.currency,
                        buy=r.buy,
                        sell=r.sell,
                        quoted_at=r.quoted_at,
                        fetched_at=now,
                        run_id=run_id,
                    )
                )
            session.commit()


if __name__ == "__main__":
    result = TcmbCollector().run()
    print(result)
