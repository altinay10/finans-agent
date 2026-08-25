from datetime import date

import pytest

from core.fund import nearest_prior_price, simulate
from core.models import FundSimInput


def test_simulate_golden_value():
    sim = FundSimInput(
        principal=100_000,
        price_start=10.0,
        price_end=12.5,
        date_start=date(2025, 8, 23),
        date_end=date(2026, 8, 23),
        withholding_rate=0.175,
        is_equity_heavy=False,
    )
    result = simulate(sim)
    assert result.units == pytest.approx(10_000.0)
    assert result.value_end == pytest.approx(125_000.0)
    assert result.gross_return == pytest.approx(25_000.0)
    assert result.net_return == pytest.approx(20_625.0)
    assert result.maturity_value == pytest.approx(120_625.0)
    assert result.withholding_applied is True


def test_simulate_loss_no_withholding():
    sim = FundSimInput(
        principal=100_000,
        price_start=10.0,
        price_end=8.0,
        date_start=date(2025, 8, 23),
        date_end=date(2026, 8, 23),
        withholding_rate=0.175,
    )
    result = simulate(sim)
    assert result.gross_return == pytest.approx(-20_000.0)
    # zararda stopaj uygulanmaz — net == brüt
    assert result.net_return == pytest.approx(result.gross_return)
    assert result.withholding_applied is False


def test_simulate_equity_heavy_fund_exempt():
    sim = FundSimInput(
        principal=100_000,
        price_start=10.0,
        price_end=12.5,
        date_start=date(2025, 8, 23),
        date_end=date(2026, 8, 23),
        withholding_rate=0.175,
        is_equity_heavy=True,
    )
    result = simulate(sim)
    assert result.withholding_applied is False
    assert result.net_return == pytest.approx(result.gross_return)


def test_nearest_prior_price_exact_match():
    prices = [(date(2026, 1, 1), 10.0), (date(2026, 1, 2), 10.5), (date(2026, 1, 3), 11.0)]
    found = nearest_prior_price(prices, date(2026, 1, 2))
    assert found == (date(2026, 1, 2), 10.5)


def test_nearest_prior_price_skips_weekend_gap():
    # Cuma fiyatı var, Cumartesi/Pazar yok — Pazar sorgusu Cuma'ya düşmeli
    prices = [(date(2026, 1, 2), 10.0), (date(2026, 1, 5), 10.8)]  # Cuma, sonraki Pazartesi
    found = nearest_prior_price(prices, date(2026, 1, 4))  # Pazar
    assert found == (date(2026, 1, 2), 10.0)


def test_nearest_prior_price_never_looks_forward():
    prices = [(date(2026, 1, 5), 10.8)]
    with pytest.raises(ValueError):
        nearest_prior_price(prices, date(2026, 1, 1))
