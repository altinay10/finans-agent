"""Kırık ayrıştırıcı için GERİ ÇEKİLME (backoff) sayacı.

SORUN: tazelik telafisi son BAŞARILI koşuya bakıyor. Bir ayrıştırıcı kalıcı
olarak kırıldığında o koşu bir daha asla başarılı olmuyor, dolayısıyla
telafi 30 dakikada bir sonsuza kadar yeniden deniyor ve her denemede LLM
yedeği bir çağrı daha yakıyor. Hafta sonu fark edilmeyen bir arıza, sessizce
48 çağrı/gün üretebilirdi.

ÇÖZÜM: art arda başarısızlık sayısına göre aralık.
    ilk 6 başarısızlık : 5 dakikada bir  (geçici arıza hızla toparlansın)
    6 ve sonrası       : 4 saatte bir    (kalıcı arıza insan eli bekler)

SAYAÇ NE ZAMAN ARTAR: yalnızca GERÇEKTEN bir LLM çağrısı yapılıp başarısız
olduğunda. "Agent kapalı" ve "bütçe doldu" durumlarında hiç çağrı yapılmıyor
(bkz. llm/extract.py) — bunlar sayacı kirletseydi, anahtarı olmayan bir
kurulumda `loan_rates_llm` 12 banka sayfasını 5 dakikada bir çekmeye
başlardı. Bugünkü davranışın tam tersi olurdu: onun tazelik sınırı token
yakmasın diye bilerek 30 GÜN'e çekilmiş.

ELLE KOŞULAR SAYILMAZ: panelin "Şimdi tazele" düğmesi ziyaretçinin kendi
anahtarıyla koşuyor. Onun başarısızlığı zamanlayıcıyı SUNUCUNUN anahtarıyla
5 dakikada bir denemeye sokarsa, "panel sunucunun anahtarını harcayamaz"
kuralı arka kapıdan delinmiş olur.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from store.clock import utc_now
from store.db import SessionLocal
from store.models import LlmFallbackState

logger = logging.getLogger(__name__)

#: Bu sayıya ULAŞILDIĞINDA yavaş aralığa geçilir.
HIZLI_DENEME_SINIRI = 6
HIZLI_ARALIK = timedelta(minutes=5)
YAVAS_ARALIK = timedelta(hours=4)

#: Sayacı ARTIRMAYAN koşu tetikleyicileri (bkz. modül başlığı).
SAYILMAYAN_TETIKLEYICILER = frozenset({"manual"})


@dataclass(frozen=True)
class BackoffState:
    collector: str
    consecutive_failures: int
    last_attempt_at: datetime | None
    last_error: str | None = None

    def interval(self) -> timedelta:
        return HIZLI_ARALIK if self.consecutive_failures < HIZLI_DENEME_SINIRI else YAVAS_ARALIK

    def due_at(self) -> datetime | None:
        if self.last_attempt_at is None:
            return None
        stamp = self.last_attempt_at
        if stamp.tzinfo is None:
            from datetime import timezone

            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp + self.interval()


def counts_for_backoff(trigger: str | None) -> bool:
    """Bu tetikleyici geri çekilme sayacını etkilesin mi."""
    return (trigger or "") not in SAYILMAYAN_TETIKLEYICILER


def record_failure(collector: str, error: str | None = None) -> None:
    """Gerçekten LLM denendi ve olmadı — sayacı artır."""
    try:
        with SessionLocal() as session:
            row = session.get(LlmFallbackState, collector)
            if row is None:
                row = LlmFallbackState(collector=collector, consecutive_failures=0)
                session.add(row)
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.last_attempt_at = utc_now()
            row.last_error = (error or "")[:500] or None
            session.commit()
            logger.warning(
                "%s: geri çekilme sayacı %s (sonraki deneme %s sonra)",
                collector, row.consecutive_failures,
                HIZLI_ARALIK if row.consecutive_failures < HIZLI_DENEME_SINIRI else YAVAS_ARALIK,
            )
    except Exception as exc:  # noqa: BLE001 - sayaç yazamamak koşuyu düşürmemeli
        logger.warning("%s: geri çekilme sayacı yazılamadı: %s", collector, exc)


def record_success(collector: str) -> None:
    """Toplayıcı toparladı — sayacı sıfırla."""
    try:
        with SessionLocal() as session:
            row = session.get(LlmFallbackState, collector)
            if row is None or not row.consecutive_failures:
                return
            logger.info("%s: toparladı, geri çekilme sayacı sıfırlandı", collector)
            row.consecutive_failures = 0
            row.last_attempt_at = utc_now()
            row.last_error = None
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: geri çekilme sayacı sıfırlanamadı: %s", collector, exc)


def read_all() -> dict[str, BackoffState]:
    try:
        with SessionLocal() as session:
            rows = session.execute(select(LlmFallbackState)).scalars().all()
            return {
                r.collector: BackoffState(
                    collector=r.collector,
                    consecutive_failures=r.consecutive_failures or 0,
                    last_attempt_at=r.last_attempt_at,
                    last_error=r.last_error,
                )
                for r in rows
            }
    except Exception as exc:  # noqa: BLE001 - tablo yoksa geri çekilme de yok
        logger.debug("geri çekilme durumu okunamadı: %s", exc)
        return {}


def backing_off() -> set[str]:
    """Şu an geri çekilme durumunda olan toplayıcı adları (veritabanı adı)."""
    return {name for name, state in read_all().items() if state.consecutive_failures > 0}


def is_due(state: BackoffState, now_utc: datetime | None = None) -> bool:
    if state.consecutive_failures <= 0:
        return False
    hedef = state.due_at()
    if hedef is None:
        return True
    return (now_utc or utc_now()) >= hedef
