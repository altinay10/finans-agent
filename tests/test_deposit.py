import pytest

from core.deposit import resolve_withholding, rollover_return, single_term_return
from core.models import DepositInput

TRY_BRACKETS = [
    {"max_days": 180, "rate": 0.175},
    {"max_days": 365, "rate": 0.150},
    {"max_days": None, "rate": 0.100},
]


def test_resolve_withholding_brackets():
    assert resolve_withholding(TRY_BRACKETS, 32) == pytest.approx(0.175)
    assert resolve_withholding(TRY_BRACKETS, 180) == pytest.approx(0.175)
    assert resolve_withholding(TRY_BRACKETS, 181) == pytest.approx(0.150)
    assert resolve_withholding(TRY_BRACKETS, 365) == pytest.approx(0.150)
    assert resolve_withholding(TRY_BRACKETS, 400) == pytest.approx(0.100)


def test_resolve_withholding_no_match_raises():
    with pytest.raises(ValueError):
        resolve_withholding([{"max_days": 10, "rate": 0.1}], 20)


def test_single_term_return_golden_value():
    deposit = DepositInput(principal=500_000, annual_rate=0.45, term_days=32, withholding_rate=0.175)
    result = single_term_return(deposit)
    assert result.gross_return == pytest.approx(19726.027397260274)
    assert result.net_return == pytest.approx(16273.972602739725)
    assert result.maturity_value == pytest.approx(500_000 + 16273.972602739725)


def test_rollover_return_golden_value():
    result = rollover_return(
        principal=500_000, annual_rate=0.45, term_days=32, withholding_rate=0.175, horizon_days=365
    )
    assert result.period_net_rate == pytest.approx(0.03254794520547945)
    assert result.annualized_net_return == pytest.approx(220499.24236844198)
    assert result.rollovers == 11


def test_rollover_differs_from_single_365_day_term():
    # Tasarım notu: 32 günü 11 kez çevirmek, 365 gün tek vadeden farklı sonuç verir
    single = single_term_return(
        DepositInput(principal=500_000, annual_rate=0.45, term_days=365, withholding_rate=0.10)
    )
    rolled = rollover_return(
        principal=500_000, annual_rate=0.45, term_days=32, withholding_rate=0.175, horizon_days=365
    )
    assert single.net_return != pytest.approx(rolled.annualized_net_return)
