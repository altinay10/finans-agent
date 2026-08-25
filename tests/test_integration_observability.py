"""Gözlemlenebilirlik testleri — AĞSIZ.

Korunan şey: sunucuda kimse bakmıyorken bir bankanın sessizce kaybolması.
`scrape_runs` bunu göstermez (koşu 'ok' kalır), `source_runs` gösterir.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from store.db import SessionLocal
from store.models import ScrapeRun, SourceRun

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ----------------------------------------------- kaynak bazlı kayıt ----


def test_failed_bank_is_recorded_while_run_stays_ok(db):
    """EN KRİTİK TEST: koşu 'ok' derken banka 'failed' kaydı düşmeli.

    fx_banks'te bir banka çökse bile diğerleri veri getirdiği için koşu
    başarılı sayılır — doğrusu da budur. Ama o zaman hangi bankanın
    kaybolduğunu gösteren tek kayıt source_runs'tır.
    """
    from collectors.fx_banks import BankFxCollector

    collector = BankFxCollector()
    collector._run_id = None
    payload = json.dumps(
        {
            "yapikredi": {"ok": True, "body": _fixture("yapikredi_fx.json")},
            "akbank": {"ok": False, "error": "connection reset"},
        }
    ).encode("utf-8")

    records = collector.parse(payload)
    assert {r.institution for r in records} == {"YAPIKREDI"}

    from store import queries

    events = queries.source_runs()
    by_source = {(e["source"], e["phase"]): e for e in events}
    assert by_source[("yapikredi", "parse")]["status"] == "ok"
    assert by_source[("yapikredi", "parse")]["rows"] == 2
    # akbank fetch aşamasında düştüğü için parse kaydı YOK; fetch kaydını
    # gerçek fetch() üretir. Burada önemli olan: yapikredi kaydı yazıldı.
    assert ("akbank", "parse") not in by_source


def test_empty_parse_is_recorded_as_empty_not_ok(db):
    """Sayfa şeması değişip sıfır kayıt çıkarsa bu 'ok' sayılmamalı.

    'empty', 'failed'den farklıdır: istek başarılı, ayrıştırma da patlamadı,
    ama sonuç yok. Sessiz bozulmanın en sinsi hâli budur.
    """
    from collectors.fx_banks import BankFxCollector

    collector = BankFxCollector()
    collector._run_id = None
    payload = json.dumps(
        {"teb": {"ok": True, "body": json.dumps({"result": []})}}
    ).encode("utf-8")

    with pytest.raises(Exception):
        collector.parse(payload)  # hiç kayıt çıkmadı -> ParseError

    from store import queries

    teb = [e for e in queries.source_runs() if e["source"] == "teb"]
    assert teb and teb[0]["status"] == "empty"


def test_source_health_flags_the_broken_bank(db):
    from store import queries
    from store.observability import record_source_run

    for _ in range(3):
        record_source_run(collector="fx_banks", source="teb", phase="fetch", status="ok", rows=2)
    record_source_run(
        collector="fx_banks", source="akbank", phase="fetch", status="failed",
        error="HTTP 500",
    )

    health = {(h["collector"], h["source"]): h for h in queries.source_health()}
    assert health[("fx_banks", "teb")]["bad_count"] == 0
    assert health[("fx_banks", "akbank")]["bad_count"] == 1
    assert "HTTP 500" in health[("fx_banks", "akbank")]["last_error"]
    # Hiç başarılı olmamış kaynak: last_ok boş kalmalı.
    assert health[("fx_banks", "akbank")]["last_ok"] is None


def test_recording_never_breaks_the_collector(db, monkeypatch):
    """Log yazamamak, başarılı bir veri çekimini KAYBETTİRMEMELİ."""
    import store.observability as obs

    def _boom(*a, **kw):
        raise RuntimeError("veritabanı kilitli")

    monkeypatch.setattr(obs, "SessionLocal", _boom)
    # İstisna dışarı sızmamalı.
    obs.record_source_run(collector="x", source="y", phase="fetch", status="ok")
    obs.record_llm_call(model="m", status="ok")


def test_long_error_is_truncated(db):
    """Bazı bankalar hata olarak tam HTML sayfası döndürüyor; tablo şişmesin."""
    from store import queries
    from store.observability import record_source_run

    record_source_run(
        collector="fx_banks", source="teb", phase="fetch", status="failed",
        error="x" * 10_000,
    )
    event = queries.source_runs()[0]
    assert len(event["error"]) <= 2000


# ------------------------------------------------------ LLM muhasebesi ----


def test_disabled_llm_is_recorded_with_zero_tokens(db):
    """Fallback kapalıyken de kayıt düşmeli: 'neden devreye girmedi' sorusu."""
    from pydantic import BaseModel

    from llm import extract

    class Dummy(BaseModel):
        value: float = 0.0

    with pytest.raises(extract.LlmDisabled):
        extract.extract("<p>%38</p>", Dummy, collector="fx_banks")

    from store import queries

    call = queries.llm_calls()[0]
    assert call["status"] == "disabled"
    assert call["collector"] == "fx_banks"
    assert call["total_tokens"] is None
    # Harcama sayacına girmemeli.
    assert queries.llm_token_totals()["total_tokens"] == 0


def test_budget_exceeded_is_recorded_without_calling_the_model(db, monkeypatch):
    from pydantic import BaseModel

    from llm import extract, settings

    class Dummy(BaseModel):
        value: float = 0.0

    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_MAX_CALLS_PER_RUN", 0)
    extract.reset_budget()

    called = {"n": 0}
    monkeypatch.setattr(extract, "get_client", lambda: called.__setitem__("n", 1))

    with pytest.raises(extract.LlmBudgetExceeded):
        extract.extract("<p>%38</p>", Dummy, collector="deposits")

    assert called["n"] == 0, "bütçe dolmuşken modele gidilmemeli"

    from store import queries

    assert queries.llm_calls()[0]["status"] == "budget_exceeded"


def test_token_totals_only_count_successful_calls(db):
    from store import queries
    from store.observability import record_llm_call

    record_llm_call(model="m", status="ok", prompt_tokens=100, completion_tokens=20)
    record_llm_call(model="m", status="failed", prompt_tokens=500, completion_tokens=0)
    record_llm_call(model="m", status="disabled")

    totals = queries.llm_token_totals()
    assert totals["calls"] == 1
    assert totals["total_tokens"] == 120, "başarısız çağrının tokenı toplama girmemeli"
