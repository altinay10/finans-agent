"""Fon getiri simülasyonu — saf fonksiyonlar.

TEFAS fiyatı o günün fiyatıdır (gerçek alımda T+1/T+2 valörü vardır) ve
toplam gider oranı zaten fiyata gömülüdür. Zararda stopaj uygulanmaz.
"""
from __future__ import annotations

from datetime import date

from core.models import FundSimInput, FundSimResult


def nearest_prior_price(prices: list[tuple[date, float]], target: date) -> tuple[date, float]:
    """Verilen tarihte veya ondan önceki en yakın fiyatı döndürür.

    prices: (tarih, fiyat) çiftleri, sırası önemli değil.
    Gelecekteki fiyata asla bakmaz — hedef tarihten sonraki kayıtlar elenir.
    """
    candidates = [p for p in prices if p[0] <= target]
    if not candidates:
        raise ValueError(f"{target} tarihinde veya öncesinde fiyat bulunamadı")
    return max(candidates, key=lambda p: p[0])


def simulate(sim: FundSimInput) -> FundSimResult:
    units = sim.principal / sim.price_start
    value_end = units * sim.price_end
    gross = value_end - sim.principal

    withholding_applied = gross > 0 and not sim.is_equity_heavy
    if withholding_applied:
        net = gross * (1 - sim.withholding_rate)
    else:
        net = gross

    return FundSimResult(
        units=units,
        value_end=value_end,
        gross_return=gross,
        net_return=net,
        maturity_value=sim.principal + net,
        withholding_applied=withholding_applied,
    )
