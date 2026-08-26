"""Ayrıntılı loglama — PLAN.md madde 7.

Kullanıcının istediği: "agent ne zaman hangi durumda devreye girdi, kaç token
harcadı, hangi api istekleri ne zaman dönmedi, daha sonraki istekleri döndü
mü, ve aklıma gelmeyen diğer önemli parametreler."

Buradaki testlerin ortak ilkesi: **kayıt tutmak asla veriyi kaybettirmemeli.**
Bir log yazma hatası yüzünden başarılı bir çekimi kaybetmek saçma olur; bu
yüzden gözlemlenebilirlik katmanının tamamı kendi istisnasını yutar ve bu
davranış testle kilitlenmiştir.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from store.db import SessionLocal
from store.models import (
    HttpRequest,
    Institution,
    LlmCall,
    RateChange,
    ScrapeRun,
    SourceRun,
)


# --------------------------------------------------- istek düzeyi kayıt ----

def test_successful_request_is_recorded_with_status_and_size(db, monkeypatch):
    from collectors import http

    def fake(method, url, **kwargs):
        return httpx.Response(
            200, content=b'{"ok":1}', headers={"content-type": "application/json"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "request", fake)
    with http.collector_context("fx_banks", None), http.source("akbank"):
        http.get("https://example.invalid/rates")

    with SessionLocal() as s:
        rows = s.execute(select(HttpRequest)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.collector, row.source, row.method) == ("fx_banks", "akbank", "GET")
    assert row.status_code == 200
    assert row.outcome == "ok"
    assert row.response_bytes == 8
    assert row.duration_ms is not None


def test_http_error_is_recorded_even_though_no_exception_is_raised(db, monkeypatch):
    """500 bir istisna DEĞİL ama 'ok' da değil.

    `raise_for_status()` çağırmak arayanın kararıdır; kayıt bunu beklememeli.
    '200 döndü ama boş' ile '500 döndü' bambaşka arıza türleridir ve
    ayrılmadıklarında teşhis imkânsızlaşır.
    """
    from collectors import http

    monkeypatch.setattr(
        httpx, "request",
        lambda m, u, **k: httpx.Response(500, content=b"", request=httpx.Request(m, u)),
    )
    with http.collector_context("deposit_rates", None), http.source("garantibbva"):
        http.get("https://example.invalid/x")

    with SessionLocal() as s:
        row = s.execute(select(HttpRequest)).scalars().one()
    assert row.status_code == 500
    assert row.outcome == "http_error"


def test_timeout_is_recorded_with_no_status_code_then_reraised(db, monkeypatch):
    """Yanıt hiç gelmediyse status_code NULL kalmalı — 0 yazmak yalan olurdu.

    Zaman aşımı geçici sayıldığı için bir kez yeniden denenir; ikisi de
    kayda geçer (bkz. collectors/http.py MAX_NETWORK_ATTEMPTS).
    """
    from collectors import http

    monkeypatch.setattr(http, "RETRY_BACKOFF_SECONDS", 0)

    def boom(method, url, **kwargs):
        raise httpx.ConnectTimeout("zaman aşımı")

    monkeypatch.setattr(httpx, "request", boom)
    with http.collector_context("loan_rates", None), http.source("teb"):
        with pytest.raises(httpx.ConnectTimeout):
            http.get("https://example.invalid/x")

    with SessionLocal() as s:
        rows = s.execute(select(HttpRequest).order_by(HttpRequest.id)).scalars().all()
    assert [r.attempt for r in rows] == [1, 2]
    for row in rows:
        assert row.status_code is None
        assert row.outcome == "timeout"
        assert "zaman aşımı" in row.error


def test_recording_failure_never_breaks_the_request(db, monkeypatch):
    """ASIL KURAL: log yazamamak veriyi kaybettirmemeli."""
    from collectors import http

    monkeypatch.setattr(
        httpx, "request",
        lambda m, u, **k: httpx.Response(200, content=b"veri", request=httpx.Request(m, u)),
    )

    def exploding_recorder(**kwargs):
        raise RuntimeError("veritabanı yok")

    monkeypatch.setattr(
        "store.observability.record_http_request", exploding_recorder, raising=True
    )
    resp = http.get("https://example.invalid/x")
    assert resp.content == b"veri"      # istek başarıyla döndü


def test_source_context_does_not_leak_after_the_block(db, monkeypatch):
    """Bir bankanın etiketi bir sonrakine sızmamalı — kayıt yanlış kaynağa yazılır."""
    from collectors import http

    monkeypatch.setattr(
        httpx, "request",
        lambda m, u, **k: httpx.Response(200, content=b"x", request=httpx.Request(m, u)),
    )
    with http.collector_context("fx_banks", None):
        with http.source("akbank"):
            http.get("https://example.invalid/a")
        http.get("https://example.invalid/b")

    with SessionLocal() as s:
        rows = s.execute(select(HttpRequest).order_by(HttpRequest.id)).scalars().all()
    assert [r.source for r in rows] == ["akbank", None]


# ------------------------------------------------- koşu aşaması ve tetik ----

def _collector(fail_at: str | None = None):
    from pydantic import BaseModel

    from collectors.base import Collector, SanityCheckError

    class Row(BaseModel):
        value: float = 1.0

    class T(Collector):
        name = "test_collector"
        schema = Row

        def fetch(self):
            if fail_at == "fetch":
                raise RuntimeError("ağ koptu")
            return b"veri"

        def parse(self, raw):
            if fail_at == "parse":
                raise RuntimeError("selector kırıldı")
            return [Row()]

        def sanity_check(self, records):
            if fail_at == "sanity":
                raise SanityCheckError("oran bant dışı")

        def persist(self, records, run_id):
            if fail_at == "persist":
                raise RuntimeError("disk dolu")

    return T()


def test_run_records_the_trigger_that_started_it(db):
    """Telafi mekanizmasının çalıştığını gösteren tek kayıt."""
    _collector().run(trigger="catchup")
    with SessionLocal() as s:
        run = s.execute(select(ScrapeRun)).scalars().one()
    assert run.trigger == "catchup"
    assert run.status == "ok"


@pytest.mark.parametrize("phase", ["fetch", "parse", "sanity", "persist"])
def test_failure_phase_is_recorded_separately(db, phase):
    """'sanity' diğerlerinden ayrı tutulmalı — o bir arıza değil, koruma."""
    result = _collector(fail_at=phase).run()
    assert not result.ok
    assert result.failure_kind == phase
    with SessionLocal() as s:
        run = s.execute(select(ScrapeRun)).scalars().one()
    assert run.failure_kind == phase


def test_sanity_rejection_is_countable_not_buried_in_the_error_text(db):
    """"Kaç kez bant dışı veri geldi" sorusu sayılabilmeli."""
    from store.queries import run_failures

    _collector(fail_at="sanity").run()
    _collector(fail_at="sanity").run()
    _collector(fail_at="parse").run()

    counts = {f["failure_kind"]: f["count"] for f in run_failures()}
    assert counts["sanity"] == 2
    assert counts["parse"] == 1


def test_single_source_collector_records_its_own_source_run(db):
    """Regresyon: tek kaynaklı toplayıcılar hiç source_runs yazmıyordu.

    Sonuç: Kaynaklar sekmesi TCMB için "envanter 'çekiliyor' diyor ama
    başarılı koşu yok" diye YANLIŞ uyarı basıyordu. Kayıt tutmayan bir
    kaynak, bozuk bir kaynaktan ayırt edilemez.
    """
    collector = _collector()
    collector.default_source = "tcmb"
    collector.run()

    with SessionLocal() as s:
        rows = s.execute(select(SourceRun).order_by(SourceRun.id)).scalars().all()
    assert [(r.source, r.phase, r.status) for r in rows] == [
        ("tcmb", "fetch", "ok"),
        ("tcmb", "parse", "ok"),
    ]


def test_multi_source_collector_is_not_double_recorded(db):
    """default_source yoksa otomatik kayıt yazılmamalı.

    Çok kaynaklı toplayıcılar banka banka kendi kaydını tutuyor; üstüne bir
    de toplayıcı adına kayıt yazmak sağlık tablosunu kirletirdi.
    """
    _collector().run()
    with SessionLocal() as s:
        assert s.execute(select(SourceRun)).scalars().all() == []


# ------------------------------------------------------ kurtarma / kesinti ----

def _source_run(collector, source, status, minutes_ago, phase="fetch"):
    return SourceRun(
        collector=collector, source=source, phase=phase, status=status, rows=1,
        started_at=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(minutes=minutes_ago),
    )


def test_recovery_detects_a_source_that_failed_then_came_back(db):
    from store.queries import source_recovery

    with SessionLocal() as s:
        s.add_all(
            [
                _source_run("fx_banks", "akbank", "failed", 180),
                _source_run("fx_banks", "akbank", "failed", 120),
                _source_run("fx_banks", "akbank", "ok", 30),
            ]
        )
        s.commit()

    row = source_recovery()[0]
    assert row["state"] == "düzeldi"
    assert row["failures"] == 2


def test_recovery_reports_an_ongoing_outage_with_its_duration(db):
    from store.queries import source_recovery

    with SessionLocal() as s:
        s.add_all(
            [
                _source_run("fx_banks", "isbank", "ok", 400),
                _source_run("fx_banks", "isbank", "failed", 90),
            ]
        )
        s.commit()

    row = source_recovery()[0]
    assert row["state"] == "hâlâ bozuk"
    assert 85 <= row["outage_minutes"] <= 95


def test_healthy_sources_are_not_listed_as_incidents(db):
    from store.queries import source_recovery

    with SessionLocal() as s:
        s.add(_source_run("fx_banks", "vakifbank", "ok", 10))
        s.commit()
    assert source_recovery() == []


# ---------------------------------------------------------- şema sapması ----

def test_schema_drift_flags_a_collapse_in_row_count(db):
    """Parser patlamadan satır kaybetmek en sinsi bozulma türü."""
    from store.queries import schema_drift

    with SessionLocal() as s:
        for rows in (100, 98, 102, 99):
            r = _source_run("deposit_rates", "teb", "ok", 100, phase="parse")
            r.rows = rows
            s.add(r)
        latest = _source_run("deposit_rates", "teb", "ok", 5, phase="parse")
        latest.rows = 12          # %88 düşüş
        s.add(latest)
        s.commit()

    alerts = schema_drift()
    assert len(alerts) == 1
    assert alerts[0]["source"] == "teb"
    assert alerts[0]["drop_pct"] > 80


def test_schema_drift_tolerates_a_small_product_change(db):
    """Banka bir vade dilimini kaldırmış olabilir; küçük düşüş alarm değil."""
    from store.queries import schema_drift

    with SessionLocal() as s:
        for rows in (100, 100, 100):
            r = _source_run("deposit_rates", "teb", "ok", 100, phase="parse")
            r.rows = rows
            s.add(r)
        latest = _source_run("deposit_rates", "teb", "ok", 5, phase="parse")
        latest.rows = 88          # %12 düşüş
        s.add(latest)
        s.commit()

    assert schema_drift() == []


def test_schema_drift_needs_history_before_it_can_judge(db):
    """Tek koşuluk geçmişle kıyas yapmak sahte alarm üretir."""
    from store.queries import schema_drift

    with SessionLocal() as s:
        r = _source_run("deposit_rates", "teb", "ok", 5, phase="parse")
        r.rows = 3
        s.add(r)
        s.commit()
    assert schema_drift() == []


# --------------------------------------------------- oran değişim / donma ----

def test_first_run_records_every_series_as_new(db):
    from store.observability import record_rate_changes

    written = record_rate_changes("deposit", {("TEB", "TRY/32/0"): 0.375})
    assert written == 1
    with SessionLocal() as s:
        row = s.execute(select(RateChange)).scalars().one()
    assert row.old_value is None
    assert float(row.new_value) == pytest.approx(0.375)


def test_unchanged_rate_writes_nothing(db):
    """Aynı değeri her koşuda yazmak tabloyu şişirir ve 'son değişim'i bozar."""
    from store.observability import record_rate_changes

    record_rate_changes("deposit", {("TEB", "TRY/32/0"): 0.375})
    assert record_rate_changes("deposit", {("TEB", "TRY/32/0"): 0.375}) == 0
    with SessionLocal() as s:
        assert len(s.execute(select(RateChange)).scalars().all()) == 1


def test_changed_rate_records_both_old_and_new(db):
    from store.observability import record_rate_changes

    record_rate_changes("deposit", {("TEB", "TRY/32/0"): 0.375})
    record_rate_changes("deposit", {("TEB", "TRY/32/0"): 0.365})

    with SessionLocal() as s:
        rows = s.execute(select(RateChange).order_by(RateChange.id)).scalars().all()
    assert float(rows[1].old_value) == pytest.approx(0.375)
    assert float(rows[1].new_value) == pytest.approx(0.365)


def test_datasets_do_not_interfere(db):
    """Mevduat ve kredi aynı seri anahtarını taşıyabilir; karışmamalı."""
    from store.observability import record_rate_changes

    record_rate_changes("deposit", {("AKBANK", "x"): 0.30})
    written = record_rate_changes("loan", {("AKBANK", "x"): 0.30})
    assert written == 1


def test_stale_values_finds_a_series_that_stopped_moving(db):
    from store.queries import stale_values

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40)
    with SessionLocal() as s:
        s.add(
            RateChange(
                dataset="deposit", institution="TEB", series_key="TRY/32/0",
                old_value=None, new_value=0.375, changed_at=old,
            )
        )
        s.commit()

    stale = stale_values("deposit", days=14)
    assert len(stale) == 1
    assert stale[0]["institution"] == "TEB"


def test_recent_change_is_not_reported_as_stale(db):
    from store.queries import stale_values

    with SessionLocal() as s:
        s.add(
            RateChange(
                dataset="deposit", institution="TEB", series_key="TRY/32/0",
                old_value=None, new_value=0.375,
                changed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        s.commit()
    assert stale_values("deposit", days=14) == []


# ------------------------------------------------------- LLM tetikleyici ----

def test_llm_call_records_what_triggered_it(db):
    """"Agent ne zaman hangi durumda devreye girdi" — cevabı tetikleyen hatadır."""
    from store.observability import record_llm_call
    from store.queries import llm_triggers

    record_llm_call(
        model="gemini-2.5-flash-lite", status="ok", collector="deposit_rates",
        prompt_tokens=1200, completion_tokens=300, rows_recovered=14,
        trigger_source="teb", trigger_error="ParseError: tablo bulunamadı",
    )
    row = llm_triggers()[0]
    assert row["trigger_source"] == "teb"
    assert "tablo bulunamadı" in row["trigger_error"]
    assert row["total_tokens"] == 1500


def test_cost_is_none_when_the_price_is_unknown(db, monkeypatch):
    """Bilinmeyen maliyet 0 değildir. 0 yazmak "bedava" demek olurdu."""
    import llm.settings as settings

    monkeypatch.setattr(settings, "LLM_PRICE_INPUT_PER_1M", None)
    monkeypatch.setattr(settings, "LLM_PRICE_OUTPUT_PER_1M", None)

    from store.observability import record_llm_call

    record_llm_call(model="m", status="ok", prompt_tokens=1000, completion_tokens=100)
    with SessionLocal() as s:
        assert s.execute(select(LlmCall)).scalars().one().cost_usd is None


def test_cost_is_computed_when_the_price_is_configured(db, monkeypatch):
    import llm.settings as settings

    monkeypatch.setattr(settings, "LLM_PRICE_INPUT_PER_1M", 0.10)
    monkeypatch.setattr(settings, "LLM_PRICE_OUTPUT_PER_1M", 0.40)

    from store.observability import record_llm_call

    record_llm_call(model="m", status="ok", prompt_tokens=1_000_000, completion_tokens=500_000)
    with SessionLocal() as s:
        cost = float(s.execute(select(LlmCall)).scalars().one().cost_usd)
    assert cost == pytest.approx(0.10 + 0.20)


def test_skipped_calls_are_recorded_with_zero_tokens(db):
    """Fallback'in neden devreye GİRMEDİĞİ de kayda geçmeli."""
    from store.observability import record_llm_call
    from store.queries import llm_cost_totals

    record_llm_call(model="m", status="disabled", error="LLM_FALLBACK_ENABLED=0")
    totals = llm_cost_totals()
    assert totals["calls"] == 1
    assert totals["total_tokens"] == 0
    assert totals["disabled_calls"] == 1


