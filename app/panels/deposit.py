"""Mevduat & Kar Payı paneli.

Tasarım §07: her kurum kendi tablosunda gösterilir. Bankaları tek bir tabloda
karıştırmak, farklı vade setleri yüzünden okunamaz bir tablo üretiyordu —
VakıfBank 6-750 gün arası onlarca vade yayınlarken TEB yalnızca 1-365,
Enpara 32/46/92/181, Yapı Kredi ise vade ARALIKLARI yayınlıyor.

SIRALAMA — buradaki tek kritik karar. Kurumlar MUTLAK net getiriye göre
sıralanırsa en uzun vadeyi sunan banka her zaman kazanır: VakıfBank'ın 750
günlük %18'lik mevduatı 332.876 TL getirip birinci olurken, Yapı Kredi'nin
367 günlük %33,50'si 303.152 TL ile ikinci kalıyordu — oysa yıllığa
çevrildiğinde VakıfBank %16,2, Yapı Kredi %30,2. Yani panel en kötü teklifi
en iyi gibi gösteriyordu. Bu yüzden hem sıralama hem kıyas sütunu
YILLIKLANDIRILMIŞ NET GETİRİ üzerinden yapılır; 32 gün ile 750 gün ancak
böyle aynı ölçekte karşılaştırılabilir.
"""
from __future__ import annotations

from collections import defaultdict

import pandas as pd
import streamlit as st

from app.panels.common import fetched_caption, institution_label, principal_input
from config.loader import source_summary
from config.loader import resolve_deposit_brackets
from core.deposit import DepositInput, resolve_withholding, single_term_return
from store import queries

# Kurum kodu -> panelde gösterilecek ad. Kod, kaynak dosyalarındaki
# institution alanıyla birebir aynı olmalı.

# Tutar kademesi TL karşılığı toplam bakiyeye göre belirlenen kurumlar.
# Döviz tablolarında kademe sınırı, yatırılan tutarla aynı birimde DEĞİL.
TL_EQUIVALENT_TIER_INSTITUTIONS = {"ENPARA"}


@st.cache_data(ttl=300)
def _rates_for_amount(principal: float, currency: str) -> list[dict]:
    return queries.deposit_rates_for_amount(principal, currency)


@st.cache_data(ttl=600)
def _institution_kinds() -> dict[str, str]:
    return queries.institution_kinds()


@st.cache_data(ttl=300)
def _profit_shares(principal: float, currency: str) -> list[dict]:
    return queries.profit_share_ratios(principal, currency)


def _render_profit_shares(principal: float, currency: str) -> None:
    """Katılım bankası kâr PAYLAŞIM oranları — en üstte, ayrı blokta.

    Bu tablo bilinçli olarak faiz tablolarından AYRI duruyor ve net getiri
    sütunu YOK. Sebep: yayınlanan sayı yıllık getiri değil, bankanın elde
    ettiği kârın müşteriye düşen yüzdesi. Gerçek getiriye çevirmek için
    bankanın gerçekleşen kâr rakamı gerekir ve katılım bankaları bunu web'de
    yayınlamıyor. Aynı tabloya "net getiri" sütunu koymak, %93'ü %93 faiz
    sanan bir sayı üretirdi (bkz. collectors/profit_shares.py).
    """
    rows = _profit_shares(principal, currency)
    if not rows:
        return

    st.info(
        "**Kâr paylaşım oranları — yukarıdaki oranlarla AYNI ŞEY DEĞİL.** "
        "Buradaki sayı (%93 gibi), bankanın katılma havuzundan elde ettiği "
        "**kârın sana düşen yüzdesidir**; yıllık %93 getiri anlamına gelmez. "
        "Katılım bankalarının mevduatla kıyaslanabilir **yıllık kâr payı oranı** "
        "yukarıdaki tablolarda, diğer bankalarla aynı sütunlarda yer alıyor "
        "(bankanın kendi hesaplama aracından/gerçekleşen oranlarından çekiliyor). "
        "Bu blok onun yerine geçmez, onu açıklar: paylaşım oranı ne kadar "
        "yüksekse havuz kârının o kadar büyük kısmı katılımcıya aktarılır."
    )

    by_institution: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_institution[row["institution"]].append(row)

    for institution, inst_rows in sorted(by_institution.items()):
        st.markdown(f"### {institution_label(institution)} — kâr paylaşım oranları")
        st.caption(
            f"**{principal:,.0f} {currency}** → kullanılan bakiye kademesi: "
            f"**{_tier_text(inst_rows, currency)}**"
        )
        df = pd.DataFrame(
            [
                {
                    "Vade": r["term_label"],
                    "Kâr paylaşım oranı": r["share_ratio"] * 100,
                    "Stopaj": (
                        r["withholding_rate"] * 100 if r["withholding_rate"] is not None else None
                    ),
                }
                for r in sorted(inst_rows, key=lambda r: r["term_days"])
            ]
        )
        styled = df.style.format(
            {"Kâr paylaşım oranı": "%{:,.0f}", "Stopaj": "%{:,.2f}"}, na_rep="—"
        )
        st.dataframe(styled, width="stretch", hide_index=True)
        share_notes = [fetched_caption([r.get("fetched_at") for r in inst_rows])]
        summary = source_summary("profit_share_endpoints", institution)
        if summary:
            share_notes.append(summary)
        st.caption("  \n".join(share_notes))

    st.divider()



