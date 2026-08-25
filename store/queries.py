"""Okuma sorguları — panelin ve toplama katmanının kullandığı tek yer.

core/ bu modülü asla import etmez; store -> core yönünde veri akar, tersi değil.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, select, text

from store.db import SessionLocal
from store.models import (
    DepositRate,
    HttpRequest,
    Fund,
    FundPrice,
    FxQuote,
    Institution,
    LlmCall,
    LoanRate,
    LoanReferenceQuote,
    ProfitShareRatio,
    RateChange,
    SourceRun,
)


def institution_kinds() -> dict[str, str]:
    """Kurum kodu -> 'bank' | 'participation' | 'reference'.

    Katılım bankalarında getiri "faiz" değil "kar payı", finansmanda da
    "faiz oranı" değil "kâr oranı" (tasarım §07). Paneller bu ayrımı elle
    tutulan bir listeden değil, config/sources.yaml'dan seed edilen
    institutions tablosundan okur — tek doğruluk kaynağı orasıdır.
    """
    with SessionLocal() as session:
        return {
            r.code: r.kind for r in session.execute(select(Institution)).scalars().all()
        }


def list_funds() -> list[dict]:
    with SessionLocal() as session:
        rows = session.execute(select(Fund)).scalars().all()
        return [
            {
                "code": r.code,
                "name": r.name,
                "is_equity_heavy": r.is_equity_heavy,
                "benchmark": r.benchmark,
            }
            for r in rows
        ]


def latest_fx_by_institution() -> list[dict]:
    """Her (kurum, döviz) çifti için en güncel kotasyon."""
    with SessionLocal() as session:
        subq = (
            select(
                FxQuote.institution,
                FxQuote.currency,
                func.max(FxQuote.quoted_at).label("max_quoted_at"),
            )
            .group_by(FxQuote.institution, FxQuote.currency)
            .subquery()
        )
        stmt = select(FxQuote).join(
            subq,
            (FxQuote.institution == subq.c.institution)
            & (FxQuote.currency == subq.c.currency)
            & (FxQuote.quoted_at == subq.c.max_quoted_at),
        )
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "institution": r.institution,
                "currency": r.currency,
                "buy": float(r.buy),
                "sell": float(r.sell),
                "quoted_at": r.quoted_at,
                "fetched_at": r.fetched_at,
                "quoted_at_is_estimated": r.quoted_at_is_estimated,
            }
            for r in rows
        ]


def deposit_rates_for_amount(principal: float, currency: str = "TRY") -> list[dict]:
    """Anaparanın düştüğü tutar kademesindeki en güncel oranlar."""
    with SessionLocal() as session:
        # DİKKAT: currency hem alt sorguda hem DIŞ sorguda süzülmeli ve join
        # anahtarına dahil olmalı. Aksi halde aynı (kurum, vade, tutar, tarih)
        # kombinasyonuna sahip TRY ve USD satırları birbirine karışır ve panel
        # USD sorgusuna TRY oranını döndürür (regresyon testi:
        # tests/test_integration_store.py::test_deposit_currency_filter_isolates_currencies).
        amount_filter = (
            DepositRate.amount_min <= principal,
            (DepositRate.amount_max.is_(None)) | (DepositRate.amount_max >= principal),
        )
        subq = (
            select(
                DepositRate.institution,
                DepositRate.currency,
                DepositRate.term_days,
                DepositRate.amount_min,
                func.max(DepositRate.valid_date).label("max_valid_date"),
            )
            .where(DepositRate.currency == currency, *amount_filter)
            .group_by(
                DepositRate.institution,
                DepositRate.currency,
                DepositRate.term_days,
                DepositRate.amount_min,
            )
            .subquery()
        )
        stmt = (
            select(DepositRate)
            .join(
                subq,
                (DepositRate.institution == subq.c.institution)
                & (DepositRate.currency == subq.c.currency)
                & (DepositRate.term_days == subq.c.term_days)
                & (DepositRate.amount_min == subq.c.amount_min)
                & (DepositRate.valid_date == subq.c.max_valid_date),
            )
            .where(DepositRate.currency == currency, *amount_filter)
        )
        rows = session.execute(stmt).scalars().all()
        result = [
            {
                "institution": r.institution,
                "currency": r.currency,
                "term_days": r.term_days,
                "annual_rate": float(r.annual_rate),
                "is_profit_share": r.is_profit_share,
                "valid_date": r.valid_date,
                "fetched_at": r.fetched_at,
                "amount_min": float(r.amount_min),
                "amount_max": float(r.amount_max) if r.amount_max is not None else None,
            }
            for r in rows
        ]
        return _resolve_overlapping_tiers(result)


def _resolve_overlapping_tiers(rows: list[dict]) -> list[dict]:
    """Aynı (kurum, döviz, vade) için birden fazla tutar kademesi eşleşirse tekini bırak.

    Bankaların kademe sınırları uçlarda ÖRTÜŞEBİLİYOR — VakıfBank canlı
    veride hem "500.001 - 1.000.000" hem "1.000.000 - 2.999.999" kademesini
    yayınlıyor, yani tam 1.000.000 TL her ikisine de düşüyor. Bu durumda
    panelde aynı vade için iki satır çıkar ve kullanıcı hangisinin geçerli
    olduğunu bilemez.

    Çözüm: müşterinin lehine olanı, yani EN YÜKSEK oranı bırak. Banka her iki
    kademeyi de bu tutara açık ilan ettiği için bu uydurma değil, ilan
    edilmiş iki seçenekten iyisidir. Eşitlikte dar kademe (üst sınırı olan)
    tercih edilir.
    """
    best: dict[tuple[str, str, int], dict] = {}
    for row in rows:
        key = (row["institution"], row["currency"], row["term_days"])
        current = best.get(key)
        if current is None:
            best[key] = row
            continue
        if row["annual_rate"] > current["annual_rate"]:
            best[key] = row
        elif row["annual_rate"] == current["annual_rate"] and current["amount_max"] is None:
            best[key] = row
    return list(best.values())


def profit_share_ratios(principal: float, currency: str = "TRY") -> list[dict]:
    """Katılım bankası KÂR PAYLAŞIM oranları — faiz değil, getiri hesabına girmez.

    Anaparanın düştüğü bakiye kademesindeki en güncel satırlar. Panel bunları
    ayrı bir blokta, ne oldukları açıkça yazılarak gösterir
    (bkz. collectors/profit_shares.py).
    """
    with SessionLocal() as session:
        amount_filter = (
            ProfitShareRatio.amount_min <= principal,
            (ProfitShareRatio.amount_max.is_(None)) | (ProfitShareRatio.amount_max >= principal),
        )
        subq = (
            select(
                ProfitShareRatio.institution,
                ProfitShareRatio.currency,
                ProfitShareRatio.term_days,
                func.max(ProfitShareRatio.valid_date).label("max_valid_date"),
            )
            .where(ProfitShareRatio.currency == currency, *amount_filter)
            .group_by(
                ProfitShareRatio.institution,
                ProfitShareRatio.currency,
                ProfitShareRatio.term_days,
            )
            .subquery()
        )
        stmt = (
            select(ProfitShareRatio)
            .join(
                subq,
                (ProfitShareRatio.institution == subq.c.institution)
                & (ProfitShareRatio.currency == subq.c.currency)
                & (ProfitShareRatio.term_days == subq.c.term_days)
                & (ProfitShareRatio.valid_date == subq.c.max_valid_date),
            )
            .where(ProfitShareRatio.currency == currency, *amount_filter)
            .order_by(ProfitShareRatio.term_days)
        )
        rows = session.execute(stmt).scalars().all()
        # Kademeler uçlarda örtüşebilir; aynı vade için en yüksek paylaşım
        # oranını bırak (mevduat tarafındaki _resolve_overlapping_tiers ile
        # aynı gerekçe).
        best: dict[tuple[str, int], dict] = {}
        for r in rows:
            row = {
                "institution": r.institution,
                "currency": r.currency,
                "term_label": r.term_label,
                "term_days": r.term_days,
                "amount_min": float(r.amount_min),
                "amount_max": float(r.amount_max) if r.amount_max is not None else None,
                "share_ratio": float(r.share_ratio),
                "withholding_rate": (
                    float(r.withholding_rate) if r.withholding_rate is not None else None
                ),
                "valid_date": r.valid_date,
                "fetched_at": r.fetched_at,
            }
            key = (r.institution, r.term_days)
            current = best.get(key)
            if current is None or row["share_ratio"] > current["share_ratio"]:
                best[key] = row
        return sorted(best.values(), key=lambda x: (x["institution"], x["term_days"]))


def latest_loan_rates(loan_type: str) -> list[dict]:
    with SessionLocal() as session:
        subq = (
            select(LoanRate.institution, func.max(LoanRate.valid_date).label("max_valid_date"))
            .where(LoanRate.loan_type == loan_type)
            .group_by(LoanRate.institution)
            .subquery()
        )
        stmt = select(LoanRate).join(
            subq,
            (LoanRate.institution == subq.c.institution)
            & (LoanRate.valid_date == subq.c.max_valid_date),
        ).where(LoanRate.loan_type == loan_type)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "institution": r.institution,
                "loan_type": r.loan_type,
                "monthly_rate": float(r.monthly_rate),
                "term_min": r.term_min,
                "term_max": r.term_max,
                "amount_max": float(r.amount_max) if r.amount_max is not None else None,
                "valid_date": r.valid_date,
                "fetched_at": r.fetched_at,
            }
            for r in rows
        ]


def loan_type_counts() -> dict[str, int]:
    """Kredi türü -> o türde verisi olan kurum sayısı.

    Panelde "Konut: 3 banka · İhtiyaç: 5 · Taşıt: 1" satırını basar.
    Kullanıcının "ödeme planında neden sadece 3 banka var" sorusu tam olarak
    buydu: veri eksik değil, o türü yayınlayan banka sayısı üç.
    """
    with SessionLocal() as session:
        subq = (
            select(
                LoanRate.loan_type,
                LoanRate.institution,
                func.max(LoanRate.valid_date).label("max_valid_date"),
            )
            .group_by(LoanRate.loan_type, LoanRate.institution)
            .subquery()
        )
        rows = session.execute(
            select(subq.c.loan_type, func.count()).group_by(subq.c.loan_type)
        ).all()
        return {r[0]: r[1] for r in rows}


def loan_reference_quotes(loan_type: str | None = None) -> list[dict]:
    """Bankaların KENDİ ilan ettiği taksitler — hesabımızın denetim çapası.

    Her (kurum, tür, tutar, vade) için en güncel satır.
    """
    with SessionLocal() as session:
        subq = (
            select(
                LoanReferenceQuote.institution,
                LoanReferenceQuote.loan_type,
                LoanReferenceQuote.principal,
                LoanReferenceQuote.term_months,
                func.max(LoanReferenceQuote.valid_date).label("max_valid_date"),
            )
            .group_by(
                LoanReferenceQuote.institution,
                LoanReferenceQuote.loan_type,
                LoanReferenceQuote.principal,
                LoanReferenceQuote.term_months,
            )
            .subquery()
        )
        stmt = select(LoanReferenceQuote).join(
            subq,
            (LoanReferenceQuote.institution == subq.c.institution)
            & (LoanReferenceQuote.loan_type == subq.c.loan_type)
            & (LoanReferenceQuote.principal == subq.c.principal)
            & (LoanReferenceQuote.term_months == subq.c.term_months)
            & (LoanReferenceQuote.valid_date == subq.c.max_valid_date),
        )
        if loan_type:
            stmt = stmt.where(LoanReferenceQuote.loan_type == loan_type)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "institution": r.institution,
                "loan_type": r.loan_type,
                "principal": float(r.principal),
                "term_months": r.term_months,
                "monthly_rate": float(r.monthly_rate),
                "bank_installment": float(r.bank_installment),
                "bank_annual_cost_rate": (
                    float(r.bank_annual_cost_rate)
                    if r.bank_annual_cost_rate is not None
                    else None
                ),
                "valid_date": r.valid_date,
                "fetched_at": r.fetched_at,
            }
            for r in rows
        ]


def fund_price_series(fund_code: str, start: date, end: date) -> list[tuple[date, float]]:
    with SessionLocal() as session:
        stmt = (
            select(FundPrice.price_date, FundPrice.price)
            .where(
                FundPrice.fund_code == fund_code,
                FundPrice.price_date >= start,
                FundPrice.price_date <= end,
            )
            .order_by(FundPrice.price_date)
        )
        return [(row[0], float(row[1])) for row in session.execute(stmt).all()]


def source_runs(limit: int = 200) -> list[dict]:
    """Son N kaynak (banka) sonucu — panelin Kayıtlar sekmesini besler."""
    with SessionLocal() as session:
        rows = session.execute(
            select(SourceRun).order_by(SourceRun.id.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "run_id": r.run_id,
                "collector": r.collector,
                "source": r.source,
                "phase": r.phase,
                "status": r.status,
                "rows": r.rows,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "started_at": r.started_at,
            }
            for r in rows
        ]


def source_health(hours: int = 48) -> list[dict]:
    """Kaynak başına son durum ve başarısızlık sayısı.

    Bir bankanın sayfası değiştiğinde koşu 'ok' görünmeye devam eder
    (diğerleri çalıştığı için). Sessiz bozulmayı yakalayan sorgu budur.
    """
    with SessionLocal() as session:
        rows = session.execute(
            text(
                """
                SELECT collector, source,
                       MAX(started_at)                                       AS last_seen,
                       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)        AS ok_count,
                       SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END)       AS bad_count,
                       MAX(CASE WHEN status = 'ok' THEN started_at END)      AS last_ok,
                       MAX(CASE WHEN status != 'ok' THEN error END)          AS last_error
                FROM source_runs
                WHERE started_at > datetime('now', :window)
                GROUP BY collector, source
                ORDER BY bad_count DESC, collector, source
                """
            ),
            {"window": f"-{int(hours)} hours"},
        ).mappings().all()
        return [dict(r) for r in rows]


def llm_calls(limit: int = 100) -> list[dict]:
    """LLM çağrı geçmişi ve token muhasebesi."""
    with SessionLocal() as session:
        rows = session.execute(
            select(LlmCall).order_by(LlmCall.id.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "collector": r.collector,
                "model": r.model,
                "status": r.status,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "rows_recovered": r.rows_recovered,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "created_at": r.created_at,
            }
            for r in rows
        ]


def llm_token_totals(days: int = 30) -> dict:
    """Son N günün token toplamı — 'ne kadar harcadı' sorusunun cevabı."""
    with SessionLocal() as session:
        row = session.execute(
            text(
                """
                SELECT COUNT(*)                     AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens
                FROM llm_calls
                WHERE status = 'ok' AND created_at > datetime('now', :window)
                """
            ),
            {"window": f"-{int(days)} days"},
        ).mappings().one()
        return dict(row)


def freshness() -> list[dict]:
    """Panelin üst şeridini besleyen sorgu — bkz. tasarım dokümanı §02."""
    with SessionLocal() as session:
        rows = session.execute(text("SELECT * FROM v_freshness")).mappings().all()
        return [dict(r) for r in rows]


# ===========================================================================
# Ayrıntılı gözlemlenebilirlik sorguları — PLAN.md madde 7.
#
# Buradaki her sorgu, panelde CEVAPLANAMAYAN bir soruya karşılık gelir.
# Soruyu docstring'e yazmak, sorgunun neden var olduğunu koruyor.
# ===========================================================================


def http_requests(limit: int = 200, only_failures: bool = False) -> list[dict]:
    """"Hangi API istekleri ne zaman dönmedi?" — istek düzeyinde döküm."""
    with SessionLocal() as session:
        stmt = select(HttpRequest).order_by(HttpRequest.id.desc()).limit(limit)
        if only_failures:
            stmt = select(HttpRequest).where(HttpRequest.outcome != "ok").order_by(
                HttpRequest.id.desc()
            ).limit(limit)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "id": r.id,
                "collector": r.collector,
                "source": r.source,
                "method": r.method,
                "url": r.url,
                "status_code": r.status_code,
                "duration_ms": r.duration_ms,
                "response_bytes": r.response_bytes,
                "outcome": r.outcome,
                "attempt": r.attempt,
                "error": r.error,
                "created_at": r.created_at,
            }
            for r in rows
        ]


def http_endpoint_health(days: int = 7) -> list[dict]:
    """Uç nokta başına başarı oranı ve gecikme — hangi kaynak kırılgan?"""
    with SessionLocal() as session:
        rows = session.execute(
            text(
                """
                SELECT collector, source, method, url,
                       COUNT(*)                                            AS calls,
                       SUM(CASE WHEN outcome = 'ok' THEN 1 ELSE 0 END)      AS ok_count,
                       SUM(CASE WHEN outcome != 'ok' THEN 1 ELSE 0 END)     AS bad_count,
                       AVG(duration_ms)                                     AS avg_ms,
                       MAX(duration_ms)                                     AS max_ms,
                       AVG(response_bytes)                                  AS avg_bytes,
                       MAX(created_at)                                      AS last_seen
                FROM http_requests
                WHERE created_at > datetime('now', :window)
                GROUP BY collector, source, method, url
                ORDER BY bad_count DESC, calls DESC
                """
            ),
            {"window": f"-{int(days)} days"},
        ).mappings().all()
        return [dict(r) for r in rows]


def source_recovery(days: int = 30) -> list[dict]:
    """"Dönmeyen istekler daha sonra döndü mü?" — kesinti ve kurtarma izi.

    Her kaynak için son başarısızlık ve ondan SONRAKİ ilk başarı bulunur.
    İkisi arasındaki fark kesinti süresidir. Hâlâ başarı gelmemişse kaynak
    şu an bozuktur ve kesinti sürüyordur.

    Bu sorgu olmadan "dün üç koşu düştü, bugün düzeldi" cümlesi
    kurulamıyordu: source_runs ham olayları tutar, ama olayları KESİNTİYE
    dönüştüren mantık burada.
    """
    with SessionLocal() as session:
        rows = session.execute(
            text(
                """
                WITH recent AS (
                    SELECT collector, source, status, started_at
                    FROM source_runs
                    WHERE started_at > datetime('now', :window)
                      AND phase IN ('fetch', 'parse')
                )
                SELECT collector, source,
                       SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END)  AS failures,
                       COUNT(*)                                          AS attempts,
                       MAX(CASE WHEN status != 'ok' THEN started_at END) AS last_failure,
                       MAX(CASE WHEN status  = 'ok' THEN started_at END) AS last_success
                FROM recent
                GROUP BY collector, source
                HAVING failures > 0
                ORDER BY failures DESC
                """
            ),
            {"window": f"-{int(days)} days"},
        ).mappings().all()

        result = []
        for row in rows:
            item = dict(row)
            last_failure = _as_dt(item["last_failure"])
            last_success = _as_dt(item["last_success"])
            if last_success and last_failure and last_success > last_failure:
                item["state"] = "düzeldi"
                item["outage_minutes"] = None
            elif last_failure and not last_success:
                item["state"] = "hiç başarılı olmadı"
                item["outage_minutes"] = None
            elif last_failure:
                item["state"] = "hâlâ bozuk"
                item["outage_minutes"] = int(
                    (datetime.now(timezone.utc).replace(tzinfo=None) - last_failure).total_seconds()
                    / 60
                )
            else:
                item["state"] = "—"
                item["outage_minutes"] = None
            result.append(item)
        return result


def schema_drift(lookback_runs: int = 5) -> list[dict]:
    """"Sayfa kısmen değişti mi?" — satır sayısındaki ani düşüş uyarısı.

    Bir banka sayfasını kısmen değiştirdiğinde parser genelde PATLAMAZ:
    bazı satırları okumaya devam eder, gerisini sessizce kaybeder. Koşu
    'ok' görünür, panel eksik veri gösterir. Tek erken sinyal, o kaynaktan
    gelen satır sayısının önceki koşulara göre çökmesidir.

    Eşik %40: banka gerçekten bir vade dilimini kaldırmış olabilir (küçük
    düşüş normaldir), ama satırların yarıya yakını kaybolduysa bu ürün
    değişikliği değil ayrıştırma kaybıdır.
    """
    with SessionLocal() as session:
        rows = session.execute(
            text(
                """
                SELECT collector, source, rows, started_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY collector, source ORDER BY id DESC
                       ) AS rn
                FROM source_runs
                WHERE phase = 'parse' AND status = 'ok'
                """
            )
        ).mappings().all()

    history: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row["rn"] > lookback_runs + 1:
            continue
        history.setdefault((row["collector"], row["source"]), []).append(dict(row))

    alerts = []
    for (collector, source), runs in history.items():
        runs.sort(key=lambda r: r["rn"])
        latest, previous = runs[0], runs[1:]
        if not previous:
            continue
        baseline = sum(r["rows"] for r in previous) / len(previous)
        if baseline <= 0:
            continue
        drop = (baseline - latest["rows"]) / baseline
        if drop >= 0.40:
            alerts.append(
                {
                    "collector": collector,
                    "source": source,
                    "latest_rows": latest["rows"],
                    "baseline_rows": round(baseline, 1),
                    "drop_pct": round(drop * 100, 1),
                    "started_at": latest["started_at"],
                }
            )
    return sorted(alerts, key=lambda a: a["drop_pct"], reverse=True)


def stale_values(dataset: str, days: int = 14) -> list[dict]:
    """"Uç nokta çalışıyor ama sayı donmuş olabilir mi?"

    Bir serinin son değişim tarihi çok eskiyse ya oran gerçekten sabit ya
    da besleme durmuş. Panel karar vermez, sadece "şu kadar gündür
    değişmedi" der — bu ikisini ayırt etmek kullanıcının işidir.
    """
    with SessionLocal() as session:
        rows = session.execute(
            text(
                """
                SELECT institution, series_key,
                       MAX(changed_at) AS last_change,
                       COUNT(*)        AS change_count
                FROM rate_changes
                WHERE dataset = :dataset
                GROUP BY institution, series_key
                HAVING last_change < datetime('now', :window)
                ORDER BY last_change
                """
            ),
            {"dataset": dataset, "window": f"-{int(days)} days"},
        ).mappings().all()
        return [dict(r) for r in rows]


def rate_changes(dataset: str | None = None, limit: int = 100) -> list[dict]:
    """Son oran değişimleri — "ne zaman neyi değiştirdiler"."""
    with SessionLocal() as session:
        stmt = select(RateChange).order_by(RateChange.id.desc()).limit(limit)
        if dataset:
            stmt = (
                select(RateChange)
                .where(RateChange.dataset == dataset)
                .order_by(RateChange.id.desc())
                .limit(limit)
            )
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "dataset": r.dataset,
                "institution": r.institution,
                "series_key": r.series_key,
                "old_value": float(r.old_value) if r.old_value is not None else None,
                "new_value": float(r.new_value),
                "changed_at": r.changed_at,
            }
            for r in rows
        ]


def run_failures(days: int = 30) -> list[dict]:
    """Aşama bazında başarısızlık sayımı — 'sanity' ayrı görünmeli.

    `sanity` bir arıza DEĞİL, korumanın çalıştığının kanıtıdır: veri geldi,
    ayrıştırıldı, ama bant dışıydı ve bilerek yazılmadı. Bu ayrım hata
    metnine gömülü kaldığı sürece "kaç kez saçma veri geldi" sorusu
    sayılamıyordu.
    """
    with SessionLocal() as session:
        rows = session.execute(
            text(
                """
                SELECT collector,
                       COALESCE(failure_kind, 'bilinmiyor') AS failure_kind,
                       COUNT(*) AS count,
                       MAX(started_at) AS last_seen
                FROM scrape_runs
                WHERE status = 'failed' AND started_at > datetime('now', :window)
                GROUP BY collector, failure_kind
                ORDER BY count DESC
                """
            ),
            {"window": f"-{int(days)} days"},
        ).mappings().all()
        return [dict(r) for r in rows]


def run_triggers(days: int = 7) -> list[dict]:
    """"Telafi mekanizması gerçekten çalışıyor mu?"

    Zamanlayıcı üç sebeple koşu başlatabilir: planlanmış saat, açılış,
    tazelik telafisi. Sunucuda erişim olmayacağı için telafinin devreye
    girip girmediğini gösteren tek kayıt budur.
    """
    with SessionLocal() as session:
        rows = session.execute(
            text(
                """
                SELECT COALESCE(trigger, 'bilinmiyor') AS trigger,
                       collector,
                       COUNT(*) AS count,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                       MAX(started_at) AS last_seen
                FROM scrape_runs
                WHERE started_at > datetime('now', :window)
                GROUP BY trigger, collector
                ORDER BY trigger, collector
                """
            ),
            {"window": f"-{int(days)} days"},
        ).mappings().all()
        return [dict(r) for r in rows]


def llm_cost_totals(days: int = 30) -> dict:
    """Token + TAHMİNİ MALİYET. Fiyat tanımlı değilse maliyet None kalır."""
    with SessionLocal() as session:
        row = session.execute(
            text(
                """
                SELECT COUNT(*)                             AS calls,
                       COALESCE(SUM(prompt_tokens), 0)      AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0)  AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)       AS total_tokens,
                       SUM(cost_usd)                        AS cost_usd,
                       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)     AS ok_calls,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_calls,
                       SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END) AS disabled_calls,
                       SUM(CASE WHEN status = 'budget_exceeded' THEN 1 ELSE 0 END)
                                                                          AS budget_calls
                FROM llm_calls
                WHERE created_at > datetime('now', :window)
                """
            ),
            {"window": f"-{int(days)} days"},
        ).mappings().one()
        return dict(row)


def llm_triggers(limit: int = 50) -> list[dict]:
    """"Agent ne zaman hangi durumda devreye girdi?"

    Tetikleyen HATA olmadan bu soru cevaplanamıyordu: `collector` alanı
    hangi toplayıcının kırıldığını söyler, hangi bankanın hangi hatasının
    modeli çağırdığını söylemez.
    """
    with SessionLocal() as session:
        rows = session.execute(
            select(LlmCall).order_by(LlmCall.id.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "created_at": r.created_at,
                "collector": r.collector,
                "status": r.status,
                "model": r.model,
                "trigger_source": r.trigger_source,
                "trigger_error": r.trigger_error,
                "total_tokens": r.total_tokens,
                "cost_usd": float(r.cost_usd) if r.cost_usd is not None else None,
                "rows_recovered": r.rows_recovered,
                "duration_ms": r.duration_ms,
            }
            for r in rows
        ]


def _as_dt(value):
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value
