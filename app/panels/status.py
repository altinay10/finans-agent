"""Panelin üst şeridi — "bu sayılara güvenebilir miyim?" sorusunun cevabı.

ESKİ TASARIM VE NEDEN DEĞİŞTİ. Şerit, `st.columns(len(rows))` ile her
toplayıcıya bir `st.metric` açıyordu. On toplayıcıyla bu, ekranın en değerli
yerini on adet daraltılmış kutuya bölüyordu ve dördü birden aynı şeyi
söylüyordu. Dört ayrı kusur vardı:

1. ETİKETLER İÇ İSİMDİ. `participation_rates_kt` kullanıcının bildiği bir
   şey değil; kullanıcı sekmelerdeki adlarla düşünüyor (Döviz, Mevduat,
   Kredi, Fon). Şerit bu dilden konuşmuyordu.

2. "1,3 sa önce" TEK BAŞINA HÜKÜM VERMİYOR. Aynı sayı `fx_banks` için arıza
   (sınırı 6 saat), `loan_rates_llm` için gayet taze (sınırı 30 gün).
   Yaş, kendi SINIRIYLA birlikte gösterilmezse kullanıcı iyi ile kötüyü
   ayıramaz — şeridin "kullanışsız" görünmesinin asıl sebebi buydu.

3. EMEKLİ TOPLAYICILAR ŞERİTTE KALIYORDU. `v_freshness` scrape_runs'ı
   grupluyor; kayıttan çıkarılan bir toplayıcı (ör. `akportfoy`) orada
   sonsuza kadar yaşlanmaya devam ediyor ve kalıcı bir kırmızı üretiyordu.

4. HİÇ KOŞMAMIŞ TOPLAYICI GÖRÜNMÜYORDU. Liste veritabanından geliyordu;
   planda olup 7 gündür hiç koşmamış bir toplayıcının satırı yoktu, yani
   panel onun yokluğunu SESSİZCE geçiyordu. En tehlikeli arıza en görünmez
   olanıydı.

YENİ TASARIM — üç katman, giderek artan ayrıntı:

  1. HÜKÜM     : tek satır. Her şey yolundaysa sessiz gri bir cümle;
                 bozuksa ne yapılacağını söyleyen renkli bir kutu.
  2. ROZETLER  : sekme adlarıyla dört grup. Grubun rengi, grubun EN KÖTÜ
                 üyesinin rengidir — zayıf halka güveni belirler.
  3. AYRINTI   : kapalı bir açılır bölümde toplayıcı toplayıcı tablo; yaş
                 ve sınır yan yana. Hiçbir bilgi kaybolmuyor, sadece
                 istendiğinde görünüyor.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from app.panels.common import ISTANBUL, as_utc
from store import heartbeat, queries

# Toplayıcılar kullanıcının sekmelerde gördüğü adlarla gruplanıyor. Anahtar
# `scrape_runs.collector` (worker.py anahtarı DEĞİL — ikisi aynı değil,
# bkz. scheduler._db_name).
GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Döviz", ("fx_banks", "fx_tcmb")),
    (
        "Mevduat & Kâr Payı",
        (
            "deposit_rates",
            "participation_rates",
            "participation_rates_kt",
            "profit_shares",
            "profit_shares_kt",
        ),
    ),
    ("Kredi", ("loan_rates", "loan_rates_llm")),
    ("Fon", ("fund_prices",)),
]

# Durum -> (nokta rengi, kısa ad). Sıra aynı zamanda KÖTÜLÜK sırası:
# bir grubun rengi, üyelerinin en yüksek indeksli durumudur.
STATES: list[tuple[str, str, str]] = [
    ("fresh", "#16a34a", "taze"),
    ("off", "#9ca3af", "agent kapalı"),
    ("late", "#d97706", "gecikti"),
    ("stale", "#dc2626", "bayat"),
    ("never", "#dc2626", "hiç başarılı olmadı"),
]
STATE_ORDER = {name: i for i, (name, _, _) in enumerate(STATES)}
STATE_COLOR = {name: color for name, color, _ in STATES}
STATE_LABEL = {name: label for name, _, label in STATES}

# Durum sütununun sözlüğü. Bir renk adı ("kapalı") tek başına ne arızayı ne
# de tercihi anlatıyor: kullanıcının bilmesi gereken şey her durumda NE
# YAPACAĞI. "agent kapalı" özellikle önemli — anahtarsız kurulumda kalıcı
# olarak görünüyor ve düzeltilecek bir bozukluk sanılırsa kullanıcı olmayan
# bir arızayı kovalar.
LEGEND = (
    "**Durum**\n"
    "- 🟢 **taze** — yaş sınırın altında\n"
    "- 🟡 **gecikti** — sınırı aştı; bir koşu kaçmış olabilir\n"
    "- 🔴 **bayat** — sınırın iki katını aştı; kaynak muhtemelen kırık\n"
    "- 🔴 **hiç başarılı olmadı** — 7 gündür tek bir başarılı koşu yok\n"
    "- ⚪ **agent kapalı** — LLM anahtarı tanımlı değil, bu toplayıcı hiç "
    "çağrılmıyor. **Arıza değil, varsayılan davranış**; açmak için `.env` "
    "içine `LLM_API_KEY` ve `LLM_FALLBACK_ENABLED=1`.\n\n"
    "**Sınır**, zamanlayıcının o kaynağı yeniden denemek için beklediği süre "
    "(`scheduler.MAX_AGE_HOURS`). Agent toplayıcısınınki bilerek 30 gün: "
    "her çağrı token harcıyor."
)


@st.cache_data(ttl=120)
def _freshness() -> list[dict]:
    return queries.freshness()


@st.cache_data(ttl=3600)
def _limits() -> dict[str, float]:
    """Toplayıcı -> tazelik sınırı (saat), scrape_runs adlarıyla.

    Sınırlar scheduler.MAX_AGE_HOURS'tan okunuyor, burada ikinci bir kopya
    tutulmuyor: panelin "bayat" dediği eşik ile zamanlayıcının "yeniden
    dene" dediği eşik AYNI olmak zorunda, yoksa panel kırmızı gösterirken
    zamanlayıcı hiçbir şey yapmaz.
    """
    from scheduler import max_age_by_db_name

    return max_age_by_db_name()


def _agent_enabled() -> bool:
    """Agent kapalıyken `loan_rates_llm` arıza değil, tercih.

    Anahtarsız kurulumda bu toplayıcı her koşuda "Agent kapalı" hatasıyla
    biter. Onu kırmızı göstermek, düzeltilecek bir arıza sanılmasına yol
    açar; oysa varsayılan davranış bu (bkz. llm/settings.py).
    """
    import llm.settings as settings

    return bool(settings.LLM_FALLBACK_ENABLED and settings.LLM_API_KEY)


def _rows(now: datetime) -> list[dict]:
    """Plandaki HER toplayıcı için bir satır — veritabanı boş olsa bile.

    Liste plandan başlatılıp veritabanıyla zenginleştiriliyor; tersi değil.
    Böylece hiç koşmamış bir toplayıcı satırsız kalıp gözden kaçmıyor.
    """
    limits = _limits()
    by_name = {r["collector"]: r for r in _freshness()}
    agent_on = _agent_enabled()

    rows: list[dict] = []
    for group, members in GROUPS:
        for name in members:
            row = by_name.get(name) or {}
            age = _age_hours(row.get("last_ok"), now)
            limit = limits.get(name)
            rows.append(
                {
                    "group": group,
                    "collector": name,
                    "last_ok": row.get("last_ok"),
                    "age_hours": age,
                    "limit_hours": limit,
                    "fallback_count": row.get("fallback_count") or 0,
                    "state": _state(age, limit, name, agent_on),
                }
            )
    return rows


def retired(now: datetime) -> list[dict]:
    """Veritabanında kaydı olan ama artık plana bağlı olmayan toplayıcılar.

    Gizlemek yerine ayrıntı tablosunda "emekli" diye göstermek gerekiyor:
    tamamen saklamak, bir toplayıcının GRUPTAN düşürülmesini de görünmez
    yapardı ve o gerçek bir arıza olurdu.
    """
    known = {name for _, members in GROUPS for name in members}
    return [
        {
            "collector": r["collector"],
            "last_ok": r["last_ok"],
            "age_hours": _age_hours(r["last_ok"], now),
        }
        for r in _freshness()
        if r["collector"] not in known
    ]


def _state(age: float | None, limit: float | None, name: str, agent_on: bool) -> str:
    if name.endswith("_llm") and not agent_on:
        return "off"
    if age is None:
        return "never"
    if limit is None or age <= limit:
        return "fresh"
    # Sınırın iki katı: "bir koşu kaçtı" ile "kimse toplamıyor" farkı.
    # Hafta sonu tek bir kaçan koşuyu kırmızı göstermek, gerçek arızanın
    # rengini değersizleştirirdi.
    return "late" if age <= limit * 2 else "stale"


def _age_hours(last_ok, now: datetime) -> float | None:
    if not last_ok:
        return None
    ts = datetime.fromisoformat(last_ok) if isinstance(last_ok, str) else last_ok
    return (now - as_utc(ts)).total_seconds() / 3600


def format_age(hours: float | None) -> str:
    """Türkçe, tek birimli ve kısa: rozete sığması gerekiyor."""
    if hours is None:
        return "—"
    if hours < 1 / 60:
        # "0 dk" hem yanlış görünüyor hem de bir an için "hiç" diye okunuyor.
        return "az önce"
    if hours < 1:
        return f"{hours * 60:.0f} dk"
    if hours < 10:
        return f"{hours:.1f} sa".replace(".", ",")
    if hours < 48:
        # 10 saatten sonra ondalık bilgi taşımıyor, sadece rozeti uzatıyor.
        return f"{hours:.0f} sa"
    return f"{hours / 24:.0f} gün"


def format_ago(hours: float | None) -> str:
    """"... önce" cümlesine giren biçim.

    `format_age` rozet için var ve döndürdüğü "az önce" zaten kendi başına
    bir zarf; sonuna bir "önce" daha eklemek "az önce önce" üretiyordu.
    """
    metin = format_age(hours)
    if hours is None or metin.endswith("önce"):
        return metin
    return f"{metin} önce"


def _worst(states: list[str]) -> str:
    return max(states, key=lambda s: STATE_ORDER.get(s, 0)) if states else "never"


# --------------------------------------------------------------- çizim ----

def render() -> None:
    now = datetime.now(timezone.utc)
    rows = _rows(now)
    beat = heartbeat.read()

    _render_alarm(rows, beat)
    _render_chips(rows)
    _render_verdict(rows, beat, now)
    _render_detail(rows, now)


def _render_chips(rows: list[dict]) -> None:
    chips = []
    for group, _members in GROUPS:
        members = [r for r in rows if r["group"] == group]
        state = _worst([r["state"] for r in members])
        # Rozetin yaşı, grubun EN ESKİ üyesinin yaşı: "her şey 5 dk taze"
        # deyip içinde 3 günlük bir kaynak saklamak yanıltıcı olur.
        ages = [r["age_hours"] for r in members if r["age_hours"] is not None]
        # Yaşı olmayan grupta rozete uzun durum adı değil kısa bir ifade
        # giriyor: "hiç başarılı olmadı" rozeti satırın yarısı kadar yapardı.
        age_text = format_age(max(ages)) if ages else (STATE_LABEL["off"] if state == "off" else "veri yok")
        detail = " · ".join(
            f"{r['collector']}: {format_age(r['age_hours'])}"
            f" / sınır {format_age(r['limit_hours'])}"
            for r in members
        )
        chips.append(_chip(STATE_COLOR[state], group, age_text, detail))

    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;align-items:center;margin:.1rem 0 .2rem;">'
        + "".join(chips)
        + "</div>",
        unsafe_allow_html=True,
    )


def _chip(color: str, label: str, value: str, tooltip: str) -> str:
    """Renk NOKTADA, metin devralınan renkte.

    Rozetin kendisi gri-şeffaf: Streamlit'in açık ve koyu temasında da
    okunur kalması için sabit bir arka plan rengi seçilmiyor.
    """
    return (
        f'<span title="{html.escape(tooltip, quote=True)}" '
        'style="display:inline-flex;align-items:center;gap:.45rem;'
        "padding:.3rem .75rem;margin:0 .45rem .35rem 0;"
        "border:1px solid rgba(128,128,128,.3);border-radius:999px;"
        'font-size:.84rem;line-height:1.2;background:rgba(128,128,128,.08);">'
        f'<span style="width:.5rem;height:.5rem;border-radius:50%;'
        f'background:{color};flex:0 0 auto;"></span>'
        f"<span>{html.escape(label)}</span>"
        f'<span style="opacity:.6;">{html.escape(value)}</span>'
        "</span>"
    )


def _render_verdict(rows: list[dict], beat: dict | None, now: datetime) -> None:
    """Tek satır, sessiz gri. Sorun varsa zaten yukarıda kutu var."""
    sayilan = [r for r in rows if r["state"] != "off"]
    taze = sum(1 for r in sayilan if r["state"] == "fresh")
    stamps = [r["age_hours"] for r in rows if r["age_hours"] is not None]

    if beat and beat["alive"]:
        zamanlayici = f"zamanlayıcı çalışıyor (nabız {beat['age_seconds']:.0f} sn önce)"
    elif beat:
        zamanlayici = "zamanlayıcı durmuş"
    else:
        zamanlayici = "zamanlayıcı hiç çalışmamış"

    son = f"son toplama {format_ago(min(stamps))}" if stamps else "hiç toplama yok"
    kapali = len(rows) - len(sayilan)
    kapali_not = f" · {kapali} agent kapalı" if kapali else ""
    # Emoji YOK: rengi rozetler taşıyor. Gri altyazıya bir de renkli daire
    # koymak, aynı bilgiyi iki kez söyleyip satırı gürültülü yapıyordu.
    st.caption(
        f"{taze}/{len(sayilan)} kaynak taze{kapali_not} · {son} · {zamanlayici}"
    )


def _render_alarm(rows: list[dict], beat: dict | None) -> None:
    """Uzun kurtarma talimatı YALNIZCA gerçekten bozukken görünür.

    Beş satırlık docker komutu her açılışta panelin tepesinde durursa
    kullanıcı onu okumayı bırakır ve gerçekten bozulduğunda da okumaz.
    """
    if beat is None:
        st.error(
            "**Zamanlayıcı hiç çalışmamış — veri toplanmıyor.** Panel veri "
            "toplamaz, ayrı bir süreç gerekir:\n\n"
            "- Tek konteyner: `docker run -e ROLE=all -p 8501:8501 finans-agent` "
            "(varsayılan `ROLE=all` zamanlayıcıyı da başlatır)\n"
            "- Compose: `docker compose up -d` (panel + scheduler birlikte)\n"
            "- systemd: `systemctl start finans-scheduler`\n"
            "- Hemen tek seferlik tazeleme: `python worker.py all`"
        )
        return

    if not beat["alive"]:
        st.error(
            f"**Zamanlayıcı durmuş.** Son nabız {beat['age_seconds'] / 3600:.1f} saat "
            f"önce (host `{beat['host']}`, pid {beat['pid']}). Süreç çökmüş ya da "
            "konteyner yeniden başlatılmış olabilir.\n\n"
            "- Docker: `docker compose up -d scheduler` · "
            "log: `docker compose logs scheduler`\n"
            "- Tek konteyner: `ROLE=all` ile mi başlatıldı?\n"
            "- systemd: `systemctl status finans-scheduler`"
        )
        return

    # Zamanlayıcı AYAKTA ama veri gelmiyorsa suçlu süreç değil, kaynaklardır.
    # Kullanıcıyı "süreci başlat" diye yanlış tarafa göndermemek için mesaj
    # ayrı: burada yapılacak şey log okumak.
    bozuk = [r for r in rows if r["state"] in {"stale", "never"}]
    if not bozuk:
        return
    adlar = ", ".join(f"`{r['collector']}` ({format_age(r['age_hours'])})" for r in bozuk)
    st.error(
        f"**Zamanlayıcı çalışıyor ama {len(bozuk)} kaynak veri getirmiyor:** {adlar}. "
        "Sorun süreçte değil, kaynaklarda: uç noktalar düşmüş ya da sayfa yapısı "
        "değişmiş olabilir. **Kayıtlar** sekmesindeki kaynak sağlığı ve HTTP "
        "istekleri tablolarına bak."
    )


def _render_detail(rows: list[dict], now: datetime) -> None:
    emekli = retired(now)
    baslik = f"Veri tazeliği — {len(rows)} toplayıcı"
    if emekli:
        baslik += f" (+{len(emekli)} emekli)"
    with st.expander(baslik):
        # Yükseklik satır sayısından hesaplanıyor: varsayılan yükseklik on
        # satırın üçünü gösterip gerisini iç kaydırmaya bırakıyor ve tablo
        # "ayrıntı" olmaktan çıkıp ikinci bir bulmacaya dönüşüyordu.
        st.dataframe(
            pd.DataFrame([_detail_row(r) for r in rows]),
            use_container_width=True,
            hide_index=True,
            height=(len(rows) + 1) * 35 + 3,
        )
        st.caption(LEGEND)
        if emekli:
            st.caption(
                "**Emekli toplayıcılar** — kayıt defterinde yok, artık koşmuyor; "
                "geçmiş kayıtları veritabanında duruyor: "
                + ", ".join(
                    f"`{r['collector']}` (son başarı {format_ago(r['age_hours'])})"
                    for r in emekli
                )
            )


def _detail_row(row: dict) -> dict:
    last_ok = row["last_ok"]
    if last_ok:
        ts = datetime.fromisoformat(last_ok) if isinstance(last_ok, str) else last_ok
        stamp = f"{as_utc(ts).astimezone(ISTANBUL):%d.%m %H:%M}"
    else:
        stamp = "—"
    return {
        "Veri": row["group"],
        "Toplayıcı": row["collector"],
        "Durum": f"{_dot(row['state'])} {STATE_LABEL[row['state']]}",
        "Son başarı": stamp,
        "Yaş": format_age(row["age_hours"]),
        "Sınır": format_age(row["limit_hours"]),
        "LLM telafisi": row["fallback_count"] or "",
    }


def _dot(state: str) -> str:
    return {"fresh": "🟢", "late": "🟡", "stale": "🔴", "never": "🔴", "off": "⚪"}[state]
