from datetime import date

import pytest

from config.loader import resolve_deposit_brackets, resolve_fund_withholding, resolve_loan_taxes


def test_resolve_loan_taxes_housing_is_zero():
    kkdf, bsmv = resolve_loan_taxes("housing", date(2026, 6, 1))
    assert kkdf == pytest.approx(0.0)
    assert bsmv == pytest.approx(0.0)


def test_resolve_loan_taxes_vehicle():
    kkdf, bsmv = resolve_loan_taxes("vehicle", date(2026, 6, 1))
    assert kkdf == pytest.approx(0.15)
    assert bsmv == pytest.approx(0.05)


def test_resolve_deposit_brackets_try():
    brackets = resolve_deposit_brackets("TRY", date(2026, 6, 1))
    assert brackets[0]["rate"] == pytest.approx(0.175)
    assert brackets[-1]["max_days"] is None


def test_resolve_fund_withholding():
    assert resolve_fund_withholding(is_equity_heavy=True) == pytest.approx(0.0)
    assert resolve_fund_withholding(is_equity_heavy=False) == pytest.approx(0.175)


def test_resolve_loan_taxes_before_any_entry_raises():
    with pytest.raises(ValueError):
        resolve_loan_taxes("housing", date(2000, 1, 1))
