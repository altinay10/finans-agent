"""Toplayıcı entegrasyon testleri — gerçek sayfalardan alınmış fixture'larla, AĞSIZ.

Amaç: bir bankanın sayfa yapısı değiştiğinde bunu ağa çıkmadan, saniyeler
içinde yakalamak. Fixture'lar `tests/fixtures/` altında ve 2026-08-23'te canlı
sayfalardan alındı. Testler ayrıca `sanity_check`'in KÖTÜ veriyi gerçekten
reddettiğini doğrular — tasarım §02: "geçemeyen çalıştırma veriyi yazmaz".
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from collectors.base import ParseError, SanityCheckError

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- TCMB ----

def test_tcmb_parses_usd_and_eur_from_real_xml():
    from collectors.fx_tcmb import TcmbCollector

    records = TcmbCollector().parse(_fixture("tcmb_today.xml").encode("utf-8"))
    by_ccy = {r.currency: r for r in records}
    assert set(by_ccy) == {"USD", "EUR"}
    for r in records:
        assert 10 < r.buy < 200
        assert r.buy <= r.sell
        assert r.quoted_at.year >= 2026


def test_tcmb_rejects_malformed_xml():
    from collectors.fx_tcmb import TcmbCollector

    with pytest.raises(ParseError):
        TcmbCollector().parse(b"<not-xml")


def test_tcmb_rejects_xml_without_expected_currencies():
    from collectors.fx_tcmb import TcmbCollector

    xml = b'<?xml version="1.0"?><Tarih_Date Tarih="21.08.2026"><Currency Kod="XYZ"></Currency></Tarih_Date>'
    with pytest.raises(ParseError):
        TcmbCollector().parse(xml)


# ------------------------------------------------------------ bank FX ----

def test_cepteteb_parser_reads_real_payload():
    from collectors.fx_banks import CepteTebParser

    records = CepteTebParser().parse(_fixture("teb_fx.json"))
    by_ccy = {r.currency: r for r in records}
    assert set(by_ccy) == {"USD", "EUR"}
    assert all(r.institution == "TEB" for r in records)
    assert all(r.buy < r.sell for r in records)
    assert all(not r.quoted_at_is_estimated for r in records)  # TEB kendi saatini veriyor


def test_enpara_parser_reads_real_html_and_flags_estimated_timestamp():
    from collectors.fx_banks import EnparaParser

    records = EnparaParser().parse(_fixture("enpara_fx.html"))
    by_ccy = {r.currency: r for r in records}
    assert set(by_ccy) == {"USD", "EUR"}
    assert by_ccy["USD"].buy < by_ccy["USD"].sell
    # Sayfa kendi zaman damgasını vermiyor -> tahmini olarak işaretlenmeli
    assert all(r.quoted_at_is_estimated for r in records)


def test_emlakkatilim_parser_reads_real_html():
    from collectors.fx_banks import EmlakKatilimParser

    records = EmlakKatilimParser().parse(_fixture("emlak_fx.html"))
    by_ccy = {r.currency: r for r in records}
    assert set(by_ccy) == {"USD", "EUR"}
    assert all(r.institution == "EMLAKKATILIM" for r in records)
    assert all(r.quoted_at_is_estimated for r in records)


def test_turkish_decimal_parsing():
    """'47,34217' ve '6.917,05 TL' gibi TR biçimli sayılar doğru okunmalı."""
    from collectors.fx_banks import _tl_to_float

    assert _tl_to_float("47,215000 TL") == pytest.approx(47.215)
    assert _tl_to_float("47,34217") == pytest.approx(47.34217)
    assert _tl_to_float("6.917,054252 TL") == pytest.approx(6917.054252)


def test_vakifbank_parser_maps_sale_and_purchase_correctly():
    """VakıfBank alan adları ters: SaleRate = banka ALIŞ, PurchaseRate = banka SATIŞ."""
    from collectors.fx_banks import VakifBankParser

    body = json.dumps(
        {"Data": {"Currency": [
            {"CurrencyCode": "USD", "RateDate": "2026-08-21T18:00:52",
             "SaleRate": "46.6893", "PurchaseRate": "49.4743"},
            {"CurrencyCode": "Altın", "RateDate": "2026-08-21T18:00:52",
             "SaleRate": "6750.20", "PurchaseRate": "7648.20"},
        ]}}
    )
    records = VakifBankParser().parse(body)
    assert len(records) == 1  # Altın filtrelenmeli
    usd = records[0]
    assert usd.buy == pytest.approx(46.6893)
    assert usd.sell == pytest.approx(49.4743)
    assert usd.buy < usd.sell


def test_vakifbank_parser_names_the_reason_when_the_body_is_empty():
    """Boş gövde ANLAŞILIR bir hata bırakmalı.

    Çıplak `json.loads` canlıda `Expecting value: line 1 column 1 (char 0)`
    yazıyordu (source_runs, 2026-09-08). O mesaj gövdenin boş mu geldiğini,
    token akışının mı düştüğünü, araya engel sayfası mı girdiğini
    ayırmıyor; kaydı okuyan kişi hiçbir şey öğrenmiyor.
    """
    from collectors.base import ParseError
    from collectors.fx_banks import VakifBankParser

    for govde in ("", "   "):
        with pytest.raises(ParseError, match="boş gövde"):
            VakifBankParser().parse(govde)


def test_vakifbank_parser_quotes_the_body_when_it_is_not_json():
    """Gövdenin başı hataya yazılmalı — sebep tek bakışta görünsün."""
    from collectors.base import ParseError
    from collectors.fx_banks import VakifBankParser

    with pytest.raises(ParseError) as hata:
        VakifBankParser().parse("<html><title>Access Denied</title>")

    mesaj = str(hata.value)
    assert "JSON değil" in mesaj
    assert "Access Denied" in mesaj, "gövdenin başı mesajda geçmeli"


def test_bank_fx_record_rejects_inverted_spread():
    from collectors.fx_banks import BankFxRecord

    with pytest.raises(ValueError):
        BankFxRecord(institution="X", currency="USD", buy=50.0, sell=40.0,
                     quoted_at=datetime.now(timezone.utc))


def test_bank_fx_sanity_check_rejects_out_of_band(seeded_db):
    from collectors.fx_banks import BankFxCollector, BankFxRecord

    absurd = [BankFxRecord(institution="TEB", currency="USD", buy=4000.0, sell=4100.0,
                           quoted_at=datetime.now(timezone.utc))]
    with pytest.raises(SanityCheckError):
        BankFxCollector().sanity_check(absurd)


def test_bank_fx_sanity_check_rejects_overnight_jump(seeded_db):
    """Tasarım §02: 'bugünkü değer düne göre %20'den fazla mı sıçramış'."""
    from datetime import datetime as dt

    from collectors.fx_banks import BankFxCollector, BankFxRecord
    from store.db import SessionLocal
    from store.models import FxQuote

    with SessionLocal() as s:
        s.add(FxQuote(institution="TEB", currency="USD", buy=47.0, sell=47.5,
                      quoted_at=dt(2026, 8, 22), fetched_at=dt(2026, 8, 22)))
        s.commit()

    jumped = [BankFxRecord(institution="TEB", currency="USD", buy=70.0, sell=71.0,
                           quoted_at=dt(2026, 8, 23))]
    with pytest.raises(SanityCheckError):
        BankFxCollector().sanity_check(jumped)




