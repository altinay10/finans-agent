from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st

from app.panels import deposit, fund, fx, loan, logs, sources
from store import heartbeat, queries
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


def _render_scheduler_status() -> None:
    """Zamanlayıcı gerçekten çalışıyor mu — TAHMİN DEĞİL, ÖLÇÜM.

    Panel eskiden yalnızca verinin yaşına bakıp "zamanlayıcı çalışmıyor
    gibi" diyordu. "Gibi" boşuna değildi: panel bunu bilmiyordu ve iki
    bambaşka arıza (süreç hiç yok / süreç var ama kaynaklar düşmüş) aynı
    mesajı üretiyordu. Artık nabza bakılıyor (store/heartbeat.py).

    Panel zamanlayıcıyı BAŞLATMAZ — bunu konteyner giriş noktası yapar
    (`run.py`, ROLE=all). Panelin başlatması, "birisi siteyi açana kadar
    veri toplanmasın" demek olurdu; bkz. run.py docstring'i.
    """
    beat = heartbeat.read()

    if beat and beat["alive"]:
        st.caption(
            f"🟢 Zamanlayıcı çalışıyor — son nabız {beat['age_seconds']:.0f} sn önce "
            f"(host `{beat['host']}`, pid {beat['pid']})."
        )
        return

    if beat:
        hours = beat["age_seconds"] / 3600
        st.error(
            f"**Zamanlayıcı DURMUŞ.** Son nabız {hours:.1f} saat önce "
            f"(host `{beat['host']}`, pid {beat['pid']}). Süreç çökmüş ya da "
            "konteyner yeniden başlatılmış olabilir.\n\n"
            "- Docker: `docker compose up -d scheduler` · log: `docker compose logs scheduler`\n"
            "- Tek konteyner: `ROLE=all` ile başlatılmış mı? (`docker run -e ROLE=all ...`)\n"
            "- systemd: `systemctl status finans-scheduler`"
        )
        return

    st.error(
        "**Zamanlayıcı hiç çalışmamış — veri toplanmıyor.** Panel veri "
        "toplamaz, ayrı bir süreç gerekir:\n\n"
        "- Tek konteyner: `docker run -e ROLE=all -p 8501:8501 finans-agent` "
        "(varsayılan `ROLE=all` zaten zamanlayıcıyı da başlatır)\n"
        "- Compose: `docker compose up -d` (panel + scheduler birlikte)\n"
        "- systemd: `systemctl start finans-scheduler`\n"
        "- Hemen tek seferlik tazeleme: `python worker.py all`"
    )


def _render_freshness_strip() -> None:
    rows = _freshness()
    if not rows:
        st.warning(
            "**Henüz hiçbir toplayıcı tamamlanmamış.** Yeni kurulumda normaldir: "
            "ilk tur birkaç dakika sürer. Birkaç dakika sonra da boşsa yukarıdaki "
            "zamanlayıcı satırına bak."
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
        # Zamanlayıcı AYAKTA ama veri yine de eskiyse suçlu süreç değil,
        # kaynaklardır. Kullanıcıyı "süreci başlat" diye yanlış tarafa
        # göndermemek için mesaj ikiye ayrılıyor.
        if heartbeat.is_alive():
            st.error(
                f"**Zamanlayıcı çalışıyor ama tüm veri {min(known):.0f} saatten eski.** "
                "Yani sorun süreçte değil, kaynaklarda: bankaların uç noktaları "
                "düşmüş ya da sayfa yapıları değişmiş olabilir. **Kayıtlar** "
                "sekmesindeki kaynak sağlığı ve HTTP istekleri tablolarına bak."
            )
        else:
            st.error(
                f"**Tüm veri {min(known):.0f} saatten eski ve zamanlayıcı süreci yok.** "
                "Panel veri toplamaz. Docker'da: `docker compose up -d scheduler` · "
                "systemd'de: `systemctl start finans-scheduler` · "
                "hemen tazelemek için: `python worker.py all`"
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

_render_scheduler_status()
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
