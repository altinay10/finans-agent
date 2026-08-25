"""Şema migrasyonu — sunucu güncellemesinin can damarı.

`Base.metadata.create_all()` yalnızca OLMAYAN tabloyu yaratır; var olan bir
tabloya sütun EKLEMEZ. Bu projede veritabanı dosyası sunucuda kalıcıdır
(docker named volume). Şemaya yeni bir sütun eklendiğinde, güncelleme sonrası
uygulama açılışta `no such column` ile patlar ve kullanıcının elinde ne
migration aracı ne de veriyi kurtaracak bir yol vardır.

Bu dosya `store/migrate.py`'nin hem YAPTIĞINI hem de KASITLI OLARAK
YAPMADIKLARINI kilitler.
"""
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text

from store.migrate import add_missing_columns


@pytest.fixture
def engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path/'m.db'}", future=True)


def _patch_metadata(monkeypatch, table: Table):
    """Modellerin yerine test tablosunu koy."""
    import store.migrate as migrate

    class FakeBase:
        metadata = table.metadata

    monkeypatch.setattr(migrate, "Base", FakeBase)


def test_adds_a_column_that_is_missing_from_an_existing_table(engine, monkeypatch):
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id INTEGER PRIMARY KEY, collector TEXT)"))
        conn.execute(text("INSERT INTO runs (collector) VALUES ('fx_tcmb')"))

    meta = MetaData()
    Table(
        "runs", meta,
        Column("id", Integer, primary_key=True),
        Column("collector", String),
        Column("trigger", String, nullable=True),      # yeni
    )
    _patch_metadata(monkeypatch, meta.tables["runs"])

    added = add_missing_columns(engine)
    assert added == ["runs.trigger"]
    assert "trigger" in {c["name"] for c in inspect(engine).get_columns("runs")}


def test_existing_rows_survive_the_migration(engine, monkeypatch):
    """ASIL GÜVENCE: güncelleme veri kaybettirmemeli."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id INTEGER PRIMARY KEY, collector TEXT)"))
        conn.execute(text("INSERT INTO runs (collector) VALUES ('fx_tcmb')"))

    meta = MetaData()
    Table(
        "runs", meta,
        Column("id", Integer, primary_key=True),
        Column("collector", String),
        Column("failure_kind", String, nullable=True),
    )
    _patch_metadata(monkeypatch, meta.tables["runs"])
    add_missing_columns(engine)

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT collector, failure_kind FROM runs")).all()
    assert rows == [("fx_tcmb", None)]


def test_running_twice_is_a_no_op(engine, monkeypatch):
    """Her açılışta çalışıyor; ikinci koşu hiçbir şey yapmamalı."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id INTEGER PRIMARY KEY)"))

    meta = MetaData()
    Table("runs", meta, Column("id", Integer, primary_key=True),
          Column("trigger", String, nullable=True))
    _patch_metadata(monkeypatch, meta.tables["runs"])

    assert add_missing_columns(engine) == ["runs.trigger"]
    assert add_missing_columns(engine) == []


def test_a_table_that_does_not_exist_yet_is_left_to_create_all(engine, monkeypatch):
    """Olmayan tabloya ALTER denemek hata verirdi; create_all'ın işi."""
    meta = MetaData()
    Table("yepyeni", meta, Column("id", Integer, primary_key=True),
          Column("x", String, nullable=True))
    _patch_metadata(monkeypatch, meta.tables["yepyeni"])

    assert add_missing_columns(engine) == []


def test_not_null_column_without_default_is_refused_not_half_applied(engine, monkeypatch, caplog):
    """SQLite varsayılansız NOT NULL sütun eklemeye izin vermez.

    Sessizce denemek yerine AÇIKÇA reddedilir ve loglanır; yarım uygulanmış
    bir şema, hiç uygulanmamış bir şemadan daha tehlikelidir.
    """
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id INTEGER PRIMARY KEY)"))

    meta = MetaData()
    Table("runs", meta, Column("id", Integer, primary_key=True),
          Column("zorunlu", String, nullable=False))
    _patch_metadata(monkeypatch, meta.tables["runs"])

    with caplog.at_level("ERROR"):
        added = add_missing_columns(engine)
    assert added == []
    assert "elle migration" in caplog.text


def test_non_sqlite_backend_does_nothing(monkeypatch):
    """Postgres'e geçildiğinde gerçek bir migration aracı kullanılmalı.

    Sessizce yanlış bir şey yapmaktansa hiçbir şey yapmamak doğru.
    """
    class FakeUrl:
        def get_backend_name(self):
            return "postgresql"

    class FakeEngine:
        url = FakeUrl()

    assert add_missing_columns(FakeEngine()) == []


def test_real_schema_is_reachable_from_an_empty_database(tmp_path, monkeypatch):
    """Uçtan uca: boş dosyadan gerçek şemanın tamamı kurulabilmeli."""
    import os

    db_path = tmp_path / "gercek.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_path}")

    from sqlalchemy import create_engine as ce

    from store.migrate import add_missing_columns as amc
    from store.models import Base

    engine = ce(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    assert amc(engine) == []          # create_all sonrası eksik sütun kalmamalı

    tables = set(inspect(engine).get_table_names())
    for required in (
        "scrape_runs", "source_runs", "llm_calls",
        "http_requests", "rate_changes", "loan_reference_quotes",
    ):
        assert required in tables, required
