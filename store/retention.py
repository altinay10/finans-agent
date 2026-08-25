"""Kayıt tablolarının budanması — sunucuda unutulan bir sistem için zorunlu.

NEDEN BAŞTAN PLANLANDI: `http_requests` koşu başına onlarca satır yazıyor.
Günde ~10 koşu x ~30 istek = ayda ~9.000 satır; yıllarca çalışacak bir
sunucuda tek başına veritabanını şişirir. Kullanıcı sunucuya kurduktan sonra
erişemeyeceği için "bir ara temizlerim" diye bir seçenek yok.

SAKLAMA SÜRELERİ neden farklı:
  * `http_requests` (30 gün) — en gürültülü, en kısa ömürlü. Değeri anlıktır:
    "dün akşam hangi istek düşmüştü".
  * `source_runs` (180 gün) — kaynak sağlığı geçmişi. Bir bankanın kaç kez
    bozulduğunu görmek için mevsimlik bir pencere gerekir.
  * `scrape_runs` (180 gün) — tazelik şeridinin ve koşu geçmişinin temeli.
  * `llm_calls` — ASLA SİLİNMEZ. Token/maliyet muhasebesi kümülatiftir;
    "bu yıl ne kadar harcadım" sorusu silinen satırla cevaplanamaz. Zaten
    en seyrek yazılan tablo (fallback nadir bir kurtarma yolu).
  * `rate_changes` — ASLA SİLİNMEZ. Yalnızca DEĞİŞİM anları yazıldığı için
    yavaş büyür ve "bu oran en son ne zaman değişti" sorusunun tek kaynağı
    odur; budamak sessiz donma tespitini bozardı.

Ayrıca `data/snapshots/` diskte birikir; ham yanıtlar oran tablolarından
kat kat büyüktür ve onlar da budanır.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete

from store.clock import utc_now
from store.db import SessionLocal
from store.models import HttpRequest, ScrapeRun, SourceRun

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshots"


def _days(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


HTTP_REQUEST_DAYS = _days("RETAIN_HTTP_REQUEST_DAYS", 30)
SOURCE_RUN_DAYS = _days("RETAIN_SOURCE_RUN_DAYS", 180)
SCRAPE_RUN_DAYS = _days("RETAIN_SCRAPE_RUN_DAYS", 180)
SNAPSHOT_DAYS = _days("RETAIN_SNAPSHOT_DAYS", 14)


def purge(now=None) -> dict[str, int]:
    """Süresi dolmuş kayıtları siler. Dönen: tablo -> silinen satır sayısı.

    Hata yutulur ve loglanır: temizlik işinin başarısızlığı zamanlayıcıyı
    durdurmamalı — veri toplamak temizlikten önce gelir.
    """
    now = now or utc_now()
    deleted: dict[str, int] = {}
    try:
        with SessionLocal() as session:
            for table, days, label in (
                (HttpRequest, HTTP_REQUEST_DAYS, "http_requests"),
                (SourceRun, SOURCE_RUN_DAYS, "source_runs"),
                (ScrapeRun, SCRAPE_RUN_DAYS, "scrape_runs"),
            ):
                if days <= 0:      # 0 = sınırsız sakla
                    continue
                cutoff = (now - timedelta(days=days)).replace(tzinfo=None)
                column = table.created_at if hasattr(table, "created_at") else table.started_at
                result = session.execute(delete(table).where(column < cutoff))
                deleted[label] = result.rowcount or 0
            session.commit()
    except Exception as exc:  # noqa: BLE001 - temizlik toplamayı durdurmamalı
        logger.error("kayıt budama başarısız: %s", exc)

    deleted["snapshots"] = purge_snapshots(now)
    kept = ", ".join(f"{k}={v}" for k, v in deleted.items() if v)
    logger.info("budama tamam%s", f": {kept}" if kept else " (silinecek bir şey yoktu)")
    return deleted


def purge_snapshots(now=None) -> int:
    """Ham yanıt dosyalarını budar.

    Snapshot'lar onarım için tutuluyor (bir parser kırıldığında elle
    bakılacak ham veri). İki haftadan eski bir snapshot'ın onarım değeri
    kalmaz ama diskte yer kaplamaya devam eder.
    """
    if SNAPSHOT_DAYS <= 0 or not SNAPSHOT_DIR.exists():
        return 0
    now = now or utc_now()
    cutoff = (now - timedelta(days=SNAPSHOT_DAYS)).timestamp()
    removed = 0
    for path in SNAPSHOT_DIR.glob("*.raw"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("snapshot silinemedi (%s): %s", path.name, exc)
    return removed
