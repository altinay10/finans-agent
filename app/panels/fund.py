from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from app.panels.common import fetched_caption
from config.loader import resolve_deposit_brackets, resolve_fund_withholding
from core.deposit import resolve_withholding, rollover_return
from core.fund import nearest_prior_price, simulate
from core.models import FundSimInput
from collectors.fund_prices import add_fund_to_registry, load_fund_registry
from collectors.fund_providers import PROVIDERS
from store import queries

SCENARIOS = [("1 ay", 30), ("6 ay", 182), ("1 yıl", 365)]


@st.cache_data(ttl=300)
def _funds() -> list[dict]:
    return queries.list_funds()


@st.cache_data(ttl=300)
def _registry():
    return load_fund_registry()


@st.cache_data(ttl=300)
def _price_series(fund_code: str, start: date, end: date) -> list[tuple[date, float]]:
    return queries.fund_price_series(fund_code, start, end)


@st.cache_data(ttl=300)
def _try_deposit_rates(principal: float) -> list[dict]:
    return queries.deposit_rates_for_amount(principal, "TRY")


def _deposit_comparison(principal: float, horizon_days: int) -> tuple[float, str] | None:
    """Aynı ufuk boyunca TL mevduatta tutmanın EN İYİ net getirisi ve o oranı veren banka.

    Tasarım §07: fon panelinde "aynı dönemde mevduatın ne getireceği kıyas çizgisi"
    isteniyor. Artık dört bankanın oranı toplandığı için "en yakın vade" tek başına
    yetmiyor — aynı vadede farklı bankalar farklı oran veriyor ve rastgele birini
    seçmek kıyas çizgisini keyfî yapardı. Bu yüzden ufka en yakın vadeler arasından
    net getirisi en yüksek olan seçilir; hangi banka olduğu da döndürülür ki
    kullanıcı kıyasın nereden geldiğini görsün.

    Uydurma oran yok: veri yoksa None döner ve arayan sütunu boş bırakır.
    """
    rates = _try_deposit_rates(principal)
    if not rates:
        return None
    brackets = resolve_deposit_brackets("TRY")
    closest_distance = min(abs(r["term_days"] - horizon_days) for r in rates)
    candidates = [r for r in rates if abs(r["term_days"] - horizon_days) == closest_distance]

    best_return: float | None = None
    best_institution = ""
    for rate in candidates:
        withholding = resolve_withholding(brackets, rate["term_days"])
        result = rollover_return(
            principal=principal,
            annual_rate=rate["annual_rate"],
            term_days=rate["term_days"],
            withholding_rate=withholding,
            horizon_days=horizon_days,
        )
        if best_return is None or result.annualized_net_return > best_return:
            best_return = result.annualized_net_return
            best_institution = rate["institution"]
    if best_return is None:
        return None
    return best_return, best_institution


