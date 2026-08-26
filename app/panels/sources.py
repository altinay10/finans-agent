"""Kaynaklar sekmesi — "hangi veriyi nereden çekiyorsun?"

PLAN.md madde 1. Bu bilgi üç yere dağılmıştı: `config/sources.yaml` (asıl
kaynak), `README.md` (özet) ve toplayıcı docstring'leri (ayrıntı). Panelde
hiç yoktu. Oysa bu bir *finansal* panel: bir oranın kaynağı, oranın kendisi
kadar önemli — ve sunucuya kurulduktan sonra kullanıcı README'yi görmüyor.

İKİ AYRI GERÇEK yan yana gösteriliyor ve bu bilinçli:

* **Envanterin iddiası** (`sources.yaml`: status/last_verified) — elle
  yazılan, eskiyebilen bilgi.
* **Çalışan sistemin gerçeği** (`source_runs`: son başarılı koşu) — kendi
  kendine biriken kanıt.

İkisi çeliştiğinde (envanter "active" diyor ama günlerdir başarılı koşu yok)
panel bunu açıkça söyler. Bu çelişki gerçekten yaşandı: Enpara kredi kaynağı
"CSRF gerekiyor" diye kapalı yazılıydı, oysa toplayıcı onu sorunsuz
çekiyordu.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from app.panels.common import ISTANBUL, as_utc, institution_label
from config.loader import DATASET_LABELS, source_inventory
from store import queries

# Hangi veri kümesini hangi toplayıcı besliyor — canlı sağlıkla eşleştirmek
# için gerekli. sources.yaml bunu bilmiyor; bilgi worker.py'nin kayıt
# defterinde duruyor ve burada tek yerde eşleniyor.
DATASET_COLLECTORS = {
    "fx_endpoints": ("fx_banks", "fx_tcmb"),
    # Mevduat kümesini üç toplayıcı besliyor: faiz bankaları için
    # deposit_rates, katılım bankalarının yıllık kâr payı oranı için
    # participation_rates*. Üçü de aynı tabloya yazıyor (deposit_rates).
    "deposit_endpoints": ("deposit_rates", "participation_rates", "participation_rates_kt"),
    "loan_endpoints": ("loan_rates",),
    "loan_llm_endpoints": ("loan_rates_llm",),
    "profit_share_endpoints": ("profit_shares", "profit_shares_kt"),
    "fund_endpoints": ("fund_prices",),
}

STATUS_LABELS = {
    "active": "🟢 çekiliyor",
    "blocked": "🔴 kapalı",
    "unverified": "⚪ doğrulanmadı",
}


@st.cache_data(ttl=3600)
def _inventory() -> list[dict]:
    return source_inventory()


@st.cache_data(ttl=120)
def _health() -> list[dict]:
    # 30 gün: bir kaynak haftalardır düşmüşse bunu "hiç kayıt yok" diye
    # göstermek yanıltıcı olur; son başarısını görmek gerekir.
    return queries.source_health(hours=24 * 30)


def render() -> None:
    st.subheader("Kaynaklar")
    st.caption(
        "Paneldeki her sayının nereden geldiği. Liste `config/sources.yaml` "
        "dosyasından okunur — ikinci bir liste tutulmuyor ki kayma olmasın."
    )

    rows = _inventory()
    active = sum(1 for r in rows if r["status"] == "active")
    blocked = sum(1 for r in rows if r["status"] == "blocked")
    unverified = sum(1 for r in rows if r["status"] == "unverified")
    overrides = sum(1 for r in rows if r["robots_override"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Çekilen kaynak", active)
    c2.metric("Kapalı", blocked)
    c3.metric("Doğrulanmadı", unverified)
    c4.metric("robots kararı uygulanan", overrides)

    health = _health()

    for dataset, label in DATASET_LABELS.items():
        dataset_rows = [r for r in rows if r["dataset"] == dataset]
        if not dataset_rows:
            continue
        st.markdown(f"#### {label}")
        st.dataframe(
            pd.DataFrame(
                [_table_row(r, health) for r in sorted(dataset_rows, key=_sort_key)]
            ),
            use_container_width=True,
            hide_index=True,
        )
        _render_reasons(dataset_rows)

    _render_robots_note(rows)


def _sort_key(row: dict) -> tuple:
    """Önce çekilenler, sonra kapalılar; her grup alfabetik."""
    order = {"active": 0, "unverified": 1, "blocked": 2}
    return (order.get(row["status"], 3), row["key"])


def _table_row(row: dict, health: list[dict]) -> dict:
    name = institution_label(row["institution"]) if row["institution"] else row["key"]
    last_ok, disagreement = _live_state(row, health)
    return {
        "Kurum": name,
        "Durum (envanter)": STATUS_LABELS.get(row["status"], row["status"]),
        "Son başarılı çekim": last_ok,
        "Uyuşmazlık": disagreement,
        "Uç nokta": _short_url(row["url"]),
        "Tip": row["type"] or "—",
        "robots kararı": "✔" if row["robots_override"] else "",
        "Son elle doğrulama": _fmt_date(row["last_verified"]),
    }


def _live_state(row: dict, health: list[dict]) -> tuple[str, str]:
    """Envanterin iddiası ile çalışan sistemin gerçeğini karşılaştırır."""
    collectors = DATASET_COLLECTORS.get(row["dataset"], ())
    matches = [
        h for h in health if h["source"] == row["key"] and h["collector"] in collectors
    ]
    last_ok_raw = max((h["last_ok"] for h in matches if h["last_ok"]), default=None)

    if last_ok_raw is None:
        if row["status"] == "active":
            # Envanter "çekiliyor" diyor ama 30 günde tek başarılı koşu yok.
            return "kayıt yok", "⚠️ envanter 'çekiliyor' diyor ama başarılı koşu yok"
        return "—", ""

    ts = _parse(last_ok_raw)
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    label = (
        f"{ts.astimezone(ISTANBUL):%d.%m %H:%M} ({age_h:.0f} sa önce)"
        if age_h < 48
        else f"{ts.astimezone(ISTANBUL):%d.%m.%Y} ({age_h / 24:.0f} gün önce)"
    )
    if row["status"] != "active":
        return label, "⚠️ envanter kapalı diyor ama veri geliyor"
    return label, ""


def _parse(value) -> datetime:
    ts = datetime.fromisoformat(value) if isinstance(value, str) else value
    return as_utc(ts)


def _short_url(url: str | None) -> str:
    if not url:
        return "—"
    stripped = str(url).split("://", 1)[-1]
    return stripped if len(stripped) <= 70 else stripped[:67] + "…"


def _fmt_date(value) -> str:
    if not value:
        return "—"
    if hasattr(value, "strftime"):
        return f"{value:%d.%m.%Y}"
    return str(value)


def _render_reasons(dataset_rows: list[dict]) -> None:
    """Kapalı/doğrulanmamış kaynakların GEREKÇESİ.

    Gerekçesiz bir "kapalı" işareti, kullanıcı için "ihmal edilmiş"ten
    ayırt edilemez. Özellikle bot tespiti (WAF, F5 Shape) ile robots.txt
    ayrımı burada görünür olmalı: birincisi bilinçli olarak aşılmıyor.
    """
    blocked = [r for r in dataset_rows if r["status"] != "active" and r["blocked_reason"]]
    if not blocked:
        return
    with st.expander(f"Kapalı kaynakların gerekçeleri ({len(blocked)})"):
        for row in blocked:
            name = institution_label(row["institution"]) if row["institution"] else row["key"]
            st.markdown(f"**{name}** — {row['blocked_reason']}")


def _render_robots_note(rows: list[dict]) -> None:
    overridden = [r for r in rows if r["robots_override"]]
    if not overridden:
        return
    names = sorted({(r["institution"] or r["key"]) for r in overridden})
    labels = ", ".join(institution_label(n) for n in names)
    st.info(
        f"**robots.txt kararı.** {labels} kaynaklarında sitenin robots.txt "
        "dosyası ilgili yolu yasaklıyor; kullanıcı kararıyla (2026-08-24) bu "
        "kısıt uygulanmıyor: kişisel kullanım, günde birkaç istek, herkese "
        "açık ve kimlik doğrulaması olmayan veri. Karar geri alınırsa "
        "kapatılacak liste tam olarak budur.\n\n"
        "Bu karar **bot tespitini kapsamaz**: aktif engel (WAF, F5 Shape "
        "challenge, CAPTCHA) bulunan kaynaklar — İş Bankası, TEFAS — "
        "bilinçli olarak açılmıyor."
    )
