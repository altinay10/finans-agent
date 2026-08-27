#!/usr/bin/env python3
"""Konteyner/sunucu içi zamanlayıcı — toplayıcıları kendi kendine tetikler.

Neden ayrı bir süreç: Streamlit paneli bankalara HİÇ gitmez, yalnızca
veritabanını okur (tasarım §01). Yani panel tek başına çalıştığında veri
asla tazelenmez. Toplama işini ya bu süreç ya da cron yapar.

Neden cron değil: Docker imajında cron yok ve olmasını istemiyoruz (cron +
konteyner ikilisi log ve sinyal yönetimini zorlaştırır). Bu dosya
`deploy/crontab.example`'daki planın aynısını Python'da uygular ve doğrudan
konteyner log'una yazar. cron tercih edilirse bu süreç hiç çalıştırılmaz.

ÜÇ AYRI GÜVENCE — sunucuya kurulup unutulacağı için hepsi gerekli:

1. PLAN        : her toplayıcı kendi saatinde koşar (aşağıdaki SCHEDULE).
2. AÇILIŞTA    : süreç başlarken bir kez hepsi çekilir. Yeni kurulan sistemin
                 boş panel göstermemesi ve yeniden başlatmada kaçırılan
                 saatin telafi edilmesi için. RUN_ON_START=0 ile kapatılır.
3. TAZELİK     : her CATCHUP_INTERVAL_MINUTES'ta bir, son BAŞARILI koşusu
                 kendi tazelik sınırını aşmış toplayıcılar yeniden denenir.
                 Bu olmadan, planlanan saatte bir hata alan toplayıcı ertesi
                 güne kadar bayat kalırdı ve kimse fark etmezdi.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from store.clock import ISTANBUL
from store import heartbeat
from store.db import SessionLocal, init_db
from store.retention import purge

from store.logging_setup import setup_logging

_log_path = setup_logging("scheduler")
logger = logging.getLogger("scheduler")
if _log_path:
    logger.info("log dosyası: %s", _log_path)

# (collector, saat, dakika) — saat None ise "HOURLY_WINDOW içinde her saat".
SCHEDULE: list[tuple[str, int | None, int]] = [
    ("fx_banks", None, 0),
    ("fx_tcmb", 16, 30),
    ("deposits", 20, 0),
    ("loan_rates", 20, 15),
    ("profit_shares", 20, 30),
    ("profit_shares_kt", 20, 35),
    ("participation_rates", 20, 40),
    ("participation_rates_kt", 20, 45),
    # Agent toplayıcısı GÜNDE BİR ve mesai saatinde: faiz kararları
    # çalışma saatlerinde açıklanıyor, gece çekmenin anlamı yok
    # (kullanıcı isteği, 2026-08-26).
    ("loan_rates_llm", 15, 0),
    ("funds", 21, 0),
]

HOURLY_WINDOW = range(9, 19)  # 09:00-18:00
WEEKDAYS = range(0, 5)        # Pazartesi-Cuma

# Bir toplayıcının verisi bu süreden eskiyse yeniden denenir. Kur gün içinde
# değişir, oranlar günlük yayınlanır — sınırlar buna göre. Hafta sonu ve
# tatilde banka veri yayınlamadığı için sınırlar cömert tutuldu; amaç
# "sürekli yeniden dene" değil, "sessizce bayatlamasın".
MAX_AGE_HOURS: dict[str, float] = {
    "fx_banks": 6,
    "fx_tcmb": 30,
    "deposits": 30,
    "loan_rates": 30,
    "profit_shares": 30,
    "profit_shares_kt": 30,
    "participation_rates": 30,
    "participation_rates_kt": 30,
    # Agent pahalı: tazelik telafisi 30 saat yerine 30 GÜN. Böylece
    # "bayat" diye yarım saatte bir yeniden çağrılıp token yakmaz;
    # planlanan 15:00 koşusu tek yetkilidir.
    "loan_rates_llm": 24 * 30,
    "funds": 30,
}

CATCHUP_INTERVAL_MINUTES = int(os.environ.get("CATCHUP_INTERVAL_MINUTES", "30"))
TICK_SECONDS = 20


def _run(name: str, trigger: str) -> bool:
    """`trigger`: 'schedule' | 'startup' | 'catchup'.

    scrape_runs'a yazılır. Sunucuya kurulduktan sonra erişim olmayacağı
    için, telafi mekanizmasının gerçekten devreye girip girmediğini
    gösterebilen tek kayıt budur (bkz. store/queries.py::run_triggers).
    """
    from llm import extract as llm_extract
    from worker import COLLECTORS

    llm_extract.reset_budget()
    try:
        result = COLLECTORS[name]().run(trigger=trigger)
        logger.info("%s (%s) -> %s", name, trigger, result)
        return bool(result.ok)
    except Exception as exc:  # noqa: BLE001 - zamanlayıcı asla ölmemeli
        logger.error("%s (%s) çalıştırılamadı: %s", name, trigger, exc)
        return False


def due_by_schedule(now: datetime) -> list[str]:
    """Bu dakikada planlanmış toplayıcılar."""
    if now.weekday() not in WEEKDAYS:
        return []
    due = []
    for name, hour, minute in SCHEDULE:
        if now.minute != minute:
            continue
        if hour is None:
            if now.hour in HOURLY_WINDOW:
                due.append(name)
        elif now.hour == hour:
            due.append(name)
    return due


def _db_name(key: str) -> str:
    """worker.py kayıt anahtarı -> scrape_runs.collector değeri.

    İKİSİ AYNI DEĞİL ve bu sessiz bir tuzak: worker'da anahtar `deposits`
    ama toplayıcının kendi `name`'i `deposit_rates`; `funds` -> `fund_prices`.
    Tazelik kontrolü anahtarla sorgulasaydı bu iki toplayıcıyı SÜREKLİ
    "hiç çalışmamış" sayar ve yarım saatte bir bankaları gereksiz yere
    yeniden çekerdi. Eşleme elle yazılmıyor, kayıt defterinden okunuyor ki
    yeni toplayıcı eklenince kayamasın.
    """
    from worker import COLLECTORS

    return COLLECTORS[key]().name


def max_age_by_db_name() -> dict[str, float]:
    """Tazelik sınırları, `scrape_runs.collector` adlarıyla anahtarlanmış.

    Panel scrape_runs'ı okur, worker anahtarlarını değil (bkz. _db_name).
    Eşlemeyi panelde ikinci kez yazmak, MAX_AGE_HOURS değiştiğinde sessizce
    kayan bir kopya üretirdi: panel "bayat" derken zamanlayıcı hiçbir şey
    yapmazdı.
    """
    return {_db_name(key): hours for key, hours in MAX_AGE_HOURS.items()}


def last_success_ages(now_utc: datetime | None = None) -> dict[str, float | None]:
    """Toplayıcı -> son BAŞARILI koşusunun kaç saat önce olduğu (None = hiç).

    Anahtarlar worker.py'nin anahtarlarıdır; sorgu ise scrape_runs'taki
    gerçek toplayıcı adıyla yapılır (bkz. _db_name).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    ages: dict[str, float | None] = {name: None for name, _, _ in SCHEDULE}
    by_db_name = {_db_name(key): key for key in ages}
    with SessionLocal() as session:
        rows = session.execute(
            text(
                "SELECT collector, MAX(finished_at) FROM scrape_runs "
                "WHERE status IN ('ok', 'llm_fallback') GROUP BY collector"
            )
        ).all()
    for db_name, finished_at in rows:
        collector = by_db_name.get(db_name)
        if collector is None or not finished_at:
            continue
        stamp = (
            datetime.fromisoformat(finished_at)
            if isinstance(finished_at, str)
            else finished_at
        )
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        ages[collector] = (now_utc - stamp).total_seconds() / 3600
    return ages


