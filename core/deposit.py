"""Vadeli mevduat / katılım kar payı net getirisi — saf fonksiyonlar.

TL mevduatta gün tabanı 365'tir. Stopaj vadeye göre kademelidir (bkz.
config/taxes.yaml); bu modül oranı değil, oranı verilen sonucu hesaplar.
"""
from __future__ import annotations

from core.models import DepositInput, DepositResult, RolloverDepositResult

DAY_BASIS = 365


def resolve_withholding(brackets: list[dict], term_days: int) -> float:
    """brackets: [{'max_days': int|None, 'rate': float}, ...] artan sırada.

    None max_days = üst sınır yok (son kademe).
    """
    for bracket in brackets:
        max_days = bracket.get("max_days")
        if max_days is None or term_days <= max_days:
            return bracket["rate"]
    raise ValueError(f"term_days={term_days} için uygun stopaj kademesi bulunamadı")


def single_term_return(deposit: DepositInput) -> DepositResult:
    gross = deposit.principal * (deposit.annual_rate / DAY_BASIS) * deposit.term_days
    net = gross * (1 - deposit.withholding_rate)
    return DepositResult(
        gross_return=gross,
        net_return=net,
        withholding_rate=deposit.withholding_rate,
        maturity_value=deposit.principal + net,
    )


def rollover_return(
    principal: float,
    annual_rate: float,
    term_days: int,
    withholding_rate: float,
    horizon_days: int = DAY_BASIS,
) -> RolloverDepositResult:
    """Aynı vadeyi ufuk boyunca peş peşe devretmenin (bileşik) getirisi."""
    period_net_rate = (annual_rate / DAY_BASIS) * term_days * (1 - withholding_rate)
    rollovers = horizon_days / term_days
    net_multiplier = (1 + period_net_rate) ** rollovers - 1
    annualized_net_return = principal * net_multiplier
    return RolloverDepositResult(
        period_net_rate=period_net_rate,
        annualized_net_return=annualized_net_return,
        annualized_net_value=principal + annualized_net_return,
        rollovers=round(rollovers),
    )
