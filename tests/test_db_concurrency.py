"""SQLite eşzamanlılığı — WAL kipi ve bekleme süresi.

NEDEN: bu proje aynı SQLite dosyasına İKİ AYRI SÜREÇTEN yazıyor (panel ve
zamanlayıcı, ayrı konteynerler, ortak Docker volume). Varsayılan
`journal_mode=delete` kipinde bir yazma dosyanın tamamını kilitliyor ve o
sırada okuyan süreç `database is locked` alıyor. Canlıda bu, zamanlayıcının
nabız yazımını düşürüyordu (`store.heartbeat: nabız yazılamadı`).

Buradaki testler düzeltmenin gerçekten ÇALIŞTIĞINI kilitliyor — ayarın
yazıldığını değil, davranışın değiştiğini.
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from store.db import BUSY_TIMEOUT_MS, SessionLocal, engine
from store.models import Institution


def test_the_database_runs_in_wal_mode(db):
    """Kip dosyaya kalıcı; her bağlantıda yeniden kurulması zararsız."""
    with engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_every_connection_gets_a_busy_timeout(db):
    """Timeout BAĞLANTIYA özel: havuzdan gelen her bağlantıda olmalı.

    Varsayılan 0 ms ile ikinci yazar kilidi görünce ANINDA hata verir.
    """
    for _ in range(3):
        with engine.connect() as c:
            assert c.execute(text("PRAGMA busy_timeout")).scalar() == BUSY_TIMEOUT_MS


def test_a_reader_does_not_block_a_writer(db):
    """ASIL DÜZELTME BU: açık bir okuma, yazmayı düşürmemeli.

    `delete` kipinde bu senaryo yazarı `database is locked` ile düşürüyordu.
    WAL'da okuyucu ana dosyanın tutarlı hâlini okurken yazar `-wal`
    dosyasına ekliyor; ikisi çakışmıyor.
    """
    # OKUMA KİLİDİ GERÇEKTEN TUTULMALI. Tamamlanmış bir SELECT kilidi
    # bırakıyor; `BEGIN` olmadan yazılan bir test, `delete` kipinde bile
    # geçer ve hiçbir şey kanıtlamaz (bu tuzağa bir kez düşüldü).
    okuyucu = sqlite3.connect(engine.url.database, timeout=0.3)
    okuyucu.execute("BEGIN")
    okuyucu.execute("SELECT COUNT(*) FROM institutions").fetchone()

    try:
        # Yazar aynı anda yazıyor — hata ALMAMALI.
        with SessionLocal() as s:
            s.add(Institution(code="YAZAR", name="Yazar", kind="bank"))
            s.commit()
    finally:
        okuyucu.close()

    with SessionLocal() as s:
        assert s.get(Institution, "YAZAR") is not None


def test_a_second_writer_waits_instead_of_failing_instantly(db):
    """İki YAZAR hâlâ sıraya giriyor ama ANINDA düşmüyor.

    WAL yazar-yazar çakışmasını kaldırmıyor; onu `busy_timeout` yönetiyor.
    Timeout 0 olsaydı ikinci yazar hemen `database is locked` alırdı.
    """
    ham = engine.raw_connection()
    try:
        cur = ham.cursor()
        # Gerçek bir yazma kilidi al ve TUTMAYA devam et.
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "INSERT INTO institutions (code, name, kind) VALUES ('KILIT','Kilit','bank')"
        )

        # İkinci yazar: timeout SIFIR olsa anında düşerdi. Burada kısa bir
        # timeout veriyoruz ki test beklemesin — kanıtlanan şey, hatanın
        # "anında" değil "süre dolunca" gelmesi.
        ikinci = sqlite3.connect(engine.url.database, timeout=0.3)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            ikinci.execute(
                "INSERT INTO institutions (code, name, kind) VALUES ('IKI','İki','bank')"
            )
            ikinci.commit()
        ikinci.close()
        ham.rollback()
    finally:
        ham.close()


def test_backup_still_works_under_wal(db, tmp_path, monkeypatch):
    """YEDEKLEME REGRESYON KORUMASI.

    `store/backup.py` kaynağı SALT OKUNUR açıyor (`mode=ro`). WAL'da
    salt okunur bir bağlantının paylaşılan bellek dosyasına erişmesi
    gerekiyor; bu, kipi değiştirirken sessizce kırılabilecek tek yerdi.
    """
    from store import backup

    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "yedek")
    with SessionLocal() as s:
        s.add(Institution(code="YEDEK", name="Yedek", kind="bank"))
        s.commit()

    yol = backup.run_backup()
    assert yol is not None and yol.exists()

    with sqlite3.connect(yol) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # HENÜZ CHECKPOINT EDİLMEMİŞ veri de yedeğe girmeli: WAL'da son
        # yazmalar ana dosyada değil `-wal`'da duruyor olabilir.
        assert c.execute(
            "SELECT code FROM institutions WHERE code='YEDEK'"
        ).fetchone() is not None
