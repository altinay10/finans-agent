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
import signal
import threading
from functools import lru_cache
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from llm import settings as llm_settings
from store.clock import ISTANBUL
from store import heartbeat
from store import llm_backoff
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
    # Agent toplayıcısı mesai saatinde: faiz kararları çalışma saatlerinde
    # açıklanıyor, gece çekmenin anlamı yok (kullanıcı isteği, 2026-08-26).
    # Sıklığı 2026-09-06'da HAFTADA İKİ'ye düşürüldü — bkz. AGENT_GUNLERI.
    ("loan_rates_llm", 15, 0),
    ("funds", 21, 0),
]

HOURLY_WINDOW = range(9, 19)  # 09:00-18:00
WEEKDAYS = range(0, 5)        # Pazartesi-Cuma

# TOPLAYICIYA ÖZEL GÜN KISITI. Yazılmayan toplayıcı için WEEKDAYS geçerli.
#
# NEDEN AYRI SÖZLÜK, SCHEDULE'a dördüncü alan DEĞİL: SCHEDULE üçlü olarak
# üç ayrı yerde açılıyor (`due_by_schedule` ve iki test). Dördüncü alan
# eklemek onları ValueError ile düşürürdü; kısıtı ayrı tutmak hem geriye
# dönük uyumlu hem de "istisnası olan tek toplayıcı" gerçeğini görünür
# kılıyor.
#
# NEDEN HAFTADA İKİ: agent tek toplayıcı içinde banka BAŞINA bir LLM çağrısı
# yapıyor (şu an 12 aktif sayfa). Günlük koşu haftada 60 çağrı demekti;
# bankalar pazarlama sayfalarındaki oranları bu sıklıkta değiştirmiyor.
# Pazartesi hafta açılışını, Perşembe hafta ortası değişimini yakalıyor
# (kullanıcı kararı, 2026-09-06).
AGENT_GUNLERI = frozenset({0, 3})  # Pazartesi, Perşembe
SCHEDULE_WEEKDAYS: dict[str, frozenset[int]] = {
    "loan_rates_llm": AGENT_GUNLERI,
}


def allowed_weekdays(name: str) -> frozenset[int]:
    """Bu toplayıcı hangi günler koşabilir."""
    return SCHEDULE_WEEKDAYS.get(name, frozenset(WEEKDAYS))

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

# DÜZGÜN KAPANMA — konteynerde bu süreç PID 1 olarak koşuyor ve PID 1'de
# sinyallerin VARSAYILAN davranışı çekirdek tarafından YOK SAYILIR; işleyici
# kurulmadıkça SIGTERM hiçbir şey yapmaz. Sonuç canlıda görüldü:
# `docker compose stop` 10 saniyelik süreyi baştan sona bekleyip konteyneri
# SIGKILL ile öldürüyordu (çıkış kodu 137) — hem de toplama turunun tam
# ortasında olabilirdi. `run.py` da çocuğu terminate() ile durduruyor;
# işleyici olmadan orada da aynı sert kesme yaşanıyordu.
#
# Bayrak time.sleep yerine Event.wait ile bekletir: sinyal geldiği anda
# uykudan çıkılır, 20 saniyelik tur sonu beklenmez.
_shutdown = threading.Event()


def _install_signal_handlers() -> None:
    def _stop(signum, _frame):
        logger.info("sinyal alındı (%s) — tur bitince kapanılıyor", signum)
        _shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except ValueError:
            # signal.signal yalnızca ana iş parçacığında çalışır. Zamanlayıcı
            # bir gün başka bir sürecin içinden iş parçacığı olarak
            # başlatılırsa burada patlamak yerine sessizce eski davranışa
            # dönmek doğru: kapanmayı o zaman dış süreç yönetir.
            logger.debug("%s işleyicisi kurulamadı (ana iş parçacığı değil)", sig)


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
        if now.weekday() not in allowed_weekdays(name):
            continue
        if hour is None:
            if now.hour in HOURLY_WINDOW:
                due.append(name)
        elif now.hour == hour:
            due.append(name)
    return due


@lru_cache(maxsize=None)
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

    # ÖNBELLEKLİ: bu fonksiyon toplayıcıyı GERÇEKTEN inşa ediyor ve
    # `LlmLoanRateCollector.__init__` her seferinde sources.yaml okuyor.
    # Geri çekilme kontrolü 20 saniyede bir koştuğu için önbelleksiz hâli
    # saatte yüzlerce gereksiz YAML ayrıştırması demekti. Eşleme statik.
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
    # GERİ ÇEKİLMEDEKİLER BURADA ATLANIR. Kırık bir ayrıştırıcının son
    # başarılı koşusu tanım gereği eskidir, yani bu liste onu her 30
    # dakikada bir "bayat" diye döndürürdü. Geri çekilme mekanizması onun
    # temposunu zaten yönetiyor; iki mekanizma birden ateşlerse 5 dakikalık
    # aralık fiilen 30 dakikada bir fazladan çağrıya dönüşür.
    geri_cekilen = llm_backoff.backing_off()
    stale = []
    for name, age in last_success_ages(now_utc).items():
        limit = MAX_AGE_HOURS.get(name)
        if limit is None:
            continue
        if _db_name(name) in geri_cekilen:
            continue
        if age is None or age > limit:
            stale.append(name)
    return stale


