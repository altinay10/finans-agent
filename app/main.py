from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st

from app.panels import agent, deposit, fund, fx, loan, logs, sources, status
from app.panels.common import principal_style
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

# ANAPARA KUTUSU SEKMELERİN İÇİNDE. Tutarla işi olan dört sekme (Döviz,
# Mevduat, Kredi, Fon) kendi kutusunu çiziyor; Agent, Kaynaklar ve Kayıtlar
# sekmelerinde kutu hiç görünmüyor. Değer sekmeler arasında ortak
# (bkz. app/panels/common.principal_input). Burada yalnızca kutuların biçim
# kuralı bir kez basılıyor — CSS sayfa düzeyinde, her sekmede tekrar
# basmanın anlamı yok.
st.markdown(principal_style(), unsafe_allow_html=True)

tab_fx, tab_dep, tab_loan, tab_fund, tab_agent, tab_sources, tab_logs = st.tabs(
    ["Döviz", "Mevduat & Kar Payı", "Kredi", "Fon Simülasyonu", "Agent",
     "Kaynaklar", "Kayıtlar"]
)

with tab_fx:
    fx.render()
with tab_dep:
    deposit.render()
with tab_loan:
    loan.render()
with tab_fund:
    fund.render()
with tab_agent:
    agent.render()
with tab_sources:
    sources.render()
with tab_logs:
    logs.render()
