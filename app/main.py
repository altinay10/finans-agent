from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st

from app.panels import agent, deposit, fund, fx, loan, logs, sources, status
from store.db import init_db

st.set_page_config(page_title="Finans Agent", layout="wide")
init_db()


st.title("Finansal Veri ve Hesaplama Agent'ı")
st.caption(
    "Kişisel kullanım içindir; hiçbir bölüm yatırım tavsiyesi değildir. "
    "Katılım bankası kar payı bir taahhüt değil, beklentidir. "
    "API'si olmayan bankaların oranları için **Agent** sekmesinden kendi API "
    "anahtarını girip veriyi anında tazeleyebilirsin."
)

status.render()
st.divider()

# Anapara, paneldeki tek para giriş alanı ve bütün sekmeler onu okuyor.
# Varsayılan 14 puntoda yedi haneli tutar zor seçiliyordu; kutuyu büyütüp
# rakamları irileştiriyoruz. Kural yalnızca bu kutuya bağlı (`st-key-anapara`);
# genel `input` seçicisi kullanmak diğer panellerin form alanlarını da bozardı.
st.markdown(
    """
    <style>
    .st-key-anapara [data-testid="stNumberInputContainer"] { min-height: 3.25rem; }
    .st-key-anapara [data-testid="stNumberInputField"] {
        font-size: 1.5rem;
        height: 3.25rem;
    }
    .st-key-anapara label p { font-size: 1rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

principal = st.number_input(
    "Anapara (TL)", min_value=0, value=1_000_000, step=50_000, key="anapara"
)

tab_fx, tab_dep, tab_loan, tab_fund, tab_agent, tab_sources, tab_logs = st.tabs(
    ["Döviz", "Mevduat & Kar Payı", "Kredi", "Fon Simülasyonu", "Agent",
     "Kaynaklar", "Kayıtlar"]
)

with tab_fx:
    fx.render()
with tab_dep:
    deposit.render(principal)
with tab_loan:
    loan.render(principal)
with tab_fund:
    fund.render(principal)
with tab_agent:
    agent.render()
with tab_sources:
    sources.render()
with tab_logs:
    logs.render()