def due_by_backoff(now: datetime, now_utc: datetime | None = None) -> list[str]:
    """Geri çekilme sırası gelen toplayıcılar.

    `now` YEREL saat (gün kısıtı için), `now_utc` ise aralık hesabı için.
    Gün kısıtı burada da geçerli: agent yalnızca Pazartesi/Perşembe
    koşuyorsa, kırıldığında da yalnızca o günlerde yeniden denenmeli —
    aksi halde haftada iki güne indirdiğimiz toplayıcı, arızalandığı anda
    her gün koşan bir toplayıcıya dönüşürdü.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    if now.weekday() not in WEEKDAYS:
        return []
    durumlar = llm_backoff.read_all()
    due = []
    for name, _, _ in SCHEDULE:
        if now.weekday() not in allowed_weekdays(name):
            continue
        state = durumlar.get(_db_name(name))
        if state is None:
            continue
        if llm_backoff.is_due(state, now_utc):
            due.append(name)
    return due


def main() -> None:
    _install_signal_handlers()
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
        # Açılışta HEPSİNİ değil, yalnızca BAYAT olanları çek. Bu maddenin
        # amacı "yeni kurulan sistem boş panel göstermesin"di ve o amaç
        # korunuyor: boş veritabanında hiçbir toplayıcının başarılı koşusu
        # yok, dolayısıyla hepsi bayat sayılıp hepsi çalışır.
        #
        # Fark, YENİDEN başlatmada ortaya çıkıyor. Eskiden her restart tüm
        # toplayıcıları koşturuyordu — agent dahil. Agent'ın tazelik sınırı
        # tam da token yakmasın diye 30 GÜN'e çekilmişken, konteyneri üç kez
        # yeniden başlatmak üç tur LLM faturası demekti (canlı gözlendi:
        # her restartta 4 çağrı).
        acilista = due_by_staleness()
        if acilista:
            logger.info("açılışta bayat olanlar çekiliyor: %s", acilista)
            for name in acilista:
                if _shutdown.is_set():
                    logger.info("kapanma istendi — açılış turu yarıda bırakıldı")
                    break
                _run(name, "startup")
        else:
            logger.info("açılışta çekilecek bayat toplayıcı yok — plan bekleniyor")

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

    while not _shutdown.is_set():
        now = datetime.now(ISTANBUL)
        now_utc = datetime.now(timezone.utc)

        # Panelden girilen anahtar bu süreci de bulsun. Ayarlar süreç
        # AÇILIRKEN bir kez okunuyordu; kullanıcı .env'e yeni bir anahtar
        # yazdığında zamanlayıcı onu ancak yeniden başlatılınca görürdü ve
        # panelde "kaydettim" yazarken planlı koşular eski anahtarla
        # başarısız olmaya devam ederdi. Tur başına bir dosya okuması.
        llm_settings.reload_from_env()

        # Dakika başına bir kez: uyku kayması yüzünden aynı dakikada iki kez
        # tetiklenmeyi engeller.
        stamp = (now.hour, now.minute)
        kosanlar: set[str] = set()
        if stamp != last_fired:
            for name in due_by_schedule(now):
                if _shutdown.is_set():
                    break
                _run(name, "schedule")
                kosanlar.add(name)
            last_fired = stamp

        # GERİ ÇEKİLME TURU — her turda bakılır (20 sn), çünkü 5 dakikalık
        # aralık 30 dakikalık telafi turuna sığmaz. Sorgu ucuz: tek küçük
        # tablo okuması, sırası gelen yoksa hiçbir şey yapılmaz.
        for name in due_by_backoff(now, now_utc):
            if _shutdown.is_set():
                break
            if name in kosanlar:
                # Planlı saat ile geri çekilme sırası aynı ana denk geldi;
                # aynı toplayıcıyı arka arkaya iki kez koşturmanın anlamı yok.
                continue
            _run(name, "backoff")
            kosanlar.add(name)

        if now_utc >= next_catchup:
            stale = due_by_staleness(now_utc)
            if stale:
                logger.warning("tazelik sınırını aşanlar yeniden deneniyor: %s", stale)
                for name in stale:
                    if _shutdown.is_set():
                        break
                    if name in kosanlar:
                        continue
                    _run(name, "catchup")
            next_catchup = now_utc + timedelta(minutes=CATCHUP_INTERVAL_MINUTES)

        if now_utc >= next_purge:
            purge()
            next_purge = now_utc + timedelta(days=1)

        _shutdown.wait(TICK_SECONDS)

    # Kilidi bırak ki bir sonraki zamanlayıcı 180 sn beklemesin. Konteynerde
    # bu, her yeniden dağıtımdaki üç dakikalık crash loop'un tek çaresi
    # (bkz. store/heartbeat.release).
    heartbeat.release()
    logger.info("zamanlayıcı düzgün kapandı")


if __name__ == "__main__":
    main()
