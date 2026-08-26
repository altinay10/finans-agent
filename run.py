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


def main() -> int:
    role = os.environ.get("ROLE", "all").strip().lower()
    if role not in {"all", "panel", "scheduler"}:
        print(f"Bilinmeyen ROLE={role!r}. Geçerli: all | panel | scheduler", file=sys.stderr)
        return 2

    if role == "scheduler":
        return subprocess.call(SCHEDULER_CMD, cwd=REPO_ROOT)

    child: subprocess.Popen | None = None
    if role == "all":
        # Zamanlayıcı ARKA PLANDA, panel önplanda. Konteynerin ömrü panele
        # bağlı kalsın ki healthcheck anlamlı olsun.
        child = subprocess.Popen(SCHEDULER_CMD, cwd=REPO_ROOT)
        print(f"[run.py] zamanlayıcı başlatıldı (pid {child.pid})", flush=True)

        def _stop(signum, _frame):
            # Konteyner durdurulurken zamanlayıcı yetim kalmasın; yetim
            # süreç, bir sonraki açılışta kilidi tutan ölü bir nabız demek.
            if child and child.poll() is None:
                child.terminate()
            sys.exit(128 + signum)

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

    try:
        return subprocess.call(STREAMLIT_CMD, cwd=REPO_ROOT)
    finally:
        if child and child.poll() is None:
            child.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
