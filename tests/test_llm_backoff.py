"""Geri çekilme (backoff) ve kalıcı anahtar zinciri.

Buradaki testlerin çoğu bir REGRESYONU bekçiliyor: tasarım gözden
geçirilirken bulunan, uygulansaydı canlıda token ve istek yakacak
davranışlar. Her testin başlığında hangisi olduğu yazıyor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import scheduler
from store import llm_backoff
from store.models import LlmFallbackState

ISTANBUL = scheduler.ISTANBUL


# ----------------------------------------------------------- aralıklar ----


def test_first_failures_retry_quickly():
    """İlk 6 başarısızlıkta 5 dakika: geçici arıza hızla toparlansın."""
    for sayac in range(1, 6):
        state = llm_backoff.BackoffState("fx_banks", sayac, None)
        assert state.interval() == timedelta(minutes=5), f"sayaç={sayac}"


def test_persistent_failure_backs_off_to_four_hours():
    """6. başarısızlıktan sonra 4 saat: kalıcı arıza insan eli bekler."""
    for sayac in (6, 7, 40):
        state = llm_backoff.BackoffState("fx_banks", sayac, None)
        assert state.interval() == timedelta(hours=4), f"sayaç={sayac}"


def test_a_collector_is_not_retried_before_its_interval():
    simdi = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    state = llm_backoff.BackoffState("fx_banks", 1, simdi - timedelta(minutes=3))
    assert llm_backoff.is_due(state, simdi) is False


def test_a_collector_is_retried_once_the_interval_passed():
    simdi = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    state = llm_backoff.BackoffState("fx_banks", 1, simdi - timedelta(minutes=6))
    assert llm_backoff.is_due(state, simdi) is True


def test_a_healthy_collector_is_never_due():
    """Sayaç sıfırsa geri çekilme yok — normal plan geçerli."""
    state = llm_backoff.BackoffState("fx_banks", 0, datetime.now(timezone.utc))
    assert llm_backoff.is_due(state) is False


# ------------------------------------------------------- sayaç kuralı ----


def test_manual_runs_do_not_arm_the_backoff():
    """REGRESYON: ziyaretçinin hatası sunucunun faturasına yazılmamalı.

    Panelin "Şimdi tazele" düğmesi ziyaretçinin KENDİ anahtarıyla koşuyor.
    O koşunun başarısızlığı sayacı armasaydı bile, zamanlayıcı bunu görüp
    SUNUCUNUN anahtarıyla 5 dakikada bir yeniden denemeye başlardı — yani
    "panel sunucunun anahtarını harcayamaz" kuralı arka kapıdan delinirdi.
    """
    assert llm_backoff.counts_for_backoff("manual") is False
    for tetik in ("schedule", "startup", "catchup", "backoff"):
        assert llm_backoff.counts_for_backoff(tetik) is True, tetik


def test_a_disabled_agent_does_not_arm_the_backoff():
    """REGRESYON: LLM kapalıyken 12 banka 5 dakikada bir çekilmemeli.

    `loan_rates_llm.parse()` anahtar yokken ParseError atıyor ve bu,
    ayrıştırma aşamasında düşen bir koşu olarak bitiyor. Bunu gerçek arıza
    saymak, anahtarı olmayan HER kurulumda bankaları sürekli dövmek
    demekti — üstelik tek token bile harcanmadan.
    """
    from collectors.base import _llm_gercekten_denendi
    from llm.extract import LlmBudgetExceeded, LlmDisabled

    assert _llm_gercekten_denendi(LlmDisabled("kapalı")) is False
    assert _llm_gercekten_denendi(LlmBudgetExceeded("bütçe")) is False
    # Model gerçekten çağrıldı ve beceremedi: bu gerçek arıza.
    assert _llm_gercekten_denendi(ValueError("yanıt ayrıştırılamadı")) is True


def test_failures_accumulate_and_success_resets(db):
    llm_backoff.record_failure("fx_banks", "deneme")
    llm_backoff.record_failure("fx_banks", "deneme")
    assert llm_backoff.read_all()["fx_banks"].consecutive_failures == 2
    assert "fx_banks" in llm_backoff.backing_off()

    llm_backoff.record_success("fx_banks")
    assert llm_backoff.read_all()["fx_banks"].consecutive_failures == 0
    assert "fx_banks" not in llm_backoff.backing_off()


# ------------------------------------------------------- zamanlayıcı ----


def test_the_agent_only_runs_on_monday_and_thursday():
    """Haftada iki: Pazartesi hafta açılışı, Perşembe hafta ortası."""
    # 2026-09-07 Pazartesi, 09-10 Perşembe; 08 Salı, 09 Çarşamba, 11 Cuma.
    for gun, bekleniyor in ((7, True), (8, False), (9, False), (10, True), (11, False)):
        simdi = datetime(2026, 9, gun, 15, 0, tzinfo=ISTANBUL)
        cikan = "loan_rates_llm" in scheduler.due_by_schedule(simdi)
        assert cikan is bekleniyor, f"2026-09-{gun:02d} için beklenen {bekleniyor}"


def test_the_other_collectors_still_run_every_weekday():
    """Gün kısıtı YALNIZCA agent'a: diğerleri Pzt-Cuma değişmedi."""
    for gun in (7, 8, 9, 10, 11):
        simdi = datetime(2026, 9, gun, 20, 0, tzinfo=ISTANBUL)
        assert "deposits" in scheduler.due_by_schedule(simdi), f"2026-09-{gun:02d}"