def render() -> None:
    st.subheader("Mevduat & Kar Payı")
    principal = principal_input("mevduat")
    currency = st.selectbox("Para birimi", ["TRY", "USD", "EUR"], key="deposit_currency")

    # "Anapara" kutusu birimsizdir (bkz. app/panels/common.PRINCIPAL_LABEL);
    # TRY dışı bir para birimi seçildiğinde aynı sayı o para biriminin
    # tutarı olarak yorumlanıyor. Birimi bilen tek yer burası, açıkça yaz.
    if currency != "TRY":
        st.caption(
            f"Hesaplama, girilen **{principal:,.0f}** tutarını **{currency}** cinsinden kabul eder "
            "(TL karşılığına çevrilmez)."
        )

    rows = _rates_for_amount(principal, currency)
    if not rows:
        _render_profit_shares(principal, currency)
        st.info(
            f"Bu tutar ve para birimi ({currency}) için toplanmış bir FAİZ oranı yok. "
            "Veriyi tazelemek için: `python worker.py deposits`"
        )
        return

    st.caption(
        f"Her satır, **{principal:,.0f} {currency}** anaparanın düştüğü tutar kademesindeki "
        "oranla hesaplandı — anaparayı değiştirirsen oranlar da değişir. Vadeler bankanın "
        "kendi yayınladığı vade setidir, o yüzden tablolar farklı satır sayısına sahip. "
        "**Yıllık net %** sütunu getiriyi 365 güne normalize eder; farklı vadeleri ancak "
        "bu sütunla kıyaslayabilirsin. Kurumlar da bu sütunun en iyisine göre sıralanır."
    )

    brackets = resolve_deposit_brackets(currency)

    by_institution: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_institution[row["institution"]].append(row)

    # Kurumlar en iyi YILLIKLANDIRILMIŞ net getirilerine göre sıralanır.
    # Mutlak getiriye göre sıralamak en uzun vadeli bankayı kayırırdı —
    # bkz. modül docstring'i.
    def _best_annualized(institution_rows: list[dict]) -> float:
        return max(_annualized_net_pct(r, principal, brackets) for r in institution_rows)

    ordered = sorted(by_institution.items(), key=lambda kv: _best_annualized(kv[1]), reverse=True)

    kinds = _institution_kinds()

    for institution, institution_rows in ordered:
        best = _best_annualized(institution_rows)
        st.markdown(f"### {institution_label(institution)} — en iyi yıllık net %{best:,.2f}")

        # Hangi tutar kademesinin kullanıldığı TABLONUN ÜSTÜNDE durur.
        # Altta küçük bir not olarak durduğunda gözden kaçıyor ve kullanıcı,
        # bankanın sayfasında gördüğü BAŞKA bir kademenin oranıyla kıyaslayıp
        # "değerler yanlış" sonucuna varıyor (Enpara'da tam bu yaşandı:
        # bankanın hesaplayıcısı varsayılan 10.000 TL ile açılıp %30,75
        # gösterirken panel 1.000.000 TL için doğru olarak %38,25 diyordu).
        st.caption(
            f"**{principal:,.0f} {currency}** → kullanılan tutar kademesi: "
            f"**{_tier_text(institution_rows, currency)}**"
        )

        records = []
        for row in sorted(institution_rows, key=lambda r: r["term_days"]):
            withholding = resolve_withholding(brackets, row["term_days"])
            result = single_term_return(
                DepositInput(
                    principal=principal,
                    annual_rate=row["annual_rate"],
                    term_days=row["term_days"],
                    withholding_rate=withholding,
                )
            )
            records.append(
                {
                    "Vade (gün)": row["term_days"],
                    "Yıllık oran": row["annual_rate"] * 100,
                    "Stopaj": withholding * 100,
                    "Brüt getiri": result.gross_return,
                    "Net getiri": result.net_return,
                    # Farklı vadeleri kıyaslanabilir kılan tek sütun bu.
                    "Yıllık net %": _annualized_net_pct(row, principal, brackets),
                    "Vade sonu değer": result.maturity_value,
                    "Tür": (
                        "Beklenen kar payı"
                        if row["is_profit_share"] or kinds.get(institution) == "participation"
                        else "Faiz"
                    ),
                }
            )

        df = pd.DataFrame(records)
        best_row = df["Yıllık net %"].idxmax()
        styled = (
            df.style.format(
                {
                    "Yıllık oran": "%{:,.2f}",
                    "Stopaj": "%{:,.2f}",
                    "Brüt getiri": "{:,.2f}",
                    "Net getiri": "{:,.2f}",
                    "Yıllık net %": "%{:,.2f}",
                    "Vade sonu değer": "{:,.2f}",
                }
            )
            # Bu kurumun en iyi vadesini vurgula — "hangi vadeyi seçmeliyim"
            # sorusunun cevabı en yüksek YILLIK net orandır, en yüksek mutlak
            # getiri değil.
            .apply(
                lambda row, best=best_row: [
                    "font-weight: 600" if row.name == best else "" for _ in row
                ],
                axis=1,
            )
        )
        st.dataframe(styled, width="stretch", hide_index=True)

        notes = [fetched_caption([r.get("fetched_at") for r in institution_rows])]
        summary = source_summary("deposit_endpoints", institution)
        if summary:
            notes.append(summary)
        if institution in TL_EQUIVALENT_TIER_INSTITUTIONS and currency != "TRY":
            notes.append(
                "Bu kurumda tutar kademesi, tüm hesaplarınızın **TL karşılığı** toplam "
                "bakiyesine göre belirlenir; yukarıdaki kademe sınırları seçtiğiniz para "
                "biriminde değildir."
            )
        if any(r["is_profit_share"] for r in institution_rows):
            notes.append(
                '"Beklenen kar payı" bir taahhüt değil, katılım bankasının beklentisidir — '
                "faizle aynı kategori değildir."
            )
        st.caption("  \n".join(notes))

    # Kâr PAYLAŞIM oranları artık EN SONDA, ek bilgi olarak.
    #
    # Eskiden en üstteydi ve tam da kullanıcının şikâyet ettiği sorunu
    # üretiyordu: %93'lük paylaşım oranı, sayfanın en görünür yerinde,
    # aşağıdaki %38'lik faizlerin ÖNÜNDE duruyordu ve faiz gibi okunuyordu.
    # Artık katılım bankaları yukarıdaki tablolarda kendi YILLIK oranlarıyla
    # (bkz. collectors/participation_rates.py) yer alıyor; paylaşım oranı
    # ise kıyas sayısı değil, o kıyası açıklayan ek bir bilgi.
    _render_profit_shares(principal, currency)