# ------------------------------------------------------------- budama ----

def test_purge_removes_old_http_requests_but_keeps_recent_ones(db, monkeypatch):
    import store.retention as retention

    monkeypatch.setattr(retention, "HTTP_REQUEST_DAYS", 30)
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        s.add_all(
            [
                HttpRequest(
                    method="GET", url="eski", outcome="ok",
                    created_at=(now - timedelta(days=45)).replace(tzinfo=None),
                ),
                HttpRequest(
                    method="GET", url="yeni", outcome="ok",
                    created_at=(now - timedelta(days=2)).replace(tzinfo=None),
                ),
            ]
        )
        s.commit()

    retention.purge(now=now)
    with SessionLocal() as s:
        urls = [r.url for r in s.execute(select(HttpRequest)).scalars().all()]
    assert urls == ["yeni"]


def test_purge_never_touches_llm_or_rate_change_history(db, monkeypatch):
    """Token muhasebesi ve oran değişim izi KÜMÜLATİF — budanmaz.

    "Bu yıl ne kadar harcadım" ve "bu oran en son ne zaman değişti"
    soruları silinen satırla cevaplanamaz.
    """
    import store.retention as retention

    now = datetime.now(timezone.utc)
    ancient = (now - timedelta(days=900)).replace(tzinfo=None)
    with SessionLocal() as s:
        s.add(LlmCall(model="m", status="ok", created_at=ancient))
        s.add(
            RateChange(
                dataset="deposit", institution="TEB", series_key="k",
                new_value=0.3, changed_at=ancient,
            )
        )
        s.commit()

    retention.purge(now=now)
    with SessionLocal() as s:
        assert len(s.execute(select(LlmCall)).scalars().all()) == 1
        assert len(s.execute(select(RateChange)).scalars().all()) == 1


