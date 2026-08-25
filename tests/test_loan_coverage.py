"""Kredi kapsam görünürlüğü — PLAN.md madde 4.

Kullanıcının şikâyeti: "ödeme planında yalnızca 3 banka var, diğerleri neden
yok?" Cevap veride değildi — konut kredisini yayınlayan banka sayısı gerçekten
üçtü. Buradaki testler iki şeyi kilitliyor:

1. Sayım doğru (tür başına kaç kurum) ve türler birbirine karışmıyor.
2. Eksik her kurum için EKRANA BASILACAK bir gerekçe var. Gerekçesiz bir
   eksiklik, kullanıcı açısından "veri kayıp"tan ayırt edilemez.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from config.loader import LOAN_TYPES, loan_coverage
from store.db import SessionLocal
from store.models import Institution, LoanRate
from store.queries import loan_type_counts


def _loan(institution: str, loan_type: str, rate: float = 0.03, day: int = 25) -> LoanRate:
    return LoanRate(
        institution=institution,
        loan_type=loan_type,
        monthly_rate=rate,
        valid_date=date(2026, 8, day),
        fetched_at=datetime.now(timezone.utc),
    )


# ------------------------------------------------------------- sayım ----

def test_counts_are_per_loan_type_not_total(db):
    """Aynı banka üç türde de varsa her türde ayrı ayrı sayılmalı."""
    with SessionLocal() as s:
        s.add_all(
            [
                Institution(code="AKBANK", name="Akbank", kind="bank"),
                Institution(code="VAKIFBANK", name="VakıfBank", kind="bank"),
            ]
        )
        s.add_all(
            [
                _loan("AKBANK", "personal"),
                _loan("AKBANK", "housing"),
                _loan("AKBANK", "vehicle"),
                _loan("VAKIFBANK", "personal"),
                _loan("VAKIFBANK", "housing"),
            ]
        )
        s.commit()

    counts = loan_type_counts()
    assert counts == {"personal": 2, "housing": 2, "vehicle": 1}


def test_a_bank_with_history_is_counted_once_not_per_day(db):
    """Aynı banka için birden fazla günün satırı varsa kurum bir kez sayılır.

    Bu tuzak gerçek: loan_rates her gün yeni bir valid_date satırı yazar.
    Naif bir COUNT(*) sorgusu bir hafta sonra "Konut: 21 banka" derdi.
    """
    with SessionLocal() as s:
        s.add(Institution(code="AKBANK", name="Akbank", kind="bank"))
        s.add_all(
            [
                _loan("AKBANK", "housing", day=23),
                _loan("AKBANK", "housing", day=24),
                _loan("AKBANK", "housing", day=25),
            ]
        )
        s.commit()

    assert loan_type_counts() == {"housing": 1}


def test_missing_type_is_absent_from_counts_not_zero(db):
    """Hiç verisi olmayan tür sözlükte yer almaz; panel 0 olarak gösterir."""
    with SessionLocal() as s:
        s.add(Institution(code="AKBANK", name="Akbank", kind="bank"))
        s.add(_loan("AKBANK", "personal"))
        s.commit()

    counts = loan_type_counts()
    assert counts.get("vehicle", 0) == 0


# --------------------------------------------------------- gerekçeler ----

def test_every_configured_institution_has_a_coverage_entry():
    coverage = loan_coverage()
    assert coverage, "sources.yaml'da kredi kaynağı yok"
    for code, info in coverage.items():
        assert info["status"] in {"active", "blocked", "unverified"}, code


def test_every_gap_has_a_human_readable_reason():
    """ASIL KURAL: gerekçesiz eksiklik olmasın.

    Aktif bir kaynak bir kredi türünü yayınlamıyorsa `missing` altında
    nedeni yazılı olmalı. Aktif olmayan kaynakta `blocked_reason` olmalı.
    Bu test, yeni bir banka eklenip gerekçesi yazılmadığında düşer — panelde
    "gerekçe kayıtlı değil" yazısı görünmeden önce burada yakalanır.
    """
    for code, info in loan_coverage().items():
        if info["status"] == "active":
            for loan_type in LOAN_TYPES:
                if loan_type in info["covers"]:
                    continue
                reason = info["missing"].get(loan_type)
                assert reason, f"{code}/{loan_type} için gerekçe yazılmamış"
                assert len(reason) > 20, f"{code}/{loan_type} gerekçesi çok kısa"
        else:
            assert info["blocked_reason"], f"{code}: kapalı ama gerekçesi yok"


def test_coverage_config_matches_what_the_collectors_actually_produce():
    """sources.yaml'daki `covers` listesi, toplayıcıların ürettiğiyle uyuşmalı.

    Bu iki liste ayrı yerlerde durduğu için kayma kaçınılmazdır — bir parser'a
    yeni bir kredi türü eklenip YAML güncellenmezse panel o bankayı "eksik"
    diye gösterip yanlış bir gerekçe basar. Test, parser'ların kod düzeyindeki
    gerçeğini YAML'a karşı sınar.
    """
    from collectors.loan_rates import AkbankLoanParser, PARSERS

    coverage = loan_coverage()

    # Akbank üç ürünü de tanımlıyor; YAML da üçünü de saymalı.
    assert set(AkbankLoanParser.PRODUCTS.values()) == set(coverage["AKBANK"]["covers"])

    # Her parser'ın kurumu YAML'da bir kapsam satırına sahip olmalı.
    for parser in PARSERS.values():
        assert parser.institution in coverage, f"{parser.institution} sources.yaml'da yok"
        assert coverage[parser.institution]["status"] == "active", (
            f"{parser.institution} toplanıyor ama sources.yaml 'active' demiyor"
        )


def test_enpara_is_marked_active_after_the_csrf_misdiagnosis_was_corrected():
    """Regresyon: Enpara "CSRF token gerekiyor" diye yanlış kapalı yazılmıştı.

    Gerçekte oran sayfaya sunucu tarafında gömülü. Kaynak envanteri ile
    çalışan kod arasındaki bu tür çelişkiler, kullanıcının "hangi veriyi
    nereden çekiyorsun" sorusunu cevaplanamaz hâle getiriyordu.
    """
    info = loan_coverage()["ENPARA"]
    assert info["status"] == "active"
    assert info["covers"] == ["personal"]
