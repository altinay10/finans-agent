"""Yapı Kredi Portföy sağlayıcısı — sitemap + sayfadaki sayısal kimlik.

Kullanıcı isteği (2026-09-05): "denizbank veya yapı kredinin api sistemi
var, bu api ile fon simülasyonunu geliştir."

Buradaki testler üç şeyi koruyor:
  1. 30 GÜNDEN UZUN ARALIK SERİYİ AYLIĞA SEYRELTİR (ölçüldü 2026-09-05:
     28 gün -> 20 nokta, 31 gün -> 2 nokta). Pencere düzeni bozulursa
     panel günlük çözünürlüğü sessizce kaybeder.
  2. Sayfanın fon kodu kayıt defterindekinden farklıysa seri YAZILMAMALI —
     Garanti Portföy'de canlıda yaşanan sessiz veri bozulmasının aynısı.
  3. Tarihler epoch değil TÜRKÇE AY ADIYLA metin geliyor; locale'e
     güvenmek sunucuda sessizce boş seri üretirdi.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from collectors.base import ParseError
from collectors.fund_providers import FundSpec
from collectors.fund_providers.ykportfoy import (
    WINDOW_DAYS,
    YapiKrediPortfoyProvider,
    _parse_turkish_date,
)

SITEMAP = """<?xml version="1.0" encoding="utf-8"?>
<urlset>
  <loc>https://www.yapikrediportfoy.com.tr/urun-ve-hizmetlerimiz/yatirim-fonlari/hisse-senedi-stratejisi/yub</loc>
  <loc>https://www.yapikrediportfoy.com.tr/urun-ve-hizmetlerimiz/yatirim-fonlari/para-piyasasi-stratejisi/ypt</loc>
  <loc>https://www.yapikrediportfoy.com.tr/urun-ve-hizmetlerimiz/yatirim-fonlari/ozel-fonlar/yzm</loc>
  <loc>https://www.yapikrediportfoy.com.tr/urun-ve-hizmetlerimiz/yatirim-fonlari/tasfiye-edilen-fonlar/yjo</loc>
  <loc>https://www.yapikrediportfoy.com.tr/hakkimizda/kurumsal-bilgilerimiz/yapi-kredi-portfoy-hakkinda</loc>
</urlset>
"""

SAYFA = '<div data-ajax-fund-detail="/getFundDetail/1573"></div>'


def _detay(code="YUB", ad=None, etiketler=None, veriler=None):
    """Canlı yanıtın (2026-09-05) biçimi."""
    return {
        "data": [{
            "code": code,
            "name": ad or "Yapı Kredi Portföy Karaköy Hisse Senedi Serbest Fon (Hisse Senedi Yoğun Fon)",
            "lineChartData": {
                "graphData": (
                    {"labels": etiketler, "datasets": [{"label": code, "data": veriler}]}
                    if etiketler else None
                ),
                "dataSource": "Yapı Kredi Bankası",
            },
        }]
    }


class _Resp:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


# ---------------------------------------------------------------- dizin --

def test_sitemap_maps_code_to_path_and_skips_non_fund_pages(monkeypatch):
    from collectors import http

    monkeypatch.setattr(http, "get", lambda *a, **k: _Resp(text=SITEMAP))
    dizin = YapiKrediPortfoyProvider().directory()

    assert set(dizin) == {"YUB", "YPT", "YZM", "YJO"}
    assert dizin["YUB"].ref.endswith("/hisse-senedi-stratejisi/yub")
    assert "hakkimizda" not in " ".join(k.ref for k in dizin.values())


def test_catalog_leaves_out_locked_and_liquidated_funds(monkeypatch):
    """Özel fonlar şifre ister (`isLocked`), tasfiye edilenler seri vermez.

    Katalogda bırakılsalardı "hepsini ekle" akışı her gece düşen onlarca
    kayıt üretirdi ve Kaynaklar sekmesi gerçek arızaların içinde kaybolurdu.
    """
    from collectors import http

    monkeypatch.setattr(http, "get", lambda *a, **k: _Resp(text=SITEMAP))
    kodlar = {k.code for k in YapiKrediPortfoyProvider().catalog()}
    assert kodlar == {"YUB", "YPT"}


# ------------------------------------------------------- tarih ayrıştırma --

def test_turkish_month_names_are_parsed_without_a_locale():
    """Sunucuda Türkçe locale kurulu olmayabilir; strptime'a güvenilemez."""
    assert _parse_turkish_date("06 Eylül 2024") == date(2024, 9, 6)
    assert _parse_turkish_date("04 Ağustos 2026") == date(2026, 8, 4)
    assert _parse_turkish_date("01 Ocak 2025") == date(2025, 1, 1)
    assert _parse_turkish_date("06 September 2024") is None
    assert _parse_turkish_date("") is None
    assert _parse_turkish_date("32 Eylül 2024") is None


# ------------------------------------------------------------- pencere ----

def test_recent_window_is_short_enough_to_stay_daily():
    """30 GÜNÜ GEÇEN aralıkta uç nokta seriyi aylığa seyreltiyor."""
    assert WINDOW_DAYS <= 30


def test_windows_always_refresh_the_recent_period(monkeypatch):
    bugun = date(2026, 9, 4)
    tam = frozenset(bugun - timedelta(days=k) for k in range(0, 800))
    pencereler = YapiKrediPortfoyProvider()._windows(bugun, tam)

    assert len(pencereler) == 1, "geçmiş elimizdeyken yalnızca güncel pencere istenmeli"
    bas, bit = pencereler[0]
    assert (bit - bas).days <= 30


