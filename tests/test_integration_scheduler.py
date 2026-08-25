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


def test_panel_warns_when_all_data_is_stale(db):
    """Sunucuda en sinsi hata: veri sessizce bayatlar ve kimse fark etmez.

    Panel, her şey eskiyse bunu "banka değişti" değil "toplayıcı koşmuyor"
    diye yorumlayıp açıkça söylemeli.
    """
    from datetime import datetime as _dt, timezone as _tz

    import app.main as main

    now = _dt.now(_tz.utc)
    fresh = (now - timedelta(hours=1)).isoformat()
    stale = (now - timedelta(hours=200)).isoformat()

    assert main._age_hours(fresh, now) == pytest.approx(1, abs=0.1)
    assert main._age_hours(stale, now) == pytest.approx(200, abs=0.1)
    assert main._age_hours(None, now) is None
    # Sınır, en cömert toplayıcı sınırının üstünde olmalı ki normal hafta
    # sonu bayatlaması uyarı üretmesin.
    assert main.NO_COLLECTOR_WARNING_HOURS > max(scheduler.MAX_AGE_HOURS.values())
