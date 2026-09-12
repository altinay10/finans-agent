"""SQLite yedeklemesi — `store/backup.py`.

Buradaki testlerin ortak ilkesi: **doğrulanmamış yedek yedek değildir.**
Bir yedekleme işinin sessizce boş, yarım ya da bozuk dosya üretmesi, hiç
yedek almamaktan kötüdür çünkü yanlış güven verir. Aşağıdaki testler o üç
sessiz başarısızlığı kilitliyor.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from store.clock import utc_now
from store.db import SessionLocal
from store.models import Institution


@pytest.fixture
def yedek_dizini(tmp_path, monkeypatch):
    from store import backup

    hedef = tmp_path / "yedekler"
    monkeypatch.setattr(backup, "BACKUP_DIR", hedef)
    return hedef


def test_backup_contains_the_real_data(db, yedek_dizini):
    """Yedek AÇILABİLMELİ ve içinde veri OLMALI.

    "Dosya oluştu" yeterli bir kanıt değil: boş ya da yarım bir dosya da
    oluşur. Test yedeği gerçekten açıp yazdığımız satırı arıyor.
    """
    from store import backup

    with SessionLocal() as s:
        s.add(Institution(code="ING", name="ING", kind="bank"))
        s.commit()

    yol = backup.run_backup()
    assert yol is not None and yol.exists()

    with sqlite3.connect(yol) as conn:
        kurumlar = conn.execute("SELECT code FROM institutions").fetchall()
    assert ("ING",) in kurumlar


def test_a_corrupt_backup_is_thrown_away_not_kept(db, yedek_dizini, monkeypatch):
    """Bütünlük denetiminden geçmeyen dosya SAKLANMAMALI.

    Bozuk bir yedeği "yedeğim var" diye tutmak, geri yükleme anına kadar
    fark edilmeyen bir arızadır — ve o an her şeyin zaten kötü gittiği
    andır.
    """
    from store import backup

    monkeypatch.setattr(backup, "_integrity_ok", lambda _yol: False)
    assert backup.run_backup() is None
    # Ne geçerli ad ne de yarım dosya kaldı.
    assert list(yedek_dizini.glob("*")) == []


def test_a_half_written_backup_never_takes_the_real_name(db, yedek_dizini, monkeypatch):
    """Yazarken ölen süreç, geçerli adlı bir yedek BIRAKMAMALI.

    Önce `.partial` adına yazılıyor, ancak doğrulandıktan sonra gerçek ada
    taşınıyor. Aksi halde bir sonraki tur "bugünün yedeği var" sanıp
    üstünden atlardı.
    """
    from store import backup

    gercek_connect = sqlite3.connect

    def _hedefte_patla(target, *a, **k):
        # Yazma hedefine bağlanırken düşen bir süreci taklit eder; kaynağı
        # okumak hâlâ çalışıyor, yani hata gerçekten yazma tarafında.
        if str(target).endswith(".partial"):
            raise sqlite3.OperationalError("disk doldu")
        return gercek_connect(target, *a, **k)

    monkeypatch.setattr(backup.sqlite3, "connect", _hedefte_patla)
    assert backup.run_backup() is None
    assert list(yedek_dizini.glob("finans_agent_*.db")) == []


def test_a_leftover_partial_file_does_not_block_the_next_backup(db, yedek_dizini):
    """Çökmeden kalan `.partial`, bir sonraki turu engellememeli.

    Yarım dosya diskte kalırsa ve tur onu ezemezse, yedekleme o günden
    sonra sessizce hiç çalışmaz — fark edilmesi en zor arıza türü.
    """
    from store import backup

    yedek_dizini.mkdir(parents=True, exist_ok=True)
    artik = yedek_dizini / (utc_now().strftime(backup.NAME_FORMAT)[:-3] + ".partial")
    artik.write_bytes(b"yarim kalmis cop")

    yol = backup.run_backup()

    assert yol is not None and yol.exists()
    assert not artik.exists(), "yarım dosya temizlenmeliydi"


def test_running_twice_in_a_day_does_not_create_a_second_file(db, yedek_dizini):
    """Süreç yeniden başladığında günlük rotasyon bozulmamalı.

    Her açılışta yeni bir dosya üretmek, sabit sayıda yedek tutan bir
    pencerede saklama süresini sessizce kısaltırdı.
    """
    from store import backup

    ilk = backup.run_backup()
    ikinci = backup.run_backup()
    assert ilk == ikinci
    assert len(list(yedek_dizini.glob("finans_agent_*.db"))) == 1


def test_old_backups_are_pruned_but_recent_ones_survive(db, yedek_dizini, monkeypatch):
    """Saklama penceresi dışındaki yedekler silinir, içindekiler kalır."""
    import os

    from store import backup

    monkeypatch.setattr(backup, "KEEP_DAYS", 14)
    yedek_dizini.mkdir(parents=True, exist_ok=True)
    simdi = utc_now()

    eski = yedek_dizini / "finans_agent_20260101.db"
    yeni = yedek_dizini / "finans_agent_20260910.db"
    for yol, gun_once in ((eski, 30), (yeni, 3)):
        yol.write_bytes(b"x")
        damga = (simdi - timedelta(days=gun_once)).timestamp()
        os.utime(yol, (damga, damga))

    backup.run_backup(now=simdi)

    assert not eski.exists(), "30 günlük yedek budanmalıydı"
    assert yeni.exists(), "3 günlük yedek korunmalıydı"


def test_backup_is_skipped_without_a_sqlite_file(yedek_dizini, monkeypatch):
    """SQLite değilse (ör. Postgres) sessizce hiçbir şey yapılmamalı.

    Yanlış bir araçla "yedek aldım" demek, hiç almamaktan kötü.
    """
    from store import backup

    monkeypatch.setattr(backup, "_db_path", lambda: None)
    assert backup.run_backup() is None