def test_windows_cover_the_scenario_anchors_when_history_is_missing():
    """"6 ay" ve "1 yıl" senaryolarının başlangıcı aylık omurgaya yuvarlanmasın."""
    bugun = date(2026, 9, 4)
    pencereler = YapiKrediPortfoyProvider()._windows(bugun, frozenset())

    def kapsiyor(hedef: date) -> bool:
        return any(b <= hedef <= s for b, s in pencereler)

    assert kapsiyor(bugun - timedelta(days=182))
    assert kapsiyor(bugun - timedelta(days=365))
    # Aylık omurga da istenmiş olmalı (grafiğin iskeleti).
    assert any((s - b).days > 300 for b, s in pencereler)


# --------------------------------------------------------------- çekim ----

def test_fetch_reads_the_numeric_id_from_the_page(monkeypatch):
    """Sayısal kimlik yalnızca sayfada; koddan türetilemiyor."""
    from collectors import http

    adresler: list[str] = []
    monkeypatch.setattr(http, "get", lambda *a, **k: _Resp(text=SAYFA))
    monkeypatch.setattr("collectors.fund_providers.ykportfoy.time.sleep", lambda *_: None)

    def _post(url, **kwargs):
        adresler.append(url)
        return _Resp(payload=_detay(etiketler=["03 Eylül 2026"], veriler=[5.38]))

    monkeypatch.setattr(http, "post", _post)
    YapiKrediPortfoyProvider().fetch(
        FundSpec(code="YUB", provider="ykportfoy", ref="/x/yub")
    )
    assert all(u.endswith("/getFundDetail/1573") for u in adresler)


def test_missing_detail_id_raises_instead_of_returning_nothing(monkeypatch):
    from collectors import http

    monkeypatch.setattr(http, "get", lambda *a, **k: _Resp(text="<html>boş</html>"))
    with pytest.raises(ParseError) as hata:
        YapiKrediPortfoyProvider().fetch(
            FundSpec(code="YUB", provider="ykportfoy", ref="/x/yub")
        )
    assert "data-ajax-fund-detail" in str(hata.value)


# ---------------------------------------------------------- ayrıştırma ----

def test_parse_merges_windows_and_drops_duplicate_days():
    ham = json.dumps({"code": "YUB", "parts": [
        _detay(etiketler=["03 Eylül 2026", "04 Eylül 2026"], veriler=[5.12, 5.38]),
        _detay(etiketler=["04 Eylül 2026", "06 Ağustos 2026"], veriler=[5.38, 4.44]),
    ]})
    seri = YapiKrediPortfoyProvider().parse(
        FundSpec(code="YUB", provider="ykportfoy", ref="/x/yub"), ham
    )
    assert seri.points == [
        (date(2026, 8, 6), 4.44), (date(2026, 9, 3), 5.12), (date(2026, 9, 4), 5.38),
    ]
    assert seri.is_equity_heavy is True


def test_parse_refuses_to_store_another_funds_series():
    """SESSİZ VERİ BOZULMASI KORUMASI.

    Kayıt defterindeki adres başka bir fonun sayfasını gösterirse seri o
    fonundur ama bizim kodumuzla saklanırdı. Garanti Portföy'de canlıda
    tam olarak bu oldu (2026-09-04) ve hiçbir yerde hata çıkmadı.
    """
    ham = json.dumps({"code": "YUB", "parts": [
        _detay(code="YPT", etiketler=["04 Eylül 2026"], veriler=[1.23]),
    ]})
    with pytest.raises(ParseError) as hata:
        YapiKrediPortfoyProvider().parse(
            FundSpec(code="YUB", provider="ykportfoy", ref="/x/yub"), ham
        )
    assert "YPT" in str(hata.value)


def test_empty_graph_data_raises_instead_of_a_silent_empty_series():
    """200 dönmesi seri geldiği anlamına GELMİYOR — boş gövde de 200 alır."""
    ham = json.dumps({"code": "YUB", "parts": [_detay(etiketler=None, veriler=None)]})
    with pytest.raises(ParseError) as hata:
        YapiKrediPortfoyProvider().parse(
            FundSpec(code="YUB", provider="ykportfoy", ref="/x/yub"), ham
        )
    assert "boş" in str(hata.value)


def test_prices_are_unit_share_values_not_an_index():
    assert YapiKrediPortfoyProvider().price_is_unit_value is True


def test_two_daily_windows_cover_the_one_month_scenario():
    """CANLI ÖLÇÜM REGRESYONU (2026-09-05).

    Tek günlük pencere -28 günde bitiyordu, panelin "1 ay" senaryosu ise
    -30 gününü hedefliyor. İki günlük boşluk yüzünden
    `nearest_prior_price` aylık omurgaya düşüyor ve senaryo 59 GÜNLÜK bir
    dönemle hesaplanıyordu (YUB'da başlangıç 2026-08-05 yerine
    2026-07-07). Senaryonun adı ile hesabı tutmuyordu.
    """
    bugun = date(2026, 9, 4)
    pencereler = YapiKrediPortfoyProvider()._windows(bugun, frozenset())
    bir_ay = bugun - timedelta(days=30)
    assert any(b <= bir_ay <= s for b, s in pencereler), "1 ay çapası kapsanmalı"
    # Kapsayan pencere GÜNLÜK olmalı — 30 günü geçen aralık aylığa seyreltilir.
    kapsayan = [(b, s) for b, s in pencereler if b <= bir_ay <= s]
    assert any((s - b).days <= 30 for b, s in kapsayan)
