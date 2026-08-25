from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st

from app.panels import deposit, fund, fx, loan, logs, sources
from store import queries
from store.db import init_db

st.set_page_config(page_title="Finans Agent", layout="wide")
init_db()


@st.cache_data(ttl=120)
def _freshness() -> list[dict]:
    return queries.freshness()


# Bu süreden eski veri, "kimse toplamıyor" demektir. En cömert toplayıcı
# sınırı 30 saat (bkz. scheduler.MAX_AGE_HOURS); onun da iki katını aşmışsa
# zamanlayıcı ya kurulmamış ya da düşmüş.
NO_COLLECTOR_WARNING_HOURS = 36


def _render_freshness_strip() -> None:
    rows = _freshness()
    if not rows:
        st.error(
            "**Hiçbir toplayıcı çalışmamış — panel boş veri gösteriyor.** "
            "Veri toplama paneli tarafından yapılmaz; ayrı bir süreç gerekir: "
            "Docker'da `scheduler` servisi, systemd'de `finans-scheduler.service`, "
            "ya da `deploy/crontab.example`. Tek seferlik: `python worker.py all`"
        )
        return
    now = datetime.now(timezone.utc)
    cols = st.columns(len(rows))
    for col, row in zip(cols, rows):
        last_ok = row["last_ok"]
        if last_ok:
            ts = datetime.fromisoformat(last_ok) if isinstance(last_ok, str) else last_ok
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_h = (now - ts).total_seconds() / 3600
            label = f"{age_h:.1f} sa önce"
        else:
            label = "hiç başarılı olmadı"
        badge = "🟡" if row["fallback_count"] else "🟢" if last_ok else "🔴"
        col.metric(row["collector"], label, delta=f"{badge}")

    # Sunucuya kurulup unutulan bir sistemde en sinsi hata, verinin sessizce
    # bayatlaması. Her şey eskiyse bu "banka değişti" değil, "toplayıcı
    # koşmuyor" demektir — açıkça söyle.
    ages = [_age_hours(row["last_ok"], now) for row in rows]
    known = [a for a in ages if a is not None]
    if known and min(known) > NO_COLLECTOR_WARNING_HOURS:
        st.error(
            f"**Tüm veri {min(known):.0f} saatten eski — zamanlayıcı çalışmıyor gibi.** "
            "Panel veri toplamaz; ayrı bir süreç gerekir: Docker'da `scheduler` "
            "servisi, systemd'de `finans-scheduler.service`, ya da cron. "
            "Hemen tazelemek için: `python worker.py all`"
        )


def _age_hours(last_ok, now: datetime) -> float | None:
    if not last_ok:
        return None
    ts = datetime.fromisoformat(last_ok) if isinstance(last_ok, str) else last_ok
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 3600


st.title("Finansal Veri ve Hesaplama Agent'ı")
st.caption(
    "Kişisel kullanım içindir; hiçbir bölüm yatırım tavsiyesi değildir. "
    "Katılım bankası kar payı bir taahhüt değil, beklentidir."
)

_render_freshness_strip()
st.divider()

principal = st.number_input("Anapara (TL)", min_value=0, value=1_000_000, step=50_000)

tab_fx, tab_dep, tab_loan, tab_fund, tab_sources, tab_logs = st.tabs(
    ["Döviz", "Mevduat & Kar Payı", "Kredi", "Fon Simülasyonu", "Kaynaklar", "Kayıtlar"]
)

with tab_fx:
    fx.render()
with tab_dep:
    deposit.render(principal)
with tab_loan:
    loan.render(principal)
with tab_fund:
    fund.render(principal)
with tab_sources:
    sources.render()
with tab_logs:
    logs.render()
