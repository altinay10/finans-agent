"""Ödeme planı şeffaflığı ve bankanın kendi rakamıyla çapraz doğrulama.

PLAN.md madde 5. Buradaki testlerin varlık sebebi tek bir cümle:
**KKDF/BSMV oranları yanlışsa hiçbir testimiz düşmez**, çünkü testler de aynı
config/taxes.yaml'ı okur. Yanlış vergi oranını yakalayabilecek tek bağımsız
tanık bankanın kendi taksit tutarıdır.

Sayılar uydurma değil: Yapı Kredi'nin `GetPersonalCreditPaymentPlan` ve
`CalculateByCreditAmount` uç noktalarının 2026-08-24/25 tarihli canlı
yanıtlarından alındı (bkz. data/snapshots/loan_rates_*.raw).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from core.loan import (
    INSTALLMENT_TOLERANCE_PCT,
    amortize,
    annual_cost_rate,
    compare_installment,
    effective_monthly_rate,
)
from core.models import LoanInput


# ------------------------------------------------- yıllık maliyet oranı ----

def test_annual_cost_rate_is_compound_not_twelve_times_monthly():
    """Aylık oranı 12 ile çarpmak yüksek oranlarda ciddi biçimde yanıltır.

    %3,75 aylık: basit çarpım %45 der, gerçek bileşik maliyet %55,4'tür.
    Kredi kıyaslarken bu 10 puanlık fark doğrudan yanlış karar demektir.
    """
    monthly = 0.0375
    naive = monthly * 12
    real = annual_cost_rate(monthly)
    assert real == pytest.approx(0.5545, abs=0.001)
    assert real - naive > 0.10


def test_annual_cost_rate_uses_effective_rate_so_taxes_are_included():
    """Vergiler efektif orana girdiği için yıllık maliyet de onları taşır."""
    r_ef = effective_monthly_rate(0.0334, kkdf=0.15, bsmv=0.15)
    assert annual_cost_rate(r_ef) > annual_cost_rate(0.0334)


# ------------------------------------- bankanın kendi taksitiyle kıyas ----

def test_our_annuity_matches_yapikredi_personal_loan_to_the_kurus():
    """Yapı Kredi ihtiyaç kredisi, canlı yanıt 2026-08-25.

    100.000 TL / 3 ay / aylık %5,99 / KKDF %15 / BSMV %15 -> banka 38.654,31 TL.
    """
    cmp = compare_installment(
        principal=100_000,
        monthly_rate=0.0599,
        term_months=3,
        kkdf=0.15,
        bsmv=0.15,
        bank_installment=38_654.31,
    )
    assert cmp.within_tolerance
    assert abs(cmp.deviation_pct) < 0.001
    assert cmp.ours == pytest.approx(38_654.31, abs=0.01)


def test_our_annuity_matches_yapikredi_housing_loan_with_zero_taxes():
    """Konut kredisi KKDF ve BSMV'den istisnadır (6802 s. Kanun m.29).

    Bankanın kendi yanıtı da KkdfRate=0, BsmvRate=0 diyor. Vergileri sıfır
    kabul eden config/taxes.yaml değeri bu satırla bağımsız olarak doğrulanır:
    1.000.000 TL / 36 ay / aylık %3,34 -> banka 48.156,85 TL.
    """
    cmp = compare_installment(
        principal=1_000_000,
        monthly_rate=0.0334,
        term_months=36,
        kkdf=0.0,
        bsmv=0.0,
        bank_installment=48_156.85,
    )
    assert cmp.within_tolerance
    assert cmp.ours == pytest.approx(48_156.85, abs=0.01)


def test_wrong_tax_rate_is_caught_by_the_bank_comparison():
    """ASIL REGRESYON TESTİ: yanlış vergi oranı yakalanabiliyor mu?

    Konut kredisine yanlışlıkla ihtiyaç kredisinin BSMV'si (%15) uygulansın.
    Hiçbir birim testi bunu göremez — anüite matematiği hâlâ kusursuzdur,
    yalnızca girdi yanlıştır. Bankanın kendi tutarıyla kıyas bunu görür.
    """
    cmp = compare_installment(
        principal=1_000_000,
        monthly_rate=0.0334,
        term_months=36,
        kkdf=0.15,          # konutta olmaması gereken vergiler
        bsmv=0.15,
        bank_installment=48_156.85,
    )
    assert not cmp.within_tolerance
    assert cmp.deviation_pct > INSTALLMENT_TOLERANCE_PCT


def test_comparison_rejects_zero_bank_installment():
    with pytest.raises(ValueError):
        compare_installment(
            principal=100_000, monthly_rate=0.03, term_months=12,
            kkdf=0.15, bsmv=0.15, bank_installment=0,
        )


# --------------------------------------- toplayıcı: referans taksit akışı ----

def _yapikredi_payload() -> str:
    """Yapı Kredi yanıtının asgari ama gerçek biçimli hâli."""
    return json.dumps(
        {
            "personal": {
                "d": {
                    "Data": {
                        "PaymentPlanList": [
                            {
                                "Maturity": 3,
                                "InterestRate": 5.99,
                                "MonthlyInstallmentAmount": 38654.31,
                                "YearlyCustomerCostRate": 144.7636,
                            },
                            {
                                "Maturity": 12,
                                "InterestRate": 4.99,
                                "MonthlyInstallmentAmount": 11500.00,
                                "YearlyCustomerCostRate": 110.0,
                            },
                        ]
                    }
                }
            },
            "housing": {
                "d": {
                    "Data": {
                        "MaturityList": [36, 120],
                        "PaymentList": [
                            {
                                "Key": "36",
                                "Value": {
                                    "Maturity": 36,
                                    "InterestRate": "3,34",
                                    "PrincipalAmount": "1.000.000,00",
                                    "MonthlyInstallmentAmount": "48.156,85",
                                    "YearlyEffectiveInterestRate": "51,43",
                                },
                            }
                        ],
                    }
                }
            },
        }
    )


def test_reference_quote_uses_the_same_maturity_as_the_stored_rate():
    """Kıyas, oranı okuduğumuz satırla AYNI vadeden yapılmalı.

    Farklı vadenin taksitiyle karşılaştırmak sahte bir sapma üretir ve
    doğrulama mekanizmasını işe yaramaz hâle getirir. Parser en kısa vadenin
    oranını yazıyor; referans taksit de o vadeden gelmeli.
    """
    from collectors.loan_rates import YapiKrediLoanParser

    quotes = YapiKrediLoanParser().reference_quotes(_yapikredi_payload())
    personal = next(q for q in quotes if q.loan_type == "personal")
    assert personal.term_months == 3          # 12 değil
    assert personal.monthly_rate == pytest.approx(0.0599)
    assert personal.bank_installment == pytest.approx(38654.31)


def test_reference_quote_parses_turkish_number_format_in_housing():
    """Konut yanıtı Türkçe biçimli metin döndürüyor: "48.156,85"."""
    from collectors.loan_rates import YapiKrediLoanParser

    quotes = YapiKrediLoanParser().reference_quotes(_yapikredi_payload())
    housing = next(q for q in quotes if q.loan_type == "housing")
    assert housing.principal == pytest.approx(1_000_000.0)
    assert housing.bank_installment == pytest.approx(48_156.85)
    assert housing.bank_annual_cost_rate == pytest.approx(0.5143)


def test_parsers_without_bank_installment_return_empty_not_fabricated():
    """Akbank kendi taksitini yayınlamıyor (urunTaksitTut null geliyor).

    Varsayılan davranış boş liste olmalı; "hesaplayıp kendi rakamımızı
    bankanın rakamıymış gibi yazmak" doğrulamayı anlamsız kılardı.
    """
    from collectors.loan_rates import AkbankLoanParser, EnparaLoanParser

    assert AkbankLoanParser().reference_quotes("{}") == []
    assert EnparaLoanParser().reference_quotes("") == []


def test_collector_persists_reference_quotes_and_records_validation(db):
    """Uçtan uca: parse -> persist -> source_runs'a 'validate' satırı."""
    from sqlalchemy import select

    from collectors.loan_rates import LoanRateCollector, LoanReferenceQuoteRecord
    from store.db import SessionLocal
    from store.models import Institution, LoanReferenceQuote, SourceRun

    with SessionLocal() as s:
        s.add(Institution(code="YAPIKREDI", name="Yapı Kredi", kind="bank"))
        s.commit()

    collector = LoanRateCollector()
    collector._reference_quotes = [
        LoanReferenceQuoteRecord(
            institution="YAPIKREDI", loan_type="personal", principal=100_000,
            term_months=3, monthly_rate=0.0599, bank_installment=38_654.31,
            bank_annual_cost_rate=1.447636,
        )
    ]
    collector._persist_reference_quotes(
        run_id=None, today=date(2026, 8, 25), now=datetime.now(timezone.utc)
    )

    with SessionLocal() as s:
        rows = s.execute(select(LoanReferenceQuote)).scalars().all()
        assert len(rows) == 1
        assert float(rows[0].bank_installment) == pytest.approx(38_654.31)

        validations = s.execute(
            select(SourceRun).where(SourceRun.phase == "validate")
        ).scalars().all()
        assert len(validations) == 1
        assert validations[0].status == "ok"
        assert "bankanın" in validations[0].error


