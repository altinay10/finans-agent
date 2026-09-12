"""SQLite yedeklemesi — sunucuda unutulacak bir sistemin tek kurtarma yolu.

NEDEN GEREKLİ: veritabanı tek bir dosya ve adlandırılmış bir Docker
volume'unda duruyor (bkz. docker-compose.yml). O volume'un altındaki
SD kart / SSD öldüğünde toplanmış bütün geçmiş — aylarca biriken oran
tarihçesi, token muhasebesi, kaynak sağlığı — geri dönüşsüz gider.
`docker-compose.yml` içinde ELLE yedekleme komutu yazılı, ama elle yapılan
yedek, kurulup unutulan bir sistemde yapılmayan yedektir.

NEDEN HOST CRON DEĞİL, ZAMANLAYICI İÇİNDE: cron'daki bir `sqlite3 .backup`
komutu, volume'un host üzerindeki gerçek yolunu bilmek zorunda
(`/var/lib/docker/volumes/<proje>_finans-data/_data/...`). O yol proje adına
göre değişiyor ve compose sürümleri arasında kayabiliyor; yanlış yola bakan
bir cron sessizce boş yedek üretir. Konteynerin içinde `data/` her zaman
doğru yerdedir ve zamanlayıcı zaten günlük bir bakım turu yapıyor
(bkz. `store/retention.purge`).

NEDEN `Connection.backup`, dosya kopyalamak DEĞİL: SQLite dosyasını
çalışırken `cp` ile kopyalamak, ortasında bir yazma varsa BOZUK bir dosya
üretir ve bozukluk ancak geri yüklerken ortaya çıkar. `backup()` SQLite'ın
kendi çevrimiçi yedekleme API'si: eşzamanlı yazmalarla tutarlı bir kopya
çıkarır.

DOĞRULANMAYAN YEDEK YEDEK DEĞİLDİR: her yedek yazıldıktan sonra açılıp
`PRAGMA integrity_check` çalıştırılıyor. Geçmezse dosya siliniyor — bozuk
bir dosyayı "yedeğim var" diye saklamak, hiç yedek olmamasından kötüdür,
çünkü yanlış güven verir.

SINIR — BUNUN ÇÖZMEDİĞİ ŞEY: varsayılan hedef `data/backups`, yani AYNI
volume. Bu, bozulmaya / yanlış göçe / yanlışlıkla silmeye karşı korur ama
diskin kendisi ölürse yedek de gider. Gerçek cihaz dışı koruma için
`BACKUP_DIR` başka bir yere (bağlanmış bir host dizini, USB, NAS)
yönlendirilmeli; `.env.example` bunu anlatıyor.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

from store.clock import utc_now
from store.db import engine

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Yedeklerin yazılacağı dizin. Cihaz dışına almak için bunu bağlanmış bir
#: host dizinine yönlendir (bkz. modül başlığındaki SINIR notu).
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or (REPO_ROOT / "data" / "backups"))

#: Kaç günlük yedek saklanır. 0 = sınırsız (budama yapılmaz).
KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "") or 14)

#: Dosya adı deseni — tarihe göre sıralanabilir olsun diye ISO.
NAME_FORMAT = "finans_agent_%Y%m%d.db"


def _db_path() -> Path | None:
    """Çalışan motorun SQLite dosyası (SQLite değilse None)."""
    if engine.url.get_backend_name() != "sqlite":
        # Postgres'e geçilirse yedekleme o sunucunun kendi aracıyla yapılır;
        # sessizce yanlış bir şey yapmaktansa hiçbir şey yapma.
        return None
    name = engine.url.database
    if not name or name == ":memory:":
        return None
    return Path(name)


def _integrity_ok(path: Path) -> bool:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            sonuc = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(sonuc) and sonuc[0] == "ok"
    except sqlite3.Error as exc:  # noqa: BLE001 - bozuk dosya zaten okunamaz
        logger.warning("yedek bütünlük denetimi açılamadı: %s", exc)
        return False


def _prune(now) -> int:
    """Süresi dolmuş yedekleri siler; dönen: silinen dosya sayısı."""
    if KEEP_DAYS <= 0:
        return 0
    cutoff = (now - timedelta(days=KEEP_DAYS)).timestamp()
    silinen = 0
    for eski in BACKUP_DIR.glob("finans_agent_*.db"):
        try:
            if eski.stat().st_mtime < cutoff:
                eski.unlink()
                silinen += 1
        except OSError as exc:  # noqa: PERF203 - tek dosyanın hatası turu bitirmesin
            logger.warning("eski yedek silinemedi (%s): %s", eski.name, exc)
    return silinen


def run_backup(now=None) -> Path | None:
    """Günlük yedeği alır, doğrular, eskileri budar. Dönen: yedek yolu ya da None.

    HATA YUTULUR: yedekleme başarısızlığı zamanlayıcıyı durdurmamalı —
    veri toplamak yedeklemeden önce gelir (aynı ilke `retention.purge`'de
    de var). Başarısızlık log'a WARNING olarak düşer.

    AYNI GÜN İKİNCİ KEZ ÇAĞRILIRSA üzerine yazar: süreç yeniden
    başladığında ikinci bir dosya üretmek, günlük rotasyonu bozar ve
    saklama penceresini sessizce kısaltırdı.
    """
    now = now or utc_now()
    kaynak = _db_path()
    if kaynak is None:
        logger.debug("yedekleme atlandı: SQLite dosyası yok")
        return None
    if not kaynak.exists():
        logger.warning("yedekleme atlandı: %s bulunamadı", kaynak)
        return None

    hedef = BACKUP_DIR / now.strftime(NAME_FORMAT)
    # ÖNCE GEÇİCİ ADA YAZ, SONRA TAŞI: yarım kalmış bir yedek, geçerli bir
    # yedek adı taşımamalı. Süreç yazarken ölürse geriye `.partial` kalır
    # ve bir sonraki tur onu ezer.
    gecici = hedef.with_suffix(".partial")
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        gecici.unlink(missing_ok=True)
        with sqlite3.connect(f"file:{kaynak}?mode=ro", uri=True) as src, \
                sqlite3.connect(gecici) as dst:
            src.backup(dst)
        if not _integrity_ok(gecici):
            gecici.unlink(missing_ok=True)
            logger.error("yedek bütünlük denetiminden GEÇMEDİ, atıldı: %s", hedef.name)
            return None
        gecici.replace(hedef)
    except (sqlite3.Error, OSError) as exc:  # noqa: BLE001
        logger.warning("yedekleme başarısız: %s", exc)
        gecici.unlink(missing_ok=True)
        return None

    silinen = _prune(now)
    logger.info(
        "yedek alındı: %s (%.1f MB)%s",
        hedef.name,
        hedef.stat().st_size / 1_048_576,
        f", {silinen} eski yedek silindi" if silinen else "",
    )
    return hedef