# --------------------------------------------------- fon test yardımcıları ----
#
# Fon toplayıcısı artık ÇOK SAĞLAYICILI (collectors/fund_providers.py):
# fetch() çıktısı fon kodundan sağlayıcı yanıtına eşleme taşıyor, parse()
# de hangi sağlayıcının ayrıştıracağını buradan öğreniyor.

def _ak_spec(code: str):
    from collectors.fund_providers import FundSpec

    return FundSpec(code=code, provider="akportfoy", ref=code)


def _ak_payload(code: str, page: str) -> bytes:
    return json.dumps(
        {code: {"ok": True, "provider": "akportfoy", "ref": code, "body": page}}
    )


# ------------------------------------------------------- fon fiyatları ----

def test_akportfoy_parses_embedded_price_series():
    from collectors.fund_prices import FundPriceCollector

    raw = _ak_payload("AK3", _fixture("akportfoy_AK3.html"))
    records = FundPriceCollector(specs=[_ak_spec("AK3")]).parse(raw)

    assert len(records) == 40  # fixture 5 + 35 nokta
    assert all(r.fund_code == "AK3" for r in records)
    assert all(r.price > 0 for r in records)
    # Ad, koddaki "AK3 - " ön eki olmadan gelmeli
    assert not records[0].fund_name.startswith("AK3")
    # "(Hisse Senedi Yoğun Fon)" ibaresi -> stopajdan muaf
    assert records[0].is_equity_heavy is True


def test_akportfoy_epoch_is_interpreted_in_istanbul_time():
    """REGRESYON: 1514840400000 = 2018-01-02 00:00 +03. UTC olarak okunursa
    2018-01-01 çıkar ve tüm fon simülasyonu bir gün kayar."""
    from collectors.fund_providers import epoch_ms_to_istanbul_date

    assert epoch_ms_to_istanbul_date(1514840400000) == date(2018, 1, 2)
    assert epoch_ms_to_istanbul_date(1514926800000) == date(2018, 1, 3)


def test_akportfoy_detects_non_equity_heavy_fund():
    from collectors.fund_prices import FundPriceCollector

    page = (
        '<html><head><title>AFA - Ak Portföy Amerika Yabancı Hisse Senedi Fonu'
        ' | Ak Portföy</title></head><body><script>'
        'var fundVals = {"AFA":[{"Close":1.1,"Date":1514840400000}]};</script></body></html>'
    )
    records = FundPriceCollector(specs=[_ak_spec("AFA")]).parse(
        _ak_payload("AFA", page)
    )
    assert records[0].is_equity_heavy is False
    assert records[0].fund_name == "Ak Portföy Amerika Yabancı Hisse Senedi Fonu"


def test_fund_parse_failure_is_recorded_and_does_not_kill_healthy_funds(db):
    """Bir fonun sayfası değişince diğerleri toplanmaya devam etmeli.

    Tek sağlayıcılı eski toplayıcıda bir fonun bozulması TÜM koşuyu
    düşürüyordu. Çok sağlayıcılı yapıda bu kabul edilemez: Garanti'nin bir
    sayfa değişikliği Ak Portföy fonlarını da karartırdı.
    """
    import json as _json

    from sqlalchemy import select

    from collectors.fund_prices import FundPriceCollector
    from store.db import SessionLocal
    from store.models import SourceRun

    payload = _json.loads(_ak_payload("AK3", _fixture("akportfoy_AK3.html")))
    payload["BOZUK"] = {
        "ok": True, "provider": "akportfoy", "ref": "BOZUK",
        "body": "<html><body>yeniden tasarlandı</body></html>",
    }
    collector = FundPriceCollector(specs=[_ak_spec("AK3"), _ak_spec("BOZUK")])
    records = collector.parse(_json.dumps(payload).encode("utf-8"))

    assert {r.fund_code for r in records} == {"AK3"}     # sağlam fon korundu
    with SessionLocal() as session:
        failed = session.execute(
            select(SourceRun).where(SourceRun.status == "failed")
        ).scalars().all()
    assert [r.source for r in failed] == ["BOZUK"]
    assert "fundVals" in failed[0].error


def test_fund_parse_raises_only_when_every_fund_fails(db):
    """Hepsi düştüyse koşu başarısız sayılmalı — sessizce boş geçmemeli."""
    from collectors.fund_prices import FundPriceCollector

    raw = _ak_payload("AK3", "<html><body>yeniden tasarlandı</body></html>")
    with pytest.raises(ParseError, match="Hiçbir fon"):
        FundPriceCollector(specs=[_ak_spec("AK3")]).parse(raw)


def test_akportfoy_sanity_check_rejects_future_dates():
    from collectors.fund_prices import FundPriceCollector, FundPriceRecord

    bad = [FundPriceRecord(fund_code="AK3", price_date=date.today() + timedelta(days=5), price=50.0)]
    with pytest.raises(SanityCheckError, match="gelecek"):
        FundPriceCollector(specs=[_ak_spec("AK3")]).sanity_check(bad)