def test_validation_failure_does_not_stop_rate_collection(db):
    """Sapma tespiti koşuyu DÜŞÜRMEMELİ, yalnızca kırmızı satır bırakmalı.

    Yanlış bir vergi oranı yüzünden gün boyu veri toplamamak aşırı tepki
    olur; ama sessiz geçmek de kabul edilemez. Denge burada test ediliyor.
    """
    from sqlalchemy import select

    from collectors.loan_rates import LoanRateCollector, LoanReferenceQuoteRecord
    from store.db import SessionLocal
    from store.models import Institution, SourceRun

    with SessionLocal() as s:
        s.add(Institution(code="YAPIKREDI", name="Yapı Kredi", kind="bank"))
        s.commit()

    collector = LoanRateCollector()
    collector._reference_quotes = [
        LoanReferenceQuoteRecord(
            institution="YAPIKREDI", loan_type="personal", principal=100_000,
            term_months=3, monthly_rate=0.0599,
            bank_installment=50_000.0,   # gerçekle uyuşmayan tutar
        )
    ]
    # Patlamadan tamamlanmalı.
    collector._persist_reference_quotes(
        run_id=None, today=date(2026, 8, 25), now=datetime.now(timezone.utc)
    )

    with SessionLocal() as s:
        validations = s.execute(
            select(SourceRun).where(SourceRun.phase == "validate")
        ).scalars().all()
        assert [v.status for v in validations] == ["failed"]
        assert "taxes.yaml" in validations[0].error


# --------------------------------------------------- panel: şeffaflık ----

def test_amortization_schedule_is_computed_by_us_not_fetched():
    """Belgeleyici test: ödeme planının kaynağı `core/loan.py`.

    Panelin "bu plan bankanın resmî planı değildir" uyarısı bu gerçeğin
    sonucudur. Plan bir gün bankadan çekilmeye başlanırsa bu test düşer ve
    uyarı metninin de güncellenmesi gerektiğini hatırlatır.
    """
    result = amortize(
        LoanInput(principal=100_000, monthly_rate=0.0599, term_months=3,
                  kkdf=0.15, bsmv=0.15)
    )
    assert len(result.schedule) == 3
    assert result.schedule[-1].remaining_balance == 0.0
    # Taksitler eşit (anüite) ve bankanın rakamıyla aynı.
    assert {round(r.installment, 2) for r in result.schedule} == {38_654.31}
