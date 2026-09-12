"""Kredi paneli.

Tasarım §07. Bu panelde dört şey kritik:

1. Hangi bankanın oranıyla hesaplandığı HER YERDE görünmeli — kıyas
   tablosunda da, ödeme planının başlığında da. Ödeme planı banka adı
   olmadan gösterildiğinde tablo teknik olarak doğru ama okunamaz oluyordu.
2. Bankanın ilan ettiği vade sınırı (term_min/term_max) dikkate alınmalı.
   Aksi halde panel, bankanın en fazla 36 ay verdiği bir ihtiyaç kredisini
   240 ay üzerinden hesaplayıp gerçekte alınamayacak bir taksit gösterir.
3. ÖDEME PLANI BANKADAN GELMİYOR — biz hesaplıyoruz (`core/loan.py`).
   Bankadan gelen tek şey aylık orandır. Bunu söylememek, kullanıcının
   ekrandaki planı bankanın resmî planı sanmasına yol açar; oysa banka kendi
   planına dosya masrafı, sigorta ve gün sayımı farkı katar.
4. Bir kredi türünde az sayıda banka görünmesi "veri eksik" demek değil,
   "o ürünü yayınlayan banka sayısı bu kadar" demek olabilir. Panel bu ikisini
   ayırt edebilmeli; edemezse kullanıcı haklı olarak veriye güvenmez.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.panels.common import fetched_caption, institution_label, principal_input
from config.loader import loan_coverage, resolve_loan_taxes, source_summary
from core.loan import amortize, annual_cost_rate, compare_installment
from core.models import LoanInput
from store import queries

LOAN_TYPE_LABELS = {"housing": "Konut", "vehicle": "Taşıt", "personal": "İhtiyaç"}


@st.cache_data(ttl=300)
def _rates(loan_type: str, include_campaign: bool = False) -> list[dict]:
    return queries.latest_loan_rates(loan_type, include_campaign=include_campaign)


@st.cache_data(ttl=600)
def _institution_kinds() -> dict[str, str]:
    return queries.institution_kinds()


@st.cache_data(ttl=600)
def _type_counts() -> dict[str, int]:
    return queries.loan_type_counts()


@st.cache_data(ttl=600)
def _reference_quotes(loan_type: str) -> list[dict]:
    return queries.loan_reference_quotes(loan_type)


@st.cache_data(ttl=3600)
def _coverage() -> dict[str, dict]:
    return loan_coverage()


@st.cache_data(ttl=300)
def _campaign_count(loan_type: str) -> int:
    return queries.loan_campaign_count(loan_type)


#: Kampanyalı satırın rozeti. Metinde de, etikette de aynı işaret.
CAMPAIGN_MARK = "🏷️"


def _row_label(row: dict) -> str:
    """Tablo ve seçici için satır etiketi; kampanyalıysa rozetli."""
    name = institution_label(row["institution"])
    return f"{name} {CAMPAIGN_MARK}" if row.get("is_campaign") else name


def _audience_text(row: dict) -> str:
    """Bu oran kime açık — tek bakışta okunacak kadar kısa.

    Gerekçe modelin kendi cümlesi (`campaign_note`) ve uzun olabiliyor;
    tabloda kırpılıyor, tamamı koşu kaydında duruyor. Gerekçe hiç yoksa
    "kampanya" demek yine de doğru bilgi: satırın kampanyalı olduğunu
    biliyoruz, yalnızca sebebini bilmiyoruz.
    """
    if not row.get("is_campaign"):
        return "Herkese açık"
    note = (row.get("campaign_note") or "").strip()
    return f"{CAMPAIGN_MARK} {note[:60]}" if note else f"{CAMPAIGN_MARK} Koşullu (gerekçe yok)"


def render() -> None:
    st.subheader("Kredi")
    # Kredi sekmesi tutarı her koşulda TL sayar; kutu birimsiz olduğu için
    # birimi burada söylüyoruz (bkz. app/panels/common.principal_input).
    principal = principal_input("kredi")
    st.caption("Çekilecek kredi tutarı **TL** kabul edilir.")
    col1, col2 = st.columns(2)
    with col1:
        loan_type = st.selectbox(
            "Kredi türü", list(LOAN_TYPE_LABELS), format_func=lambda k: LOAN_TYPE_LABELS[k]
        )
    with col2:
        term_months = st.number_input("Vade (ay)", min_value=1, max_value=360, value=12, step=1)

    # KAMPANYA ORANLARI VARSAYILAN OLARAK KAPALI. Bunlar gerçek oranlar ama
    # herkesin alabildiği oranlar değil (yalnızca yeni müşteriye, ön
    # onaylıya, belirli bir meslek grubuna). Tabloya karışırlarsa "en ucuz
    # banka" sıralaması, kullanıcının başvurup ALAMAYACAĞI bir oranla
    # belirlenir — düzeltilen hatanın ta kendisi (ING %0,99, 2026-08-30).
    # Görmek isteyen açıyor; açtığında satırlar 🏷️ ile işaretli.
    kampanyali_sayisi = _campaign_count(loan_type)
    include_campaign = False
    if kampanyali_sayisi:
        include_campaign = st.checkbox(
            f"Kampanyalı / yeni müşteri oranlarını da göster ({kampanyali_sayisi})",
            key=f"kampanya_{loan_type}",
            help=(
                "Bu oranlar yalnızca belirli müşterilere açık. Koşulu, satırın "
                "**Kime açık** sütununda yazıyor."
            ),
        )

    rows = _rates(loan_type, include_campaign)
    kkdf, bsmv = resolve_loan_taxes(loan_type)

    _render_coverage(
        loan_type, [r["institution"] for r in rows if not r.get("is_campaign")]
    )

    if not rows:
        st.info(
            f"**{LOAN_TYPE_LABELS[loan_type]}** kredisi için toplanmış banka oranı yok. "
            "Aşağıda elle bir oran girip hesaplama motorunu deneyebilirsin "
            "(`python worker.py loan_rates` ile veri toplanır)."
        )
        manual_rate = st.number_input(
            "Aylık brüt faiz oranı (%)", min_value=0.0, max_value=20.0, value=3.5, step=0.1
        ) / 100
        result = amortize(
            LoanInput(
                principal=principal, monthly_rate=manual_rate, term_months=term_months,
                kkdf=kkdf, bsmv=bsmv,
            )
        )
        _render_schedule(
            result,
            institution="Elle girilen oran",
            term_note=None,
            reference=None,
            loan_type=loan_type,
        )
        return

    kinds = _institution_kinds()
    comparisons = []
    results_by_institution: dict[str, object] = {}
    notes_by_institution: dict[str, str | None] = {}
    code_by_label: dict[str, str] = {}
    campaign_labels: set[str] = set()

    for row in rows:
        # ETİKET KAMPANYA ROZETİNİ İÇERİYOR, sadece süs olsun diye değil:
        # aynı banka hem genel hem kampanyalı oranla listelenebiliyor ve
        # aşağıdaki üç sözlük ile "Ödeme planını göster" seçicisi bu
        # etiketle anahtarlanıyor. Rozet olmasaydı ikinci satır birinciyi
        # sessizce ezer, kullanıcı kampanya oranını seçtiğini sanıp genel
        # oranın planını görürdü.
        name = _row_label(row)
        code_by_label[name] = row["institution"]
        if row.get("is_campaign"):
            campaign_labels.add(name)
        # Katılım bankasında bu bir faiz oranı değil, kâr oranıdır (§07).
        is_profit_share = kinds.get(row["institution"]) == "participation"
        result = amortize(
            LoanInput(
                principal=principal,
                monthly_rate=row["monthly_rate"],
                term_months=term_months,
                kkdf=kkdf,
                bsmv=bsmv,
            )
        )
        results_by_institution[name] = result
        notes_by_institution[name] = _limit_warning(row, term_months, principal)
        comparisons.append(
            {
                "Banka": name,
                "Oran türü": "Kâr oranı" if is_profit_share else "Faiz",
                "Kime açık": _audience_text(row),
                "Aylık oran": row["monthly_rate"] * 100,
                "Yıllık maliyet": annual_cost_rate(result.effective_monthly_rate) * 100,
                "Taksit": result.installment,
                "Toplam ödenen": result.total_paid,
                "Toplam vergi (KKDF+BSMV)": result.total_kkdf + result.total_bsmv,
                "Vade aralığı (ay)": _term_range_text(row),
                "Uyarı": "⚠️" if notes_by_institution[name] else "",
            }
        )

    df = pd.DataFrame(comparisons).sort_values("Taksit").reset_index(drop=True)
    styled = df.style.format(
        {
            "Aylık oran": "%{:,.2f}",
            "Yıllık maliyet": "%{:,.1f}",
            "Taksit": "{:,.2f}",
            "Toplam ödenen": "{:,.2f}",
            "Toplam vergi (KKDF+BSMV)": "{:,.2f}",
        }
    )
    st.dataframe(styled, width="stretch", hide_index=True)

    caption_lines = [
        fetched_caption([r.get("fetched_at") for r in rows]),
        f"Vergiler efektif orana katılıyor: KKDF %{kkdf*100:.0f}, BSMV %{bsmv*100:.0f} "
        "(faiz tutarı üzerinden).",
        "**Yıllık maliyet** = efektif aylık oranın bileşik yıllığı "
        "((1+oran)¹²−1); aylık oranı 12 ile çarpmak değildir. Vadeden bağımsız "
        "olduğu için taksit tutarından daha dürüst bir kıyas ölçüsüdür.",
    ]
    if any(kinds.get(r["institution"]) == "participation" for r in rows):
        caption_lines.append(
            '"Kâr oranı" katılım bankasının kâr payı oranıdır; faizle aynı kategori '
            "değildir. Taksit matematiği aynıdır, isimlendirmesi değil."
        )
    warnings = [n for n in notes_by_institution.values() if n]
    if warnings:
        caption_lines.append("⚠️ " + "  \n⚠️ ".join(warnings))
    st.caption("  \n".join(caption_lines))
    _render_sources(rows)

    chosen = st.selectbox("Ödeme planını göster", df["Banka"].tolist())
    # KAMPANYALI SATIRA REFERANS TAKSİT BAĞLANMAZ. Referans, bankanın KENDİ
    # ilan ettiği taksit tutarı ve o tutar bankanın GENEL oranına ait
    # (bkz. collectors/loan_rates.py::LoanReferenceQuoteRecord). Kampanya
    # satırının taksitini o rakamla karşılaştırmak, hesabımız doğruyken
    # bile "bizim taksitimiz bankanınkini tutmuyor" diye sahte bir sapma
    # gösterirdi — oysa karşılaştırılan şey iki FARKLI orandır.
    reference = (
        None if chosen in campaign_labels
        else _find_reference(loan_type, code_by_label.get(chosen))
    )
    _render_schedule(
        results_by_institution[chosen],
        institution=chosen,
        term_note=notes_by_institution[chosen],
        reference=reference,
        loan_type=loan_type,
    )


# ------------------------------------------------------------- kapsam ----


def _render_coverage(loan_type: str, present: list[str]) -> None:
    """Hangi türde kaç kurum var ve eksik olanlar NEDEN eksik.

    Kullanıcı "ödeme planında yalnızca 3 banka var, diğerleri neden yok?"
    diye sorduğunda cevap veride değildi: konut kredisini yayınlayan banka
    sayısı gerçekten üçtü. Panel bunu söylemediği için eksiklik gibi
    görünüyordu. Gerekçeler config/sources.yaml'da yapısal olarak duruyor —
    ikinci bir liste tutmak kayma üretirdi.
    """
    counts = _type_counts()
    summary = " · ".join(
        f"{label}: **{counts.get(key, 0)}**" for key, label in LOAN_TYPE_LABELS.items()
    )
    st.caption(f"Veri bulunan kurum sayısı — {summary}")

    coverage = _coverage()
    present_set = set(present)
    missing: list[tuple[str, str]] = []
    for code, info in sorted(coverage.items()):
        if code in present_set:
            continue
        reason = info["missing"].get(loan_type) or info["blocked_reason"]
        if reason is None:
            if info["status"] != "active":
                reason = f"Kaynak durumu: {info['status']}."
            else:
                reason = "Bu kredi türü bu kurumda toplanmıyor; gerekçe kayıtlı değil."
        missing.append((institution_label(code), reason))

    if not missing:
        return
    with st.expander(
        f"{LOAN_TYPE_LABELS[loan_type]} kredisinde verisi olmayan kurumlar ({len(missing)}) — neden?"
    ):
        for name, reason in missing:
            st.markdown(f"- **{name}** — {reason}")
        st.caption(
            "Gerekçeler `config/sources.yaml` dosyasından okunur; ayrıntı için "
            "**Kaynaklar** sekmesine bak."
        )


def _render_sources(rows: list[dict]) -> None:
    """Kıyas tablosundaki her oranın uç noktası.

    Tek kurumlu tablolarda kaynak künyesi doğrudan tablonun altına yazılıyor
    (bkz. fx/deposit panelleri); burada tablo çok kurumlu olduğu için
    açılır bir bloğa alındı — yoksa altı satırlık bir URL yığını asıl
    sayıların önüne geçerdi.
    """
    entries = []
    for row in rows:
        summary = source_summary("loan_endpoints", row["institution"])
        if summary:
            entries.append((institution_label(row["institution"]), summary))
    if not entries:
        return
    with st.expander(f"Bu tablodaki oranların kaynakları ({len(entries)})"):
        for name, summary in sorted(entries):
            st.markdown(f"- **{name}** — {summary}")


def _term_range_text(row: dict) -> str:
    low, high = row.get("term_min"), row.get("term_max")
    if low is None and high is None:
        return "—"
    if low is None:
        return f"≤ {high}"
    if high is None:
        return f"≥ {low}"
    return f"{low} – {high}"


def _limit_warning(row: dict, term_months: int, principal: float) -> str | None:
    """Bankanın ilan ettiği sınırların dışına çıkıldıysa açıkça söyle.

    Hesap yine de yapılır (kullanıcı senaryo denemek isteyebilir) ama sayının
    gerçekte alınamayacak bir kredinin taksiti olduğu gizlenmez.
    """
    name = institution_label(row["institution"])
    problems = []
    low, high = row.get("term_min"), row.get("term_max")
    if low is not None and term_months < low:
        problems.append(f"en az {low} ay")
    if high is not None and term_months > high:
        problems.append(f"en fazla {high} ay")
    amount_max = row.get("amount_max")
    if amount_max is not None and principal > amount_max:
        problems.append(f"en fazla {amount_max:,.0f} TL")
    if not problems:
        return None
    return (
        f"**{name}**: bu ürün {', '.join(problems)} ile sınırlı. "
        "Aşağıdaki sayılar seçtiğin değerlerle hesaplandı ama bu koşullarda kredi verilmez."
    )


# ------------------------------------------------------- ödeme planı ----


def _find_reference(loan_type: str, institution_code: str | None) -> dict | None:
    if not institution_code:
        return None
    for quote in _reference_quotes(loan_type):
        if quote["institution"] == institution_code:
            return quote
    return None


def _render_schedule(
    result,
    *,
    institution: str,
    term_note: str | None,
    reference: dict | None,
    loan_type: str,
) -> None:
    st.markdown(f"#### Ödeme planı — {institution}")
    if term_note:
        st.warning(term_note)

    # Şeffaflık notu: bu plan bankanın resmî planı DEĞİL.
    st.info(
        "**Bu plan bankanın resmî ödeme planı değildir.** Bankadan alınan tek "
        "veri aylık orandır; taksit, anapara/faiz ayrışması ve KKDF/BSMV "
        "kırılımı bu orandan hesaplanır "
        "(anüite formülü, `core/loan.py`). Bankanın kendi planı dosya masrafı, "
        "hayat sigortası ve gün sayımı farklarıyla birkaç lira sapabilir."
    )
    _render_validation(reference, loan_type)

    st.markdown(
        f"**Banka:** {institution} &nbsp;·&nbsp; "
        f"**Taksit:** {result.installment:,.2f} TL &nbsp;·&nbsp; "
        f"**Efektif aylık oran:** %{result.effective_monthly_rate*100:.3f} &nbsp;·&nbsp; "
        f"**Yıllık maliyet:** %{annual_cost_rate(result.effective_monthly_rate)*100:,.1f} "
        f"&nbsp;·&nbsp; **Toplam ödenen:** {result.total_paid:,.2f} TL"
    )
    schedule_df = pd.DataFrame(
        [
            {
                "Taksit no": r.period,
                "Taksit": round(r.installment, 2),
                "Anapara": round(r.principal_paid, 2),
                "Faiz": round(r.interest, 2),
                "KKDF": round(r.kkdf_amount, 2),
                "BSMV": round(r.bsmv_amount, 2),
                "Kalan bakiye": round(r.remaining_balance, 2),
            }
            for r in result.schedule
        ]
    )
    st.dataframe(schedule_df, width="stretch", hide_index=True, height=320)


def _render_validation(reference: dict | None, loan_type: str) -> None:
    """Hesabımızı bankanın KENDİ taksitiyle karşılaştırıp sonucu gösterir.

    Bu, "biz hesaplıyoruz" uyarısının panzehiridir: kullanıcıya hesabın
    uydurma olmadığını, bankanın kendi rakamıyla sınandığını gösterir.
    Yalnızca kendi taksitini yayınlayan bankalarda mümkün.
    """
    if not reference:
        st.caption(
            "Bu kurum kendi taksit tutarını yayınlamıyor, bu yüzden hesabımız "
            "onun rakamıyla karşılaştırılamıyor. Aynı formül, kendi taksitini "
            "yayınlayan bankalarda doğrulanıyor (bkz. Kayıtlar sekmesi)."
        )
        return

    kkdf, bsmv = resolve_loan_taxes(loan_type)
    try:
        cmp = compare_installment(
            principal=reference["principal"],
            monthly_rate=reference["monthly_rate"],
            term_months=reference["term_months"],
            kkdf=kkdf,
            bsmv=bsmv,
            bank_installment=reference["bank_installment"],
        )
    except ValueError:
        return

    detail = (
        f"{reference['principal']:,.0f} TL / {reference['term_months']} ay · "
        f"bizim hesap **{cmp.ours:,.2f} TL** · bankanın kendi tutarı "
        f"**{cmp.theirs:,.2f} TL** · fark **%{cmp.deviation_pct:.4f}**"
    )
    if cmp.within_tolerance:
        st.success(f"✅ Hesap bankanın kendi rakamıyla doğrulandı — {detail}")
    else:
        st.error(
            f"⚠️ Hesabımız bankanın kendi tutarından sapıyor — {detail}. "
            "En olası sebep `config/taxes.yaml`'daki KKDF/BSMV oranının eskimesi."
        )

    bank_annual = reference.get("bank_annual_cost_rate")
    if bank_annual:
        st.caption(
            f"Bankanın ilan ettiği yıllık maliyet oranı: **%{bank_annual*100:,.2f}**. "
            "Bizim yıllık maliyetimiz yalnızca faiz + vergi içerir; bankanınki "
            "komisyon/masraf da içerebilir, bu yüzden ikisi birebir tutmayabilir."
        )
