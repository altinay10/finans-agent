"""Kayıt tutan HTTP istemcisi — her isteğin izi `http_requests` tablosuna düşer.

NEDEN GEREKLİ: `source_runs` bir bankanın SONUCUNU söyler ("akbank/fetch
failed"). Ama bir banka için birden çok istek atılıyor — Akbank'ta üç ürün
kodu, VakıfBank'ta önce token sonra veri, Yapı Kredi'de dört adımlık zincir.
Zincirin hangi halkasının koptuğu, HTTP kodunun kaç olduğu, ne kadar
sürdüğü, yanıtın kaç bayt geldiği yalnızca burada durur. Kullanıcının
"hangi API istekleri ne zaman dönmedi" sorusunun cevabı bu tablodur.

KULLANIM: `httpx.get(...)` yerine `http.get(...)`. İmza aynıdır; tek fark
çağrıdan önce/sonra kayıt tutulmasıdır.

BAĞLAM: hangi toplayıcının hangi kaynağı için istek atıldığı contextvar ile
taşınır — her çağrıya elle parametre geçirmek onlarca yerde tekrar demekti
ve biri unutulduğunda kayıt sessizce anonimleşirdi.

    with http.source("akbank"):
        http.post(url, json=payload)

TASARIM KURALI: kayıt yazamamak isteği DÜŞÜRMEZ. Gözlemlenebilirlik
katmanının bir hatası yüzünden gerçek veriyi kaybetmek saçma olur
(store/observability.py ile aynı ilke, testle kilitli).
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

logger = logging.getLogger(__name__)

_collector: ContextVar[str | None] = ContextVar("http_collector", default=None)
_run_id: ContextVar[int | None] = ContextVar("http_run_id", default=None)
_source: ContextVar[str | None] = ContextVar("http_source", default=None)

# Yanıt gövdesi SAKLANMIYOR, yalnızca boyutu. Gövdeler zaten
# data/snapshots/ altında duruyor; tabloda tekrarlamak veritabanını
# gereksiz büyütürdü.


@contextmanager
def collector_context(collector: str, run_id: int | None):
    """Collector.run() tarafından kurulur; içindeki tüm istekler etiketlenir."""
    tokens = (_collector.set(collector), _run_id.set(run_id))
    try:
        yield
    finally:
        _collector.reset(tokens[0])
        _run_id.reset(tokens[1])


@contextmanager
def source(name: str):
    """Çok kaynaklı toplayıcıların banka döngüsünde kullanılır."""
    token = _source.set(name)
    try:
        yield
    finally:
        _source.reset(token)


# AĞ HATASI için tek bir yeniden deneme. Canlı gözlem (2026-08-25): Garanti
# Portföy sunucusu ara sıra yanıt vermeden bağlantıyı kapatıyor
# (`RemoteProtocolError: Server disconnected`) ve bir sonraki istek sorunsuz
# çalışıyor — yani geçici. Tek denemede bırakmak, o fonu günlük koşuda
# sebepsiz yere kaybettiriyordu.
#
# SINIR: yalnızca ağ/zaman aşımı hataları yeniden denenir. HTTP 4xx/5xx
# DENENMEZ — 500 dönen bir uç noktayı dövmek ne sorunu çözer ne de nazik
# olur; onun yeri tazelik telafisidir (scheduler.py).
MAX_NETWORK_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 3.0


def get(url: str, **kwargs) -> httpx.Response:
    return _request("GET", url, **kwargs)


def post(url: str, **kwargs) -> httpx.Response:
    return _request("POST", url, **kwargs)


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_NETWORK_ATTEMPTS + 1):
        try:
            return _attempt(method, url, attempt=attempt, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt < MAX_NETWORK_ATTEMPTS:
                logger.warning(
                    "%s %s: ağ hatası (%s), %s sn sonra tekrar denenecek",
                    method, url, type(exc).__name__, RETRY_BACKOFF_SECONDS,
                )
                time.sleep(RETRY_BACKOFF_SECONDS)
    raise last_exc  # type: ignore[misc]


def _attempt(method: str, url: str, *, attempt: int, **kwargs) -> httpx.Response:
    started = time.monotonic()
    try:
        resp = httpx.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        _record(method, url, None, started, attempt, "timeout", str(exc) or type(exc).__name__)
        raise
    except Exception as exc:  # noqa: BLE001 - ağ hatası da kayda geçmeli
        _record(method, url, None, started, attempt, "network_error",
                str(exc) or type(exc).__name__)
        raise

    # 4xx/5xx bir istisna DEĞİLDİR (raise_for_status çağıran karar verir) ama
    # kayıtta ayrı görünmeli: "200 döndü ama boş" ile "500 döndü" bambaşka
    # arıza türleridir.
    outcome = "ok" if resp.status_code < 400 else "http_error"
    _record(method, url, resp, started, attempt, outcome, None)
    return resp


def _record(
    method: str,
    url: str,
    resp: httpx.Response | None,
    started: float,
    attempt: int,
    outcome: str,
    error: str | None,
) -> None:
    try:
        from store.observability import record_http_request

        record_http_request(
            collector=_collector.get(),
            source=_source.get(),
            run_id=_run_id.get(),
            method=method,
            url=url,
            status_code=resp.status_code if resp is not None else None,
            duration_ms=int((time.monotonic() - started) * 1000),
            response_bytes=len(resp.content) if resp is not None else None,
            content_type=(resp.headers.get("content-type") if resp is not None else None),
            attempt=attempt,
            outcome=outcome,
            error=error,
        )
    except Exception as exc:  # noqa: BLE001 - kayıt hatası isteği düşürmemeli
        logger.error("http_request yazılamadı (%s %s): %s", method, url, exc)
