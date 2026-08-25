"""Log kurulumu — hem konsola hem DÖNEN dosyaya.

Neden dosya: sunucuda toplayıcılar konteyner/servis olarak koşar ve stdout
log'u yeniden başlatmada ya da `docker logs` tamponu dolduğunda kaybolur.
Banka bazlı sonuçlar ve token harcaması zaten veritabanına yazılıyor, ama
istisna izleri (traceback) oraya sığmaz — onlar buraya düşer.

Dönen dosya (rotating): sınırsız büyüyen bir log dosyası, günü gelince diski
doldurup asıl uygulamayı düşürür. 5 x 2 MB ile sınırlı.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

_configured = False


def setup_logging(component: str) -> Path | None:
    """Konsol + dosya log'unu kurar. İki kez çağrılırsa ikincisi yok sayılır."""
    global _configured
    if _configured:
        return None
    _configured = True

    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(console)

    # LOG_DIR boşsa dosyaya yazma (ör. testler).
    log_dir = os.environ.get("LOG_DIR", "data/logs")
    if not log_dir:
        return None
    try:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        target = path / f"{component}.log"
        handler = RotatingFileHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
        return target
    except OSError as exc:  # salt-okunur dosya sistemi vb. — uygulamayı düşürme
        root.warning("dosya log'u kurulamadı (%s): %s", log_dir, exc)
        return None
