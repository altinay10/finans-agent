"""Kaynak şeffaflığı — PLAN.md madde 1.

Kullanıcının sorusu: "hangi verileri nerelerden çekiyorsun, buna açıklık
getirelim." Buradaki testler envanterin PANELDE GÖSTERİLEBİLİR ve GERÇEKLE
UYUMLU olmasını koruyor. En kritik olanı sonuncusu: envanterin iddiası ile
çalışan toplayıcıların gerçeği çeliştiğinde panel bunu söyleyebilmeli —
Enpara'nın "kapalı" yazılıp aslında çalışıyor olması bu boşluk yüzünden
aylarca fark edilmemişti.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config.loader import (
    DATASET_LABELS,
    endpoint_for,
    source_inventory,
    source_summary,
)


# ------------------------------------------------------------ envanter ----

def test_inventory_covers_every_dataset_the_panel_renders():
    """Panel DATASET_LABELS üzerinde dönüyor; hepsinin karşılığı olmalı.

    Bir veri kümesi etiketi tanımlanıp sources.yaml'da bölümü yoksa panel
    o başlığı sessizce atlar ve kullanıcı o verinin kaynağını hiç göremez.
    """
    datasets = {row["dataset"] for row in source_inventory()}
    assert datasets == set(DATASET_LABELS)


def test_profit_share_sources_are_in_the_inventory():
    """Regresyon: kâr payı toplayıcısı vardı ama envanterde kaynağı yoktu.

    Panel "bu %93 nereden geliyor" sorusunu bu yüzden cevaplayamıyordu.
    """
    rows = [r for r in source_inventory() if r["dataset"] == "profit_share_endpoints"]
    institutions = {r["institution"] for r in rows}
    assert institutions == {"EMLAKKATILIM", "KUVEYTTURK"}
    assert all(r["url"] for r in rows)


def test_fund_sources_are_in_the_inventory_including_the_blocked_one():
    rows = {r["key"]: r for r in source_inventory() if r["dataset"] == "fund_endpoints"}
    assert rows["akportfoy"]["status"] == "active"
    assert rows["tefas"]["status"] == "blocked"
    # TEFAS'ın kapalı olma sebebi robots.txt DEĞİL, bot tespiti. Bu ayrımın
    # gerekçe metninde durması önemli: biri kullanıcı kararıyla aşılabilir,
    # diğeri aşılmıyor. (Türkçe büyük İ'nin .lower() dönüşümü birleşik nokta
    # ürettiği için metin küçültülmeden aranıyor.)
    reason = rows["tefas"]["blocked_reason"]
    assert "bot challenge" in reason
    assert "KAPSAMIYOR" in reason


def test_fund_providers_are_not_treated_as_institutions():
    """Ak Portföy bir banka değil; institutions tablosunda karşılığı yok.

    `institution` alanına 'AKPORTFOY' yazılsaydı panel etiket ararken
    ham kodu basardı.
    """
    rows = {r["key"]: r for r in source_inventory() if r["dataset"] == "fund_endpoints"}
    assert rows["akportfoy"]["institution"] is None


def test_institution_code_is_derived_from_the_endpoint_key():
    """Her uç noktaya elle `institution:` yazmak gereksiz tekrar olurdu."""
    row = endpoint_for("fx_endpoints", "VAKIFBANK")
    assert row is not None
    assert row["key"] == "vakifbank"


def test_every_source_declares_a_status_and_blocked_ones_explain_why():
    for row in source_inventory():
        assert row["status"] in {"active", "blocked", "unverified"}, row["key"]
        if row["status"] == "blocked":
            assert row["blocked_reason"], f"{row['dataset']}/{row['key']}: gerekçe yok"


def test_robots_override_flag_is_explicit_and_enumerable():
    """robots kararı geri alınırsa kapatılacak liste tam olarak çıkarılabilmeli."""
    overridden = {
        (r["dataset"], r["key"]) for r in source_inventory() if r["robots_override"]
    }
    assert ("fx_endpoints", "akbank") in overridden
    assert ("deposit_endpoints", "akbank") in overridden
    # WAF/bot-tespiti olan kaynaklar bu bayrağı ASLA taşımamalı: onlar
    # kullanıcının robots kararının kapsamında değil.
    for dataset, key in overridden:
        row = next(
            r for r in source_inventory() if r["dataset"] == dataset and r["key"] == key
        )
        assert row["status"] == "active"


# --------------------------------------------------- tablo altı künyesi ----

def test_source_summary_is_short_and_names_the_endpoint():
    summary = source_summary("deposit_endpoints", "TEB")
    assert summary is not None
    assert "VadeliHesapFaizOranList" in summary
    assert "https://" not in summary          # şema kırpılıyor, satır kısa kalsın
    assert "son doğrulama" in summary


def test_source_summary_flags_the_robots_decision_where_it_applies():
    assert "robots" in source_summary("fx_endpoints", "AKBANK")
    assert "robots" not in source_summary("fx_endpoints", "VAKIFBANK")


def test_source_summary_is_none_when_there_is_no_endpoint():
    """Kaynağı olmayan bir kurum için uydurma künye basılmamalı."""
    assert source_summary("fx_endpoints", "BILINMEYEN") is None


# ----------------------------------- envanter iddiası vs çalışan gerçek ----

def _row(dataset="loan_endpoints", key="enpara", status="active"):
    return {
        "dataset": dataset,
        "key": key,
        "status": status,
        "institution": key.upper(),
        "url": "https://example.invalid/x",
        "type": "html",
        "robots_override": False,
        "last_verified": None,
        "blocked_reason": None,
        "note": None,
    }


def test_panel_flags_a_source_that_claims_active_but_never_succeeded():
    """Envanter 'çekiliyor' diyor ama 30 günde tek başarılı koşu yok."""
    from app.panels.sources import _live_state

    last_ok, disagreement = _live_state(_row(status="active"), health=[])
    assert last_ok == "kayıt yok"
    assert "başarılı koşu yok" in disagreement


def test_panel_flags_a_source_that_claims_blocked_but_is_delivering_data():
    """Enpara vakası: kapalı yazılı ama veri geliyor.

    Bu çelişkiyi görünür kılmak, kaynak envanterinin eskimesini yakalayan
    tek mekanizma.
    """
    from app.panels.sources import _live_state

    health = [
        {
            "collector": "loan_rates",
            "source": "enpara",
            "last_ok": datetime.now(timezone.utc) - timedelta(hours=2),
        }
    ]
    last_ok, disagreement = _live_state(_row(status="blocked"), health)
    assert "envanter kapalı diyor ama veri geliyor" in disagreement
    assert "sa önce" in last_ok


def test_healthy_active_source_produces_no_disagreement_noise():
    from app.panels.sources import _live_state

    health = [
        {
            "collector": "loan_rates",
            "source": "enpara",
            "last_ok": datetime.now(timezone.utc) - timedelta(hours=1),
        }
    ]
    _, disagreement = _live_state(_row(status="active"), health)
    assert disagreement == ""


def test_health_rows_from_another_collector_do_not_count():
    """Aynı banka birden çok toplayıcıda var; kaynak eşleşmesi kümeye bağlı.

    'akbank' hem fx_banks hem deposit_rates hem loan_rates altında koşuyor.
    Kredi uç noktası düşmüşken döviz koşusunun başarısını 'iyi' saymak,
    bozulmayı gizlerdi.
    """
    from app.panels.sources import _live_state

    health = [
        {
            "collector": "fx_banks",              # kredi değil
            "source": "enpara",
            "last_ok": datetime.now(timezone.utc),
        }
    ]
    last_ok, disagreement = _live_state(_row(status="active"), health)
    assert last_ok == "kayıt yok"
    assert disagreement != ""


def test_every_dataset_has_a_collector_mapping():
    """Eşleme eksikse o veri kümesinin canlı sağlığı hiç gösterilemez."""
    from app.panels.sources import DATASET_COLLECTORS

    assert set(DATASET_COLLECTORS) == set(DATASET_LABELS)
    from worker import COLLECTORS

    known = {COLLECTORS[key]().name for key in COLLECTORS}
    for dataset, collectors in DATASET_COLLECTORS.items():
        for name in collectors:
            assert name in known, f"{dataset}: '{name}' diye bir toplayıcı yok"


# ---------------------------------------------------------------- caveat ----


def test_source_caveat_surfaces_measurement_basis_warning():
    """Aynı tabloda farklı ÖLÇÜM TABANI varsa panel bunu söylemeli.

    CepteTEB'in herkese açık ucu nakit/efektif kuru veriyor (makas %9);
    paneldeki diğer bankalar döviz HESABI kuru yayınlıyor (%2 dolayında).
    Sayı yanlış değil, kıyas yanlış olur — bu yüzden uyarı kullanıcıya
    gösterilen bir alanda (`caveat`) tutuluyor, geliştirici notunda değil.
    """
    from config.loader import clear_cache, source_caveat

    clear_cache()
    caveat = source_caveat("fx_endpoints", "TEB")
    assert caveat, "TEB kur satırının ölçüm tabanı uyarısı kaybolmuş"
    # str.lower() ile aramak TÜRKÇE'DE TUZAK: "NAKİT".lower() Python'da
    # "naki\u0307t" üretir (İ -> i + birleşen nokta), yani "nakit" ile
    # eşleşmez. Metin zaten küçük harfli parçalarla sınanıyor.
    assert "hesabı kuru değil" in caveat
    assert "makas" in caveat
    # Uyarı yalnızca gerektiği yerde; her kaynağa yapıştırılmamalı.
    assert source_caveat("fx_endpoints", "AKBANK") is None


def test_source_caveat_is_none_for_unknown_institution():
    from config.loader import clear_cache, source_caveat

    clear_cache()
    assert source_caveat("fx_endpoints", "OLMAYAN_BANKA") is None
