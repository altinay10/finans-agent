"""TEFAS fon fiyat serisi — EMEKLİYE AYRILDI, KULLANILMIYOR.

!!! Bu toplayıcı worker.py'de KAYITLI DEĞİL ve çalıştırılmamalıdır. !!!
Yerine collectors/fund_prices.py + fund_providers.py kullanılıyor.

2026-08-23'te elle doğrulanan üç bağımsız engel:
  1. Aşağıdaki uç nokta HTTP 404 veriyor (site Next.js'e taşınmış).
  2. tefas.gov.tr/robots.txt açıkça `Disallow: /api/` diyor.
  3. Ana sayfaya normal tarayıcı gezintisinde bile WAF "Request Rejected"
     ("consult with your administrator") blok sayfası dönüyor.

Dosya, gelecekte biri "TEFAS'ı neden kullanmıyoruz?" diye sorduğunda
cevabın kayıtlı kalması için duruyor. Silme; ama çalıştırmayı da deneme —
engelleri aşmaya çalışmak tasarım §02 toplama disiplinine aykırıdır.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import yaml
from pydantic import BaseModel
from sqlalchemy import select

from collectors.base import Collector, ParseError, SanityCheckError
from store.db import REPO_ROOT, SessionLocal
from store.models import FundPrice

URL = "https://www.tefas.gov.tr/api/DB/BindHistoryInfo"
HEADERS = {
    "User-Agent": "finans-agent/0.1",
    "Referer": "https://www.tefas.gov.tr/TarihselVeriler.aspx",
    "Content-Type": "application/x-www-form-urlencoded",
}
BACKFILL_DAYS = 400  # >1 yıl senaryosu için biraz pay bırak
SANITY_MAX_DAILY_MOVE = 0.30  # tek günde %30'dan fazla fiyat hareketi şüpheli


class TefasPriceRecord(BaseModel):
    fund_code: str
    price_date: date
    price: float


def _load_fund_codes() -> list[str]:
    with open(REPO_ROOT / "config" / "sources.yaml", encoding="utf-8") as f:
        sources = yaml.safe_load(f)
    return [row["code"] for row in sources.get("funds", [])]


class TefasCollector(Collector):
    name = "tefas"
    schema = TefasPriceRecord

    def __init__(self, fund_codes: list[str] | None = None, backfill_days: int = BACKFILL_DAYS) -> None:
        super().__init__()
        self.fund_codes = fund_codes or _load_fund_codes()
        self.backfill_days = backfill_days

    def fetch(self) -> bytes:
        end = date.today()
        start = end - timedelta(days=self.backfill_days)
        payload_by_fund: dict[str, str] = {}
        with httpx.Client(timeout=20, headers=HEADERS) as client:
            for code in self.fund_codes:
                data = {
                    "fontip": "YAT",
                    "sfontur": "",
                    "fonkod": code,
                    "fongrup": "",
                    "bastarih": start.strftime("%d.%m.%Y"),
                    "bittarih": end.strftime("%d.%m.%Y"),
                    "fonturkod": "",
                    "fonunvantip": "",
                }
                resp = client.post(URL, data=data)
                resp.raise_for_status()
                payload_by_fund[code] = resp.text
        return json.dumps(payload_by_fund).encode("utf-8")

    def parse(self, raw: bytes) -> list[TefasPriceRecord]:
        try:
            payload_by_fund: dict[str, str] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError(str(exc)) from exc

        records: list[TefasPriceRecord] = []
        for code, body in payload_by_fund.items():
            try:
                parsed = json.loads(body)
                rows = parsed["data"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ParseError(f"{code}: beklenmeyen TEFAS yanıtı ({exc})") from exc

            for row in rows:
                try:
                    price_date = datetime.strptime(row["TARIH"], "%d.%m.%Y").date()
                    price = float(row["FIYAT"])
                except (KeyError, ValueError) as exc:
                    raise ParseError(f"{code}: satır ayrıştırılamadı ({exc})") from exc
                records.append(TefasPriceRecord(fund_code=code, price_date=price_date, price=price))

        if not records:
            raise ParseError("Hiçbir fon için fiyat satırı bulunamadı")
        return records

    def sanity_check(self, records: list[TefasPriceRecord]) -> None:
        by_fund: dict[str, list[TefasPriceRecord]] = {}
        for r in records:
            if r.price <= 0:
                raise SanityCheckError(f"{r.fund_code} {r.price_date}: fiyat pozitif olmalı")
            by_fund.setdefault(r.fund_code, []).append(r)

        for code, rows in by_fund.items():
            rows.sort(key=lambda r: r.price_date)
            for prev, cur in zip(rows, rows[1:]):
                if prev.price == 0:
                    continue
                move = abs(cur.price - prev.price) / prev.price
                if move > SANITY_MAX_DAILY_MOVE:
                    raise SanityCheckError(
                        f"{code}: {prev.price_date}->{cur.price_date} arası %{move*100:.1f} hareket"
                    )

    def persist(self, records: list[TefasPriceRecord], run_id: int) -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal() as session:
            for r in records:
                exists = session.execute(
                    select(FundPrice).where(
                        FundPrice.fund_code == r.fund_code, FundPrice.price_date == r.price_date
                    )
                ).scalar_one_or_none()
                if exists:
                    exists.price = r.price
                    exists.fetched_at = now
                else:
                    session.add(
                        FundPrice(
                            fund_code=r.fund_code, price_date=r.price_date, price=r.price, fetched_at=now
                        )
                    )
            session.commit()


if __name__ == "__main__":
    result = TefasCollector().run()
    print(result)
