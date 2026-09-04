"""Döviz paneli — her kurum kendi tablosunda.

Önceki hâli para birimi başına tek tablo gösteriyordu (USD tablosu, EUR
tablosu). Kurum başına tablo, "şu bankada kurlar ne" sorusunu tek bakışta
cevaplıyor ve tazelik/tahmini-zaman notunu kurumun kendi tablosunun altına
koymayı mümkün kılıyor — o not zaten kurum bazlı bir gerçek.

TCMB bir banka değil, referans kur; bu yüzden ilk sırada ve ayrıca
işaretlenmiş olarak gösteriliyor (tasarım §02: "resmi çapa").
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from app.panels.common import age_hours, fetched_caption, format_local, institution_label
from config.loader import source_caveat, source_summary
from store import queries

STALE_HOURS = 2
ESTIMATED_MARK = "≈"



@st.cache_data(ttl=300)
def _latest_fx() -> list[dict]:
    return queries.latest_fx_by_institution()



def render() -> None:
    st.subheader("Döviz Kurları")
    rows = _latest_fx()
    if not rows:
        st.info("Henüz kur verisi toplanmadı. `python worker.py fx_tcmb` veya `fx_banks` çalıştırılmalı.")
        return

    df = pd.DataFrame(rows)
    now = datetime.now(timezone.utc)
    df["age_h"] = df["quoted_at"].apply(lambda q: age_hours(q, now))
    df["is_fresh"] = df["age_h"] <= STALE_HOURS

    st.caption(
        f"Her kurum kendi tablosunda. {STALE_HOURS} saatten eski satırlar soluk gösterilir; "
        f"**{ESTIMATED_MARK}** işaretli satırlarda kaynak kendi güncelleme saatini vermiyor, "
        "gösterilen an bizim çektiğimiz andır. Saatler Europe/Istanbul."
    )

    # TCMB önce (referans), kalanlar alfabetik.
    institutions = sorted(df["institution"].unique(), key=lambda i: (i != "TCMB", i))

    for institution in institutions:
        sub = df[df["institution"] == institution].sort_values("currency").copy()
        st.markdown(f"### {institution_label(institution)}")

        display = pd.DataFrame(
            {
                "Döviz": sub["currency"] + "/TRY",
                "Alış": sub["buy"],
                "Satış": sub["sell"],
                "Makas": sub["sell"] - sub["buy"],
                "Geçerlilik anı": [
                    (ESTIMATED_MARK + " " if est else "") + format_local(q)
                    for q, est in zip(sub["quoted_at"], sub["quoted_at_is_estimated"])
                ],
                "Yaş (saat)": sub["age_h"],
            }
        )

        stale_mask = (~sub["is_fresh"]).tolist()

        def _gray_stale(row, mask=stale_mask, frame=display):
            pos = frame.index.get_indexer([row.name])[0]
            return ["color: gray" if mask[pos] else ""] * len(row)

        styled = display.style.apply(_gray_stale, axis=1).format(
            {"Alış": "{:,.4f}", "Satış": "{:,.4f}", "Makas": "{:,.4f}", "Yaş (saat)": "{:,.1f}"}
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

        notes = [fetched_caption(sub["fetched_at"].tolist())]
        # Sayının kaynağı sayının YANINDA dursun; README sunucuda görünmüyor.
        summary = source_summary("fx_endpoints", institution)
        if summary:
            notes.append(summary)
        if bool((~sub["is_fresh"]).any()):
            notes.append(f"{STALE_HOURS} saatten eski — kotasyon bayat olabilir.")
        if bool(sub["quoted_at_is_estimated"].any()):
            notes.append(
                f"{ESTIMATED_MARK} Kaynak kendi kotasyon saatini yayınlamıyor; "
                "kotasyonun gerçek yaşı bilinmiyor."
            )
        # Ölçüm tabanı farklıysa kullanıcı bunu tablonun DİBİNDE değil,
        # tablonun hemen altında görmeli (bkz. config/loader.source_caveat).
        caveat = source_caveat("fx_endpoints", institution)
        if caveat:
            notes.append(f"⚠️ {caveat}")
        st.caption("  \n".join(notes))