def test_akportfoy_sanity_check_rejects_duplicate_dates():
    from collectors.fund_prices import FundPriceCollector, FundPriceRecord

    d = date(2026, 8, 20)
    bad = [
        FundPriceRecord(fund_code="AK3", price_date=d, price=50.0),
        FundPriceRecord(fund_code="AK3", price_date=d, price=51.0),
    ]
    with pytest.raises(SanityCheckError):
        FundPriceCollector(specs=[_ak_spec("AK3")]).sanity_check(bad)


def test_akportfoy_sanity_check_rejects_absurd_price_move():
    from collectors.fund_prices import FundPriceCollector, FundPriceRecord

    bad = [
        FundPriceRecord(fund_code="AK3", price_date=date(2026, 8, 19), price=50.0),
        FundPriceRecord(fund_code="AK3", price_date=date(2026, 8, 20), price=500.0),
    ]
    with pytest.raises(SanityCheckError):
        FundPriceCollector(specs=[_ak_spec("AK3")]).sanity_check(bad)


def test_akportfoy_persist_is_idempotent(seeded_db):
    """Aynı seri iki kez yazılınca satır çoğalmamalı (PK: fund_code+price_date)."""
    from collectors.fund_prices import FundPriceCollector

    raw = _ak_payload("AK3", _fixture("akportfoy_AK3.html"))
    c = FundPriceCollector(specs=[_ak_spec("AK3")])
    records = c.parse(raw)
    c.persist(records, run_id=1)
    c.persist(records, run_id=2)

    from sqlalchemy import func, select

    from store.db import SessionLocal
    from store.models import FundPrice

    with SessionLocal() as s:
        count = s.execute(
            select(func.count()).select_from(FundPrice).where(FundPrice.fund_code == "AK3")
        ).scalar_one()
    assert count == len(records)


# ------------------------------------------------------ mevduat / kredi ----

def test_vakifbank_deposit_parser_builds_amount_term_matrix():
    from collectors.deposit_rates import VakifBankDepositParser

    payload = json.dumps([
        {
            "currency": "TL",
            "amount_min": 50000,
            "amount_max": 25000000,
            "body": {"Data": {"DepositInfo": {"InterestRates": [
                {"TermDaysEnd": "30", "TermDaysStart": "1", "CurrentInterestRate": "41.0",
                 "AmountStart": "0", "AmountEnd": "10000.0"},
                {"TermDaysEnd": "30", "TermDaysStart": "1", "CurrentInterestRate": "42.5",
                 "AmountStart": "10001.0", "AmountEnd": "50000.0"},
            ]}}},
        }
    ])
    records = VakifBankDepositParser().parse(payload)
    assert len(records) == 2
    assert records[0].currency == "TRY"  # 'TL' -> 'TRY' eşlemesi
    assert records[0].annual_rate == pytest.approx(0.41)  # yüzde -> oran
    assert records[1].amount_min == pytest.approx(10001.0)
    assert all(not r.is_profit_share for r in records)


def test_deposit_record_rejects_absurd_rate():
    from collectors.deposit_rates import DepositRateRecord

    with pytest.raises(ValueError):
        DepositRateRecord(institution="X", term_days=32, amount_min=0,
                          amount_max=None, annual_rate=5.0, is_profit_share=False)


def test_vakifbank_loan_parser_maps_products_and_dedupes_campaigns():
    from collectors.loan_rates import VakifBankLoanParser

    body = json.dumps({"Data": {"LoanProduct": [
        {"ProductName": "TİK", "ProductCode": "55500094", "InterestRate": "4.99",
         "Kkdf": "15.0", "Bsmv": "15.0", "MinimumLoanTerm": "3", "MaximumLoanTerm": "24",
         "MaksimumLoanAmount": "250000.0000", "CampaignName": "A"},
        {"ProductName": "TİK", "ProductCode": "55500094", "InterestRate": "4.99",
         "Kkdf": "15.0", "Bsmv": "15.0", "MinimumLoanTerm": "3", "MaximumLoanTerm": "12",
         "MaksimumLoanAmount": "1000000.0000", "CampaignName": "B"},
        {"ProductName": "Konut Kredisi", "ProductCode": "55500113", "InterestRate": "2.95",
         "Kkdf": "0.00", "Bsmv": "0.00", "MinimumLoanTerm": "3", "MaximumLoanTerm": "120",
         "MaksimumLoanAmount": "5000000.0000", "CampaignName": "C"},
        {"ProductName": "Bilinmeyen Ürün", "ProductCode": "999", "InterestRate": "1.0"},
    ]}})
    records = VakifBankLoanParser().parse(body)
    types = {r.loan_type: r for r in records}
    assert set(types) == {"personal", "housing"}  # bilinmeyen ürün atlanmalı, kampanya tekrarı elenmeli
    assert types["personal"].monthly_rate == pytest.approx(0.0499)
    assert types["housing"].monthly_rate == pytest.approx(0.0295)


def test_loan_record_rejects_absurd_monthly_rate():
    from collectors.loan_rates import LoanRateRecord

    with pytest.raises(ValueError):
        LoanRateRecord(institution="X", loan_type="personal", monthly_rate=0.95)


# ------------------------------------------- mevduat: yeni bankalar (3. tur) ----
#
# Bu üç banka 4. oturumda eklendi. Hepsi CANLI çekiliyor — hiçbir oran koda
# gömülü değil. Fixture'lar 2026-08-23'te gerçek yanıtlardan alındı.


def test_teb_deposit_parses_amount_term_matrix():
    from collectors.deposit_rates import CepteTebDepositParser

    records = CepteTebDepositParser().parse(_fixture("teb_deposit.json"))
    assert records, "TEB mevduat matrisi boş çıktı"
    assert {r.institution for r in records} == {"TEB"}
    assert {r.currency for r in records} == {"TRY", "USD", "EUR"}
    for r in records:
        assert 0 <= r.annual_rate <= 2.0
        assert r.term_days > 0
        assert r.amount_max is None or r.amount_max > r.amount_min
        assert r.is_profit_share is False


