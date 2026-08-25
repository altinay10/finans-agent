"""Depolama katmanı entegrasyon testleri — gerçek SQLite, gerçek sorgular.

Bunlar birim testi değil: şema, ORM ve `store/queries.py` birlikte çalışıyor mu
onu doğrular. Özellikle tasarım §03'ün "tek büyük tuzak" dediği tutar kademesi
seçimi burada kilitleniyor.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from store.db import SessionLocal
from store.models import DepositRate, FundPrice, FxQuote, LoanRate, ScrapeRun
from store import queries


def _fx(institution, currency, buy, sell, quoted_at, **kw):
    return FxQuote(
        institution=institution,
        currency=currency,
        buy=buy,
        sell=sell,
        quoted_at=quoted_at,
        fetched_at=datetime.now(timezone.utc),
        **kw,
    )


def _deposit(institution, term_days, amount_min, amount_max, rate, valid_date, currency="TRY"):
    return DepositRate(
        institution=institution,
        currency=currency,
        term_days=term_days,
        amount_min=amount_min,
        amount_max=amount_max,
        annual_rate=rate,
        is_profit_share=False,
        valid_date=valid_date,
        fetched_at=datetime.now(timezone.utc),
    )


# ------------------------------------------------------------------- fx ----

def test_latest_fx_returns_only_newest_per_institution_currency(seeded_db):
    base = datetime(2026, 8, 20, 10, 0)
    with SessionLocal() as s:
        s.add_all(
            [
                _fx("TCMB", "USD", 40.0, 40.1, base),
                _fx("TCMB", "USD", 47.0, 47.1, base + timedelta(days=1)),  # en güncel
                _fx("TCMB", "EUR", 55.0, 55.1, base),
                _fx("TEB", "USD", 46.0, 46.9, base + timedelta(days=1)),
            ]
        )
        s.commit()

    rows = queries.latest_fx_by_institution()
    assert len(rows) == 3
    usd_tcmb = next(r for r in rows if r["institution"] == "TCMB" and r["currency"] == "USD")
    assert usd_tcmb["buy"] == pytest.approx(47.0)
    assert usd_tcmb["quoted_at"] == base + timedelta(days=1)


def test_fx_estimated_timestamp_flag_round_trips(seeded_db):
    with SessionLocal() as s:
        s.add(_fx("EMLAKKATILIM", "USD", 47.3, 49.3, datetime(2026, 8, 23), quoted_at_is_estimated=True))
        s.commit()
    row = next(r for r in queries.latest_fx_by_institution() if r["institution"] == "EMLAKKATILIM")
    assert row["quoted_at_is_estimated"] is True


# -------------------------------------------------------------- deposit ----

def test_deposit_bracket_selection_picks_correct_tier(seeded_db):
    """Tasarım §03/§10: anapara yanlış kademeye düşerse panel yanlış rakam gösterir."""
    today = date(2026, 8, 23)
    with SessionLocal() as s:
        s.add_all(
            [
                _deposit("VAKIFBANK", 32, 0, 1_000_000, 0.42, today),
                _deposit("VAKIFBANK", 32, 1_000_001, None, 0.38, today),
            ]
        )
        s.commit()

    low = queries.deposit_rates_for_amount(500_000)
    assert len(low) == 1 and low[0]["annual_rate"] == pytest.approx(0.42)

    high = queries.deposit_rates_for_amount(2_000_000)
    assert len(high) == 1 and high[0]["annual_rate"] == pytest.approx(0.38)


def test_deposit_bracket_boundaries_are_inclusive(seeded_db):
    today = date(2026, 8, 23)
    with SessionLocal() as s:
        s.add_all(
            [
                _deposit("VAKIFBANK", 32, 0, 1_000_000, 0.42, today),
                _deposit("VAKIFBANK", 32, 1_000_001, None, 0.38, today),
            ]
        )
        s.commit()
    # Tam sınır değeri alt kademeye ait olmalı
    exact = queries.deposit_rates_for_amount(1_000_000)
    assert len(exact) == 1 and exact[0]["annual_rate"] == pytest.approx(0.42)


def test_deposit_null_amount_max_means_unbounded(seeded_db):
    today = date(2026, 8, 23)
    with SessionLocal() as s:
        s.add(_deposit("VAKIFBANK", 365, 1_000_001, None, 0.38, today))
        s.commit()
    assert len(queries.deposit_rates_for_amount(999_999_999)) == 1


def test_deposit_returns_only_latest_valid_date(seeded_db):
    with SessionLocal() as s:
        s.add_all(
            [
                _deposit("VAKIFBANK", 32, 0, None, 0.40, date(2026, 8, 22)),
                _deposit("VAKIFBANK", 32, 0, None, 0.45, date(2026, 8, 23)),  # daha güncel
            ]
        )
        s.commit()
    rows = queries.deposit_rates_for_amount(100_000)
    assert len(rows) == 1
    assert rows[0]["annual_rate"] == pytest.approx(0.45)


def test_deposit_currency_filter_isolates_currencies(seeded_db):
    today = date(2026, 8, 23)
    with SessionLocal() as s:
        s.add_all(
            [
                _deposit("VAKIFBANK", 32, 0, None, 0.45, today, currency="TRY"),
                _deposit("VAKIFBANK", 32, 0, None, 0.0005, today, currency="USD"),
            ]
        )
        s.commit()
    assert queries.deposit_rates_for_amount(100_000, "TRY")[0]["annual_rate"] == pytest.approx(0.45)
    assert queries.deposit_rates_for_amount(100_000, "USD")[0]["annual_rate"] == pytest.approx(0.0005)


# ----------------------------------------------------------------- loan ----

def test_latest_loan_rates_filters_by_type_and_recency(seeded_db):
    with SessionLocal() as s:
        s.add_all(
            [
                LoanRate(institution="VAKIFBANK", loan_type="housing", monthly_rate=0.030,
                         valid_date=date(2026, 8, 22), fetched_at=datetime.now(timezone.utc)),
                LoanRate(institution="VAKIFBANK", loan_type="housing", monthly_rate=0.0295,
                         valid_date=date(2026, 8, 23), fetched_at=datetime.now(timezone.utc)),
                LoanRate(institution="VAKIFBANK", loan_type="personal", monthly_rate=0.0499,
                         valid_date=date(2026, 8, 23), fetched_at=datetime.now(timezone.utc)),
            ]
        )
        s.commit()

    housing = queries.latest_loan_rates("housing")
    assert len(housing) == 1 and housing[0]["monthly_rate"] == pytest.approx(0.0295)
    assert queries.latest_loan_rates("personal")[0]["monthly_rate"] == pytest.approx(0.0499)
    assert queries.latest_loan_rates("vehicle") == []  # veri yoksa boş liste, hata değil


# ----------------------------------------------------------------- fund ----

def test_fund_price_series_respects_range_and_order(seeded_db):
    with SessionLocal() as s:
        for i in range(10):
            s.add(FundPrice(fund_code="AK3", price_date=date(2026, 8, 1) + timedelta(days=i),
                            price=50 + i, fetched_at=datetime.now(timezone.utc)))
        s.commit()

    series = queries.fund_price_series("AK3", date(2026, 8, 3), date(2026, 8, 6))
    assert [d for d, _ in series] == [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)]
    assert series == sorted(series)  # tarih sırasında olmalı


def test_list_funds_exposes_equity_flag(seeded_db):
    funds = {f["code"]: f for f in queries.list_funds()}
    assert funds["AK3"]["is_equity_heavy"] is True
    assert funds["AFA"]["is_equity_heavy"] is False


# ------------------------------------------------------------ freshness ----

def test_freshness_view_reports_last_ok_and_fallback_count(seeded_db):
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        s.add_all(
            [
                ScrapeRun(collector="fx_tcmb", status="ok", started_at=now, finished_at=now, rows_written=2),
                ScrapeRun(collector="fx_banks", status="failed", started_at=now, finished_at=now, error="boom"),
                ScrapeRun(collector="fx_banks", status="llm_fallback", started_at=now, finished_at=now),
            ]
        )
        s.commit()

    rows = {r["collector"]: r for r in queries.freshness()}
    assert rows["fx_tcmb"]["last_ok"] is not None
    assert rows["fx_tcmb"]["fallback_count"] == 0
    # Hiç başarılı çalışmayan collector last_ok=None ile görünmeli (sessiz bayatlama sinyali)
    assert rows["fx_banks"]["last_ok"] is None
    assert rows["fx_banks"]["fallback_count"] == 1


def test_overlapping_amount_tiers_collapse_to_the_better_rate(db):
    """Kademe sınırları uçta örtüşünce aynı vade için iki satır dönmemeli.

    VakıfBank canlı veride hem 500.001-1.000.000 hem 1.000.000-2.999.999
    kademesini yayınlıyor; tam 1.000.000 TL ikisine de düşüyor.
    """
    from store import queries

    with SessionLocal() as s:
        s.add_all(
            [
                DepositRate(
                    institution="VAKIFBANK", currency="TRY", term_days=32,
                    amount_min=500_001, amount_max=1_000_000, annual_rate=0.41,
                    is_profit_share=False, valid_date=date.today(),
                    fetched_at=datetime.now(timezone.utc),
                ),
                DepositRate(
                    institution="VAKIFBANK", currency="TRY", term_days=32,
                    amount_min=1_000_000, amount_max=2_999_999, annual_rate=0.36,
                    is_profit_share=False, valid_date=date.today(),
                    fetched_at=datetime.now(timezone.utc),
                ),
            ]
        )
        s.commit()

    rows = queries.deposit_rates_for_amount(1_000_000, "TRY")
    matching = [r for r in rows if r["institution"] == "VAKIFBANK" and r["term_days"] == 32]
    assert len(matching) == 1, "örtüşen kademeler tek satıra indirgenmedi"
    assert matching[0]["annual_rate"] == pytest.approx(0.41), "müşteri lehine oran seçilmedi"


def test_non_overlapping_tiers_are_untouched(db):
    """İndirgeme yalnızca ÖRTÜŞEN kademeler için — farklı vadeler korunmalı."""
    from store import queries

    with SessionLocal() as s:
        s.add_all(
            [
                DepositRate(
                    institution="TEB", currency="TRY", term_days=32,
                    amount_min=0, amount_max=None, annual_rate=0.40,
                    is_profit_share=False, valid_date=date.today(),
                    fetched_at=datetime.now(timezone.utc),
                ),
                DepositRate(
                    institution="TEB", currency="TRY", term_days=92,
                    amount_min=0, amount_max=None, annual_rate=0.38,
                    is_profit_share=False, valid_date=date.today(),
                    fetched_at=datetime.now(timezone.utc),
                ),
            ]
        )
        s.commit()

    rows = [r for r in queries.deposit_rates_for_amount(1_000_000, "TRY") if r["institution"] == "TEB"]
    assert {r["term_days"] for r in rows} == {32, 92}
