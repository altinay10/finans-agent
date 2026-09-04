"""Fon sağlayıcı mimarisi — PLAN.md madde 6.

Kullanıcının isteği: "fon simülasyonuna tefastan istediğimiz fon ekleme
özelliği gelsin... garanti bist100 fonu ve bi kaç para piyasası fonu,
değerli madenler fonu gibi çeşitli fonlar ekle."

Asıl engel "hangi site" değildi: fon listesi sources.yaml'a gömülüydü ve tek
bir sağlayıcı okunabiliyordu, yani fon eklemek KOD DEĞİŞİKLİĞİ demekti.
Buradaki testler o bağın bir daha kurulmamasını koruyor.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from collectors.base import ParseError
from collectors.fund_prices import (
    FundPriceCollector,
    add_fund_to_registry,
    load_fund_registry,
)
from collectors.fund_providers import (
    PROVIDERS,
    FundSpec,
    GarantiPortfoyProvider,
    epoch_ms_to_istanbul_date,
)


# ----------------------------------------------------------- kayıt defteri --

def test_shipped_registry_covers_the_requested_categories():
    """Kullanıcının saydığı kategoriler kayıt defterinde olmalı."""
    specs = {s.code: s for s in load_fund_registry()}
    assert "GAE" in specs        # BIST 30 endeks fonu
    # Para piyasası fonu. Kod 2026-09-04'te GPB'den GTL'ye DÜZELTİLDİ:
    # kayıt `birinci-para-piyasasi-fonu` sayfasını gösteriyordu ve o
    # sayfanın gerçek fon kodu GTL'dir (GPB = SMART Temkinli Değişken Fon).
    assert "GTL" in specs        # para piyasası
    assert "GTA" in specs        # altın
    assert "GTZ" in specs        # gümüş
    # Ak Portföy fonları korunmuş olmalı — sağlayıcı geçişi veri kaybettirmesin.
    assert {"AK3", "ADP", "AFA", "AFO"} <= set(specs)


def test_every_registry_entry_names_a_known_provider():
    for spec in load_fund_registry():
        assert spec.provider in PROVIDERS, f"{spec.code}: {spec.provider}"


def test_unknown_provider_is_skipped_not_fatal(tmp_path: Path):
    """Panelden elle eklenen bir satırdaki yazım hatası toplamayı düşürmemeli."""
    path = tmp_path / "funds.yaml"
    path.write_text(
        "funds:\n"
        "  - {code: AK3, provider: akportfoy, ref: AK3}\n"
        "  - {code: XXX, provider: yanlisportfoy, ref: xxx}\n",
        encoding="utf-8",
    )
    specs = load_fund_registry(path)
    assert [s.code for s in specs] == ["AK3"]


def test_missing_registry_file_returns_empty_not_crash(tmp_path: Path):
    assert load_fund_registry(tmp_path / "yok.yaml") == []


def test_ref_defaults_to_the_code_when_omitted(tmp_path: Path):
    path = tmp_path / "funds.yaml"
    path.write_text("funds:\n  - {code: AK3, provider: akportfoy}\n", encoding="utf-8")
    assert load_fund_registry(path)[0].ref == "AK3"


def test_registry_does_not_carry_is_equity_heavy():
    """Stopaj kararı elle girilen bir bayrağa BIRAKILMAMALI.

    Resmî işaret fon adındaki "(Hisse Senedi Yoğun Fon)" ibaresidir ve
    sağlayıcı onu sayfadan okur. YAML'da bir `is_equity_heavy: true`
    satırının yanlışlıkla doğru sanılması, vergiyi doğrudan sıfırlardı.
    """
    for spec in load_fund_registry():
        assert spec.is_equity_heavy is None


# --------------------------------------------------------- panelden ekleme --

def test_add_fund_appends_a_row_to_the_registry(tmp_path: Path):
    path = tmp_path / "funds.yaml"
    path.write_text("funds:\n  - {code: AK3, provider: akportfoy, ref: AK3}\n", encoding="utf-8")

    add_fund_to_registry(
        code="gta", provider="garantiportfoy", ref="altin-fonu",
        name="Altın Fonu", path=path,
    )
    specs = {s.code: s for s in load_fund_registry(path)}
    assert "GTA" in specs                      # kod büyük harfe normalize
    assert specs["GTA"].provider == "garantiportfoy"
    assert specs["GTA"].ref == "altin-fonu"
    assert len(specs) == 2                     # mevcut satır korundu


def test_add_fund_rejects_a_duplicate_code(tmp_path: Path):
    path = tmp_path / "funds.yaml"
    path.write_text("funds:\n  - {code: AK3, provider: akportfoy, ref: AK3}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="zaten kayıtlı"):
        add_fund_to_registry(code="ak3", provider="akportfoy", ref="AK3", path=path)


def test_add_fund_creates_the_file_when_missing(tmp_path: Path):
    path = tmp_path / "funds.yaml"
    add_fund_to_registry(code="GTA", provider="garantiportfoy", ref="altin-fonu", path=path)
    assert [s.code for s in load_fund_registry(path)] == ["GTA"]


def test_added_fund_survives_a_yaml_round_trip_with_turkish_characters(tmp_path: Path):
    """allow_unicode olmadan 'Altın' -> '\\u0131' kaçışına dönerdi."""
    path = tmp_path / "funds.yaml"
    add_fund_to_registry(
        code="GTZ", provider="garantiportfoy", ref="gumus-fon-sepeti-fonu",
        name="Gümüş Fon Sepeti Fonu", path=path,
    )
    assert "Gümüş" in path.read_text(encoding="utf-8")


# ---------------------------------------------------- Garanti sağlayıcısı --

def _garanti_payload(points, name="Altın Fonu"):
    return json.dumps(
        {"code": "GTA", "page_title": name, "series": [{"Id": 1, "Name": "GTA", "Data": points}]}
    )


def test_garanti_parses_the_live_series_shape():
    """Canlı yanıt: [[epoch_ms, 1000 TL'nin değeri, yüzde değişim], ...]"""
    spec = FundSpec(code="GTA", provider="garantiportfoy", ref="altin-fonu")
    series = GarantiPortfoyProvider().parse(
        spec,
        _garanti_payload([[1754013600000, 1000.0, 0.0], [1754272800000, 1009.978242, 0.997824]]),
    )
    assert series.code == "GTA"
    assert series.name == "Altın Fonu"
    assert len(series.points) == 2
    assert series.points[0][1] == pytest.approx(1000.0)
    assert series.points[1][1] == pytest.approx(1009.978242)


def test_garanti_reads_equity_heavy_from_the_official_fund_name():
    spec = FundSpec(code="GAE", provider="garantiportfoy", ref="bist-30-endeksi")
    series = GarantiPortfoyProvider().parse(
        spec,
        _garanti_payload(
            [[1754013600000, 1000.0, 0.0]],
            name="BIST 30 Endeksi Hisse Senedi (TL) Fonu (Hisse Senedi Yoğun Fon)",
        ),
    )
    assert series.is_equity_heavy is True


def test_garanti_empty_series_raises_instead_of_writing_nothing_silently():
    spec = FundSpec(code="GTA", provider="garantiportfoy", ref="altin-fonu")
    with pytest.raises(ParseError, match="serisi boş"):
        GarantiPortfoyProvider().parse(spec, _garanti_payload([]))


def test_garanti_series_is_flagged_as_an_index_not_a_unit_price():
    """Panel etiketini buna göre seçiyor.

    Garanti uç noktası birim pay fiyatı vermiyor; "1.000 TL yatırılsaydı"
    endeksi veriyor. Getiri hesabı yalnızca oranı kullandığı için doğru,
    ama sayıyı "fiyat" diye göstermek yanıltıcı olurdu.
    """
    assert PROVIDERS["garantiportfoy"].price_is_unit_value is False
    assert PROVIDERS["akportfoy"].price_is_unit_value is True


def test_epoch_conversion_uses_istanbul_time():
    """REGRESYON: UTC olarak okunursa tüm seri bir gün kayar."""
    assert epoch_ms_to_istanbul_date(1514840400000) == date(2018, 1, 2)


# ------------------------------------------------- çok sağlayıcılı koşu ----

def _ak_page(code: str, name: str, points: list[tuple[int, float]]) -> str:
    series = json.dumps([{"Close": p, "Date": d} for d, p in points])
    return (
        f"<html><head><title>{code} - {name} | Ak Portföy</title></head><body>"
        f'<script>var fundVals = {{"{code}":{series}}};</script></body></html>'
    )


def test_one_run_can_mix_providers(db):
    """Ak Portföy ve Garanti fonları AYNI koşuda toplanabilmeli."""
    payload = {
        "AK3": {
            "ok": True, "provider": "akportfoy", "ref": "AK3",
            "body": _ak_page("AK3", "Ak Portföy Hisse Senedi (TL) Fonu",
                             [(1754013600000, 10.0), (1754272800000, 10.2)]),
        },
        "GTA": {
            "ok": True, "provider": "garantiportfoy", "ref": "altin-fonu",
            "body": _garanti_payload([[1754013600000, 1000.0, 0.0]]),
        },
    }
    collector = FundPriceCollector(
        specs=[
            FundSpec(code="AK3", provider="akportfoy", ref="AK3"),
            FundSpec(code="GTA", provider="garantiportfoy", ref="altin-fonu"),
        ]
    )
    records = collector.parse(json.dumps(payload).encode("utf-8"))
    assert {r.fund_code for r in records} == {"AK3", "GTA"}


def test_provider_summary_row_is_written_so_the_sources_tab_can_match(db):
    """Kaynaklar sekmesi fon SAĞLAYICISINI koşularla eşleştirebilmeli.

    Fon başına kayıt "hangi fon düştü" sorusunu cevaplar ama
    sources.yaml'daki `fund_endpoints.akportfoy` satırıyla eşleşmez;
    o satır sağlayıcı adına ihtiyaç duyar.
    """
    from sqlalchemy import select

    from store.db import SessionLocal
    from store.models import SourceRun

    collector = FundPriceCollector(
        specs=[FundSpec(code="AK3", provider="akportfoy", ref="AK3")]
    )
    collector._record_provider_summary({"AK3": {"ok": True}})

    with SessionLocal() as session:
        rows = session.execute(select(SourceRun)).scalars().all()
    assert [(r.source, r.status) for r in rows] == [("akportfoy", "ok")]


def test_provider_summary_reports_partial_failure(db):
    from sqlalchemy import select

    from store.db import SessionLocal
    from store.models import SourceRun

    collector = FundPriceCollector(
        specs=[
            FundSpec(code="GTA", provider="garantiportfoy", ref="altin-fonu"),
            FundSpec(code="GTZ", provider="garantiportfoy", ref="gumus-fon-sepeti-fonu"),
        ]
    )
    collector._record_provider_summary({"GTA": {"ok": True}, "GTZ": {"ok": False}})

    with SessionLocal() as session:
        row = session.execute(select(SourceRun)).scalars().one()
    assert row.status == "ok"          # sağlayıcı ayakta
    assert "1/2 fon çekilemedi" in row.error


def test_add_fund_preserves_the_file_header_comments(tmp_path: Path):
    """yaml.safe_dump yorumları atar; başlık elle korunmalı.

    İlk panelden ekleme, alanların ne anlama geldiğini ve `is_equity_heavy`in
    neden kayıt defterinde tutulmadığını anlatan açıklamaları sessizce
    silmişti. Bu bilgi kaybı, dosyayı sonradan elle düzenleyecek olan için
    doğrudan hata kaynağı.
    """
    path = tmp_path / "funds.yaml"
    path.write_text(
        "# Fon kayıt defteri.\n"
        "# is_equity_heavy BURADA TUTULMAZ — sağlayıcı sayfadan okur.\n\n"
        "funds:\n  - {code: AK3, provider: akportfoy, ref: AK3}\n",
        encoding="utf-8",
    )
    add_fund_to_registry(code="GTA", provider="garantiportfoy", ref="altin-fonu", path=path)

    text = path.read_text(encoding="utf-8")
    assert "is_equity_heavy BURADA TUTULMAZ" in text
    assert {s.code for s in load_fund_registry(path)} == {"AK3", "GTA"}


def test_seeding_survives_a_fund_added_without_a_name(db, tmp_path: Path, monkeypatch):
    """Panelden ad girmeden fon eklenebiliyor; açılış bunu kaldırmalı.

    REGRESYON: `seed_reference_data` `row["name"]` diyordu ve adsız bir
    satır uygulamayı AÇILIŞTA KeyError ile düşürüyordu — yani panelden fon
    eklemek, bir sonraki başlatmada tüm paneli karartıyordu. Canlı akış
    testinde yakalandı (2026-08-25).
    """
    import store.db as db_module
    from store.db import SessionLocal, seed_reference_data
    from store.models import Fund

    path = tmp_path / "funds.yaml"
    path.write_text(
        "funds:\n  - {code: TGT, provider: garantiportfoy, ref: kisa-vadeli-fon}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db_module, "FUNDS_YAML", path)

    seed_reference_data()      # patlamamalı

    with SessionLocal() as session:
        fund = session.get(Fund, "TGT")
    assert fund is not None
    assert fund.name == "TGT"       # yer tutucu; gerçek adı toplayıcı yazar


# ------------------------------------------- fon koduyla çözümleme (URL'siz) --

GARANTI_ANASAYFA = """
<ul>
  <li><a href="/ucuncu-para-piyasasi-fonu"><span class="d-none">GPZ</span><span>Üçüncü Para Piyasası Fonu</span></a></li>
  <li><a href="/smart-buyume-degisken-fon-ucuncu-degisken-fon"><span class="d-none">GPU</span><span>SMART B&uuml;y&uuml;me De&#287;i&#351;ken Fon</span></a></li>
  <li><a href="/birinci-para-piyasasi-fonu"><span class="d-none">GTL</span><span>Birinci Para Piyasası (TL) Fonu</span></a></li>
</ul>
"""


def test_garanti_index_maps_code_to_slug_and_name(monkeypatch):
    """Kullanıcı isteği: "sadece fon koduyla url olmadan".

    Ana sayfadaki gizli dizin (`<span class="d-none">KOD</span>`) tek
    istekle kod -> slug -> ad veriyor; fon sayfalarını tek tek gezmek 70+
    istek ederdi.
    """
    from collectors import http
    from collectors.fund_providers import GarantiPortfoyProvider

    class _Resp:
        text = GARANTI_ANASAYFA
        status_code = 200
        def raise_for_status(self): pass

    monkeypatch.setattr(http, "get", lambda *a, **k: _Resp())
    index = GarantiPortfoyProvider().fund_index()

    assert index["GPU"].ref == "smart-buyume-degisken-fon-ucuncu-degisken-fon"
    assert index["GTL"].ref == "birinci-para-piyasasi-fonu"
    # HTML varlıkları çözülmeli, yoksa ad "B&uuml;y&uuml;me" diye kaydedilir.
    assert "Büyüme" in index["GPU"].name


def test_resolve_fund_returns_none_for_an_unknown_code(monkeypatch):
    """Tanınmayan kod SESSİZCE bir sağlayıcıya yazılmamalı.

    Yanlış sağlayıcıya yazmak, her koşuda düşen ve kimsenin bakmadığı bir
    kayıt üretirdi; hiç yazmamak ve kullanıcıya söylemek doğrusu.
    """
    from collectors import http
    from collectors.fund_prices import resolve_fund

    class _Resp:
        text = GARANTI_ANASAYFA
        status_code = 404
        def raise_for_status(self): pass

    monkeypatch.setattr(http, "get", lambda *a, **k: _Resp())
    assert resolve_fund("ZZZ") is None


def test_garanti_refuses_to_store_another_funds_series(monkeypatch):
    """SESSİZ VERİ BOZULMASI REGRESYONU (canlıda yakalandı, 2026-09-04).

    Kayıt defterinde GPB kodu `birinci-para-piyasasi-fonu` sayfasını
    gösteriyordu; o sayfanın gerçek kodu GTL. Sağlayıcı `sayfadaki_kod or
    fund.code` yazdığı için seriyi GTL olarak çekip GPB adına saklıyordu.
    Panelde GPB seçen kullanıcı 530 günlük BAŞKA BİR FONUN getirisini
    görüyordu ve hiçbir yerde hata çıkmıyordu.
    """
    from collectors import http
    from collectors.fund_providers import GarantiPortfoyProvider

    class _Page:
        text = "window.fundCode = 'GTL'\n<h1>Birinci Para Piyasası</h1>"
        status_code = 200
        def raise_for_status(self): pass

    monkeypatch.setattr(http, "get", lambda *a, **k: _Page())

    with pytest.raises(ParseError) as hata:
        GarantiPortfoyProvider().fetch(
            FundSpec(code="GPB", provider="garantiportfoy", ref="birinci-para-piyasasi-fonu")
        )
    # Hata kullanıcıya HANGİ fonun geldiğini söylemeli.
    assert "GTL" in str(hata.value)


def test_registry_write_goes_to_the_data_dir_and_keeps_existing_funds(tmp_path, monkeypatch):
    """KAYBOLAN FON REGRESYONU (2026-09-04).

    Panelden eklenen fon `<repo>/config/funds.yaml`a yazılıyordu; konteynerde
    o dosya İMAJ KATMANINDA ve volume yalnızca /app/data'ya bağlı. Yani her
    `docker compose up -d` kullanıcının eklediği fonu siliyordu.

    Veri dizinine geçerken mevcut kayıt defteri oraya KOPYALANMALI; yoksa
    dosya yalnızca yeni fonu içerir ve 11 fon bir anda kaybolur.
    """
    from collectors import fund_prices

    veri = tmp_path / "data"
    veri.mkdir()
    monkeypatch.setenv("DATA_DIR", str(veri))
    monkeypatch.delenv("FUNDS_YAML_PATH", raising=False)

    hedef = fund_prices._writable_funds_yaml()
    assert hedef == veri / "funds.yaml"
    # Depodaki fonlar taşınmış olmalı.
    tasinan = {s.code for s in fund_prices.load_fund_registry(hedef)}
    assert {"AK3", "GTL", "GTA"} <= tasinan

    fund_prices.add_fund_to_registry(code="GPU", provider="garantiportfoy", ref="smart-x")
    sonra = {s.code for s in fund_prices.load_fund_registry(hedef)}
    assert "GPU" in sonra and {"AK3", "GTL", "GTA"} <= sonra
