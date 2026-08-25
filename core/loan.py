"""Kredi amortismanı — saf fonksiyonlar, I/O yok.

KKDF ve BSMV faiz üzerine eklenerek efektif aylık oran üretilir, sonra
standart anüite formülü uygulanır. Vergiyi taksit üzerine sonradan eklemek
yaygın bir hatadır (bkz. tasarım dokümanı §04).
"""
from __future__ import annotations

from core.models import AmortizationRow, InstallmentComparison, LoanInput, LoanResult


def effective_monthly_rate(monthly_rate: float, kkdf: float, bsmv: float) -> float:
    return monthly_rate * (1 + kkdf + bsmv)


def calculate_installment(principal: float, r_ef: float, n: int) -> float:
    if r_ef == 0:
        return principal / n
    return principal * r_ef / (1 - (1 + r_ef) ** -n)


def amortize(loan: LoanInput) -> LoanResult:
    r_ef = effective_monthly_rate(loan.monthly_rate, loan.kkdf, loan.bsmv)
    installment = calculate_installment(loan.principal, r_ef, loan.term_months)

    balance = loan.principal
    rows: list[AmortizationRow] = []
    total_interest = total_kkdf = total_bsmv = 0.0

    for period in range(1, loan.term_months + 1):
        interest = balance * loan.monthly_rate
        kkdf_amount = interest * loan.kkdf
        bsmv_amount = interest * loan.bsmv
        principal_paid = installment - interest - kkdf_amount - bsmv_amount
        balance = balance - principal_paid
        if period == loan.term_months:
            # kalan yuvarlama farkını son taksitte kapat
            balance = 0.0

        rows.append(
            AmortizationRow(
                period=period,
                installment=installment,
                principal_paid=principal_paid,
                interest=interest,
                kkdf_amount=kkdf_amount,
                bsmv_amount=bsmv_amount,
                remaining_balance=balance,
            )
        )
        total_interest += interest
        total_kkdf += kkdf_amount
        total_bsmv += bsmv_amount

    return LoanResult(
        installment=installment,
        effective_monthly_rate=r_ef,
        total_paid=installment * loan.term_months,
        total_interest=total_interest,
        total_kkdf=total_kkdf,
        total_bsmv=total_bsmv,
        schedule=tuple(rows),
    )


# Bankanın kendi taksitiyle bizim hesabımız arasında kabul edilebilir fark.
# %0,5 cömert bir eşik: canlı doğrulamada (Yapı Kredi, 2026-08-24) gerçek
# sapma %0,0002 çıktı — yani eşiği aşan bir fark yuvarlama değil, model
# hatasıdır (yanlış vergi oranı, yanlış gün sayımı, ürüne gömülü masraf).
INSTALLMENT_TOLERANCE_PCT = 0.5


def annual_cost_rate(effective_monthly_rate: float) -> float:
    """Efektif AYLIK orandan yıllık bileşik maliyet oranı (0.9 = %90).

    Bankaların "yıllık maliyet oranı" diye ilan ettiği büyüklüğün karşılığı.
    Aylık oranı 12 ile ÇARPMAK yaygın ve yanlış bir kısayoldur: yüksek
    oranlarda bileşik etki devasadır (aylık %3,75 -> yıllık %55,4 değil %55,5
    değil, %55,45 basit ama %55,4'ün çok üzerinde bileşik). Kıyaslamada
    taksit tutarından daha dürüst bir ölçüdür çünkü vadeden bağımsızdır.
    """
    return (1 + effective_monthly_rate) ** 12 - 1


def compare_installment(
    *,
    principal: float,
    monthly_rate: float,
    term_months: int,
    kkdf: float,
    bsmv: float,
    bank_installment: float,
    tolerance_pct: float = INSTALLMENT_TOLERANCE_PCT,
) -> InstallmentComparison:
    """Bizim anüite hesabımızı bankanın kendi taksitiyle karşılaştırır.

    Bu fonksiyon projedeki tek "kendi kendini denetleyen" parçadır: KKDF/BSMV
    oranları mevzuatla değişir ve config/taxes.yaml elle güncellenir. Yanlış
    bir vergi oranı hiçbir testi düşürmez — çünkü testler de aynı yanlış
    oranı kullanır. Onu yakalayan tek şey, bankanın kendi rakamıdır.
    """
    r_ef = effective_monthly_rate(monthly_rate, kkdf, bsmv)
    ours = calculate_installment(principal, r_ef, term_months)
    if bank_installment == 0:
        raise ValueError("bankanın taksiti 0 olamaz")
    deviation = (ours - bank_installment) / bank_installment * 100
    return InstallmentComparison(
        ours=ours,
        theirs=bank_installment,
        deviation_pct=deviation,
        within_tolerance=abs(deviation) <= tolerance_pct,
    )
