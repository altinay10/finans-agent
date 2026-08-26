"""Zamanlayıcı yaşam sinyali — "süreç ayakta mı?" sorusunun tek kaynağı.

Panel veri toplamaz (tasarım §01); toplamayı ayrı bir süreç yapar. Bu ayrım
doğru ama bir bedeli var: **panel, o sürecin var olup olmadığını bilmiyordu.**
Sunucuya kurup unutan biri için en sık yaşanan arıza tam olarak buydu —
konteyner ayakta, site açılıyor, veri günlerce eskiyor.

Bu modül o boşluğu kapatır ve iki işi birden görür:
  1. Panele kesin cevap verir: "zamanlayıcı 12 saniye önce nabız attı" ya da
     "hiç nabız yok".
  2. İKİ zamanlayıcının aynı anda koşmasını engeller. Tek konteynerli
     kurulumda panel kendi zamanlayıcısını başlatabiliyor; compose'da ayrı
     bir `scheduler` servisi var. İkisi birden koşarsa bankalar iki kat
     istek alır. `claim()` bunu tek satırlık kilitle çözer.
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import timedelta

from store.clock import utc_now
from store.db import SessionLocal
from store.models import SchedulerHeartbeat

logger = logging.getLogger(__name__)

ROW_ID = 1

# Nabız bu süreden eskiyse süreç ölmüş sayılır. Zamanlayıcının tur süresi
# 20 saniye (scheduler.TICK_SECONDS); 3 dakika, uzun süren bir toplama
# turunun (VakıfBank mevduat matrisi ~60 sn) nabzı geciktirmesine rağmen
# yanlış alarm üretmeyecek kadar geniş.
STALE_AFTER_SECONDS = 180


def beat() -> None:
    """Zamanlayıcı her turda çağırır. Hata YUTULUR — nabız yazamamak
    toplamayı durdurmamalı (store/observability.py ile aynı ilke)."""
    try:
        now = utc_now()
        with SessionLocal() as session:
            row = session.get(SchedulerHeartbeat, ROW_ID)
            if row is None:
                session.add(
                    SchedulerHeartbeat(
                        id=ROW_ID, host=socket.gethostname(), pid=os.getpid(),
                        started_at=now, last_beat=now,
                    )
                )
            else:
                row.host = socket.gethostname()
                row.pid = os.getpid()
                row.last_beat = now
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("nabız yazılamadı: %s", exc)


def read() -> dict | None:
    """Panelin okuduğu ham kayıt. Hiç zamanlayıcı koşmamışsa None."""
    try:
        with SessionLocal() as session:
            row = session.get(SchedulerHeartbeat, ROW_ID)
            if row is None:
                return None
            beat_at = row.last_beat
            age = (utc_now().replace(tzinfo=None) - beat_at).total_seconds()
            return {
                "host": row.host,
                "pid": row.pid,
                "started_at": row.started_at,
                "last_beat": beat_at,
                "age_seconds": age,
                "alive": age <= STALE_AFTER_SECONDS,
            }
    except Exception as exc:  # noqa: BLE001
        logger.error("nabız okunamadı: %s", exc)
        return None


def is_alive() -> bool:
    state = read()
    return bool(state and state["alive"])


def _pid_running(pid: int) -> bool:
    """Bu makinede bu pid gerçekten yaşıyor mu?"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # süreç var ama bize ait değil
    except OSError:
        return True          # emin olamıyorsak yaşıyor say (temkinli)
    return True


def claim() -> bool:
    """Zamanlayıcı rolünü sahiplen. Başkası GERÇEKTEN canlıysa False döner.

    Yarış durumu: panel ve ayrı scheduler servisi aynı anda kalkarsa ikisi de
    "nabız yok" görebilir. Bu yüzden sahiplenme, nabzı YAZIP hemen geri
    okuyarak doğrulanır — son yazan kazanır, diğeri çekilir.

    ÖLÜ SAHİP TUZAĞI (canlıda yakalandı, 2026-08-26): bir zamanlayıcı
    çökerse ya da konteyner yeniden başlarsa nabzı 3 dakika daha "canlı"
    görünür. O aralıkta başlayan YENİ zamanlayıcı kendini kapatıyordu ve
    `run.py` çocuğu yeniden denemediği için panel toplayıcısız kalıyordu —
    tam da çözmeye çalıştığımız arıza. Bu yüzden nabız AYNI MAKİNEDEN
    geliyorsa pid'in gerçekten yaşadığı ayrıca sınanıyor; ölmüşse kilit
    anında devralınır.

    Farklı bir host'tan (ör. ayrı konteyner) gelen nabızda pid kontrolü
    anlamsızdır; orada zaman aşımı tek ölçüttür.
    """
    state = read()
    if state and state["alive"] and state["pid"] != os.getpid():
        ayni_makine = state["host"] == socket.gethostname()
        if not ayni_makine or _pid_running(state["pid"]):
            return False
        logger.warning(
            "önceki zamanlayıcı (pid %s) ölmüş — kilit devralınıyor", state["pid"]
        )
    beat()
    after = read()
    return bool(after and after["pid"] == os.getpid())
