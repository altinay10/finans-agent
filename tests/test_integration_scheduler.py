"""Zamanlayıcı entegrasyon testleri — AĞSIZ.

Bu dosya sunucuya kurulup unutulacak bir sistemin en kritik parçasını
korur: veri kendi kendine tazelenmezse panel sessizce yanlış olur ve kimse
fark etmez.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from store.clock import ISTANBUL
from store.db import SessionLocal
from store.models import ScrapeRun

import scheduler


# ------------------------------------------------------------ plan (saat) ----


@pytest.mark.parametrize(
    "moment, expected",
    [
        ((2026, 8, 24, 10, 0), ["fx_banks"]),      # Pazartesi mesai içi, saat başı
        ((2026, 8, 24, 16, 30), ["fx_tcmb"]),
        ((2026, 8, 24, 20, 0), ["deposits"]),
        ((2026, 8, 24, 20, 15), ["loan_rates"]),
        ((2026, 8, 24, 20, 30), ["profit_shares"]),
        ((2026, 8, 24, 20, 35), ["profit_shares_kt"]),
        ((2026, 8, 24, 21, 0), ["funds"]),
        ((2026, 8, 24, 3, 0), []),                 # gece: kur çekmenin anlamı yok
        ((2026, 8, 24, 10, 17), []),               # saat başı değil
        ((2026, 8, 29, 10, 0), []),                # Cumartesi
        ((2026, 8, 30, 20, 0), []),                # Pazar
    ],
)
def test_schedule_fires_only_at_the_right_moment(moment, expected):
    now = datetime(*moment, tzinfo=ISTANBUL)
    assert scheduler.due_by_schedule(now) == expected


def test_every_scheduled_collector_exists_in_the_worker_registry():
    """Plana yazılıp worker'da olmayan bir ad sessizce hiç çalışmaz."""
    from worker import COLLECTORS

    planned = {name for name, _, _ in scheduler.SCHEDULE}
    assert planned <= set(COLLECTORS), f"worker'da yok: {planned - set(COLLECTORS)}"


def test_every_collector_is_scheduled():
    """Tersi de doğru olmalı: kayıtlı ama planlanmamış toplayıcı unutulmuş demektir."""
    from worker import COLLECTORS

    planned = {name for name, _, _ in scheduler.SCHEDULE}
    assert set(COLLECTORS) <= planned, f"planda yok: {set(COLLECTORS) - planned}"


def test_every_scheduled_collector_has_a_staleness_limit():
    planned = {name for name, _, _ in scheduler.SCHEDULE}
    assert planned == set(scheduler.MAX_AGE_HOURS)


# ------------------------------------------------- tazelik telafisi (catch-up) ----


def test_worker_key_maps_to_the_real_collector_name():
    """worker anahtarı ile scrape_runs.collector AYNI DEĞİL.

    `deposits` -> `deposit_rates`, `funds` -> `fund_prices`. Tazelik kontrolü
    anahtarla sorgulasaydı bu ikisini sürekli "hiç çalışmamış" sayar ve
    yarım saatte bir bankaları gereksiz yere yeniden çekerdi.
    """
    assert scheduler._db_name("deposits") == "deposit_rates"
    assert scheduler._db_name("funds") == "fund_prices"
    assert scheduler._db_name("fx_banks") == "fx_banks"


def _add_run(collector: str, hours_ago: float, status: str = "ok") -> None:
    finished = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    with SessionLocal() as s:
        s.add(
            ScrapeRun(
                collector=collector,
                status=status,
                started_at=finished,
                finished_at=finished,
                rows_written=1,
            )
        )
        s.commit()


def test_fresh_collectors_are_not_retried(db):
    for key in scheduler.MAX_AGE_HOURS:
        _add_run(scheduler._db_name(key), hours_ago=0.1)
    assert scheduler.due_by_staleness() == []