def _net_return(row: dict, principal: float, brackets) -> float:
    withholding = resolve_withholding(brackets, row["term_days"])
    return single_term_return(
        DepositInput(
            principal=principal,
            annual_rate=row["annual_rate"],
            term_days=row["term_days"],
            withholding_rate=withholding,
        )
    ).net_return


def _annualized_net_pct(row: dict, principal: float, brackets) -> float:
    """Net getiriyi 365 güne normalize eder — farklı vadeleri kıyaslanabilir kılan sayı.

    Basit (bileşiklenmemiş) yıllıklandırma: vade sonunda paranın tekrar aynı
    oranla bağlanacağını VARSAYMAZ. Bankalar uzun vadeye düşük oran verdiği
    için bileşiklemek uzun vadeyi haksız yere iyi gösterirdi.
    """
    if principal <= 0 or row["term_days"] <= 0:
        return 0.0
    return _net_return(row, principal, brackets) / principal * (365 / row["term_days"]) * 100


def _tier_text(rows: list[dict], currency: str) -> str:
    """Bu kurumda anaparanın hangi tutar kademesine düştüğünü yazar."""
    tiers = {(r.get("amount_min"), r.get("amount_max")) for r in rows}
    parts = []
    for low, high in sorted(tiers, key=lambda t: (t[0] is None, t[0])):
        if low is None:
            continue
        parts.append(
            f"{low:,.0f} {currency} ve üzeri" if high is None else f"{low:,.0f} – {high:,.0f} {currency}"
        )
    return ", ".join(parts) if parts else "belirtilmemiş"