def test_the_schedule_tuple_shape_is_unchanged():
    """SCHEDULE üçlü kalmalı — gün kısıtı ayrı sözlükte.

    Dördüncü alan eklemek `due_by_schedule` dışında iki testi daha
    ValueError ile düşürürdü; kısıt bu yüzden SCHEDULE_WEEKDAYS'te.
    """
    for girdi in scheduler.SCHEDULE:
        assert len(girdi) == 3


def test_backoff_retries_respect_the_agent_days(db):
    """Arızalanan agent her gün koşan bir toplayıcıya DÖNÜŞMEMELİ."""
    llm_backoff.record_failure("loan_rates_llm", "model beceremedi")
    eski = datetime.now(timezone.utc) - timedelta(hours=9)
    with scheduler.SessionLocal() as session:
        row = session.get(LlmFallbackState, "loan_rates_llm")
        row.last_attempt_at = eski
        session.commit()

    sali = datetime(2026, 9, 8, 11, 0, tzinfo=ISTANBUL)
    persembe = datetime(2026, 9, 10, 11, 0, tzinfo=ISTANBUL)
    assert scheduler.due_by_backoff(sali) == []
    assert "loan_rates_llm" in scheduler.due_by_backoff(persembe)


def test_backoff_does_not_run_at_the_weekend(db):
    llm_backoff.record_failure("fx_banks", "deneme")
    with scheduler.SessionLocal() as session:
        row = session.get(LlmFallbackState, "fx_banks")
        row.last_attempt_at = datetime.now(timezone.utc) - timedelta(hours=9)
        session.commit()
    cumartesi = datetime(2026, 9, 12, 11, 0, tzinfo=ISTANBUL)
    assert scheduler.due_by_backoff(cumartesi) == []


def test_staleness_skips_collectors_that_are_backing_off(db):
    """İKİ MEKANİZMA AYNI ANDA ATEŞLEMEMELİ.

    Kırık bir ayrıştırıcının son başarılı koşusu tanım gereği eskidir, yani
    tazelik telafisi onu her 30 dakikada bir "bayat" diye döndürürdü. Geri
    çekilme temposunu zaten yönetiyorken bu, fazladan çağrı demekti.
    """
    assert "fx_banks" in scheduler.due_by_staleness()  # hiç koşmamış: bayat
    llm_backoff.record_failure("fx_banks", "deneme")
    assert "fx_banks" not in scheduler.due_by_staleness()


def test_the_name_mapping_is_cached():
    """`_db_name` toplayıcıyı İNŞA ediyor; 20 saniyede bir çağrılıyor.

    Önbelleksiz hâli `LlmLoanRateCollector.__init__` üzerinden saatte
    yüzlerce gereksiz sources.yaml ayrıştırması demekti.
    """
    scheduler._db_name.cache_clear()
    scheduler._db_name("deposits")
    scheduler._db_name("deposits")
    assert scheduler._db_name.cache_info().hits >= 1
