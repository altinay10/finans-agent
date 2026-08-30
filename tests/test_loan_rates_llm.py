"""Agent tabanlı kredi oranı toplayıcısı.

Bir modelin çıktısını doğrudan finansal tabloya yazmak kabul edilemez.
Buradaki testler, uydurma bir faiz oranının veritabanına girmesini engelleyen
üç katmanı kilitliyor — en önemlisi ZEMİNLEME: modelin döndürdüğü her oran
sayfanın metninde gerçekten geçmek zorunda.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from collectors.base import ParseError, SanityCheckError
from collectors.loan_rates import LoanRateRecord
from collectors.loan_rates_llm import (
    LlmLoanRateCollector,
    LlmLoanRow,
    _appears_in_text,
    _looks_blocked,
    _rate_regions,
    _visible_text,
)
from store.db import SessionLocal
from store.models import Institution, LoanRate


# ------------------------------------------------------------ zeminleme ----

@pytest.mark.parametrize(
    "rate, text, beklenen",
    [
        (2.99, "36 ay vadede aylık %2,99 faiz", True),     # Türkçe virgül
        (2.99, "monthly rate 2.99%", True),                # nokta
        (3.19, "%3,19 oranıyla", True),
        (1.70, "aylık %1,70", True),                       # sondaki sıfır
        (3.00, "aylık %2,99", False),                      # YAKIN ama başka oran
        (4.19, "hiç oran yok", False),
    ],
)
def test_grounding_accepts_only_rates_that_really_appear(rate, text, beklenen):
    """Model bir sayı uydurursa kaynakta bulunmaz — asıl güvence bu.

    Yuvarlama toleransı YOK ve olmamalı: %2,99 ile %3,00 aynı oran değildir
    ve aradaki fark 1.000.000 TL'lik bir kredide binlerce lira eder.
    """
    assert _appears_in_text(rate, text) is beklenen


def test_hallucinated_rate_is_dropped_and_counted(db, caplog):
    """Sayfada olmayan oran ATILIR, atıldığı da kayda geçer."""
    collector = LlmLoanRateCollector(banks={})
    rows = [
        LlmLoanRow(loan_type="personal", monthly_rate_percent=2.99, available_to_all=True),
        LlmLoanRow(loan_type="housing", monthly_rate_percent=9.99,
                   available_to_all=True),                            # uydurma
    ]
    from collectors.loan_rates_llm import _ground

    kept, dropped = _ground(rows, "ihtiyaç kredisi aylık %2,99", "TEST")
    assert [r.loan_type for r in kept] == ["personal"]
    assert dropped == 1


# ------------------------------------------------------- bant kontrolü ----

def test_annual_rate_mistaken_for_monthly_is_rejected():
    """En olası model hatası: yıllık maliyet oranını aylık sanmak.

    QNB sayfasında hem %3,19 (aylık) hem %71,24 (yıllık maliyet) yazıyor;
    ikisi de sayfada geçtiği için zeminleme bunu yakalayamaz. Bandı aşan
    oranı reddeden katman burası.
    """
    with pytest.raises(ValueError):
        LlmLoanRow(loan_type="personal", monthly_rate_percent=71.24)


def test_zero_and_negative_rates_are_rejected():
    for bad in (0.0, -1.5):
        with pytest.raises(ValueError):
            LlmLoanRow(loan_type="personal", monthly_rate_percent=bad)


def test_unknown_loan_type_is_rejected():
    with pytest.raises(ValueError):
        LlmLoanRow(loan_type="kredi_karti", monthly_rate_percent=2.99)


# --------------------------------------------------------- token disiplini ----

def test_rate_regions_shrink_the_page_but_keep_the_rate():
    """Modele 105 KB göndermek token'ın çoğunu menüye harcar.

    Daha kötüsü: genel kırpma aranan oranı pencerenin dışında bırakabilir
    ve model sayfada yazan oranı GÖREMEDEN cevap üretmeye zorlanır — bu,
    uydurmayı davet eden tek durumdur.
    """
    text = ("menü " * 3000) + "ihtiyaç kredisi aylık %2,99 faiz" + (" yasal metin" * 3000)
    focused = _rate_regions(text)
    assert "%2,99" in focused
    assert len(focused) < len(text) / 10


def test_page_without_any_rate_yields_empty_so_the_model_is_not_called():
    """Oransız sayfaya token harcamak anlamsız — çağrı hiç yapılmamalı."""
    assert _rate_regions("bu sayfada hiç oran yok, sadece metin") == ""


def test_rate_regions_respects_the_character_budget():
    text = " ".join(f"aylık %{i % 9 + 1},50 faiz oranı" for i in range(2000))
    assert len(_rate_regions(text)) <= 6_000


def test_collector_skips_cleanly_when_the_agent_is_disabled(db, monkeypatch):
    """Anahtar yoksa TEK TOKEN harcanmadan, açık bir mesajla bitmeli."""
    import llm.settings as settings

    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", False)
    collector = LlmLoanRateCollector(banks={})
    payload = json.dumps({"HALKBANK": {"ok": True, "body": "aylık %2,99"}}).encode()
    with pytest.raises(ParseError, match="Agent kapalı"):
        collector.parse(payload)


# ----------------------------------------------------------- bot tespiti ----

def test_waf_block_page_is_detected_and_never_sent_to_the_model():
    """İş Bankası HTTP 200 dönüyor ama içerik engel sayfası.

    Modele göndermek hem token yakar hem de engel sayfasından oran
    "çıkarmaya" zorlar. Ayrıca bu bir robots.txt nezaket kuralı değil,
    aktif bot tespiti — aşılmıyor.
    """
    assert _looks_blocked("<html><body>İstek Engellenmiştir. Referans no: 123</body></html>")
    assert _looks_blocked('window["bobcmn"] = "101111"')
    assert not _looks_blocked("<html><body>aylık %2,99</body></html>")


# ------------------------------------------------------------- sanity ----

def test_wildly_conflicting_rates_from_the_agent_are_rejected(db):
    collector = LlmLoanRateCollector(banks={})
    with pytest.raises(SanityCheckError, match="sapan"):
        collector.sanity_check(
            [
                LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0299),
                LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.1899),
            ]
        )


def test_term_bounds_must_be_ordered(db):
    collector = LlmLoanRateCollector(banks={})
    with pytest.raises(SanityCheckError, match="term_min"):
        collector.sanity_check(
            [
                LoanRateRecord(
                    institution="ING", loan_type="personal", monthly_rate=0.0299,
                    term_min=60, term_max=12,
                )
            ]
        )


# ------------------------------------------------------------ persist ----

def test_persist_keeps_the_lowest_rate_when_a_page_lists_several(db):
    """Kampanya oranı ile tabela oranı yan yana durabiliyor.

    Düşük göstermek yüksek göstermekten güvenlidir: kullanıcı bankaya
    gidince beklediğinden iyi bir oranla karşılaşır, tersi değil.
    """
    with SessionLocal() as s:
        s.add(Institution(code="ING", name="ING", kind="bank"))
        s.commit()

    LlmLoanRateCollector(banks={}).persist(
        [
            LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0299),
            LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0169),
            LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0348),
        ],
        run_id=None,
    )
    with SessionLocal() as s:
        rows = s.execute(select(LoanRate)).scalars().all()
    assert len(rows) == 1
    assert float(rows[0].monthly_rate) == pytest.approx(0.0169)


# ------------------------------------------------------- yapılandırma ----

def test_only_active_banks_are_collected():
    """`unverified`/`blocked` bankalar listeye girmemeli.

    İş Bankası WAF arkasında, TEB ve Garanti'nin sayfasında oran yok:
    üçü de kapalı ve toplayıcı onlara hiç istek atmamalı.
    """
    from collectors.loan_rates_llm import _load_banks

    banks = _load_banks()
    assert set(banks) == {"HALKBANK", "QNB", "DENIZBANK", "ING"}
    assert "ISBANK" not in banks


def test_visible_text_strips_scripts_and_tags():
    html = "<html><script>var x='%9,99';</script><p>aylık <b>%2,99</b></p></html>"
    text = _visible_text(html)
    assert "%2,99" in text
    # Script içindeki sayı metne SIZMAMALI: zeminleme kontrolü metin
    # üzerinde çalışıyor ve script'teki bir sayı sahte bir onay üretirdi.
    assert "%9,99" not in text


# ------------------------------------------- herkese açık olmayan oranlar ----
#
# CANLI GÖZLEM (2026-08-30): panel ING'yi aylık %0,99 gösteriyordu —
# DenizBank'ın %2,99'unun üçte biri. Sayı sayfada gerçekten yazıyordu, yani
# zeminleme kusursuz çalışmıştı; %0,99 "Turuncu Ekstra Avantajlı Kredi"nin
# oranıydı ve yalnızca kampanyanın İLK kredi kullanımında geçerliydi.

ING_SAYFA = (
    "İlk kez ING'li olanlara %1,69 6 ay vadede 25.000 TL'ye kadar kredi "
    "fırsatı ya da %2,99'dan başlayan faiz oranlarıyla 36 ay vadeli ihtiyaç "
    "kredisi. Mevcut ING'liler de %3,48'den başlayan faiz oranı ile tüketici "
    "kredisi kullanabilir. Turuncu Ekstra Avantajlı Kredi'de sözleşme faizi "
    "değişmeyecek olup %0,99'dan başlar."
)


def _satir(oran, herkese, why=""):
    return LlmLoanRow(loan_type="personal", monthly_rate_percent=oran,
                      available_to_all=herkese, why=why)


def test_a_rate_that_is_not_open_to_everyone_is_dropped():
    """`persist` en düşüğü saklıyor; eleme olmadan panel en yanıltıcı sayıyı
    gösteriyordu. Kullanıcı ING'yi DenizBank'tan üç kat ucuz sanırdı."""
    from collectors.loan_rates_llm import _ground

    rows = [
        _satir(0.99, False, "yalnızca kampanyanın ilk kredi kullanımında"),
        _satir(1.69, False, "ilk kez ING'li olanlara"),
        _satir(2.99, False, "yeni ING'lilerin kullanımlarında"),
        _satir(3.48, True, "Mevcut ING'liler de kullanabilir"),
    ]
    kept, dropped = _ground(rows, ING_SAYFA, "ING")

    assert [r.monthly_rate_percent for r in kept] == [3.48]
    assert dropped == 3
    # persist en düşüğü alıyor: eleme sonrası doğru cevap %3,48.
    assert min(r.monthly_rate_percent for r in kept) == 3.48


def test_grounding_still_runs_before_the_availability_check():
    """Erişilebilirlik kontrolü zeminlemenin YERİNE geçmiyor, ÜSTÜNE biniyor.

    Uydurulmuş bir oranı model "herkese açık" diye işaretleyerek geçiremez.
    """
    from collectors.loan_rates_llm import _ground

    kept, dropped = _ground([_satir(7.77, True, "uydurma")], ING_SAYFA, "ING")
    assert kept == [] and dropped == 1


def test_a_general_rate_survives_even_when_the_page_is_a_campaign_page():
    """Anahtar kelimeyle elemek İKİ bankayı birden kaybettirirdi.

    Halkbank'ın gerçek gerekçesi "yeni müşterilere özel olduğuna dair kısıt
    YOK" — "yeni müşteri" kelimesini arayan bir filtre onu da elerdi. Karar
    bu yüzden kelimede değil, modelin cevapladığı dar soruda.
    """
    from collectors.loan_rates_llm import _ground

    sayfa = "Avantajlı İhtiyaç Kredisini %4,19 faiz oranından kullanın."
    kept, _ = _ground(
        [_satir(4.19, True, "yalnızca yeni müşterilere özel olduğuna dair kısıt yok")],
        sayfa, "HALKBANK",
    )
    assert [r.monthly_rate_percent for r in kept] == [4.19]
