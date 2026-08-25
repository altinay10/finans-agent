"""Dataclasses shared by the pure calculation layer.

No I/O, no framework imports beyond stdlib. core/ takes these in, returns
these out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


# ---------------------------------------------------------------- loan ----

@dataclass(frozen=True)
class LoanInput:
    principal: float
    monthly_rate: float          # bankanın ilan ettiği brüt aylık faiz (0.035 = %3.5)
    term_months: int
    kkdf: float                  # oran, 0.15 = %15
    bsmv: float                  # oran, 0.05 = %5


@dataclass(frozen=True)
class AmortizationRow:
    period: int
    installment: float
    principal_paid: float
    interest: float
    kkdf_amount: float
    bsmv_amount: float
    remaining_balance: float


@dataclass(frozen=True)
class LoanResult:
    installment: float
    effective_monthly_rate: float
    total_paid: float
    total_interest: float
    total_kkdf: float
    total_bsmv: float
    schedule: tuple[AmortizationRow, ...]


@dataclass(frozen=True)
class InstallmentComparison:
    """Bizim hesabımız ile bankanın kendi taksitinin karşılaştırması.

    Panel ödeme planını bankadan almaz, bankanın ilan ettiği aylık orandan
    kendi hesaplar. Bu karşılaştırma o hesabın denetimidir: vergi oranlarımız
    (config/taxes.yaml) eskirse ya da anüite formülümüz kayarsa sapma büyür
    ve görünür olur.

    `within_tolerance` yanlışsa suçlu genelde bizim tarafımızdadır, ama
    bankanın kendi tutarına dosya masrafı/sigorta katmış olması da mümkün —
    bu yüzden panel "hata" demez, "fark" der ve sayıyı gösterir.
    """

    ours: float
    theirs: float
    deviation_pct: float          # (bizim - onun) / onun * 100
    within_tolerance: bool


# ------------------------------------------------------------- deposit ----

@dataclass(frozen=True)
class DepositInput:
    principal: float
    annual_rate: float           # brüt yıllık, 0.4550 = %45.50
    term_days: int
    withholding_rate: float      # stopaj oranı, taxes.yaml'dan çözülür


@dataclass(frozen=True)
class DepositResult:
    gross_return: float
    net_return: float
    withholding_rate: float
    maturity_value: float        # principal + net_return


@dataclass(frozen=True)
class RolloverDepositResult:
    period_net_rate: float
    annualized_net_return: float     # 365 güne bileşiklenmiş net getiri (TL)
    annualized_net_value: float      # principal + annualized_net_return
    rollovers: int                   # kaç kez devredildi (365 / term_days, tam sayı değilse yaklaşık)


# ---------------------------------------------------------------- fund ----

@dataclass(frozen=True)
class FundSimInput:
    principal: float
    price_start: float
    price_end: float
    date_start: date
    date_end: date
    withholding_rate: float
    is_equity_heavy: bool = False


@dataclass(frozen=True)
class FundSimResult:
    units: float
    value_end: float
    gross_return: float
    net_return: float
    maturity_value: float
    withholding_applied: bool    # brüt < 0 ise False (zararda stopaj yok)