def test_teb_deposit_uses_cepteteb_rates_not_branch_board_rates():
    """Yanlış uç noktaya geri dönülmesine karşı koruma.

    /services/GetMevduatFaizOranlari TL için her vadeye %3 döndürüyor; bu
    şube tabelası, dijital CepteTEB müşterisinin oranı değil. Gerçek oran
    (32 gün, 1M TL) %35'in üzerinde. Bu test, biri kolay uç noktaya geri
    dönerse anında patlar.
    """
    from collectors.deposit_rates import CepteTebDepositParser

    records = CepteTebDepositParser().parse(_fixture("teb_deposit.json"))
    one_month = [
        r for r in records
        if r.currency == "TRY" and r.term_days == 32 and r.amount_min <= 1_000_000
        and (r.amount_max is None or r.amount_max >= 1_000_000)
    ]
    assert one_month, "32 günlük TL satırı bulunamadı"
    assert max(r.annual_rate for r in one_month) > 0.25, (
        "TL mevduat oranı piyasa dışı derecede düşük — şube tabelası uç noktası mı kullanılıyor?"
    )


def test_teb_deposit_uses_range_start_as_term():
    """Vade aralığı (ör. 32-45 gün) başlangıç günüyle temsil edilir."""
    from collectors.deposit_rates import CepteTebDepositParser

    records = CepteTebDepositParser().parse(_fixture("teb_deposit.json"))
    raw = json.loads(_fixture("teb_deposit.json"))["TL"]
    expected = {int(row["vadeMin"]) for row in raw}
    assert {r.term_days for r in records if r.currency == "TRY"} == expected


def test_teb_deposit_marks_top_tier_as_unbounded():
    from collectors.deposit_rates import CepteTebDepositParser

    records = CepteTebDepositParser().parse(_fixture("teb_deposit.json"))
    assert any(r.amount_max is None for r in records), "en üst tutar kademesi sınırsız olmalı"


def test_teb_deposit_skips_unsupported_currencies():
    """GBP satırları gelirse sessizce atlanmalı, çökmemeli."""
    from collectors.deposit_rates import CepteTebDepositParser

    payload = json.dumps(
        {
            "GBP": [
                {"tutarMin": 0, "tutarMax": 50000, "vadeMin": 32, "vadeMax": 45,
                 "faizOran": 1.0, "paraKodu": "GBP"}
            ]
        }
    )
    assert CepteTebDepositParser().parse(payload) == []


def test_enpara_deposit_parses_all_three_currencies():
    from collectors.deposit_rates import EnparaDepositParser

    records = EnparaDepositParser().parse(_fixture("enpara_deposit.html"))
    assert records
    assert {r.institution for r in records} == {"ENPARA"}
    assert {r.currency for r in records} == {"TRY", "USD", "EUR"}
    try_rows = [r for r in records if r.currency == "TRY"]
    assert {r.term_days for r in try_rows} == {32, 46, 92, 181}
    # En yüksek kademe üst sınırsız olmalı ("1.500.000 TL ve üzeri").
    assert any(r.amount_max is None for r in try_rows)
    for r in records:
        assert 0 < r.annual_rate <= 2.0


def test_enpara_deposit_rate_increases_with_amount_tier():
    """Kademe sınırlarının doğru okunduğunun dolaylı kanıtı: TL oranı kademeyle artıyor."""
    from collectors.deposit_rates import EnparaDepositParser

    records = EnparaDepositParser().parse(_fixture("enpara_deposit.html"))
    tiers = sorted(
        (r.amount_min, r.annual_rate)
        for r in records
        if r.currency == "TRY" and r.term_days == 32
    )
    rates = [rate for _, rate in tiers]
    assert rates == sorted(rates), f"kademeler karışmış: {tiers}"
    assert len(rates) >= 4


def test_yapikredi_deposit_crosses_tenor_groups_with_amount_levels():
    from collectors.deposit_rates import YapiKrediDepositParser

    records = YapiKrediDepositParser().parse(_fixture("yapikredi_deposit.json"))
    assert records
    assert {r.institution for r in records} == {"YAPIKREDI"}
    assert {r.currency for r in records} == {"TRY", "USD", "EUR"}
    # Vade olarak grubun BAŞLANGIÇ günü yazılmalı (28-31 kovası -> 28).
    assert 28 in {r.term_days for r in records if r.currency == "TRY"}
    for r in records:
        assert r.amount_max is None or r.amount_max > r.amount_min
        assert 0 <= r.annual_rate <= 2.0


def test_yapikredi_deposit_skips_groups_with_mismatched_rate_count():
    """Rates dizisi kademe sayısıyla uyuşmuyorsa o grup yazılmaz.

    Yanlış hizalama sessizce YANLIŞ kademeye oran yazardı — reddetmek yeğdir.
    """
    from collectors.deposit_rates import YapiKrediDepositParser

    payload = json.loads(_fixture("yapikredi_deposit.json"))
    block = payload["YTL"]["d"]["Data"]["RateList"][0]
    before = len(YapiKrediDepositParser().parse(json.dumps({"YTL": payload["YTL"]})))
    block["GroupedRateList"][0]["Rates"] = block["GroupedRateList"][0]["Rates"][:-1]
    after = len(YapiKrediDepositParser().parse(json.dumps({"YTL": payload["YTL"]})))
    assert after < before


def test_deposit_collector_survives_one_bank_failing():
    """Dört bankadan biri düşerse diğer üçünün verisi yazılmaya devam etmeli."""
    from collectors.deposit_rates import DepositRateCollector

    payload = json.dumps(
        {
            "teb": {"ok": True, "body": _fixture("teb_deposit.json")},
            "vakifbank": {"ok": False, "error": "connection reset"},
            "enpara": {"ok": True, "body": _fixture("enpara_deposit.html")},
            "yapikredi": {"ok": True, "body": _fixture("yapikredi_deposit.json")},
        }
    )
    records = DepositRateCollector().parse(payload)
    assert {r.institution for r in records} == {"TEB", "ENPARA", "YAPIKREDI"}


def test_deposit_collector_raises_when_every_bank_fails():
    from collectors.deposit_rates import DepositRateCollector

    payload = json.dumps(
        {"teb": {"ok": False, "error": "boom"}, "enpara": {"ok": False, "error": "boom"}}
    )
    with pytest.raises(ParseError):
        DepositRateCollector().parse(payload)


# ----------------------------------------------- kredi: Yapı Kredi (3. tur) ----


def test_yapikredi_loan_parses_personal_and_housing():
    from collectors.loan_rates import YapiKrediLoanParser

    records = YapiKrediLoanParser().parse(_fixture("yapikredi_loan.json"))
    by_type = {r.loan_type: r for r in records}
    assert set(by_type) == {"personal", "housing"}
    for r in records:
        assert r.institution == "YAPIKREDI"
        assert 0 < r.monthly_rate < 0.20
        assert r.term_min is not None and r.term_max is not None
        assert r.term_min <= r.term_max


