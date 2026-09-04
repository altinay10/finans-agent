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
import threading
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
    anlamsızdır; orada zaman aşımı tek ölçüttür. Düzgün kapanan zamanlayıcı
    kilidi zaten bırakır, o yüzden bu bekleme normalde yaşanmaz (bkz.
    release()).

    KİMLİK (host, pid) ÇİFTİDİR — yalnızca pid DEĞİL. Konteynerde
    zamanlayıcı HER ZAMAN pid 1'dir; tek başına pid'e bakan bir kontrol,
    başka bir konteynerdeki zamanlayıcının nabzını "benim nabzım" sanıp
    kilidi doğrudan alıyordu. İki konteyner birden koşarsa bankalar iki kat
    istek alır — bu kilidin var olma sebebi tam olarak buydu. Aynı kusur
    sahiplenmenin doğrulama adımında da vardı: iki pid-1 süreci de "kazandım"
    diye okuyordu (2026-09-04'te fark edildi).
    """
    ben = (socket.gethostname(), os.getpid())
    state = read()
    if state and state["alive"] and (state["host"], state["pid"]) != ben:
        ayni_makine = state["host"] == ben[0]
        if not ayni_makine or _pid_running(state["pid"]):
            return False
        logger.warning(
            "önceki zamanlayıcı (pid %s) ölmüş — kilit devralınıyor", state["pid"]
        )
    beat()
    after = read()
    return bool(after and (after["host"], after["pid"]) == ben)


def release() -> None:
    """Kilidi BIRAK — yalnızca düzgün kapanışta çağrılır.

    NEDEN GEREKLİ (2026-09-04, canlıda ölçüldü): nabız satırı süreç
    öldükten sonra da 180 saniye "canlı" görünüyor. Aynı makinede bu
    zararsız — claim() pid'in gerçekten yaşadığını sınayıp kilidi
    devralıyor. Ama FARKLI BİR HOST'tan bakan bir zamanlayıcı için pid
    kontrolü anlamsız, tek ölçüt zaman aşımı.

    Konteynerde her `docker compose up -d` yeni bir hostname üretiyor.
    Yani her yeniden dağıtımda yeni zamanlayıcı, üç dakika boyunca eski
    konteynerin nabzını canlı sanıp "başkası çalışıyor" deyip çıkıyor,
    restart politikası onu tekrar başlatıyor — üç dakikalık crash loop ve
    Docker Desktop'ta "Restarting". Host'tan konteynere veritabanı
    taşırken de aynısı oluyordu.

    Kilidi düzgün kapanışta bırakmak bunu tamamen bitirir. ÇÖKME durumu
    korumasız kalmaz: satır orada durur ve zaman aşımı yine devreye girer.

    SAHİPLİK KONTROLÜ şart: bu süreç kilidi zaten kaybetmişse (başkası
    devralmışsa) satırı silmek, ÇALIŞAN bir zamanlayıcının kilidini
    çalmak olurdu.
    """
    try:
        with SessionLocal() as session:
            row = session.get(SchedulerHeartbeat, ROW_ID)
            if row is None:
                return
            if row.pid != os.getpid() or row.host != socket.gethostname():
                logger.info("nabız bize ait değil (host=%s pid=%s) — bırakılmıyor",
                            row.host, row.pid)
                return
            session.delete(row)
            session.commit()
            logger.info("zamanlayıcı kilidi bırakıldı")
    except Exception as exc:  # noqa: BLE001 - kapanışı hiçbir şey engellememeli
        logger.error("nabız bırakılamadı: %s", exc)


BEAT_INTERVAL_SECONDS = 30


def start_beating() -> threading.Thread:
    """Nabzı ARKA PLAN İŞ PARÇACIĞINDAN at.

    NEDEN: nabız zamanlayıcının ana döngüsünden atılıyordu ve döngü,
    toplama sırasında BLOKLANIYOR. Açılış turunda dokuz toplayıcı sırayla
    koşuyor (VakıfBank mevduat matrisi tek başına ~60 sn); bu sürede hiç
    nabız atılmıyor ve panel çalışan bir zamanlayıcıya "DURMUŞ" diyor —
    hem de en kritik anda, ilk kurulumda.

    Nabız "döngü dönüyor mu"yu değil "SÜREÇ YAŞIYOR MU"yu ölçmeli; doğru
    yeri bu yüzden ayrı bir iş parçacığı.
    """
    def _loop() -> None:
        while True:
            beat()
            _stop.wait(BEAT_INTERVAL_SECONDS)

    _stop = threading.Event()
    thread = threading.Thread(target=_loop, name="heartbeat", daemon=True)
    thread.start()
    return thread
