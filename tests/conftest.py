"""Test altyapısı.

ÖNEMLİ: `store/db.py` engine'i import anında `DB_URL` ortam değişkeninden
kuruyor. Bu yüzden DB_URL burada, herhangi bir `store.*` importundan ÖNCE
geçici bir dosyaya ayarlanıyor — aksi halde testler kullanıcının gerçek
`data/finans_agent.db` dosyasına yazar.

DOSYA ADI SÜREÇ BAŞINA BENZERSİZ. Sabit bir ad (`finans_agent_test.db`)
kullanılıyordu ve aynı makinede AYNI ANDA iki pytest koşusu olduğunda
birbirlerini düşürüyorlardı: `db` fixture'ı her testte `drop_all` çağırıyor,
yani bir koşu diğerinin şemasını siliyor ve kurban koşu rastgele testlerde
`no such table: scrape_runs` ile patlıyordu. Hata testlerin kendisiyle
ilgisiz olduğu ve her koşuda BAŞKA testlerde çıktığı için teşhisi
gereksizce zor (2026-09-12'de canlı olarak yaşandı: iki ayrı oturum aynı
depoda test koşuyordu). PID, aynı makinede eşzamanlı koşuları birbirinden
ayırmaya yetiyor.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP_DB = Path(tempfile.gettempdir()) / f"finans_agent_test_{os.getpid()}.db"
os.environ["DB_URL"] = f"sqlite:///{_TMP_DB}"


@pytest.fixture(scope="session", autouse=True)
def _gecici_veritabanini_temizle():
    """Koşu bitince kendi dosyasını siler.

    Ad artık PID taşıdığı için dosyalar birikirdi; her koşu kendi çöpünü
    topluyor. Koşu çökerse dosya kalır — bu bilinçli, çöken bir koşunun
    veritabanı incelenebilir olmalı.
    """
    yield
    for artik in (_TMP_DB, Path(f"{_TMP_DB}-wal"), Path(f"{_TMP_DB}-shm")):
        try:
            artik.unlink(missing_ok=True)
        except OSError:
            pass


@pytest.fixture
def db():
    """Her test için sıfırdan, boş bir şema. Gerçek veritabanına dokunmaz."""
    from store.db import Base, engine, init_db

    Base.metadata.drop_all(engine)
    init_db(seed=False)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def seeded_db(db):
    """Şema + testlerin ihtiyaç duyduğu asgari referans veri (FK'ler için)."""
    from store.db import SessionLocal
    from store.models import Fund, Institution

    with SessionLocal() as session:
        session.add_all(
            [
                Institution(code="TCMB", name="T.C. Merkez Bankası", kind="reference"),
                Institution(code="VAKIFBANK", name="VakıfBank", kind="bank"),
                Institution(code="TEB", name="CepteTEB", kind="bank"),
                Institution(code="EMLAKKATILIM", name="Emlak Katılım", kind="participation"),
            ]
        )
        session.add_all(
            [
                Fund(code="AK3", name="Ak Portföy Hisse Senedi", is_equity_heavy=True),
                Fund(code="AFA", name="Ak Portföy Amerika", is_equity_heavy=False),
            ]
        )
        session.commit()
    yield


@pytest.fixture(autouse=True)
def _llm_kapali(monkeypatch):
    """Testler geliştiricinin `.env` dosyasına BAĞLI OLMAMALI.

    Bu gerçekten yaşandı: `.env`'e çalışan bir anahtar ve
    `LLM_FALLBACK_ENABLED=1` yazılınca "hiçbiri ağa çıkmaz" güvencesi sessizce
    düştü — test paketi Google'a GERÇEK bir istek attı ve token harcadı.
    Testin sonucu artık makinede hangi dosyanın durduğuna göre değişiyordu.

    Bu yüzden her test varsayılan olarak KAPALI agent ile başlıyor. Açık
    olmasını isteyen testler zaten kendileri `monkeypatch` ile açıyor; bu
    fixture onların üstüne yazmaz çünkü önce bu çalışır.
    """
    import llm.settings as settings

    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
