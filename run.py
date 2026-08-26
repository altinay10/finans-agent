#!/usr/bin/env python3
"""Konteyner giriş noktası — tek imaj, üç rol.

NEDEN VAR: panel veri toplamaz (tasarım §01); toplamayı `scheduler.py`
yapar. Doğru ayrım ama sunucuda EN SIK YAŞANAN ARIZAYI üretiyordu:
`docker run ... finans-agent` denince yalnızca panel kalkıyor, hiçbir şey
toplanmıyor ve site günlerce eskiyen veri gösteriyordu. Kullanıcı bunu
ancak tarihe bakarak fark ediyordu.

Panelin zamanlayıcıyı kendi başlatması denendi ve REDDEDİLDİ: Streamlit
betiği yalnızca bir tarayıcı bağlandığında çalışır. Yani "panel başlatsın"
demek "birisi siteyi açana kadar veri toplanmasın" demek olurdu — sunucuda
tam da olmaması gereken davranış.

ROLLER (`ROLE` ortam değişkeni):
  all       (VARSAYILAN) zamanlayıcı arka planda + panel önplanda.
            Tek konteynerli kurulum için: `docker run -p 8501:8501 imaj`
  panel     yalnızca Streamlit. docker-compose'da ayrı bir scheduler
            servisi olduğu için panel servisi bunu kullanır.
  scheduler yalnızca toplama döngüsü.

ÇİFT ÇALIŞMA RİSKİ YOK: `store/heartbeat.claim()` tek satırlık kilit tutar;
yanlışlıkla iki zamanlayıcı başlatılsa bile ikincisi kendini kapatır.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

STREAMLIT_CMD = [
    sys.executable, "-m", "streamlit", "run", "app/main.py",
    "--server.address", os.environ.get("PANEL_ADDRESS", "0.0.0.0"),
    "--server.port", os.environ.get("PANEL_PORT", "8501"),
    "--server.headless", "true",
    "--browser.gatherUsageStats", "false",
]
SCHEDULER_CMD = [sys.executable, "-u", "scheduler.py"]

# Zamanlayıcı çocuğu ölmüşse ne kadar sonra yeniden denensin.
# store.heartbeat.STALE_AFTER_SECONDS'ten uzun olmalı ki ölü bir
# kilidin süresi dolmuş olsun.
SUPERVISE_INTERVAL_SECONDS = int(os.environ.get("SUPERVISE_INTERVAL_SECONDS", "60"))


def main() -> int:
    role = os.environ.get("ROLE", "all").strip().lower()
    if role not in {"all", "panel", "scheduler"}:
        print(f"Bilinmeyen ROLE={role!r}. Geçerli: all | panel | scheduler", file=sys.stderr)
        return 2

    if role == "scheduler":
        return subprocess.call(SCHEDULER_CMD, cwd=REPO_ROOT)

    state: dict = {"child": None, "stop": False}
    if role == "all":
        # Zamanlayıcı ARKA PLANDA, panel önplanda. Konteynerin ömrü panele
        # bağlı kalsın ki healthcheck anlamlı olsun.
        _spawn_scheduler(state)

        def _stop(signum, _frame):
            # Konteyner durdurulurken zamanlayıcı yetim kalmasın; yetim
            # süreç, bir sonraki açılışta kilidi tutan ölü bir nabız demek.
            state["stop"] = True
            child = state["child"]
            if child and child.poll() is None:
                child.terminate()
            sys.exit(128 + signum)

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        # GÖZETİM: zamanlayıcı çocuğu ölürse yeniden başlatılır.
        # Olmazsa tek bir çıkış (ör. ölü bir kilidi görüp çekilmek) paneli
        # kalıcı olarak toplayıcısız bırakırdı — çözmeye çalıştığımız
        # arızanın ta kendisi.
        threading.Thread(target=_supervise, args=(state,), daemon=True).start()

    try:
        return subprocess.call(STREAMLIT_CMD, cwd=REPO_ROOT)
    finally:
        state["stop"] = True
        child = state["child"]
        if child and child.poll() is None:
            child.terminate()


def _spawn_scheduler(state: dict) -> None:
    child = subprocess.Popen(SCHEDULER_CMD, cwd=REPO_ROOT)
    state["child"] = child
    print(f"[run.py] zamanlayıcı başlatıldı (pid {child.pid})", flush=True)


def _supervise(state: dict) -> None:
    """Zamanlayıcı çocuğunu diri tutar.

    Bekleme süresi, nabzın bayatlaması için gereken süreden uzun tutuluyor:
    çocuk "başka zamanlayıcı çalışıyor" diyip çıktıysa ve o zamanlayıcı
    gerçekten yaşıyorsa tekrar denemek zararsız (yine çekilir); ölmüşse
    bu bekleme sonunda kilit devralınabilir hâle gelir.
    """
    while not state["stop"]:
        time.sleep(SUPERVISE_INTERVAL_SECONDS)
        if state["stop"]:
            return
        child = state["child"]
        if child is not None and child.poll() is not None:
            print(
                f"[run.py] zamanlayıcı çıktı (kod {child.returncode}) — yeniden başlatılıyor",
                flush=True,
            )
            _spawn_scheduler(state)


if __name__ == "__main__":
    raise SystemExit(main())
