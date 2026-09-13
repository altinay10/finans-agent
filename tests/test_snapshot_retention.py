"""Snapshot budaması — `store/retention.purge_snapshots`.

NE TUTULUYOR: her başarılı `fetch`ten sonra, ayrıştırmadan ÖNCE, sunucunun
döndürdüğü ham gövde (`collectors/base._write_snapshot`). Banka
sayfalarının HTML'i, TCMB'nin XML'i, API'lerin JSON'u. Tek amacı bir
ayrıştırıcı kırıldığında "o an sayfa ne diyordu" sorusuna bakabilmek;
hiçbir kod bunları okumuyor.

NEDEN KISA PENCERE: saklama süresinin değeri, bir bozulmayı fark etmek
için geçen süreye eşit. Panel bozulmayı hızlı gösteriyor, yani iki
haftalık pencere pratikte hiç kullanılmıyordu — ama ham banka sayfaları
oran tablolarından kat kat büyük olduğu için diskte birikiyordu.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest

from store import retention
from store.clock import utc_now


@pytest.fixture
def snapshot_dizini(tmp_path, monkeypatch):
    d = tmp_path / "snapshots"
    d.mkdir()
    monkeypatch.setattr(retention, "SNAPSHOT_DIR", d)
    return d


def _snapshot(dizin, ad: str, gun_once: float, boyut: int = 1024):
    yol = dizin / ad
    yol.write_bytes(b"x" * boyut)
    damga = (utc_now() - timedelta(days=gun_once)).timestamp()
    os.utime(yol, (damga, damga))
    return yol


def test_the_built_in_default_is_three_days(monkeypatch):
    """Varsayılan 14 gündü ve pratikte kullanılmıyordu (kullanıcı kararı).

    MODÜL SABİTİNE BAKMIYOR, VARSAYILANA BAKIYOR. `SNAPSHOT_DAYS` import
    anında `os.environ`den okunuyor ve `llm/settings.py` açılışta
    `load_dotenv()` çağırdığı için geliştiricinin `.env` dosyası o değeri
    ezebiliyor. İlk yazımda test tam olarak buna takıldı: tek başına
    geçiyor, tam takımda düşüyordu — yani kodu değil, o makinedeki `.env`'i
    ölçüyordu.
    """
    import importlib

    monkeypatch.delenv("RETAIN_SNAPSHOT_DAYS", raising=False)
    yeniden = importlib.reload(retention)
    try:
        assert yeniden.SNAPSHOT_DAYS == 3
    finally:
        importlib.reload(retention)


def test_the_environment_overrides_the_default(monkeypatch):
    """`.env`'deki değer kodun varsayılanını EZİYOR — sessiz tuzak buydu.

    Kod varsayılanını 3'e çekmek, `.env`'inde hâlâ 14 yazan bir kurulumda
    HİÇBİR ŞEY yapmıyor. Canlıda tam olarak bu durumdaydı (2026-09-13);
    dosyayı da güncellemek gerekti. Test önceliği açıkça yazıyor ki bir
    daha kimse "kodu değiştirdim, neden değişmedi" diye aramasın.
    """
    import importlib

    monkeypatch.setenv("RETAIN_SNAPSHOT_DAYS", "14")
    yeniden = importlib.reload(retention)
    try:
        assert yeniden.SNAPSHOT_DAYS == 14
    finally:
        monkeypatch.delenv("RETAIN_SNAPSHOT_DAYS", raising=False)
        importlib.reload(retention)


def test_snapshots_older_than_the_window_are_removed(snapshot_dizini, monkeypatch):
    monkeypatch.setattr(retention, "SNAPSHOT_DAYS", 3)
    eski = _snapshot(snapshot_dizini, "loan_rates_llm_20260901T120000Z.raw", 5)
    yeni = _snapshot(snapshot_dizini, "loan_rates_llm_20260913T120000Z.raw", 1)

    silinen = retention.purge_snapshots()

    assert silinen == 1
    assert not eski.exists()
    assert yeni.exists(), "pencere içindeki snapshot korunmalı"


def test_a_monday_snapshot_survives_until_thursday(snapshot_dizini, monkeypatch):
    """ÜÇ GÜNÜN SEBEBİ BU: agent Pazartesi ve Perşembe koşuyor.

    Pazartesi'nin ham sayfası, Perşembe koşusuyla karşılaştırılabilsin diye
    o güne kadar elde kalmalı. Bir günlük pencere bunu keserdi.
    """
    monkeypatch.setattr(retention, "SNAPSHOT_DAYS", 3)
    pazartesi = _snapshot(snapshot_dizini, "loan_rates_llm_20260907T120000Z.raw", 2.9)

    retention.purge_snapshots()

    assert pazartesi.exists()


def test_zero_means_keep_everything(snapshot_dizini, monkeypatch):
    """0 = sınırsız sakla; kazara her şeyi silmemeli."""
    monkeypatch.setattr(retention, "SNAPSHOT_DAYS", 0)
    eski = _snapshot(snapshot_dizini, "fx_banks_20260101T120000Z.raw", 400)

    assert retention.purge_snapshots() == 0
    assert eski.exists()


def test_only_raw_snapshots_are_touched(snapshot_dizini, monkeypatch):
    """Dizine düşmüş başka bir dosya SİLİNMEMELİ.

    Budama `*.raw` ile sınırlı: yanlışlıkla oraya konmuş bir yedek ya da
    not dosyasını silmek, geri dönüşü olmayan bir sürpriz olurdu.
    """
    monkeypatch.setattr(retention, "SNAPSHOT_DAYS", 3)
    baska = _snapshot(snapshot_dizini, "onemli_not.txt", 400)
    ham = _snapshot(snapshot_dizini, "fx_banks_20260101T120000Z.raw", 400)

    retention.purge_snapshots()

    assert baska.exists(), ".raw olmayan dosyaya dokunulmamalı"
    assert not ham.exists()


def test_purge_survives_an_unreadable_file(snapshot_dizini, monkeypatch):
    """Tek bir dosyanın hatası bütün budamayı düşürmemeli."""
    monkeypatch.setattr(retention, "SNAPSHOT_DAYS", 3)
    _snapshot(snapshot_dizini, "fx_banks_20260101T120000Z.raw", 400)
    _snapshot(snapshot_dizini, "fx_tcmb_20260101T120000Z.raw", 400)

    gercek_unlink = type(snapshot_dizini).unlink
    cagri = {"n": 0}

    def _bazen_patla(self, *a, **k):
        cagri["n"] += 1
        if cagri["n"] == 1:
            raise OSError("izin yok")
        return gercek_unlink(self, *a, **k)

    monkeypatch.setattr(type(snapshot_dizini), "unlink", _bazen_patla)
    # Patlamamalı ve diğerini yine de silmeli.
    assert retention.purge_snapshots() == 1