def test_yapikredi_loan_uses_shortest_maturity_rate_for_personal():
    """Banka uzun vadede indirim uyguluyor; tabela oranı EN KISA vadeninkidir."""
    from collectors.loan_rates import YapiKrediLoanParser

    payload = json.loads(_fixture("yapikredi_loan.json"))
    plans = payload["personal"]["d"]["Data"]["PaymentPlanList"]
    shortest = min(plans, key=lambda p: p["Maturity"])

    records = YapiKrediLoanParser().parse(json.dumps(payload))
    personal = next(r for r in records if r.loan_type == "personal")
    assert personal.monthly_rate == pytest.approx(shortest["InterestRate"] / 100)


def test_yapikredi_loan_handles_empty_response_without_crashing():
    """Taşıt uç noktası canlıda boş dönüyor; bu bir çökme değil, 'kayıt yok' olmalı."""
    from collectors.loan_rates import YapiKrediLoanParser

    empty = json.dumps(
        {
            "personal": {"d": {"Data": {"PaymentPlanList": None}}},
            "housing": {"d": {"Data": {"PaymentList": None}}},
        }
    )
    assert YapiKrediLoanParser().parse(empty) == []


def test_loan_collector_survives_one_bank_failing():
    from collectors.loan_rates import LoanRateCollector

    payload = json.dumps(
        {
            "vakifbank": {"ok": False, "error": "timeout"},
            "yapikredi": {"ok": True, "body": _fixture("yapikredi_loan.json")},
        }
    )
    records = LoanRateCollector().parse(payload)
    assert {r.institution for r in records} == {"YAPIKREDI"}


def test_deposit_persist_removes_stale_rows_for_the_same_day(seeded_db):
    """Banka vade setini değiştirirse aynı güne ait eski satırlar silinmeli.

    Upsert tek başına yetmez: eski anahtar hiç güncellenmez ve panelde artık
    geçerli olmayan bir oranla yan yana görünür. Bu tam olarak TEB'de yaşandı
    (yanlış uç noktadan doğrusuna geçişte 3%'lik satırlar takılı kalmıştı).
    """
    from datetime import date as _date, datetime as _dt, timezone as _tz

    from collectors.deposit_rates import DepositRateCollector, DepositRateRecord
    from store.db import SessionLocal
    from store.models import DepositRate

    with SessionLocal() as s:
        s.add(
            DepositRate(
                institution="TEB", currency="TRY", term_days=30, amount_min=0,
                amount_max=None, annual_rate=0.03, is_profit_share=False,
                valid_date=_date.today(), fetched_at=_dt.now(_tz.utc),
            )
        )
        s.commit()

    fresh = [
        DepositRateRecord(
            institution="TEB", currency="TRY", term_days=32, amount_min=0,
            amount_max=None, annual_rate=0.365, is_profit_share=False,
        )
    ]
    DepositRateCollector().persist(fresh, run_id=1)

    from store import queries

    rows = [r for r in queries.deposit_rates_for_amount(1_000_000, "TRY") if r["institution"] == "TEB"]
    assert {r["term_days"] for r in rows} == {32}, "eski 30 günlük %3 satırı silinmedi"


def test_deposit_persist_leaves_other_institutions_alone(seeded_db):
    """Temizlik yalnızca o koşuda verisi gelen kuruma dokunmalı."""
    from datetime import date as _date, datetime as _dt, timezone as _tz

    from collectors.deposit_rates import DepositRateCollector, DepositRateRecord
    from store.db import SessionLocal
    from store.models import DepositRate

    with SessionLocal() as s:
        s.add(
            DepositRate(
                institution="VAKIFBANK", currency="TRY", term_days=91, amount_min=0,
                amount_max=None, annual_rate=0.37, is_profit_share=False,
                valid_date=_date.today(), fetched_at=_dt.now(_tz.utc),
            )
        )
        s.commit()

    DepositRateCollector().persist(
        [
            DepositRateRecord(
                institution="TEB", currency="TRY", term_days=32, amount_min=0,
                amount_max=None, annual_rate=0.365, is_profit_share=False,
            )
        ],
        run_id=1,
    )

    from store import queries

    institutions = {r["institution"] for r in queries.deposit_rates_for_amount(1_000_000, "TRY")}
    assert institutions == {"VAKIFBANK", "TEB"}


# ------------------------------------------------- döviz: saat dilimi + YK ----


def test_bank_fx_timestamps_are_converted_from_istanbul_to_utc():
    """Bankalar kotasyon saatini yerel (Europe/Istanbul) yayınlıyor.

    Doğrudan UTC diye etiketlemek kotasyonu üç saat ileri kaydırıyordu:
    taze bir kur "gelecekten" gelmiş gibi görünüyor, tazelik hesabı negatif
    yaş üretiyordu. Bu test yaz saatinde 3 saatlik farkı kilitler.
    """
    from collectors.fx_banks import CepteTebParser

    payload = json.dumps(
        {"result": [{"paraKodu": "USD", "tebAlis": 45.9, "tebSatis": 50.0,
                     "fiyatZaman": "23/08/2026 14:00:00"}]}
    )
    record = CepteTebParser().parse(payload)[0]
    assert record.quoted_at == datetime(2026, 8, 23, 11, 0, tzinfo=timezone.utc)


def test_vakifbank_fx_timestamp_is_converted_from_istanbul_to_utc():
    from collectors.fx_banks import VakifBankParser

    payload = json.dumps(
        {"Data": {"Currency": [{"CurrencyCode": "USD", "SaleRate": "46.68",
                                "PurchaseRate": "49.47",
                                "RateDate": "2026-08-21T18:00:52"}]}}
    )
    record = VakifBankParser().parse(payload)[0]
    assert record.quoted_at == datetime(2026, 8, 21, 15, 0, 52, tzinfo=timezone.utc)