def due_by_staleness(now_utc: datetime | None = None) -> list[str]:
    """Tazelik sınırını aşmış toplayıcılar.

    Planlanan saatte hata alan bir toplayıcı bu sayede ertesi güne kadar
    bayat kalmaz.
    """
    stale = []
    for name, age in last_success_ages(now_utc).items():
        limit = MAX_AGE_HOURS.get(name)
        if limit is None:
            continue
        if age is None or age > limit:
            stale.append(name)
    return stale


def main() -> None:
    init_db()

    # Aynı anda İKİ zamanlayıcı koşmamalı: tek konteynerli kurulumda panel
    # kendi zamanlayıcısını başlatabiliyor, compose'da ayrı bir servis var.
    # İkisi birden koşarsa bankalar iki kat istek alır.
    if not heartbeat.claim():
        state = heartbeat.read() or {}
        logger.warning(
            "başka bir zamanlayıcı zaten çalışıyor (host=%s pid=%s) — çıkılıyor",
            state.get("host"), state.get("pid"),
        )
        return

    # Nabız arka planda atılır: toplama turları ana döngüyü dakikalarca
    # bloklar ve nabız ona bağlı olsaydı panel çalışan bir zamanlayıcıyı
    # "durmuş" sanardı (bkz. store/heartbeat.start_beating).
    heartbeat.start_beating()

    if os.environ.get("RUN_ON_START", "1") not in {"0", "false", "no"}:
        logger.info("açılışta ilk toplama (RUN_ON_START=0 ile kapatılır)")
        for name, _, _ in SCHEDULE:
            _run(name, "startup")

    logger.info(
        "zamanlayıcı başladı — plan: %s | tazelik telafisi: %d dakikada bir",
        [n for n, _, _ in SCHEDULE],
        CATCHUP_INTERVAL_MINUTES,
    )

    last_fired: tuple[int, int] | None = None
    next_catchup = datetime.now(timezone.utc) + timedelta(minutes=CATCHUP_INTERVAL_MINUTES)
    # Kayıt tabloları sınırsız büyüyemez; http_requests koşu başına onlarca
    # satır yazıyor. Sunucuda kimse elle temizleyemeyeceği için budama
    # zamanlayıcının işi (bkz. store/retention.py).
    next_purge = datetime.now(timezone.utc)

    while True:
        now = datetime.now(ISTANBUL)
        now_utc = datetime.now(timezone.utc)

        # Dakika başına bir kez: uyku kayması yüzünden aynı dakikada iki kez
        # tetiklenmeyi engeller.
        stamp = (now.hour, now.minute)
        if stamp != last_fired:
            for name in due_by_schedule(now):
                _run(name, "schedule")
            last_fired = stamp

        if now_utc >= next_catchup:
            stale = due_by_staleness(now_utc)
            if stale:
                logger.warning("tazelik sınırını aşanlar yeniden deneniyor: %s", stale)
                for name in stale:
                    _run(name, "catchup")
            next_catchup = now_utc + timedelta(minutes=CATCHUP_INTERVAL_MINUTES)

        if now_utc >= next_purge:
            purge()
            next_purge = now_utc + timedelta(days=1)

        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
