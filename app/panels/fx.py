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

from app.panels.common import (
    age_hours, fetched_caption, format_local, institution_label, principal_input,
)
from config.loader import source_caveat, source_summary
from store import queries

#: Ticari banka kotasyonu için "bu yaştan sonra soluk göster" sınırı.
STALE_HOURS = 2

#: Kendi sınırı olan kurumlar -> sınırı taşıyan toplayıcı adı. Burada
#: yazmayan kurum için STALE_HOURS geçerli.
#
# NEDEN TCMB AYRI: ticari bankalar gün içinde kotasyonu sürekli yeniliyor,
# TCMB gösterge kurunu iş günlerinde GÜNDE BİR kez yayımlıyor. Tek bir 2
# saatlik sınır TCMB'ye de uygulandığı için panel günün ~22 saatinde resmî
# çapayı soluk gösterip "kotasyon bayat olabilir" diye uyarıyordu (canlı
# gözlem 2026-09-08: "Yaş 20,5 saat"). Uyarı yanlıştı — o kur o günün
# GEÇERLİ kuruydu — ve sürekli görünen bir uyarı, gerçekten bayatlayan
# günü de görünmez kılıyordu.
#
# SAYI BURADA YAZMIYOR, `scheduler.MAX_AGE_HOURS`'tan okunuyor: panelin
# "bayat göster" eşiği ile zamanlayıcının "yeniden dene" eşiği aynı gerçeğe
# (TCMB günde bir yayımlar) dayanıyor ve ikinci bir kopya, biri değişince
# sessizce kayardı — panel bayat derken zamanlayıcı hiçbir şey yapmazdı.
# Aynı ilke durum bandında da uygulanıyor (bkz. app/panels/status._limits).
STALE_COLLECTOR_BY_INSTITUTION = {"TCMB": "fx_tcmb"}

ESTIMATED_MARK = "≈"


def stale_hours(institution: str) -> float:
    """Bu kurumun kotasyonu kaç saat sonra 'bayat' sayılır."""
    collector = STALE_COLLECTOR_BY_INSTITUTION.get(institution)
    if collector is None:
        return STALE_HOURS
    # Geç import: `scheduler` modül düzeyinde log dosyası açıyor, panelin
    # açılış maliyetine girmesin (status paneli de böyle çağırıyor).
    from scheduler import MAX_AGE_HOURS

    return MAX_AGE_HOURS[collector]



@st.cache_data(ttl=300)
def _latest_fx() -> list[dict]:
    return queries.latest_fx_by_institution()



def tcmb_conversion(df: pd.DataFrame, principal: float) -> pd.DataFrame:
    """Girilen tutarın TCMB kuruyla döviz karşılığı — saf hesap.

    NEDEN TCMB: bu sekmede kur veren onlarca kurum var ve "1.000.000 TL kaç
    dolar" sorusunun bankaya göre değişen bir cevabı olurdu. TCMB gösterge
    kuru resmî çapa (tasarım §02); hesabın tek ve tartışmasız bir tabanı
    olsun diye karşılık ondan hesaplanıyor, hangi kurla hesaplandığı da
    tablonun altına açıkça yazılıyor.

    SATIŞ KURU: TL verip döviz alan taraf satış kurundan alır. Alış kuru
    ters yön içindir (elindeki dövizi TL'ye çevirmek) ve TCMB tablosunda
    aşağıda zaten görünüyor.

    Sıfır/eksi satış kuru olan satırlar eleniyor: bozuk tek bir satır
    yüzünden bölme hatası alıp bütün sekmenin çökmesi, o satırı atlamaktan
    kötü.
    """
    tcmb = df[(df["institution"] == "TCMB") & (df["sell"] > 0)].sort_values("currency").copy()
    tcmb["converted"] = principal / tcmb["sell"]
    return tcmb


def _render_tcmb_conversion(df: pd.DataFrame, principal: float) -> None:
    tcmb = tcmb_conversion(df, principal)
    if tcmb.empty:
        st.caption(
            "TCMB kuru henüz toplanmadı; tutarın döviz karşılığı hesaplanamıyor "
            "(`python worker.py fx_tcmb`)."
        )
        return

    karsilik_basligi = f"{principal:,.0f} TL karşılığı"
    display = pd.DataFrame(
        {
            "Döviz": tcmb["currency"],
            "TCMB satış kuru": tcmb["sell"],
            karsilik_basligi: tcmb["converted"],
        }
    )
    st.dataframe(
        display.style.format({"TCMB satış kuru": "{:,.4f}", karsilik_basligi: "{:,.2f}"}),
        width="stretch",
        hide_index=True,
    )

    notes = [
        f"**{principal:,.0f} TL**, **TCMB satış kuru** ile hesaplandı "
        f"(kur geçerlilik anı: **{format_local(max(tcmb['quoted_at']))}**). "
        "Bu sekmede girilen tutar TL kabul edilir.",
        "TCMB gösterge kurudur; bankaların gişe/işlem kuru farklıdır — "
        "aşağıdaki banka tablolarıyla karşılaştır.",
    ]
    if bool((~tcmb["is_fresh"]).any()):
        notes.append(
            f"⚠️ TCMB kuru {stale_hours('TCMB'):g} saatten eski — karşılık bayat kurla "
            "hesaplanmış olabilir."
        )
    st.caption("  \n".join(notes))


def render() -> None:
    st.subheader("Döviz Kurları")
    principal = principal_input("doviz")
    rows = _latest_fx()
    if not rows:
        st.info("Henüz kur verisi toplanmadı. `python worker.py fx_tcmb` veya `fx_banks` çalıştırılmalı.")
        return

    df = pd.DataFrame(rows)
    now = datetime.now(timezone.utc)
    df["age_h"] = df["quoted_at"].apply(lambda q: age_hours(q, now))
    df["is_fresh"] = [
        yas <= stale_hours(kurum) for yas, kurum in zip(df["age_h"], df["institution"])
    ]

    st.markdown("### Tutarın döviz karşılığı")
    _render_tcmb_conversion(df, principal)
    st.divider()

    st.caption(
        f"Her kurum kendi tablosunda. Bankalarda {STALE_HOURS:g} saatten, TCMB'de "
        f"{stale_hours('TCMB'):g} saatten eski satırlar soluk gösterilir "
        "(TCMB gösterge kurunu günde bir kez yayımlar); "
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
            notes.append(
                f"{stale_hours(institution):g} saatten eski — kotasyon bayat olabilir."
            )
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