def test_yapikredi_fx_parses_usd_and_eur_and_skips_gold():
    from collectors.fx_banks import YapiKrediParser

    records = YapiKrediParser().parse(_fixture("yapikredi_fx.json"))
    by_ccy = {r.currency: r for r in records}
    assert set(by_ccy) == {"USD", "EUR"}, "XAU (altın) satırı atlanmalıydı"
    for r in records:
        assert r.institution == "YAPIKREDI"
        assert 10 < r.buy < r.sell < 200
        # Kendi zaman damgasını veriyor -> tahmini DEĞİL.
        assert r.quoted_at_is_estimated is False


def test_fx_collector_survives_one_institution_failing():
    from collectors.fx_banks import BankFxCollector

    payload = json.dumps(
        {
            "yapikredi": {"ok": True, "body": _fixture("yapikredi_fx.json")},
            "vakifbank": {"ok": False, "error": "token alınamadı"},
        }
    )
    records = BankFxCollector().parse(payload)
    assert {r.institution for r in records} == {"YAPIKREDI"}


def test_fx_collector_raises_when_every_institution_fails():
    from collectors.fx_banks import BankFxCollector

    payload = json.dumps({"teb": {"ok": False, "error": "boom"}})
    with pytest.raises(ParseError):
        BankFxCollector().parse(payload)


def test_emlak_katilim_loan_reads_profit_rate_from_example_table():
    from collectors.loan_rates import EmlakKatilimLoanParser

    records = EmlakKatilimLoanParser().parse(_fixture("emlak_loan.html"))
    assert len(records) == 1
    r = records[0]
    assert r.institution == "EMLAKKATILIM"
    assert r.loan_type == "personal"
    assert 0 < r.monthly_rate < 0.20
    # Sayfadaki tablo tek örnek satırdır (30.000 TL / 12 ay); vade sınırı
    # buna kilitlenmeli ki panel başka vadede uyarsın.
    assert r.term_min == r.term_max == 12


def test_emlak_katilim_loan_ignores_broken_data_title_attributes():
    """Sayfadaki data-title'lar bozuk (oran hücresi data-title='Yabancı Para').

    Sütun eşlemesi thead başlıklarından yapılmalı; data-title'a güvenen bir
    parser yanlış hücreyi okur.
    """
    from collectors.loan_rates import EmlakKatilimLoanParser

    raw = _fixture("emlak_loan.html")
    assert 'data-title="Yabancı Para">1,69%' in raw.replace("\n", ""), (
        "fixture değişmiş — bozuk data-title artık yok, test anlamsızlaştı"
    )
    assert EmlakKatilimLoanParser().parse(raw)[0].monthly_rate == pytest.approx(0.0169)


def test_emlak_katilim_loan_returns_nothing_when_table_missing():
    from collectors.loan_rates import EmlakKatilimLoanParser

    assert EmlakKatilimLoanParser().parse("<html><body>oran yok</body></html>") == []


# ------------------------------------ robots kısıtı kaldırılan kaynaklar (5. tur) ----
#
# Akbank ve Ziraat, kullanıcı kararıyla (2026-08-24) açıldı; ikisinin de
# uç noktası robots.txt'in /_layouts* yasağındaydı.


def test_akbank_fx_parses_deposit_rates_not_cash_rates():
    """DovizAlis/DovizSatis kullanılmalı; EfektifAlis/Satis nakit kurudur."""
    from collectors.fx_banks import AkbankParser

    records = AkbankParser().parse(_fixture("akbank_fx.json"))
    by_ccy = {r.currency: r for r in records}
    assert set(by_ccy) == {"USD", "EUR"}
    raw = json.loads(_fixture("akbank_fx.json"))["d"]["Data"]["DovizKurlari"]
    usd = next(r for r in raw if r["AlfaKod"] == "USD")
    assert by_ccy["USD"].buy == pytest.approx(float(usd["DovizAlis"]))
    for r in records:
        assert r.quoted_at_is_estimated is False  # kendi saatini veriyor
        assert 10 < r.buy < r.sell < 200


def test_akbank_fx_timestamp_is_converted_from_istanbul():
    from collectors.fx_banks import AkbankParser

    payload = json.dumps(
        {"d": {"Data": {"DovizKurlari": [{
            "AlfaKod": "USD", "DovizAlis": "46.581", "DovizSatis": "49.281",
            "KurGuncellemeZamani": "24.08.2026 14:00:00",
        }]}}}
    )
    record = AkbankParser().parse(payload)[0]
    assert record.quoted_at == datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc)


def test_ziraat_fx_parses_html_wrapped_json():
    from collectors.fx_banks import ZiraatParser

    records = ZiraatParser().parse(_fixture("ziraat_fx.json"))
    by_ccy = {r.currency: r for r in records}
    assert set(by_ccy) == {"USD", "EUR"}
    for r in records:
        assert 10 < r.buy < r.sell < 200
        # Sayfa kendi kotasyon saatini vermiyor.
        assert r.quoted_at_is_estimated is True


def test_akbank_deposit_crosses_headers_with_gross_rates():
    from collectors.deposit_rates import AkbankDepositParser

    records = AkbankDepositParser().parse(_fixture("akbank_deposit.json"))
    assert records
    assert {r.institution for r in records} == {"AKBANK"}
    assert {r.currency for r in records} == {"TRY"}
    for r in records:
        assert 0 <= r.annual_rate <= 2.0
        assert r.amount_max is None or r.amount_max > r.amount_min
    # En üst kademe sentinel değil, sınırsız olmalı.
    assert any(r.amount_max is None for r in records)


def test_akbank_deposit_skips_dash_cells():
    """Oran '-' ise o kademede o vade açık değildir; satır üretilmemeli."""
    from collectors.deposit_rates import AkbankDepositParser

    payload = json.dumps(
        {"d": {"Data": {"ServiceData": {
            "Headers": ["1.000 - 9.999", "10.000 - 99.999"],
            "GrossRates": [{"Period": "7 - 27", "GRates": [{"Rate": "-"}, {"Rate": "2,00"}]}],
        }}}}
    )
    records = AkbankDepositParser().parse(payload)
    assert len(records) == 1
    assert records[0].amount_min == 10_000
    assert records[0].annual_rate == pytest.approx(0.02)


def test_akbank_deposit_skips_misaligned_rate_rows():
    from collectors.deposit_rates import AkbankDepositParser

    payload = json.dumps(
        {"d": {"Data": {"ServiceData": {
            "Headers": ["1.000 - 9.999", "10.000 - 99.999"],
            "GrossRates": [{"Period": "7 - 27", "GRates": [{"Rate": "2,00"}]}],
        }}}}
    )
    assert AkbankDepositParser().parse(payload) == []


