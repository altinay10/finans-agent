"""Katılım bankası YILLIK kâr payı oranları — PLAN.md madde 2+3.

Kullanıcının şikâyeti: "emlak katılım ve kuveyt türk kar paylaşım oranları
yanlış duruyor". Veri doğruydu; sorun %93'ün, yanındaki %38'lik faizlerin
arasında **faiz gibi okunmasıydı**. Çözüm paylaşım oranını getiriye
"çevirmek" değil (uydurma olurdu), bankaların KENDİ yayınladığı yıllık oranı
bulmaktı.

Buradaki testler iki şeyi koruyor:
1. İki büyüklük BİRBİRİNE KARIŞMASIN — paylaşım oranı (%93) asla annual_rate
   olarak yazılmasın.
2. Yıllık oranlar faizle aynı boru hattından geçsin, aynı sıralamaya girsin.

Sayılar uydurma değil: bankaların 2026-08-25 tarihli canlı yanıtlarından.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from collectors.base import SanityCheckError
from collectors.participation_rates import (
    EmlakKatilimAnnualRateCollector,
    KuveytTurkAnnualRateCollector,
    ParticipationRateRecord,
    _tr_number,
)
from store.db import SessionLocal
from store.models import DepositRate, Institution


# --------------------------------------------------------- Emlak Katılım ----

def _emlak_cell(currency, amount, term, rate, segment="Platin", success=True):
    return {
        "currency": currency,
        "amount": amount,
        "term": term,
        "body": {
            "Success": success,
            "Message": "",
            "Data": None if not success else {
                "GrossProfitShareYearly": rate,
                "NetProfitShareYearly": rate * 0.82,
                "SegmentName": segment,
            },
        },
    }


def test_emlak_parses_the_live_response_shape():
    """Canlı yanıt: 1.000.000 TL / 91 gün -> brüt yıllık %33,97, segment Platin."""
    raw = json.dumps([_emlak_cell("TRY", 1_000_000, 91, 33.97)]).encode()
    records = EmlakKatilimAnnualRateCollector().parse(raw)
    assert len(records) == 1
    r = records[0]
    assert r.institution == "EMLAKKATILIM"
    assert r.annual_rate == pytest.approx(0.3397)
    assert r.segment == "Platin"
    assert r.term_days == 91


def test_amount_tiers_are_derived_from_the_sampled_ladder():
    """Kademe sınırı: bir sonraki örneğin bir altı; son örnekte üst sınır yok."""
    raw = json.dumps(
        [
            _emlak_cell("TRY", 10_000, 91, 31.35, "Klasik"),
            _emlak_cell("TRY", 100_000, 91, 33.60, "Altın"),
            _emlak_cell("TRY", 500_000, 91, 33.97, "Platin"),
        ]
    ).encode()
    records = sorted(
        EmlakKatilimAnnualRateCollector().parse(raw), key=lambda r: r.amount_min
    )
    assert [(r.amount_min, r.amount_max) for r in records] == [
        (10_000, 99_999),
        (100_000, 499_999),
        (500_000, None),
    ]


def test_tier_derivation_is_conservative_never_overstating_the_rate():
    """Ara tutar BİR ALT örneğin oranını görmeli.

    750.000 TL, 500.000 örneğinin kademesine düşer. Ters yönde hata yapmak
    (üst örneğin daha yüksek oranını göstermek) kullanıcıyı gerçekte
    alamayacağı bir orana göre karar vermeye iterdi.
    """
    raw = json.dumps(
        [
            _emlak_cell("TRY", 500_000, 91, 33.97),
            _emlak_cell("TRY", 2_500_000, 91, 34.71),
        ]
    ).encode()
    records = EmlakKatilimAnnualRateCollector().parse(raw)
    matching = [
        r for r in records
        if r.amount_min <= 750_000 and (r.amount_max is None or r.amount_max >= 750_000)
    ]
    assert len(matching) == 1
    assert matching[0].annual_rate == pytest.approx(0.3397)   # 0.3471 DEĞİL


def test_unsuccessful_cells_are_skipped_not_invented():
    """Banka "bu kombinasyon için hesaplama yok" diyebiliyor."""
    raw = json.dumps(
        [
            _emlak_cell("TRY", 10_000, 31, 0, success=False),
            _emlak_cell("TRY", 100_000, 31, 31.38),
        ]
    ).encode()
    records = EmlakKatilimAnnualRateCollector().parse(raw)
    assert len(records) == 1
    assert records[0].amount_min == 100_000


def test_currencies_do_not_share_tier_boundaries():
    """TL ve USD merdivenleri ayrı; sınırlar birbirine karışmamalı."""
    raw = json.dumps(
        [
            _emlak_cell("TRY", 10_000, 91, 31.35),
            _emlak_cell("TRY", 100_000, 91, 33.60),
            _emlak_cell("USD", 1_000, 91, 0.70),
            _emlak_cell("USD", 50_000, 91, 0.90),
        ]
    ).encode()
    records = EmlakKatilimAnnualRateCollector().parse(raw)
    usd = sorted([r for r in records if r.currency == "USD"], key=lambda r: r.amount_min)
    assert [(r.amount_min, r.amount_max) for r in usd] == [(1_000, 49_999), (50_000, None)]


# ----------------------------------------------------------- Kuveyt Türk ----

def test_kuveytturk_parses_realized_rates_for_every_currency():
    """Canlı yanıt biçimi: Türkçe ondalık ayırıcı, ay cinsinden vade."""
    raw = json.dumps(
        {
            "endpoint": "ck0d84?TEST",
            "rates": [
                {"MaturityTerm": 1, "GrossTL": "32,99", "GrossUSD": "1,16", "GrossEUR": "0,84"},
                {"MaturityTerm": 12, "GrossTL": "39,32", "GrossUSD": "0,90", "GrossEUR": "0,67"},
            ],
        }
    ).encode()
    records = KuveytTurkAnnualRateCollector().parse(raw)
    by_key = {(r.currency, r.term_days): r.annual_rate for r in records}
    assert by_key[("TRY", 31)] == pytest.approx(0.3299)
    assert by_key[("TRY", 365)] == pytest.approx(0.3932)
    assert by_key[("USD", 31)] == pytest.approx(0.0116)


def test_kuveytturk_terms_are_normalised_to_the_project_day_scale():
    """Ay cinsinden vade, mevduat tarafıyla aynı gün ölçeğine çevrilmeli.

    Aksi halde katılım satırları 1/3/6/12 "gün" olarak görünür ve yıllık net %
    hesabı 365/1 çarpanıyla astronomik bir sayı üretirdi.
    """
    raw = json.dumps(
        {"rates": [{"MaturityTerm": m, "GrossTL": "30,00"} for m in (1, 3, 6, 12)]}
    ).encode()
    records = KuveytTurkAnnualRateCollector().parse(raw)
    assert sorted({r.term_days for r in records}) == [31, 92, 182, 365]


def test_kuveytturk_does_not_invent_amount_tiers():
    """Uç nokta tutar kademesi vermiyor; uydurma kademe yazılmamalı."""
    raw = json.dumps({"rates": [{"MaturityTerm": 1, "GrossTL": "32,99"}]}).encode()
    r = KuveytTurkAnnualRateCollector().parse(raw)[0]
    assert r.amount_min == 0.0
    assert r.amount_max is None


def test_unknown_maturity_is_skipped():
    raw = json.dumps({"rates": [{"MaturityTerm": 24, "GrossTL": "45,00"}]}).encode()
    with pytest.raises(Exception):     # sıfır kayıt -> ParseError
        KuveytTurkAnnualRateCollector().parse(raw)


def test_turkish_number_parser():
    assert _tr_number("32,99") == pytest.approx(32.99)
    assert _tr_number("1.234,56") == pytest.approx(1234.56)
    assert _tr_number(None) is None
    assert _tr_number("") is None
    assert _tr_number("—") is None


# ------------------------------------------------------------ bant/sanity ----

def test_profit_share_ratio_cannot_be_written_as_an_annual_rate():
    """ASIL KORUMA: %93 paylaşım oranı yıllık orana kaçarsa yakalansın.

    0.93 geçerli bir yıllık oran olabilir (%93 çok yüksek ama imkânsız
    değil), o yüzden bant tek başına yetmez — ama 93.0 (yüzde yerine oran
    yazılması) kesin hatadır ve bant onu yakalar.
    """
    with pytest.raises(ValueError):
        ParticipationRateRecord(
            institution="EMLAKKATILIM", currency="TRY", term_days=92,
            amount_min=0, amount_max=None, annual_rate=93.0,
        )


def test_all_zero_try_rates_are_rejected_as_an_empty_response(db):
    collector = KuveytTurkAnnualRateCollector()
    records = [
        ParticipationRateRecord(
            institution="KUVEYTTURK", currency="TRY", term_days=92,
            amount_min=0, amount_max=None, annual_rate=0.0,
        )
    ]
    with pytest.raises(SanityCheckError):
        collector.sanity_check(records)


def test_near_zero_fx_rates_are_accepted_because_they_are_real(db):
    """USD kâr payı gerçekten %0,70 civarında; döviz satırlarını atmak
    doğru veriyi atmak olurdu."""
    collector = KuveytTurkAnnualRateCollector()
    collector.sanity_check(
        [
            ParticipationRateRecord(
                institution="KUVEYTTURK", currency="USD", term_days=92,
                amount_min=0, amount_max=None, annual_rate=0.007,
            )
        ]
    )   # patlamamalı


# ----------------------------------------------- mevduat tablosuna yazma ----

def _seed_institutions():
    with SessionLocal() as s:
        s.add_all(
            [
                Institution(code="EMLAKKATILIM", name="Emlak Katılım", kind="participation"),
                Institution(code="KUVEYTTURK", name="Kuveyt Türk", kind="participation"),
            ]
        )
        s.commit()


def test_rates_land_in_deposit_rates_flagged_as_profit_share(db):
    """Aynı tabloya yazılıyor ki aynı hesaplama boru hattından geçsin.

    Ayrı tabloda tutmak panelde ikinci bir hesaplama yolu açardı ve iki yol
    kaçınılmaz olarak ayrışırdı. `is_profit_share` bayrağı ayrımı koruyor.
    """
    _seed_institutions()
    collector = KuveytTurkAnnualRateCollector()
    collector.persist(
        [
            ParticipationRateRecord(
                institution="KUVEYTTURK", currency="TRY", term_days=92,
                amount_min=0, amount_max=None, annual_rate=0.3442,
            )
        ],
        run_id=None,
    )
    with SessionLocal() as s:
        row = s.execute(select(DepositRate)).scalars().one()
    assert row.is_profit_share is True
    assert float(row.annual_rate) == pytest.approx(0.3442)


def test_a_removed_tier_is_cleaned_up_not_left_behind(db):
    """Bankanın kademe yapısı değişirse eski satır ortada kalmamalı.

    Kalırsa panel, artık var olmayan bir kademenin oranını göstermeye
    devam eder — hem de "bugünün verisi" etiketiyle.
    """
    _seed_institutions()
    collector = EmlakKatilimAnnualRateCollector()
    first = [
        ParticipationRateRecord(
            institution="EMLAKKATILIM", currency="TRY", term_days=91,
            amount_min=amount, amount_max=None, annual_rate=0.33,
        )
        for amount in (10_000, 100_000)
    ]
    collector.persist(first, run_id=None)
    # Banka 10.000'lik kademeyi kaldırdı.
    collector.persist(first[1:], run_id=None)

    with SessionLocal() as s:
        rows = s.execute(select(DepositRate)).scalars().all()
    assert [float(r.amount_min) for r in rows] == [100_000]


def test_cleanup_never_touches_interest_bank_rows(db):
    """Temizlik yalnızca is_profit_share satırlarını hedeflemeli.

    Aksi halde katılım toplayıcısı, aynı gün toplanmış banka faizlerini
    silerdi — iki toplayıcı aynı tabloyu paylaşıyor.
    """
    _seed_institutions()
    with SessionLocal() as s:
        s.add(Institution(code="VAKIFBANK", name="VakıfBank", kind="bank"))
        s.commit()
    from store.clock import istanbul_today
    from datetime import datetime, timezone

    with SessionLocal() as s:
        s.add(
            DepositRate(
                institution="VAKIFBANK", currency="TRY", term_days=92, amount_min=0,
                amount_max=None, annual_rate=0.34, is_profit_share=False,
                valid_date=istanbul_today(), fetched_at=datetime.now(timezone.utc),
            )
        )
        s.commit()

    KuveytTurkAnnualRateCollector().persist(
        [
            ParticipationRateRecord(
                institution="KUVEYTTURK", currency="TRY", term_days=92,
                amount_min=0, amount_max=None, annual_rate=0.3442,
            )
        ],
        run_id=None,
    )
    with SessionLocal() as s:
        institutions = sorted(
            r.institution for r in s.execute(select(DepositRate)).scalars().all()
        )
    assert institutions == ["KUVEYTTURK", "VAKIFBANK"]


def test_participation_rows_are_comparable_with_interest_rows(db):
    """Uçtan uca: katılım oranı, faizle AYNI sıralamaya girmeli.

    Kullanıcının asıl istediği buydu — %93 yerine kıyaslanabilir bir sayı.
    """
    from datetime import datetime, timezone

    from store.clock import istanbul_today
    from store.queries import deposit_rates_for_amount

    _seed_institutions()
    with SessionLocal() as s:
        s.add(Institution(code="VAKIFBANK", name="VakıfBank", kind="bank"))
        s.add_all(
            [
                DepositRate(
                    institution="VAKIFBANK", currency="TRY", term_days=92, amount_min=0,
                    amount_max=None, annual_rate=0.34, is_profit_share=False,
                    valid_date=istanbul_today(), fetched_at=datetime.now(timezone.utc),
                ),
                DepositRate(
                    institution="KUVEYTTURK", currency="TRY", term_days=92, amount_min=0,
                    amount_max=None, annual_rate=0.3442, is_profit_share=True,
                    valid_date=istanbul_today(), fetched_at=datetime.now(timezone.utc),
                ),
            ]
        )
        s.commit()

    rows = sorted(
        deposit_rates_for_amount(1_000_000), key=lambda r: -r["annual_rate"]
    )
    assert rows[0]["institution"] == "KUVEYTTURK"
    assert rows[0]["is_profit_share"] is True
    assert rows[1]["institution"] == "VAKIFBANK"


# ------------------------------- bankanın KENDİ net oranıyla çapraz kontrol ----

@pytest.mark.parametrize(
    "term_days, gross_pct, bank_net_pct",
    [
        # Emlak Katılım'ın kendi hesaplama aracının 2026-08-25 tarihli canlı
        # yanıtı (1.000.000 TL): GrossProfitShareYearly / NetProfitShareYearly.
        (31, 31.38, 25.89),
        (91, 33.97, 28.02),
        (180, 35.56, 29.34),
        (364, 41.41, 35.20),
    ],
)
def test_our_withholding_matches_the_banks_own_net_rate(term_days, gross_pct, bank_net_pct):
    """STOPAJ MODELİNİN BAĞIMSIZ DENETİMİ.

    config/taxes.yaml'daki stopaj dilimleri (≤180g %17,5 · 182-365g %15 ·
    ≥366g %10) elle girilmiş değerlerdir; yanlış olsalar hiçbir testimiz
    düşmezdi, çünkü testler de aynı dosyayı okur. Emlak Katılım kendi
    hesaplayıcısında hem BRÜT hem NET yıllık oranı yayınlıyor — bu, dilimleri
    dışarıdan sınayan bağımsız bir tanık.

    Kredi tarafında aynı işi Yapı Kredi'nin taksit tutarı yapıyor
    (tests/test_loan_validation.py); mevduat tarafında bu.
    """
    from config.loader import resolve_deposit_brackets
    from core.deposit import resolve_withholding

    withholding = resolve_withholding(resolve_deposit_brackets("TRY"), term_days)
    ours = gross_pct * (1 - withholding)
    assert ours == pytest.approx(bank_net_pct, abs=0.02), (
        f"{term_days} gün: bizim net %{ours:.2f}, bankanın %{bank_net_pct}"
    )