def test_stale_collector_is_retried(db):
    for key in scheduler.MAX_AGE_HOURS:
        _add_run(scheduler._db_name(key), hours_ago=0.1)
    # fx_banks sınırı 6 saat.
    _add_run(scheduler._db_name("fx_banks"), hours_ago=0.1)
    assert "fx_banks" not in scheduler.due_by_staleness()

    with SessionLocal() as s:
        s.query(ScrapeRun).filter(ScrapeRun.collector == "fx_banks").delete()
        s.commit()
    _add_run("fx_banks", hours_ago=8)
    assert "fx_banks" in scheduler.due_by_staleness()


def test_never_run_collector_is_retried(db):
    """Hiç çalışmamış toplayıcı da telafi listesine girmeli (ilk kurulum)."""
    assert set(scheduler.due_by_staleness()) == set(scheduler.MAX_AGE_HOURS)


def test_failed_runs_do_not_count_as_fresh(db):
    """Başarısız koşu tazelik sayılmamalı — yoksa kırık toplayıcı hiç denenmez."""
    for key in scheduler.MAX_AGE_HOURS:
        _add_run(scheduler._db_name(key), hours_ago=0.1)
    with SessionLocal() as s:
        s.query(ScrapeRun).filter(ScrapeRun.collector == "loan_rates").delete()
        s.commit()
    _add_run("loan_rates", hours_ago=0.1, status="failed")
    assert "loan_rates" in scheduler.due_by_staleness()


def test_llm_fallback_run_counts_as_fresh(db):
    """LLM ile kurtarılmış koşu da başarılıdır; tekrar tekrar denenmemeli."""
    for key in scheduler.MAX_AGE_HOURS:
        _add_run(scheduler._db_name(key), hours_ago=0.1)
    with SessionLocal() as s:
        s.query(ScrapeRun).filter(ScrapeRun.collector == "fx_tcmb").delete()
        s.commit()
    _add_run("fx_tcmb", hours_ago=0.1, status="llm_fallback")
    assert "fx_tcmb" not in scheduler.due_by_staleness()


# ------------------------------------------- "kimse toplamıyor" uyarısı ----


def test_panel_and_scheduler_share_one_staleness_limit(db):
    """Panelin "bayat" eşiği ile zamanlayıcının "yeniden dene" eşiği AYNI olmalı.

    İki ayrı sabit tutulsaydı panel kırmızı gösterirken zamanlayıcı hiçbir
    şey yapmaz, kullanıcı da düzelmeyen bir uyarıya bakardı. Panel bu yüzden
    sabit bir saat sayısı değil, scheduler.MAX_AGE_HOURS'u okuyor — ama
    scrape_runs'taki ADLARLA (worker anahtarı değil, bkz. _db_name).
    """
    from app.panels.status import GROUPS

    limits = scheduler.max_age_by_db_name()
    assert set(limits.values()) == set(scheduler.MAX_AGE_HOURS.values())

    # Panelin gruplarındaki her toplayıcının bir sınırı OLMALI: sınırsız
    # kalan bir toplayıcı hiç "bayat" sayılmaz ve sessizce eskir.
    gruplananlar = {name for _, members in GROUPS for name in members}
    assert gruplananlar <= set(limits), (
        f"panelde sınırsız toplayıcı var: {gruplananlar - set(limits)}"
    )
    # Tersi de: plandaki her toplayıcı panelde bir gruba düşmeli, yoksa
    # arızası üst şeritte hiç görünmez.
    assert set(limits) <= gruplananlar, (
        f"panelde gösterilmeyen toplayıcı var: {set(limits) - gruplananlar}"
    )

    assert scheduler.MAX_AGE_HOURS["loan_rates_llm"] > 24 * 7, (
        "agent toplayıcısı tazelik telafisiyle sık sık çağrılmamalı — token yakar"
    )