def test_akbank_loan_covers_all_three_types_including_vehicle():
    """Taşıt kredisi projede SADECE burada var — kaybolursa fark edilmeli."""
    from collectors.loan_rates import AkbankLoanParser

    records = AkbankLoanParser().parse(_fixture("akbank_loan.json"))
    by_type = {r.loan_type: r for r in records}
    assert set(by_type) == {"personal", "housing", "vehicle"}
    for r in records:
        assert r.institution == "AKBANK"
        assert 0 < r.monthly_rate < 0.20
        assert r.term_min is not None and r.term_max is not None and r.term_min <= r.term_max
        assert r.amount_max is not None and r.amount_max > 0


def test_akbank_loan_uses_shortest_maturity_rate():
    from collectors.loan_rates import AkbankLoanParser

    payload = json.loads(_fixture("akbank_loan.json"))
    rates = payload["10-10-10-8207"]["d"]["Data"][0]["ServisData"]["ucretOut"]["urunFaizListesi"]
    shortest = min(rates, key=lambda r: r["faizMinVade"])

    records = AkbankLoanParser().parse(json.dumps(payload))
    personal = next(r for r in records if r.loan_type == "personal")
    assert personal.monthly_rate == pytest.approx(shortest["faizOranTL"] / 100)


# --------------------------------------- katılım: kâr paylaşım oranı (5. tur) ----


def test_emlak_profit_shares_parse_all_currencies():
    from collectors.profit_shares import EmlakKatilimProfitShareCollector

    records = EmlakKatilimProfitShareCollector().parse(
        _fixture("emlak_profit_share.html").encode("utf-8")
    )
    assert {r.currency for r in records} >= {"TRY", "USD", "EUR"}
    for r in records:
        # Paylaşım oranı bir yüzdedir; 1'i aşarsa ayrıştırma bozulmuştur.
        assert 0 < r.share_ratio <= 1
        assert r.term_days > 0


def test_emlak_profit_share_withholding_matches_our_tax_config():
    """Bağımsız teyit: bankanın yayınladığı stopaj, taxes.yaml ile örtüşmeli.

    Sayfada TL için 17,5 / 17,5 / 17,5 / 17,5 / 15 / 10 ve döviz için 25
    yazıyor. Bu, config/taxes.yaml'daki kademelerin ikinci kaynaktan
    doğrulanmasıdır.
    """
    from collectors.profit_shares import EmlakKatilimProfitShareCollector

    records = EmlakKatilimProfitShareCollector().parse(
        _fixture("emlak_profit_share.html").encode("utf-8")
    )
    by_label = {
        (r.currency, r.term_label): r.withholding_rate
        for r in records
        if r.withholding_rate is not None
    }
    assert by_label[("TRY", "3 Aylık")] == pytest.approx(0.175)
    assert by_label[("TRY", "Yıllık")] == pytest.approx(0.15)
    assert by_label[("TRY", "1 Yıldan Uzun")] == pytest.approx(0.10)
    assert by_label[("USD", "Yıllık")] == pytest.approx(0.25)


def test_emlak_profit_share_number_formats_are_not_confused():
    """Aynı sayfada nokta hem binlik hem ondalık ayırıcı olarak kullanılıyor.

    "24.999" yirmi dört bin, "17.50%" yüzde on yedi buçuk. Tek ayrıştırıcı
    kullanmak stopajı 1750 okurdu.
    """
    from collectors.profit_shares import _amount, _percent

    assert _amount("24.999") == 24_999
    assert _amount("+1.250.000") == 1_250_000
    assert _amount("99,99") == pytest.approx(99.99)
    assert _percent("17.50%") == pytest.approx(17.5)
    assert _percent("15%") == pytest.approx(15)
    assert _percent("92") == pytest.approx(92)


def test_emlak_profit_share_rejects_out_of_band_ratio():
    from collectors.profit_shares import ProfitShareRecord

    with pytest.raises(Exception):
        ProfitShareRecord(
            institution="EMLAKKATILIM", currency="TRY", term_label="Yıllık",
            term_days=365, amount_min=0, amount_max=None, share_ratio=92.0,
        )


def test_emlak_profit_share_sanity_requires_try_table():
    from collectors.profit_shares import EmlakKatilimProfitShareCollector, ProfitShareRecord

    only_usd = [
        ProfitShareRecord(
            institution="EMLAKKATILIM", currency="USD", term_label="Yıllık",
            term_days=365, amount_min=0, amount_max=None, share_ratio=0.5,
        )
    ]
    with pytest.raises(SanityCheckError):
        EmlakKatilimProfitShareCollector().sanity_check(only_usd)


# ------------------------------------------- Halkbank / Ziraat mevduat ----
#
# Fixture'lar 2026-09-04'te canlı uç noktalardan alındı.


def test_halkbank_deposit_parses_real_matrix():
    from collectors.deposit_rates import HalkbankDepositParser

    records = HalkbankDepositParser().parse(_fixture("halkbank_deposit.json"))
    assert records, "Halkbank mevduat matrisi boş çıktı"
    assert {r.institution for r in records} == {"HALKBANK"}
    assert {r.currency for r in records} == {"TRY"}
    for r in records:
        assert 0 <= r.annual_rate <= 2.0
        assert r.term_days > 0
        assert r.amount_max is None or r.amount_max > r.amount_min
        assert r.is_profit_share is False


def test_halkbank_deposit_reads_internet_rates_not_branch_rates():
    """Şube tablosuna geri dönülmesine karşı koruma.

    interestType=1 (şube) TÜM vadelerde %5-6 döndürüyor; gerçek oran
    internet/mobil kanalda (interestType=2) ve 1M TL / 32 gün için %35.
    Biri kolay olsun diye tipi değiştirirse bu test patlar.
    """
    from collectors.deposit_rates import HalkbankDepositParser

    assert HalkbankDepositParser.INTEREST_TYPE == 2
    records = HalkbankDepositParser().parse(_fixture("halkbank_deposit.json"))
    bir_milyon = [
        r for r in records
        if r.term_days == 32 and r.amount_min <= 1_000_000 <= (r.amount_max or float("inf"))
    ]
    assert bir_milyon, "32 gün / 1M TL kademesi bulunamadı"
    assert max(r.annual_rate for r in bir_milyon) > 0.25


