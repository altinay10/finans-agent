"""Kayıtlar paneli — sunucuya SSH'lamadan ne olup bittiğini görmek için.

Bu panel, sunucuya kurulup unutulacak bir sistemin TEK gözlem penceresi.
Cevapladığı sorular (kullanıcının saydıkları + saymadıkları):

1. Hangi banka çalışıyor, hangisi bozuldu?      → kaynak sağlığı
   En kritik olanı: bir bankanın sayfası değiştiğinde koşu 'ok' görünmeye
   devam eder çünkü diğerleri çalışır. Sessiz bozulma yalnızca burada görünür.
2. Hangi API istekleri ne zaman dönmedi?        → istek dökümü
3. Dönmeyenler daha sonra döndü mü?             → kurtarma izi
4. Agent ne zaman hangi durumda devreye girdi?  → LLM tetikleyicileri
5. Kaç token, ne kadar maliyet?                 → token muhasebesi
6. Sayfa sessizce kısmen değişti mi?            → şema sapması
7. Uç nokta çalışıyor ama sayı donmuş olabilir mi? → oran değişim izi
8. Koşuyu ne başlattı; telafi çalışıyor mu?     → tetikleyici dökümü
9. Kaç kez bant dışı veri geldi?                → aşama bazında hata
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from app.panels.common import format_local
from store import queries

STATUS_ICON = {"ok": "🟢", "failed": "🔴", "empty": "🟠"}
LLM_STATUS_LABEL = {
    "ok": "🟢 çağrıldı",
    "failed": "🔴 hata",
    "disabled": "⚪ kapalı (token harcanmadı)",
    "budget_exceeded": "🟠 bütçe doldu (token harcanmadı)",
}
OUTCOME_ICON = {
    "ok": "🟢",
    "http_error": "🔴",
    "timeout": "⏱️",
    "network_error": "🔌",
}
FAILURE_KIND_LABEL = {
    "fetch": "İstek (ağ/HTTP)",
    "parse": "Ayrıştırma",
    # 'sanity' bir arıza DEĞİL: veri geldi, ayrıştırıldı, ama bant dışıydı ve
    # BİLEREK yazılmadı. Etiketi bunu söylemeli, yoksa kullanıcı korumanın
    # çalışmasını hata sanır.
    "sanity": "Bant dışı veri — yazılmadı (koruma çalıştı)",
    "persist": "Veritabanına yazma",
    "bilinmiyor": "Bilinmiyor (eski kayıt)",
}
TRIGGER_LABEL = {
    "schedule": "Planlanmış saat",
    "startup": "Süreç açılışı",
    "catchup": "Tazelik telafisi",
    "manual": "Elle (worker.py)",
    "bilinmiyor": "Bilinmiyor (eski kayıt)",
}


@st.cache_data(ttl=60)
def _health(hours: int) -> list[dict]:
    return queries.source_health(hours)


@st.cache_data(ttl=60)
def _events(limit: int) -> list[dict]:
    return queries.source_runs(limit)


@st.cache_data(ttl=60)
def _llm_triggers(limit: int) -> list[dict]:
    return queries.llm_triggers(limit)


@st.cache_data(ttl=60)
def _llm_totals(days: int) -> dict:
    return queries.llm_cost_totals(days)


@st.cache_data(ttl=60)
def _http(limit: int, only_failures: bool) -> list[dict]:
    return queries.http_requests(limit, only_failures)


@st.cache_data(ttl=60)
def _endpoints(days: int) -> list[dict]:
    return queries.http_endpoint_health(days)


@st.cache_data(ttl=60)
def _recovery(days: int) -> list[dict]:
    return queries.source_recovery(days)


@st.cache_data(ttl=60)
def _drift() -> list[dict]:
    return queries.schema_drift()


@st.cache_data(ttl=60)
def _changes(dataset: str, limit: int) -> list[dict]:
    return queries.rate_changes(dataset, limit)


@st.cache_data(ttl=60)
def _stale(dataset: str, days: int) -> list[dict]:
    return queries.stale_values(dataset, days)


@st.cache_data(ttl=60)
def _triggers(days: int) -> list[dict]:
    return queries.run_triggers(days)


@st.cache_data(ttl=60)
def _failures(days: int) -> list[dict]:
    return queries.run_failures(days)


def render() -> None:
    st.subheader("Kayıtlar")
    st.caption(
        "Toplayıcılar banka banka çalışır. Bir banka düşse bile koşu **'ok'** "
        "görünür (diğerleri çalıştığı için) — o yüzden koşu durumu tek başına "
        "yeterli değil."
    )

    window = st.selectbox(
        "Zaman aralığı", [24, 48, 168, 720],
        format_func=lambda h: f"son {h//24} gün" if h >= 24 else f"son {h} saat",
        index=1,
    )
    days = max(1, window // 24)

    _render_alerts(days)

    tabs = st.tabs(
        ["Kaynak sağlığı", "HTTP istekleri", "Kurtarma", "Oran değişimleri",
         "Agent (LLM)", "Koşular", "Olay akışı"]
    )
    with tabs[0]:
        _render_health(window)
    with tabs[1]:
        _render_http(days)
    with tabs[2]:
        _render_recovery(days)
    with tabs[3]:
        _render_rate_changes()
    with tabs[4]:
        _render_llm()
    with tabs[5]:
        _render_runs(days)
    with tabs[6]:
        _render_events()


# ------------------------------------------------------------- uyarılar ----


def _render_alerts(days: int) -> None:
    """En üstte: şu an dikkat gerektiren durumlar.

    Sekmelere gömülü bir uyarı, kimsenin açmadığı bir sekmede kalır. Sunucuda
    erişim olmayacağı için bozulmanın ilk bakışta görünmesi gerekiyor.
    """
    drift = _drift()
    broken = [r for r in _recovery(days) if r["state"] == "hâlâ bozuk"]

    if drift:
        lines = [
            f"**{d['collector']}/{d['source']}** — son koşuda {d['latest_rows']} satır, "
            f"önceki ortalama {d['baseline_rows']} (%{d['drop_pct']} düşüş)"
            for d in drift
        ]
        st.warning(
            "**Şema sapması şüphesi.** Aşağıdaki kaynaklardan gelen satır sayısı "
            "çöktü. Parser patlamadığı için koşu 'ok' göründü, ama sayfa kısmen "
            "değişmiş ve satırların bir kısmı sessizce kaybolmuş olabilir.\n\n"
            + "\n".join(f"- {line}" for line in lines)
        )
    if broken:
        lines = [
            f"**{r['collector']}/{r['source']}** — {r['outage_minutes']} dakikadır bozuk"
            for r in broken
        ]
        st.error(
            "**Şu an bozuk kaynaklar** (son denemesi başarısız, sonrasında hiç "
            "başarılı olmamış):\n\n" + "\n".join(f"- {line}" for line in lines)
        )
    if not drift and not broken:
        st.success("Şu an bekleyen bir bozulma sinyali yok.")


# -------------------------------------------------------- kaynak sağlığı ----


def _render_health(window: int) -> None:
    health = _health(window)
    if not health:
        st.info("Bu aralıkta kayıt yok. Toplayıcı hiç çalışmamış olabilir.")
        return
    rows = []
    for h in health:
        total = (h["ok_count"] or 0) + (h["bad_count"] or 0)
        rows.append(
            {
                "Durum": "🔴" if h["bad_count"] and not h["last_ok"]
                else ("🟠" if h["bad_count"] else "🟢"),
                "Toplayıcı": h["collector"],
                "Kaynak": h["source"],
                "Başarı": h["ok_count"] or 0,
                "Hata": h["bad_count"] or 0,
                "Başarı oranı": (h["ok_count"] or 0) / total * 100 if total else 0.0,
                "Son başarılı": _stamp(h["last_ok"]),
                "Son hata": (h["last_error"] or "")[:80],
            }
        )
    st.dataframe(
        pd.DataFrame(rows).style.format({"Başarı oranı": "%{:,.0f}"}),
        use_container_width=True, hide_index=True,
    )
    st.caption(
        "`validate` aşaması: bizim taksit hesabımızın bankanın kendi tutarıyla "
        "karşılaştırılması (bkz. Kredi sekmesi). `reference`: bankanın kendi "
        "taksitinin okunması."
    )


# --------------------------------------------------------- HTTP istekleri ----


def _render_http(days: int) -> None:
    st.markdown("**Uç nokta bazında** — hangi istek kırılgan, ne kadar sürüyor?")
    endpoints = _endpoints(days)
    if not endpoints:
        st.info(
            "Henüz istek kaydı yok. Bu tablo `collectors/http.py` üzerinden geçen "
            "isteklerle dolar; bir toplayıcı çalıştığında görünür."
        )
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Durum": "🔴" if e["bad_count"] else "🟢",
                        "Toplayıcı": e["collector"] or "—",
                        "Kaynak": e["source"] or "—",
                        "Method": e["method"],
                        "Uç nokta": _short(e["url"]),
                        "İstek": e["calls"],
                        "Hata": e["bad_count"],
                        "Ort. süre (ms)": round(e["avg_ms"] or 0),
                        "En yavaş (ms)": e["max_ms"],
                        "Ort. yanıt (KB)": round((e["avg_bytes"] or 0) / 1024, 1),
                        "Son görülme": _stamp(e["last_seen"]),
                    }
                    for e in endpoints
                ]
            ),
            use_container_width=True, hide_index=True, height=300,
        )

    only_failures = st.checkbox("Yalnızca başarısız istekler", value=False)
    requests = _http(300, only_failures)
    st.markdown("**İstek dökümü**")
    if not requests:
        st.info("Bu filtreye uyan istek yok.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Zaman": _stamp(r["created_at"]),
                    "": OUTCOME_ICON.get(r["outcome"], "⚪"),
                    "HTTP": r["status_code"] if r["status_code"] is not None else "yanıt yok",
                    "Toplayıcı": r["collector"] or "—",
                    "Kaynak": r["source"] or "—",
                    "Method": r["method"],
                    "URL": _short(r["url"]),
                    "Süre (ms)": r["duration_ms"],
                    "Yanıt (KB)": round((r["response_bytes"] or 0) / 1024, 1),
                    "Deneme": r["attempt"],
                    "Hata": (r["error"] or "")[:100],
                }
                for r in requests
            ]
        ),
        use_container_width=True, hide_index=True, height=340,
    )
    st.caption(
        "**HTTP = 'yanıt yok'** satırlarında istek hiç tamamlanmadı (zaman aşımı "
        "ya da ağ hatası). Yanıt gövdeleri burada saklanmıyor; ham yanıtlar "
        "`data/snapshots/` altında."
    )


# ------------------------------------------------------------- kurtarma ----


def _render_recovery(days: int) -> None:
    st.markdown(
        "**\"Dönmeyen istekler daha sonra döndü mü?\"** Bir kaynak bozulup "
        "düzeldiğinde bu tablo kesintiyi ve kurtarmayı gösterir."
    )
    rows = _recovery(days)
    if not rows:
        st.success("Bu aralıkta hiçbir kaynak hata almamış.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Durum": {"düzeldi": "🟢 düzeldi",
                              "hâlâ bozuk": "🔴 hâlâ bozuk",
                              "hiç başarılı olmadı": "⚫ hiç başarılı olmadı"}.get(
                        r["state"], r["state"]
                    ),
                    "Toplayıcı": r["collector"],
                    "Kaynak": r["source"],
                    "Deneme": r["attempts"],
                    "Hata": r["failures"],
                    "Son hata": _stamp(r["last_failure"]),
                    "Son başarı": _stamp(r["last_success"]),
                    "Kesinti (dk)": r["outage_minutes"] or "—",
                }
                for r in rows
            ]
        ),
        use_container_width=True, hide_index=True,
    )
    st.caption(
        "🟢 **düzeldi**: son hatadan SONRA başarılı bir koşu var — geçici bir "
        "arızaydı. 🔴 **hâlâ bozuk**: son hatadan sonra hiç başarı gelmedi, "
        "kesinti sürüyor."
    )


# ------------------------------------------------------ oran değişimleri ----


def _render_rate_changes() -> None:
    st.markdown(
        "**Sessiz donma tespiti.** Bir uç nokta HTTP 200 dönmeye devam edebilir "
        "ama arkasındaki besleme durmuş olabilir; koşu 'ok' görünür, panel "
        "veriyi taze gösterir, oysa sayı haftalardır aynıdır. Bunu yakalayan "
        "tek soru: *bu oran en son ne zaman değişti?*"
    )
    dataset = st.radio(
        "Veri kümesi", ["deposit", "loan"],
        format_func=lambda d: {"deposit": "Mevduat", "loan": "Kredi"}[d],
        horizontal=True,
    )
    stale_days = st.slider("Kaç gündür değişmemiş olanları göster", 3, 60, 14)

    stale = _stale(dataset, stale_days)
    if stale:
        st.warning(
            f"**{len(stale)} seri {stale_days} gündür hiç değişmedi.** Oran "
            "gerçekten sabit olabilir (uzun vadeli ürünlerde normaldir) ya da "
            "besleme durmuş olabilir — panel karar vermez, sayıyı gösterir."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Kurum": s["institution"],
                        "Seri": s["series_key"],
                        "Son değişim": _stamp(s["last_change"]),
                        "Toplam değişim": s["change_count"],
                    }
                    for s in stale
                ]
            ),
            use_container_width=True, hide_index=True, height=260,
        )
    else:
        st.success(f"{stale_days} günden uzun süredir donmuş bir seri yok.")

    st.markdown("**Son değişimler**")
    changes = _changes(dataset, 200)
    if not changes:
        st.info(
            "Henüz değişim kaydı yok. İlk koşuda tüm seriler 'yeni' olarak "
            "yazılır; ikinci koşudan itibaren yalnızca gerçek değişimler."
        )
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Zaman": _stamp(c["changed_at"]),
                    "Kurum": c["institution"],
                    "Seri": c["series_key"],
                    "Eski": _pct(c["old_value"]),
                    "Yeni": _pct(c["new_value"]),
                    "Fark": _delta(c["old_value"], c["new_value"]),
                }
                for c in changes
            ]
        ),
        use_container_width=True, hide_index=True, height=340,
    )


# ------------------------------------------------------------------ LLM ----


def _render_llm() -> None:
    totals = _llm_totals(30)
    cols = st.columns(5)
    cols[0].metric("Çağrı (30 gün)", totals["calls"])
    cols[1].metric("Girdi token", f"{totals['prompt_tokens']:,}")
    cols[2].metric("Çıktı token", f"{totals['completion_tokens']:,}")
    cols[3].metric("Toplam token", f"{totals['total_tokens']:,}")
    cost = totals.get("cost_usd")
    cols[4].metric("Tahmini maliyet", f"${cost:,.4f}" if cost else "—")
    if not cost:
        st.caption(
            "Maliyet **bilinmiyor** (sıfır değil): birim fiyat tanımlı değil. "
            "`.env` içine `LLM_PRICE_INPUT_PER_1M` ve `LLM_PRICE_OUTPUT_PER_1M` "
            "girilirse hesaplanır. Uydurma bir fiyat yazmaktansa boş bırakılıyor."
        )

    calls = _llm_triggers(100)
    if not calls:
        st.success(
            "Hiç LLM çağrısı yapılmamış — **sıfır token harcandı.** Fallback "
            "yalnızca bir ayrıştırıcı kırıldığında devreye girer."
        )
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Zaman": _stamp(c["created_at"]),
                    "Durum": LLM_STATUS_LABEL.get(c["status"], c["status"]),
                    "Toplayıcı": c["collector"] or "—",
                    "Tetikleyen kaynak": c["trigger_source"] or "—",
                    "Tetikleyen hata": (c["trigger_error"] or "—")[:120],
                    "Model": c["model"],
                    "Token": c["total_tokens"],
                    "Maliyet": f"${c['cost_usd']:,.5f}" if c["cost_usd"] else "—",
                    "Kurtarılan satır": c["rows_recovered"],
                    "Süre (ms)": c["duration_ms"],
                }
                for c in calls
            ]
        ),
        use_container_width=True, hide_index=True, height=320,
    )
    st.caption(
        "**⚪ kapalı** ve **🟠 bütçe doldu** satırlarında çağrı hiç yapılmadı, "
        "token harcanmadı — kayıt yalnızca fallback'in neden devreye girmediğini "
        "gösterir. **Tetikleyen hata**, modeli çağıran ayrıştırma hatasıdır."
    )


# ---------------------------------------------------------------- koşular ----


def _render_runs(days: int) -> None:
    st.markdown("**Koşuyu ne başlattı?** Telafi mekanizmasının kanıtı.")
    triggers = _triggers(days)
    if triggers:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Tetikleyici": TRIGGER_LABEL.get(t["trigger"], t["trigger"]),
                        "Toplayıcı": t["collector"],
                        "Koşu": t["count"],
                        "Başarısız": t["failed"],
                        "Son": _stamp(t["last_seen"]),
                    }
                    for t in triggers
                ]
            ),
            use_container_width=True, hide_index=True,
        )
        if not any(t["trigger"] == "catchup" for t in triggers):
            st.caption(
                "Bu aralıkta **tazelik telafisi** hiç devreye girmemiş — planlanan "
                "koşuların hepsi zamanında çalışmış demektir."
            )
    else:
        st.info("Bu aralıkta koşu kaydı yok.")

    st.markdown("**Başarısızlıklar hangi aşamada?**")
    failures = _failures(days)
    if not failures:
        st.success("Bu aralıkta başarısız koşu yok.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Toplayıcı": f["collector"],
                    "Aşama": FAILURE_KIND_LABEL.get(f["failure_kind"], f["failure_kind"]),
                    "Adet": f["count"],
                    "Son": _stamp(f["last_seen"]),
                }
                for f in failures
            ]
        ),
        use_container_width=True, hide_index=True,
    )
    st.caption(
        "**Bant dışı veri — yazılmadı**: bu bir arıza değil, korumanın "
        "çalıştığının kanıtıdır. Veri geldi, ayrıştırıldı, ama makul aralığın "
        "dışındaydı ve veritabanına BİLEREK yazılmadı."
    )


# ------------------------------------------------------------ olay akışı ----


def _render_events() -> None:
    events = _events(300)
    if not events:
        st.info("Henüz kaynak bazlı kayıt yok.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Zaman": _stamp(e["started_at"]),
                    "": STATUS_ICON.get(e["status"], "⚪"),
                    "Toplayıcı": e["collector"],
                    "Kaynak": e["source"],
                    "Aşama": e["phase"],
                    "Satır": e["rows"],
                    "Süre (ms)": e["duration_ms"],
                    "Not": (e["error"] or "")[:140],
                }
                for e in events
            ]
        ),
        use_container_width=True, hide_index=True, height=420,
    )


# ---------------------------------------------------------------- yardım ----


def _stamp(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return format_local(value)


def _short(url: str | None, limit: int = 60) -> str:
    if not url:
        return "—"
    stripped = str(url).split("://", 1)[-1]
    return stripped if len(stripped) <= limit else stripped[: limit - 1] + "…"


def _pct(value) -> str:
    return "—" if value is None else f"%{value * 100:,.2f}"


def _delta(old, new) -> str:
    if old is None:
        return "yeni"
    diff = (new - old) * 100
    return f"{diff:+,.2f} puan"
