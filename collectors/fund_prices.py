"""Fon fiyat toplayıcısı — çok sağlayıcılı.

`config/funds.yaml`'daki her fon, kendi sağlayıcısı üzerinden çekilir
(bkz. collectors/fund_providers.py). Eski `collectors/akportfoy.py`'nin
yerini alır; tek sağlayıcıya bağlı olması, kullanıcının "istediğim fonu
ekleyebileyim" isteğinin önündeki asıl engeldi.

DAYANIKLILIK: bir fonun sayfası bozulduğunda diğerleri toplanmaya devam
eder (fx_banks/deposit_rates ile aynı desen). Her fon `source_runs`'a
kendi satırını yazar — "hangi fon düştü" sorusu ancak böyle cevaplanır.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel
from sqlalchemy import select

from collectors import http
from collectors.base import Collector, ParseError, SanityCheckError
from collectors.fund_providers import PROVIDERS, REQUEST_DELAY_SECONDS, FundSpec
from store.clock import istanbul_today, utc_now
from store.db import REPO_ROOT, SessionLocal
from store.models import Fund, FundPrice

logger = logging.getLogger(__name__)

FUNDS_YAML = REPO_ROOT / "config" / "funds.yaml"

# Tek günde bundan fazla hareket, fiyat serisinde bir ölçek hatası
# (kuruş/lira karışması, bölünme) işaretidir. Fon fiyatları böyle
# sıçramaz; sıçrıyorsa veri yazılmaz.
SANITY_MAX_DAILY_MOVE = 0.30


class FundPriceRecord(BaseModel):
    fund_code: str
    price_date: date
    price: float
    fund_name: str | None = None
    is_equity_heavy: bool | None = None


def load_fund_registry(path: Path | None = None) -> list[FundSpec]:
    """config/funds.yaml -> FundSpec listesi.

    Bilinmeyen sağlayıcılı satırlar ATLANIR ve loglanır: panelden elle
    eklenen bir satırdaki yazım hatası tüm toplamayı düşürmemeli.
    """
    path = path or FUNDS_YAML
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        body = yaml.safe_load(f) or {}
    specs: list[FundSpec] = []
    for row in body.get("funds", []) or []:
        provider = row.get("provider")
        if provider not in PROVIDERS:
            logger.warning(
                "funds.yaml: '%s' fonunun sağlayıcısı tanınmıyor (%s) — atlanıyor",
                row.get("code"), provider,
            )
            continue
        specs.append(
            FundSpec(
                code=row["code"],
                provider=provider,
                ref=str(row.get("ref") or row["code"]),
                name=row.get("name"),
                benchmark=row.get("benchmark"),
            )
        )
    return specs


def add_fund_to_registry(
    *, code: str, provider: str, ref: str, name: str | None = None,
    benchmark: str | None = None, path: Path | None = None,
) -> None:
    """Panelden fon ekleme — YAML'a bir satır yazar.

    Neden YAML'a yazıyoruz da doğrudan veritabanına değil: kayıt defteri
    tek doğruluk kaynağı olmalı. Fon yalnızca veritabanına eklenseydi,
    `seed_reference_data` bir sonraki açılışta onu "kayıt defterinde yok"
    diye silerdi.
    """
    path = path or FUNDS_YAML
    body = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            body = yaml.safe_load(f) or {}
    funds = body.get("funds") or []
    if any((row.get("code") or "").upper() == code.upper() for row in funds):
        raise ValueError(f"{code} zaten kayıtlı")
    entry = {"code": code.upper(), "provider": provider, "ref": ref}
    if name:
        entry["name"] = name
    if benchmark:
        entry["benchmark"] = benchmark
    funds.append(entry)
    body["funds"] = funds

    # Dosyanın BAŞLIK YORUMLARI korunur. yaml.safe_dump yorumları atar;
    # ilk panelden ekleme, alanların ne anlama geldiğini ve
    # `is_equity_heavy`in neden burada tutulmadığını anlatan açıklamaları
    # sessizce silmişti. Bir sonraki okuyan (insan ya da model) o bilgiyi
    # kaybetmesin diye `funds:` anahtarından önceki blok aynen geri yazılır.
    header = ""
    if path.exists():
        original = path.read_text(encoding="utf-8")
        marker = original.find("funds:")
        if marker > 0:
            header = original[:marker]

    with open(path, "w", encoding="utf-8") as f:
        if header:
            f.write(header)
        yaml.safe_dump(body, f, allow_unicode=True, sort_keys=False)


class FundPriceCollector(Collector):
    name = "fund_prices"
    schema = FundPriceRecord

    def __init__(self, specs: list[FundSpec] | None = None) -> None:
        super().__init__()
        self.specs = specs if specs is not None else load_fund_registry()

    def fetch(self) -> bytes:
        payloads: dict[str, dict] = {}
        for index, spec in enumerate(self.specs):
            if index:
                time.sleep(REQUEST_DELAY_SECONDS)
            provider = PROVIDERS[spec.provider]
            started = time.monotonic()
            with http.source(spec.code):
                try:
                    payloads[spec.code] = {
                        "ok": True,
                        "provider": spec.provider,
                        "ref": spec.ref,
                        "body": provider.fetch(spec),
                    }
                    self.record_source(
                        spec.code, phase="fetch", status="ok",
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                except Exception as exc:  # noqa: BLE001 - tek fon hepsini düşürmesin
                    logger.warning("fund_prices/%s: fetch başarısız: %s", spec.code, exc)
                    payloads[spec.code] = {"ok": False, "error": str(exc)}
                    self.record_source(
                        spec.code, phase="fetch", status="failed", error=str(exc),
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
        self._record_provider_summary(payloads)
        return json.dumps(payloads).encode("utf-8")

    def _record_provider_summary(self, payloads: dict[str, dict]) -> None:
        """Fon başına satırın YANINDA, SAĞLAYICI başına bir özet satır.

        İki farklı soruya iki farklı kayıt gerekiyor:
          * "hangi FON düştü"       -> source = fon kodu (yukarıdaki döngü)
          * "SAĞLAYICI ayakta mı"   -> source = sağlayıcı anahtarı (burası)
        İkincisi olmadan Kaynaklar sekmesi, `config/sources.yaml`'daki
        `fund_endpoints.akportfoy` satırını hiçbir koşuyla eşleştiremez ve
        çalışan bir kaynağı "başarılı koşu yok" diye işaretler.
        """
        by_provider: dict[str, list[bool]] = {}
        for spec in self.specs:
            entry = payloads.get(spec.code) or {}
            by_provider.setdefault(spec.provider, []).append(bool(entry.get("ok")))
        for provider, results in by_provider.items():
            ok_count = sum(results)
            self.record_source(
                provider,
                phase="fetch",
                status="ok" if ok_count else "failed",
                rows=ok_count,
                error=(
                    None if ok_count == len(results)
                    else f"{len(results) - ok_count}/{len(results)} fon çekilemedi"
                ),
            )

    def parse(self, raw: bytes) -> list[FundPriceRecord]:
        payloads: dict[str, dict] = json.loads(raw)
        by_code = {spec.code: spec for spec in self.specs}
        records: list[FundPriceRecord] = []

        for code, entry in payloads.items():
            if not entry.get("ok"):
                continue
            spec = by_code.get(code) or FundSpec(
                code=code, provider=entry.get("provider", ""), ref=entry.get("ref", code)
            )
            provider = PROVIDERS.get(spec.provider)
            if provider is None:
                self.record_source(
                    code, phase="parse", status="failed",
                    error=f"sağlayıcı tanınmıyor: {spec.provider}",
                )
                continue
            try:
                series = provider.parse(spec, entry["body"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("fund_prices/%s: parse başarısız: %s", code, exc)
                self.record_source(code, phase="parse", status="failed", error=str(exc))
                continue

            if not series.points:
                self.record_source(
                    code, phase="parse", status="empty",
                    error="sıfır fiyat noktası — sayfa şeması değişmiş olabilir",
                )
                continue
            self.record_source(code, phase="parse", status="ok", rows=len(series.points))
            for price_date, price in series.points:
                records.append(
                    FundPriceRecord(
                        fund_code=series.code,
                        price_date=price_date,
                        price=price,
                        fund_name=series.name,
                        is_equity_heavy=series.is_equity_heavy,
                    )
                )

        if not records:
            raise ParseError("Hiçbir fon için fiyat satırı bulunamadı")
        return records

    def sanity_check(self, records: list[FundPriceRecord]) -> None:
        today = istanbul_today()
        by_fund: dict[str, list[FundPriceRecord]] = {}
        for r in records:
            if r.price <= 0:
                raise SanityCheckError(f"{r.fund_code} {r.price_date}: fiyat pozitif olmalı")
            if r.price_date > today:
                raise SanityCheckError(f"{r.fund_code}: gelecek tarihli fiyat ({r.price_date})")
            by_fund.setdefault(r.fund_code, []).append(r)

        for code, rows in by_fund.items():
            rows.sort(key=lambda r: r.price_date)
            dates = [r.price_date for r in rows]
            if len(dates) != len(set(dates)):
                raise SanityCheckError(f"{code}: aynı tarih için birden fazla fiyat var")
            for prev, cur in zip(rows, rows[1:]):
                move = abs(cur.price - prev.price) / prev.price
                if move > SANITY_MAX_DAILY_MOVE:
                    raise SanityCheckError(
                        f"{code}: {prev.price_date}->{cur.price_date} arası %{move*100:.1f} hareket"
                    )

    def persist(self, records: list[FundPriceRecord], run_id: int) -> None:
        now = utc_now()
        benchmarks = {spec.code: spec.benchmark for spec in self.specs}
        with SessionLocal() as session:
            # Fon kataloğunu sağlayıcıdan okunan GERÇEK ad/sınıflandırmayla hizala.
            meta: dict[str, FundPriceRecord] = {}
            for r in records:
                if r.fund_code not in meta and r.fund_name:
                    meta[r.fund_code] = r
            for code, r in meta.items():
                fund = session.get(Fund, code)
                if fund is None:
                    session.add(
                        Fund(
                            code=code,
                            name=r.fund_name,
                            is_equity_heavy=bool(r.is_equity_heavy),
                            benchmark=benchmarks.get(code),
                        )
                    )
                else:
                    fund.name = r.fund_name or fund.name
                    if r.is_equity_heavy is not None:
                        fund.is_equity_heavy = r.is_equity_heavy
                    if benchmarks.get(code):
                        fund.benchmark = benchmarks[code]

            existing = {
                (fp.fund_code, fp.price_date): fp
                for fp in session.execute(
                    select(FundPrice).where(
                        FundPrice.fund_code.in_({r.fund_code for r in records})
                    )
                ).scalars()
            }
            for r in records:
                found = existing.get((r.fund_code, r.price_date))
                if found is not None:
                    found.price = r.price
                    found.fetched_at = now
                else:
                    session.add(
                        FundPrice(
                            fund_code=r.fund_code,
                            price_date=r.price_date,
                            price=r.price,
                            fetched_at=now,
                        )
                    )
            session.commit()


if __name__ == "__main__":
    print(FundPriceCollector().run())