def render(principal: float) -> None:
    st.subheader("Fon Simülasyonu")
    funds = _funds()
    if not funds:
        st.info("Fon kataloğu boş — `config/funds.yaml` dosyasını kontrol et.")
        _render_add_fund()
        return

    fund = st.selectbox("Fon", funds, format_func=lambda f: f"{f['code']} — {f['name']}")
    today = date.today()
    prices = _price_series(fund["code"], today - timedelta(days=400), today)

    if not prices:
        st.info(
            f"{fund['code']} için son 400 günde fiyat verisi yok. Fiyatlar sağlayıcı "
            "adaptörlerinden toplanır: `python worker.py funds`"
        )
        _render_add_fund()
        return

    withholding = resolve_fund_withholding(fund["is_equity_heavy"])
    price_end_date, price_end = max(prices, key=lambda p: p[0])

    rows = []
    comparison_sources: set[str] = set()
    for label, days_back in SCENARIOS:
        target = price_end_date - timedelta(days=days_back)
        try:
            price_start_date, price_start = nearest_prior_price(prices, target)
        except ValueError:
            continue
        sim = simulate(
            FundSimInput(
                principal=principal,
                price_start=price_start,
                price_end=price_end,
                date_start=price_start_date,
                date_end=price_end_date,
                withholding_rate=withholding,
                is_equity_heavy=fund["is_equity_heavy"],
            )
        )
        comparison = _deposit_comparison(principal, days_back)
        deposit_net = comparison[0] if comparison else None
        if comparison:
            comparison_sources.add(comparison[1])
        rows.append(
            {
                "Senaryo": label,
                "Başlangıç tarihi": price_start_date.isoformat(),
                "Başlangıç fiyatı": price_start,
                "Bugünkü fiyat": price_end,
                "Brüt getiri": round(sim.gross_return, 2),
                "Net getiri (fon)": round(sim.net_return, 2),
                "Net getiri (TL mevduat, kıyas)": round(deposit_net, 2) if deposit_net is not None else None,
                "Vade sonu değer": round(sim.maturity_value, 2),
            }
        )

    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        if fund["is_equity_heavy"]:
            st.caption("Hisse yoğun fon — stopajdan muaf.")
        if all(r["Net getiri (TL mevduat, kıyas)"] is None for r in rows):
            st.caption(
                "Mevduat kıyas sütunu boş — banka bazlı mevduat oranı henüz toplanmadı "
                "(`python worker.py deposits`)."
            )
        else:
            source_text = ", ".join(sorted(comparison_sources)) or "toplanmış banka oranları"
            st.caption(
                "Mevduat kıyası, aynı ufuk boyunca en yakın vadeli TL mevduatın devirli (rollover) "
                f"net getirisidir; aynı vadedeki bankalar arasından **en yükseği** alınır ({source_text}). "
                "Gerçek, toplanmış banka oranıyla hesaplanır — uydurma oran yok."
            )
    else:
        st.warning("Seçilen senaryolar için yeterli geçmiş fiyat verisi yok.")

    spec = _spec_for(fund["code"])
    provider = PROVIDERS.get(spec.provider) if spec else None
    provider_label = provider.label if provider else "bilinmiyor"
    unit_value = provider.price_is_unit_value if provider else True

    st.caption(
        f"Son fiyat tarihi: **{price_end_date.strftime('%d.%m.%Y')}** "
        f"({len(prices)} işlem günü, kaynak: {provider_label})."
    )
    if not unit_value:
        # Garanti Portföy uç noktası birim pay fiyatı DEĞİL, "1.000 TL
        # yatırılsaydı bugün ne olurdu" endeksi döndürüyor. Getiri hesabı
        # yalnızca ORANI kullandığı için doğru; ama sayıyı "fiyat" diye
        # göstermek yanıltır, bu yüzden açıkça yazılıyor.
        st.caption(
            f"⚠️ {provider_label} birim pay fiyatı yayınlamıyor; grafikteki değer "
            "**1.000 TL'nin zaman içindeki karşılığıdır** (endeks). Getiri "
            "hesabı yalnızca başlangıç/bitiş oranını kullandığı için sonuç "
            "doğrudur, ama bu sayı fon fiyatı değildir."
        )

    value_label = "Fiyat" if unit_value else "1.000 TL'nin değeri"
    price_df = pd.DataFrame(prices, columns=["Tarih", value_label]).sort_values("Tarih")
    fig = px.line(
        price_df, x="Tarih", y=value_label,
        title=f"{fund['code']} — {'fiyat serisi' if unit_value else 'endeks serisi'}",
    )
    st.plotly_chart(fig, use_container_width=True)

    _render_add_fund()


# ------------------------------------------------------------- fon ekle ----


def _spec_for(code: str):
    for spec in _registry():
        if spec.code == code:
            return spec
    return None


def _render_add_fund() -> None:
    """Panelden fon ekleme — kullanıcının asıl istediği özellik.

    Kayıt defterine (config/funds.yaml) bir satır yazar. Veritabanına
    doğrudan yazmak yanlış olurdu: `seed_reference_data` kayıt defterinde
    olmayan fonu bir sonraki açılışta siler.

    Fiyat serisi burada ÇEKİLMEZ; panel bankalara/sağlayıcılara hiç gitmez
    (tasarım §01). Eklendikten sonra bir sonraki toplama koşusu doldurur.
    """
    with st.expander("➕ Fon ekle"):
        st.caption(
            "Fon `config/funds.yaml` kayıt defterine eklenir. Fiyatlar bir "
            "sonraki toplama koşusunda gelir — panel veri toplamaz."
        )
        provider_key = st.selectbox(
            "Sağlayıcı", list(PROVIDERS),
            format_func=lambda k: PROVIDERS[k].label,
            key="add_fund_provider",
        )
        hint = {
            "akportfoy": "Adres parçası = fon kodu. Örnek: `AK3` "
                         "(akportfoy.com.tr/tr/fon/**AK3**)",
            "garantiportfoy": "Adres parçası = sayfa kısa adı. Örnek: `altin-fonu` "
                              "(garantibbvaportfoy.com.tr/**altin-fonu**)",
        }.get(provider_key, "")
        if hint:
            st.caption(hint)
        code = st.text_input("Fon kodu (TEFAS, 3 harf)", key="add_fund_code").strip().upper()
        ref = st.text_input("Sağlayıcıdaki adres parçası", key="add_fund_ref").strip()
        name = st.text_input("Ad (isteğe bağlı)", key="add_fund_name").strip()

        if st.button("Kayıt defterine ekle", key="add_fund_submit"):
            if not code or not ref:
                st.error("Fon kodu ve adres parçası zorunlu.")
                return
            try:
                add_fund_to_registry(
                    code=code, provider=provider_key, ref=ref, name=name or None
                )
            except ValueError as exc:
                st.warning(str(exc))
                return
            except OSError as exc:
                st.error(f"`config/funds.yaml` yazılamadı: {exc}")
                return
            _registry.clear()
            _funds.clear()
            st.success(
                f"**{code}** eklendi. Fiyatları getirmek için toplayıcıyı çalıştır: "
                "`python worker.py funds` (zamanlayıcı kuruluysa kendi saatinde gelir)."
            )
