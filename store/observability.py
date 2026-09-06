"""Gözlemlenebilirlik kayıtları — banka bazlı sonuçlar ve LLM token muhasebesi.

Neden ayrı modül: bu fonksiyonlar toplayıcıların içinden çağrılır ve
ASLA çağıranı düşürmemelidir. Bir log yazma hatası yüzünden başarılı bir
veri çekimini kaybetmek saçma olur — bu yüzden hepsi kendi istisnasını yutar
ve yalnızca log'a düşer.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from store.clock import utc_now
from store.db import SessionLocal
from store.models import HttpRequest, LlmCall, RateChange, SourceRun

logger = logging.getLogger(__name__)


def record_source_run(
    *,
    collector: str,
    source: str,
    phase: str,
    status: str,
    rows: int = 0,
    duration_ms: int | None = None,
    error: str | None = None,
    run_id: int | None = None,
) -> None:
    """Tek bir kaynağın (banka) tek bir aşamadaki sonucunu yazar."""
    try:
        with SessionLocal() as session:
            session.add(
                SourceRun(
                    run_id=run_id,
                    collector=collector,
                    source=source,
                    phase=phase,
                    status=status,
                    rows=rows,
                    duration_ms=duration_ms,
                    # Hata metni çok uzun olabilir (bazı bankalar tam HTML
                    # sayfası döndürüyor); tabloyu şişirmesin.
                    error=(error[:2000] if error else None),
                    started_at=utc_now(),
                )
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - log yazamamak veriyi kaybettirmemeli
        logger.error("source_run yazılamadı (%s/%s): %s", collector, source, exc)


def record_llm_call(
    *,
    model: str,
    status: str,
    collector: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    input_chars: int | None = None,
    rows_recovered: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    run_id: int | None = None,
    trigger_error: str | None = None,
    trigger_source: str | None = None,
) -> None:
    """LLM çağrısının (ya da neden yapılmadığının) kaydı.

    `trigger_error` "agent ne zaman hangi durumda devreye girdi" sorusunun
    asıl cevabıdır: collector adı hangi banka sayfasının bozulduğunu
    söylemez, fallback'i çağıran hata söyler.
    """
    total = None
    if prompt_tokens is not None or completion_tokens is not None:
        total = (prompt_tokens or 0) + (completion_tokens or 0)
    try:
        from llm.settings import estimate_cost_usd
        from store.app_settings import price_rates

        # Birim fiyat artık panelden de girilebiliyor ve veritabanına
        # yazılıyor; kayıt yoksa `.env`'deki değere düşülür.
        girdi_fiyat, cikti_fiyat = price_rates()
        cost = estimate_cost_usd(prompt_tokens, completion_tokens, girdi_fiyat, cikti_fiyat)
    except Exception:  # noqa: BLE001 - fiyat okunamıyorsa maliyet bilinmiyor
        cost = None
    try:
        with SessionLocal() as session:
            session.add(
                LlmCall(
                    run_id=run_id,
                    collector=collector,
                    model=model,
                    status=status,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total,
                    input_chars=input_chars,
                    rows_recovered=rows_recovered,
                    duration_ms=duration_ms,
                    error=(error[:2000] if error else None),
                    trigger_source=trigger_source,
                    trigger_error=(trigger_error[:2000] if trigger_error else None),
                    cost_usd=cost,
                    created_at=utc_now(),
                )
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("llm_call yazılamadı (%s): %s", collector, exc)


def record_http_request(
    *,
    method: str,
    url: str,
    outcome: str,
    collector: str | None = None,
    source: str | None = None,
    run_id: int | None = None,
    status_code: int | None = None,
    duration_ms: int | None = None,
    response_bytes: int | None = None,
    content_type: str | None = None,
    attempt: int = 1,
    error: str | None = None,
) -> None:
    """Tek bir HTTP isteğinin kaydı — bkz. collectors/http.py.

    Projedeki EN GÜRÜLTÜLÜ kayıt türü (koşu başına onlarca satır); saklama
    süresi store/retention.py tarafından yönetilir.
    """
    try:
        with SessionLocal() as session:
            session.add(
                HttpRequest(
                    run_id=run_id,
                    collector=collector,
                    source=source,
                    method=method,
                    # Sorgu dizesi anahtar/token taşıyabilir (VakıfBank akışı);
                    # URL kırpılmıyor ama uzunluk sınırlanıyor.
                    url=url[:1000],
                    status_code=status_code,
                    duration_ms=duration_ms,
                    response_bytes=response_bytes,
                    content_type=(content_type[:120] if content_type else None),
                    attempt=attempt,
                    outcome=outcome,
                    error=(error[:2000] if error else None),
                    created_at=utc_now(),
                )
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - kayıt yazamamak isteği kaybettirmemeli
        logger.error("http_request yazılamadı (%s %s): %s", method, url, exc)


def record_rate_change(
    *,
    dataset: str,
    institution: str,
    series_key: str,
    old_value: float | None,
    new_value: float,
    run_id: int | None = None,
) -> None:
    """Bir oranın DEĞİŞTİĞİ anı yazar — sessiz donmayı yakalayan kayıt.

    Yalnızca gerçek değişimde çağrılmalı; aynı değeri tekrar yazmak tabloyu
    şişirir ve "son değişim" sorgusunu anlamsız kılar.
    """
    try:
        with SessionLocal() as session:
            session.add(
                RateChange(
                    dataset=dataset,
                    institution=institution,
                    series_key=series_key,
                    old_value=old_value,
                    new_value=new_value,
                    changed_at=utc_now(),
                    run_id=run_id,
                )
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("rate_change yazılamadı (%s/%s): %s", dataset, institution, exc)


def record_rate_changes(
    dataset: str, values: dict[tuple[str, str], float], *, run_id: int | None = None
) -> int:
    """Bir koşunun tüm oranlarını önceki değerlerle karşılaştırır, DEĞİŞENLERİ yazar.

    `values`: {(kurum, seri_anahtarı): yeni_değer}

    NEDEN: bir uç nokta HTTP 200 dönmeye devam edebilir ama arkasındaki
    besleme durmuş olabilir. `source_runs` bunu 'ok' diye kaydeder, panel
    veriyi taze gösterir, oysa sayı haftalardır aynıdır. 'empty' kadar sinsi
    bir bozulma türüdür ve tek tespiti "bu oran en son ne zaman değişti"
    sorusudur.

    Satır satır sorgu yerine önceki değerlerin TAMAMI tek seferde okunur:
    mevduat koşusu ~800 satır yazıyor, satır başına sorgu bunu yavaşlatırdı.

    Dönen sayı: yazılan değişim satırı adedi.
    """
    if not values:
        return 0
    try:
        with SessionLocal() as session:
            previous: dict[tuple[str, str], float] = {}
            rows = session.execute(
                select(
                    RateChange.institution,
                    RateChange.series_key,
                    RateChange.new_value,
                    RateChange.id,
                ).where(RateChange.dataset == dataset).order_by(RateChange.id)
            ).all()
            for institution, series_key, new_value, _ in rows:
                previous[(institution, series_key)] = float(new_value)

            written = 0
            now = utc_now()
            for (institution, series_key), new_value in values.items():
                old = previous.get((institution, series_key))
                # float karşılaştırması: oranlar en fazla 4 ondalık
                # taşıdığı için 1e-9 eşiği güvenli.
                if old is not None and abs(old - new_value) < 1e-9:
                    continue
                session.add(
                    RateChange(
                        dataset=dataset,
                        institution=institution,
                        series_key=series_key,
                        old_value=old,
                        new_value=new_value,
                        changed_at=now,
                        run_id=run_id,
                    )
                )
                written += 1
            session.commit()
            return written
    except Exception as exc:  # noqa: BLE001
        logger.error("rate_change toplu yazım başarısız (%s): %s", dataset, exc)
        return 0