def test_startup_only_collects_what_is_actually_stale(db):
    """Her yeniden başlatmada agent'ı koşturmak boşuna token yakıyordu.

    Agent'ın tazelik sınırı tam da bunun için 30 GÜNE çekilmişken, açılışta
    koşulsuz "hepsini çek" demek konteyner her yeniden başladığında bir tur
    LLM faturası üretiyordu (canlı gözlendi: restart başına 4 çağrı).

    Maddenin asıl amacı korunmalı: BOŞ veritabanında hepsi çalışmalı ki
    yeni kurulan sistem boş panel göstermesin.
    """
    assert set(scheduler.due_by_staleness()) == {n for n, _, _ in scheduler.SCHEDULE}, (
        "boş veritabanında hepsi bayat sayılmalı — yeni kurulum boş panel göstermesin"
    )

    _add_run("loan_rates_llm", hours_ago=1)
    assert "loan_rates_llm" not in scheduler.due_by_staleness(), (
        "1 saat önce başarıyla koşmuş agent yeniden çağrılmamalı"
    )


# ----------------------------------------------- kilidin bırakılması ----


def test_release_clears_lock_so_next_scheduler_starts_immediately(seeded_db):
    """Düzgün kapanan zamanlayıcı kilidi bırakmalı.

    Bırakmazsa nabız 180 sn daha "canlı" görünür. Aynı makinede claim()
    pid kontrolüyle devralıyor, ama FARKLI HOST'tan bakan bir zamanlayıcı
    için tek ölçüt zaman aşımı — konteynerde her yeniden dağıtım yeni bir
    hostname ürettiği için bu, her deploy'da üç dakikalık crash loop
    demekti (2026-09-04'te canlıda görüldü).
    """
    from store import heartbeat

    assert heartbeat.claim() is True
    assert heartbeat.is_alive() is True

    heartbeat.release()

    assert heartbeat.read() is None
    # Kilit boş: yeni bir zamanlayıcı beklemeden sahiplenebilir.
    assert heartbeat.claim() is True


def test_release_does_not_steal_a_lock_owned_by_someone_else(seeded_db):
    """Sahiplik kontrolü olmasaydı, kilidi kaybetmiş bir süreç kapanırken
    ÇALIŞAN zamanlayıcının kilidini silerdi."""
    import socket

    from store import heartbeat
    from store.db import SessionLocal
    from store.models import SchedulerHeartbeat

    assert heartbeat.claim() is True
    with SessionLocal() as session:
        row = session.get(SchedulerHeartbeat, heartbeat.ROW_ID)
        row.pid = 999999          # kilit artık başkasında
        row.host = socket.gethostname()
        session.commit()

    heartbeat.release()

    state = heartbeat.read()
    assert state is not None, "başkasının kilidi silinmemeli"
    assert state["pid"] == 999999


def test_release_is_safe_when_no_lock_exists(seeded_db):
    from store import heartbeat

    heartbeat.release()   # patlamamalı
    assert heartbeat.read() is None


def test_claim_does_not_mistake_another_host_pid1_for_itself(seeded_db, monkeypatch):
    """Konteynerde zamanlayıcı HER ZAMAN pid 1.

    Kimlik yalnızca pid'e bakarsa, başka bir konteynerdeki CANLI
    zamanlayıcının nabzı (host=X, pid=1) bu sürece (host=Y, pid=1) "benim
    nabzım" gibi görünür ve kilit doğrudan alınır. İki zamanlayıcı birden
    koşar, bankalar iki kat istek alır — kilidin engellemesi gereken tam
    olarak bu (2026-09-04).
    """
    import os
    import socket

    from store import heartbeat
    from store.db import SessionLocal
    from store.models import SchedulerHeartbeat
    from store.clock import utc_now

    # Başka bir konteynerin TAZE nabzı: farklı host, aynı pid.
    with SessionLocal() as session:
        session.add(
            SchedulerHeartbeat(
                id=heartbeat.ROW_ID,
                host="baska-konteyner",
                pid=os.getpid(),
                started_at=utc_now(),
                last_beat=utc_now(),
            )
        )
        session.commit()

    monkeypatch.setattr(socket, "gethostname", lambda: "bu-konteyner")
    assert heartbeat.claim() is False, "başka host'un canlı kilidi çalındı"
