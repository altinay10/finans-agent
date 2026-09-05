from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st

from app.panels import agent, deposit, fund, fx, loan, logs, sources, status
from app.panels.common import format_amount, parse_amount
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
    .st-key-anapara input {
        font-size: 1.5rem;
        height: 3.25rem;
    }
    .st-key-anapara label p { font-size: 1rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# NEDEN number_input DEĞİL: basamakları ayıramıyor. `format` bir printf
# dizgesi (sprintf.js) ve orada binlik ayırıcı bayrağı yok; Streamlit ayrıca
# dizgeyi `float(format % 2)` ile doğruluyor, yani "%,d" gibi bir şey daha
# oluşturulurken hata veriyor. Bu yüzden kutu bir metin alanı ve tutar
# `parse_amount`/`format_amount` ile çevriliyor.
#
# BEDELİ: kutunun -/+ adım düğmeleri gitti (eski adım 50.000). Rakamların
# okunurluğu, klavyeden zaten yazılan bir alandaki düğmelerden önce geldi.
DEFAULT_PRINCIPAL = 1_000_000

if "anapara" not in st.session_state:
    st.session_state.anapara = format_amount(DEFAULT_PRINCIPAL)
    st.session_state.anapara_gecerli = DEFAULT_PRINCIPAL


def _anapara_duzelt() -> None:
    """Kutuyu her değişiklikten sonra kanonik gösterime çevirir.

    Kullanıcı "2500000" yazsa da kutuda "2,500,000" görür. İçinde hiç rakam
    yoksa son geçerli tutara dönülür; sessizce sıfıra düşmek bütün sekmelerin
    hesabını fark edilmeden bozardı.
    """
    parsed = parse_amount(st.session_state.anapara)
    if parsed is None:
        parsed = st.session_state.anapara_gecerli
    st.session_state.anapara_gecerli = parsed
    st.session_state.anapara = format_amount(parsed)


st.text_input("Anapara (TL)", key="anapara", on_change=_anapara_duzelt)
principal = st.session_state.anapara_gecerli

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
