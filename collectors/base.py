"""Collector kontratı — tasarım dokümanı §02.

fetch() -> parse() -> sanity_check() -> persist(). parse() kırılırsa LLM
fallback devreye girer; sanity_check() geçemezse veritabanına hiçbir şey
yazılmaz.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from collectors import http

from store.clock import utc_now
from store.db import SessionLocal
from store.models import ScrapeRun

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshots"


class ParseError(Exception):
    """parse() bir sayfadan beklenen kaydı çıkaramadığında fırlatılır."""


class SanityCheckError(Exception):
    """Çıkarılan kayıtlar makul aralığın dışındaysa fırlatılır. Yazma engellenir."""


@dataclass
class RunResult:
    ok: bool
    rows: int = 0
    status: str = "ok"          # 'ok' | 'failed' | 'llm_fallback'
    error: str | None = None
    # Hata hangi aşamada: 'fetch' | 'parse' | 'sanity' | 'persist'
    failure_kind: str | None = None


class Collector(ABC):
    name: str
    schema: type[BaseModel]

    # Tek kaynaklı toplayıcılar (TCMB, Ak Portföy) için sources.yaml
    # anahtarı. Doluysa run() fetch/parse sonuçlarını OTOMATİK olarak
    # source_runs'a yazar.
    #
    # Neden gerekli: çok kaynaklı toplayıcılar (fx_banks, deposit_rates)
    # banka banka kendi kaydını tutuyor, tek kaynaklılar hiç tutmuyordu.
    # Sonuç: Kaynaklar sekmesi TCMB için "envanter 'çekiliyor' diyor ama
    # başarılı koşu yok" diye YANLIŞ uyarı basıyordu — kaynak gayet
    # çalışıyorken. Kayıt tutmayan bir kaynak, bozuk bir kaynaktan ayırt
    # edilemez.
    default_source: str | None = None

    def __init__(self) -> None:
        self._snapshot_path: str | None = None
        # fetch()/parse() içindeki kaynak bazlı kayıtların hangi koşuya ait
        # olduğunu bilmesi için run() bunu doldurur.
        self._run_id: int | None = None

    # ------------------------------------------------------------ hooks --

    @abstractmethod
    def fetch(self) -> bytes:
        """Ham yanıtı çeker. Snapshot'a yazmak run() içinde otomatik yapılır."""

    @abstractmethod
    def parse(self, raw: bytes) -> list[BaseModel]:
        """Ham veriyi şemaya uyan kayıtlara çevirir. Kırılırsa ParseError fırlat."""

    @abstractmethod
    def sanity_check(self, records: list[BaseModel]) -> None:
        """Aralık kontrolü. Geçemezse SanityCheckError fırlat — veri yazılmaz."""

    @abstractmethod
    def persist(self, records: list[BaseModel], run_id: int) -> None:
        """Kayıtları store/ katmanına yazar."""

    # -------------------------------------------------------------- run --

    def run(self, trigger: str = "manual") -> RunResult:
        """Toplayıcıyı çalıştırır. `trigger` bu koşuyu NE başlattığıdır.

        'manual' (worker.py) | 'schedule' | 'startup' | 'catchup'. Sunucuda
        erişim olmayacağı için "telafi mekanizması gerçekten çalışıyor mu"
        sorusunu cevaplayabilen tek kayıt budur (bkz. scheduler.py).
        """
        started_at = utc_now()
        with SessionLocal() as session:
            run = ScrapeRun(
                collector=self.name, status="failed", started_at=started_at, trigger=trigger
            )
            session.add(run)
            session.commit()
            run_id = run.id

        self._run_id = run_id
        # Bu koşu boyunca atılan HER HTTP isteği bu toplayıcıyla etiketlenir
        # (collectors/http.py). Elle parametre geçirmek onlarca çağrıda
        # tekrar demekti ve biri unutulduğunda kayıt sessizce anonimleşirdi.
        http_ctx = http.collector_context(self.name, run_id)
        http_ctx.__enter__()
        # Tek kaynaklı toplayıcıda istekler de o kaynakla etiketlensin;
        # aksi halde HTTP dökümünde 'kaynak: —' diye anonim satırlar kalır
        # ve uç nokta sağlığı kaynak bazında gruplanamaz.
        source_ctx = http.source(self.default_source) if self.default_source else None
        if source_ctx is not None:
            source_ctx.__enter__()
        # Hata HANGİ aşamada oldu? 'sanity' özellikle önemli: veri geldi,
        # ayrıştırıldı, ama bant dışıydı ve BİLEREK yazılmadı — bu bir arıza
        # değil, korumanın çalıştığının kanıtı. Hata metnine gömülü kalırsa
        # "kaç kez saçma veri geldi" sorusu sayılamaz.
        phase = "fetch"
        try:
            fetch_started = time.monotonic()
            raw = self.fetch()
            self._record_default_source(
                "fetch", "ok", duration_ms=int((time.monotonic() - fetch_started) * 1000)
            )
            self._write_snapshot(raw)

            phase = "parse"
            status = "ok"
            try:
                records = self.parse(raw)
                if not records:
                    raise ParseError(f"{self.name}: parse() sıfır kayıt döndürdü")
                self._record_default_source("parse", "ok", rows=len(records))
            except (ParseError, Exception) as exc:  # noqa: BLE001 - parser kırılganlığı beklenen durum
                logger.warning("%s: parse başarısız (%s), LLM fallback deneniyor", self.name, exc)
                self._record_default_source("parse", "failed", error=str(exc))
                try:
                    records = self._llm_fallback(raw, trigger_error=str(exc))
                    status = "llm_fallback"
                except Exception as fallback_exc:  # noqa: BLE001
                    # Fallback kapalı, bütçe dolmuş ya da model de beceremedi.
                    # Orijinal parse hatasını kaybetmeden çık — asıl sorun o.
                    logger.warning("%s: LLM fallback devreye giremedi: %s", self.name, fallback_exc)
                    raise exc

            phase = "sanity"
            self.sanity_check(records)
            phase = "persist"
            self.persist(records, run_id)
            self._finish_run(run_id, status=status, rows_written=len(records))
            return RunResult(ok=True, rows=len(records), status=status)

        except Exception as exc:  # noqa: BLE001 - toplayıcı asla process'i düşürmemeli
            logger.error("%s: çalıştırma %s aşamasında başarısız: %s", self.name, phase, exc)
            if phase == "fetch":
                self._record_default_source("fetch", "failed", error=str(exc))
            self._finish_run(
                run_id, status="failed", rows_written=0, error=str(exc), failure_kind=phase
            )
            return RunResult(ok=False, status="failed", error=str(exc), failure_kind=phase)
        finally:
            if source_ctx is not None:
                source_ctx.__exit__(None, None, None)
            http_ctx.__exit__(None, None, None)

    def _record_default_source(
        self, phase: str, status: str, *, rows: int = 0,
        duration_ms: int | None = None, error: str | None = None,
    ) -> None:
        if not self.default_source:
            return
        self.record_source(
            self.default_source, phase=phase, status=status,
            rows=rows, duration_ms=duration_ms, error=error,
        )

    # --------------------------------------------------------- internals --

    def _llm_fallback(self, raw: bytes, *, trigger_error: str | None = None) -> list[BaseModel]:
        from llm.extract import extract  # gecikmeli import — normal akışta hiç yüklenmez

        self.flag_for_repair(raw)
        return extract(
            raw.decode("utf-8", errors="ignore"),
            self.schema,
            collector=self.name,
            run_id=self._run_id,
            # "Agent ne zaman hangi durumda devreye girdi" sorusunun cevabı:
            # collector adı tek başına yetmez, asıl bilgi ONU ÇAĞIRAN hatadır.
            trigger_error=trigger_error,
        )

    # ------------------------------------------- kaynak bazlı kayıt yardımcısı --

    def record_source(
        self,
        source: str,
        *,
        phase: str,
        status: str,
        rows: int = 0,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        """Bir bankanın bu koşudaki sonucunu KALICI olarak yazar.

        scrape_runs koşunun tamamını özetler; beş bankadan biri düşse bile
        'ok' der. Hangi bankanın kaybolduğu yalnızca burada durur.
        """
        from store.observability import record_source_run

        record_source_run(
            collector=self.name,
            source=source,
            phase=phase,
            status=status,
            rows=rows,
            duration_ms=duration_ms,
            error=error,
            run_id=self._run_id,
        )

    def flag_for_repair(self, raw: bytes) -> None:
        """Selector'ın elle onarılması gerektiğini işaretler. Snapshot zaten kaydedildi."""
        logger.warning("%s: onarım gerekiyor — snapshot: %s", self.name, self._snapshot_path)

    def _write_snapshot(self, raw: bytes) -> None:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = SNAPSHOT_DIR / f"{self.name}_{ts}.raw"
        path.write_bytes(raw)
        self._snapshot_path = str(path)

    def _finish_run(
        self, run_id: int, *, status: str, rows_written: int,
        error: str | None = None, failure_kind: str | None = None,
    ) -> None:
        with SessionLocal() as session:
            run = session.get(ScrapeRun, run_id)
            run.status = status
            run.finished_at = utc_now()
            run.rows_written = rows_written
            run.error = error
            run.failure_kind = failure_kind
            run.snapshot_path = self._snapshot_path
            session.commit()