def test_purge_survives_a_broken_database(db, monkeypatch):
    """Temizlik işinin başarısızlığı zamanlayıcıyı durdurmamalı."""
    import store.retention as retention

    def boom():
        raise RuntimeError("kilitli")

    monkeypatch.setattr(retention, "SessionLocal", boom)
    retention.purge()      # patlamamalı


# ------------------------------------------------- geçici ağ hatası / retry ----

def test_transient_network_error_is_retried_once_and_both_attempts_recorded(db, monkeypatch):
    """Canlı gözlem: Garanti Portföy ara sıra yanıt vermeden bağlantıyı kapatıyor.

    Bir sonraki istek sorunsuz çalışıyor — yani geçici. Tek denemede bırakmak
    o fonu günlük koşuda sebepsiz kaybettiriyordu. Her iki deneme de KAYDA
    GEÇMELİ; `attempt` sütununun varlık sebebi bu.
    """
    from collectors import http

    monkeypatch.setattr(http, "RETRY_BACKOFF_SECONDS", 0)
    calls = {"n": 0}

    def flaky(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, content=b"veri", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", flaky)
    resp = http.get("https://example.invalid/x")

    assert resp.content == b"veri"
    with SessionLocal() as s:
        rows = s.execute(select(HttpRequest).order_by(HttpRequest.id)).scalars().all()
    assert [(r.attempt, r.outcome) for r in rows] == [(1, "network_error"), (2, "ok")]


def test_http_500_is_not_retried(db, monkeypatch):
    """500 dönen bir uç noktayı dövmek ne sorunu çözer ne de nazik olur.

    Onun yeri tazelik telafisidir (scheduler.py), anlık yeniden deneme değil.
    """
    from collectors import http

    monkeypatch.setattr(http, "RETRY_BACKOFF_SECONDS", 0)
    calls = {"n": 0}

    def always_500(method, url, **kwargs):
        calls["n"] += 1
        return httpx.Response(500, content=b"", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", always_500)
    http.get("https://example.invalid/x")
    assert calls["n"] == 1


def test_persistent_network_error_still_raises_after_the_retry(db, monkeypatch):
    from collectors import http

    monkeypatch.setattr(http, "RETRY_BACKOFF_SECONDS", 0)

    def always_down(method, url, **kwargs):
        raise httpx.ConnectError("bağlanılamadı")

    monkeypatch.setattr(httpx, "request", always_down)
    with pytest.raises(httpx.ConnectError):
        http.get("https://example.invalid/x")

    with SessionLocal() as s:
        rows = s.execute(select(HttpRequest)).scalars().all()
    assert [r.attempt for r in rows] == [1, 2]


# --------------------------------------------------- zamanlayıcı nabzı ----

def test_panel_can_tell_a_stopped_scheduler_from_a_broken_source(db):
    """Panelin "çalışmıyor GİBİ" demesinin sebebi buydu: bilmiyordu.

    İki bambaşka arıza aynı mesajı üretiyordu — süreç hiç yok / süreç var
    ama kaynaklar düşmüş. Birincisinde çözüm "süreci başlat", ikincisinde
    "kaynak arızasına bak". Yanlış tarafı gösteren teşhis, teşhis değildir.
    """
    from store import heartbeat

    assert heartbeat.read() is None          # hiç çalışmamış
    assert heartbeat.is_alive() is False

    heartbeat.beat()
    state = heartbeat.read()
    assert state["alive"] is True
    assert state["age_seconds"] < 5


def test_a_stale_heartbeat_counts_as_dead(db, monkeypatch):
    """Süreç çökerse nabız donar; panel bunu "durmuş" diye okumalı."""
    from datetime import timedelta

    from sqlalchemy import select as _select

    from store import heartbeat
    from store.models import SchedulerHeartbeat

    heartbeat.beat()
    with SessionLocal() as s:
        row = s.execute(_select(SchedulerHeartbeat)).scalars().one()
        row.last_beat = row.last_beat - timedelta(hours=3)
        s.commit()

    state = heartbeat.read()
    assert state["alive"] is False
    assert state["age_seconds"] > heartbeat.STALE_AFTER_SECONDS


def test_second_scheduler_refuses_to_start_while_one_is_alive(db, monkeypatch):
    """İKİ zamanlayıcı aynı anda koşarsa bankalar iki kat istek alır.

    Tek konteynerli kurulumda `run.py` bir zamanlayıcı başlatıyor,
    compose'da ayrı bir servis var. Kilit bu çakışmayı çözer.
    """
    import os

    from store import heartbeat

    assert heartbeat.claim() is True          # ilk süreç sahiplenir

    # Başka bir süreç gibi davran: nabız canlı ama pid farklı.
    # (Gerçek pid ÖNCE yakalanmalı; lambda içinde os.getpid() çağırmak
    # yamanmış fonksiyonun kendisini çağırıp sonsuz özyineleme yapar.)
    baska_pid = os.getpid() + 1
    monkeypatch.setattr(os, "getpid", lambda: baska_pid)
    assert heartbeat.claim() is False


def test_claim_succeeds_when_the_previous_owner_is_stale(db, monkeypatch):
    """Konteyner yeniden başlarsa yeni süreç devralabilmeli."""
    from datetime import timedelta

    from sqlalchemy import select as _select

    from store import heartbeat
    from store.models import SchedulerHeartbeat

    heartbeat.beat()
    with SessionLocal() as s:
        row = s.execute(_select(SchedulerHeartbeat)).scalars().one()
        row.pid = 999999
        row.last_beat = row.last_beat - timedelta(hours=2)
        s.commit()

    assert heartbeat.claim() is True


def test_heartbeat_failure_never_breaks_collection(db, monkeypatch):
    """Nabız yazamamak toplamayı durdurmamalı (gözlem katmanı ilkesi)."""
    import store.heartbeat as hb

    def boom():
        raise RuntimeError("veritabanı kilitli")

    monkeypatch.setattr(hb, "SessionLocal", boom)
    hb.beat()                    # patlamamalı
    assert hb.read() is None     # okuma da sessizce None döner


def test_dead_owner_does_not_lock_out_a_new_scheduler(db, monkeypatch):
    """ÖLÜ SAHİP TUZAĞI — canlıda yakalandı (2026-08-26).

    Zamanlayıcı çökerse ya da konteyner yeniden başlarsa nabzı 3 dakika daha
    "canlı" görünür. O aralıkta başlayan yeni zamanlayıcı kendini kapatıyor
    ve panel toplayıcısız kalıyordu — tam da çözmeye çalıştığımız arıza.
    Aynı makinedeki bir nabızda pid'in gerçekten yaşadığı sınanmalı.
    """
    import os
    import socket

    from sqlalchemy import select as _select

    from store import heartbeat
    from store.models import SchedulerHeartbeat

    heartbeat.beat()
    with SessionLocal() as s:
        row = s.execute(_select(SchedulerHeartbeat)).scalars().one()
        row.pid = 999_999            # bu makinede var olmayan pid
        row.host = socket.gethostname()
        s.commit()

    state = heartbeat.read()
    assert state["alive"] is True     # zamana göre hâlâ taze
    assert heartbeat.claim() is True  # ama sahip ölü -> devralınır

    with SessionLocal() as s:
        assert s.execute(_select(SchedulerHeartbeat)).scalars().one().pid == os.getpid()


def test_a_live_owner_on_another_host_still_blocks(db, monkeypatch):
    """Farklı konteynerden gelen nabızda pid kontrolü anlamsızdır.

    Oradaki pid bizim makinemizde başka bir sürece ait olabilir; tek
    güvenilir ölçüt zaman aşımıdır.
    """
    from sqlalchemy import select as _select

    from store import heartbeat
    from store.models import SchedulerHeartbeat

    heartbeat.beat()
    with SessionLocal() as s:
        row = s.execute(_select(SchedulerHeartbeat)).scalars().one()
        row.pid = 999_999
        row.host = "baska-konteyner"
        s.commit()

    assert heartbeat.claim() is False
