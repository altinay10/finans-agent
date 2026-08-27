"""Test altyapısı.

ÖNEMLİ: `store/db.py` engine'i import anında `DB_URL` ortam değişkeninden
kuruyor. Bu yüzden DB_URL burada, herhangi bir `store.*` importundan ÖNCE
geçici bir dosyaya ayarlanıyor — aksi halde testler kullanıcının gerçek
`data/finans_agent.db` dosyasına yazar.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP_DB = Path(tempfile.gettempdir()) / "finans_agent_test.db"
os.environ["DB_URL"] = f"sqlite:///{_TMP_DB}"


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