def test_halkbank_deposit_skips_rows_without_day_based_term():
    """"Birikimli Mevduat Hesabı" gibi ürünlerde minMaturity=0 gelir.

    Bunlara uydurma bir vade yazmak panelin vade kıyasını bozar; parser
    atlamalı. Satır fixture'a EKLENEREK sınanıyor: bugünkü internet
    tablosunda böyle bir ürün yok (yalnızca şube tablosunda var), ama banka
    onu buraya taşıdığı gün parser hazır olmalı.
    """
    from collectors.deposit_rates import HalkbankDepositParser

    payload = json.loads(_fixture("halkbank_deposit.json"))
    kademe_sayisi = len(payload["data"]["amountRangeList"])
    payload["data"]["rateDetails"].append(
        {
            "minMaturity": 0,
            "maxMaturity": 0,
            "maturityRange": "Birikimli Mevduat Hesabı",
            "amountRateList": [
                {"minAmount": etiket.split("-")[0].strip(), "rate": "30,00"}
                for etiket in payload["data"]["amountRangeList"]
            ],
        }
    )
    assert kademe_sayisi > 0
    records = HalkbankDepositParser().parse(json.dumps(payload))
    assert records, "gerçek satırlar da düşmemeli"
    assert all(r.term_days > 0 for r in records)


def test_halkbank_deposit_skips_row_when_tier_count_mismatches():
    """Şema kayarsa oran YANLIŞ kademeye yazılmamalı — satır düşmeli."""
    from collectors.deposit_rates import HalkbankDepositParser

    payload = json.loads(_fixture("halkbank_deposit.json"))
    for detail in payload["data"]["rateDetails"]:
        detail["amountRateList"] = detail["amountRateList"][:-1]
    assert HalkbankDepositParser().parse(json.dumps(payload)) == []


def test_ziraat_deposit_parses_real_table():
    from collectors.deposit_rates import ZiraatDepositParser

    records = ZiraatDepositParser().parse(_fixture("ziraat_deposit.html"))
    assert records, "Ziraat mevduat tablosu boş çıktı"
    assert {r.institution for r in records} == {"ZIRAAT"}
    assert {r.currency for r in records} == {"TRY"}
    for r in records:
        assert 0 <= r.annual_rate <= 2.0
        assert r.term_days > 0
        assert r.amount_max is None or r.amount_max > r.amount_min


def test_ziraat_deposit_reads_internet_table_not_branch_table():
    """2026-09-04'te canlıda yakalanan hatanın regresyon testi.

    Sayfada aynı ad iki kez geçiyor: radio düğmesinin `id=` ve tabloyu saran
    div'in `data-id=` özniteliği. `id=` ile eşleşen desen radio'yu bulup
    ondan sonraki ilk tabloyu — yani ŞUBE tablosunu — okuyordu. Fixture her
    iki bölümü de içerir; şube tablosunda 32 gün %5,00, internet tablosunda
    %34,00.
    """
    from collectors.deposit_rates import ZiraatDepositParser

    ham = _fixture("ziraat_deposit.html")
    assert 'data-id="rdBranchVadeliTL"' in ham, "fixture şube bölümünü içermeli"
    records = ZiraatDepositParser().parse(ham)
    yuksek = [
        r for r in records
        if r.term_days == 32 and r.amount_min <= 1_000_000 <= (r.amount_max or float("inf"))
    ]
    assert yuksek, "32 gün / 1M TL kademesi bulunamadı"
    assert max(r.annual_rate for r in yuksek) > 0.25, "şube tablosu okunmuş olabilir"


def test_ziraat_deposit_raises_when_section_missing():
    from collectors.deposit_rates import ZiraatDepositParser

    with pytest.raises(ValueError):
        ZiraatDepositParser().parse("<html><body><table><tr><td>yok</td></tr></table></body></html>")


def test_deposit_number_helpers_handle_both_locales():
    """Ziraat'in aynı tablosunda tutar Türkçe, oran İngilizce biçimde.

    Tek biçim varsayan bir dönüştürücü ikisinden birini mutlaka bozar:
    '%30.00' -> 3000 ya da '5.000' -> 5.0.
    """
    from collectors.deposit_rates import _rate_number, _tr_number

    assert _tr_number("25.001,00") == 25001.0
    assert _tr_number("5.000") == 5000.0          # binlik
    assert _tr_number("30.00") == 30.0            # ondalık
    assert _tr_number("0.001") == 0.001           # binlik ayracı 0'dan sonra gelmez
    assert _tr_number("99.999.999.999,99") == 99999999999.99
    assert _rate_number("%30.00") == 30.0
    assert _rate_number("36,00") == 36.0
    assert _rate_number("%5.000") == 5.0          # oranda binlik yoktur


# ---------------------------------------------------- Kuveyt Türk kur ----


def test_kuveytturk_fx_parses_real_payload():
    from collectors.fx_banks import KuveytTurkParser

    records = KuveytTurkParser().parse(_fixture("kuveytturk_fx.json"))
    by_ccy = {r.currency: r for r in records}
    assert set(by_ccy) == {"USD", "EUR"}
    for r in records:
        assert r.institution == "KUVEYTTURK"
        assert 10 < r.buy < 200
        assert r.buy <= r.sell
        # Kaynak kendi kotasyon saatini yayınlamıyor; panel bunu "≈" ile
        # işaretliyor. Bayrağı kaldırmak kullanıcıya olmayan bir kesinlik
        # vaat ederdi.
        assert r.quoted_at_is_estimated is True


def test_kuveytturk_fx_ignores_metals_and_tl():
    """Yanıt altın/gümüş/platin ve TL=1,0 satırlarını da içeriyor.

    TL satırı (BuyRate=SellRate=1,0) SANITY_BAND'e takılmaz ama panelde
    anlamsız bir "kur" satırı üretirdi.
    """
    from collectors.fx_banks import KuveytTurkParser

    ham = _fixture("kuveytturk_fx.json")
    assert '"CurrencyCode": "TL"' in ham or '"CurrencyCode":"TL"' in ham
    records = KuveytTurkParser().parse(ham)
    assert {r.currency for r in records} == {"USD", "EUR"}
