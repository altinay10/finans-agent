import pytest

from core.loan import amortize, calculate_installment, effective_monthly_rate
from core.models import LoanInput


def test_effective_monthly_rate():
    assert effective_monthly_rate(0.03, 0.15, 0.05) == pytest.approx(0.036)


def test_installment_golden_value():
    # P=100.000, %3 aylık, 12 ay, taşıt kredisi vergileriyle (kkdf %15, bsmv %5)
    inst = calculate_installment(100_000, 0.036, 12)
    assert inst == pytest.approx(10409.390302552507)


def test_amortize_schedule_sums_and_zeros_out():
    loan = LoanInput(principal=100_000, monthly_rate=0.03, term_months=12, kkdf=0.15, bsmv=0.05)
    result = amortize(loan)

    assert result.installment == pytest.approx(10409.390302552507)
    assert result.effective_monthly_rate == pytest.approx(0.036)
    assert result.total_interest == pytest.approx(20760.569692191846)
    assert result.total_kkdf == pytest.approx(3114.0854538287767)
    assert result.total_bsmv == pytest.approx(1038.0284846095924)
    assert result.total_paid == pytest.approx(124912.68363063008)

    assert len(result.schedule) == 12
    # son satırda bakiye sıfırlanmalı
    assert result.schedule[-1].remaining_balance == pytest.approx(0.0, abs=1e-6)
    # her taksit sabit olmalı (anüite)
    assert all(row.installment == pytest.approx(result.installment) for row in result.schedule)
    # anapara + faiz + vergiler taksite eşit olmalı, her satırda
    for row in result.schedule:
        assert row.principal_paid + row.interest + row.kkdf_amount + row.bsmv_amount == pytest.approx(
            row.installment
        )


def test_housing_loan_zero_taxes():
    # Konut kredisi: KKDF %0, BSMV %0 — efektif oran = ilan edilen oran
    loan = LoanInput(principal=1_000_000, monthly_rate=0.025, term_months=120, kkdf=0.0, bsmv=0.0)
    result = amortize(loan)
    assert result.effective_monthly_rate == pytest.approx(0.025)
    assert result.total_kkdf == pytest.approx(0.0)
    assert result.total_bsmv == pytest.approx(0.0)
    assert result.schedule[-1].remaining_balance == pytest.approx(0.0, abs=1e-6)


def test_zero_rate_loan_is_linear():
    loan = LoanInput(principal=12_000, monthly_rate=0.0, term_months=12, kkdf=0.0, bsmv=0.0)
    result = amortize(loan)
    assert result.installment == pytest.approx(1000.0)
    assert result.total_interest == pytest.approx(0.0)
