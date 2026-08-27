"""Agent sekmesi — kendi API anahtarını gir, veriyi anında tazele.

NEDEN VAR. Agent toplayıcısı (`collectors/loan_rates_llm.py`) API'si olmayan
bankaların kredi oranlarını sayfa metninden çıkarıyor ve bunun için bir LLM
anahtarı gerekiyor. Anahtar yalnızca `.env`'den okunsaydı, anahtarı biten
ya da hiç anahtarı olmayan bir kullanıcı için sistem kalıcı olarak yarım
kalırdı: sunucuya kurulduktan sonra `.env`'i düzenleyip süreci yeniden
başlatmak, panele bakan kişinin yapabileceği bir şey değil.

ÜÇ AYRI SORUYU AYRI AYRI CEVAPLIYOR:

  1. Anahtar var mı, nereden geliyor?  (durum kartı)
  2. Yenisini nasıl veririm?           (oturumluk / kalıcı)
  3. Şimdi çalıştı mı, ne harcadı?     (çalıştır + token/maliyet)

ANAHTAR EKRANDA GÖSTERİLMEZ. Panel sunucuda açık duruyor olabilir; ekranda
duran bir anahtar `.env`'i korumanın bütün anlamını götürür. Yalnızca son
dört hane gösteriliyor — "hangi anahtar takılı" sorusuna yetiyor.

PANEL NORMALDE BANKALARA GİTMEZ (tasarım §01) ve bu sekme o kuralın
BİLİNÇLİ istisnası: "anlık yenileme" tanım gereği kullanıcının bastığı anda
çalışmak zorunda. Zamanlayıcıyı taklit etmiyor, tek seferlik bir koşu
yapıyor ve sonucunu aynı `scrape_runs` kaydına yazıyor.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import llm.settings as settings
from app.panels.common import ISTANBUL, as_utc
from config import env_file
from store import queries

SESSION_KEY = "agent_api_key"


def apply_session_key() -> str:
    """Ayarları tazeler, oturumluk anahtar varsa onu üste yazar.

    Sıralama önemli: önce `.env` okunuyor (başka bir süreç ya da kullanıcı
    dosyayı değiştirmiş olabilir), sonra bu tarayıcı oturumuna özel anahtar
    uygulanıyor. Ters sırada, dosyadaki eski anahtar kullanıcının az önce
    girdiğini sessizce geri alırdı.

    Dönen değer anahtarın KAYNAĞI: 'oturum' | '.env' | 'yok'.
    """
    settings.reload_from_env()
    oturum = st.session_state.get(SESSION_KEY)
    if oturum:
        settings.apply(LLM_API_KEY=oturum, LLM_FALLBACK_ENABLED=True)
        return "oturum"
    return ".env" if settings.LLM_API_KEY else "yok"


def render() -> None:
    kaynak = apply_session_key()

    st.subheader("Agent (LLM) anahtarı")
    st.caption(
        "Bazı bankaların açık bir API'si yok; oranları sayfa metninden bir dil "
        "modeli çıkarıyor. Bunun için bir anahtar gerekiyor. **Kendi anahtarını "
        "buradan girebilirsin** — anahtarın biterse ya da hiç yoksa sistem bu "
        "sayede yarım kalmaz."
    )

    _render_state(kaynak)
    _render_key_form(kaynak)
    st.divider()
    _render_run()
    st.divider()
    _render_usage()


# ------------------------------------------------------------- durum ----

def _render_state(kaynak: str) -> None:
    c1, c2, c3 = st.columns(3)
    c1.metric("Agent", "açık" if settings.LLM_FALLBACK_ENABLED else "kapalı")
    c2.metric("Anahtar", settings.masked_key(), help=f"kaynak: {kaynak}")
    c3.metric("Model", settings.LLM_MODEL)

    if kaynak == "yok":
        st.warning(
            "**Anahtar yok — agent çalışmıyor.** Bu, panelin geri kalanını "
            "etkilemez: API'si olan bankaların oranları normal toplayıcılarla "
            "geliyor. Yalnızca agent'a bağlı bankalar (Halkbank, QNB, "
            "DenizBank, ING) güncellenmiyor.\n\n"
            "Ücretsiz anahtar: https://aistudio.google.com/apikey"
        )
    elif kaynak == "oturum":
        st.info(
            "Anahtar **yalnızca bu tarayıcı oturumunda** geçerli: diske "
            "yazılmadı, zamanlayıcı süreci onu görmüyor. Planlı koşuların da "
            "kullanması için aşağıdan kalıcı kaydet."
        )
        # Geri dönüş yolu olmadan, yanlış anahtar giren kullanıcı tarayıcıyı
        # kapatana kadar sıkışıp kalıyordu.
        if st.button("Oturumluk anahtarı kaldır", help="`.env`'deki anahtara geri dön"):
            st.session_state.pop(SESSION_KEY, None)
            settings.reload_from_env()
            st.rerun()


# ----------------------------------------------------------- giriş ----

def _render_key_form(kaynak: str) -> None:
    # clear_on_submit: gönderilen anahtar kutuda (ve DOM'da) asılı
    # kalmasın — ekranı gören herkes onu "göster" düğmesiyle okuyabilirdi.
    with st.form("agent_key", clear_on_submit=True):
        girilen = st.text_input(
            "API anahtarı",
            type="password",
            placeholder="AIzaSy… veya sağlayıcının verdiği anahtar",
            help="Yazdığın anahtar ekranda görünmez ve hiçbir yere gönderilmez; "
                 "yalnızca seçtiğin LLM sağlayıcısına gider.",
        )
        model = st.text_input("Model (isteğe bağlı)", value=settings.LLM_MODEL)
        c1, c2 = st.columns(2)
        oturumluk = c1.form_submit_button("Bu oturumda kullan", use_container_width=True)
        kalici = c2.form_submit_button(
            "Kalıcı kaydet (.env)", type="primary", use_container_width=True
        )

    if not (oturumluk or kalici):
        return
    if not girilen.strip():
        st.error("Anahtar boş. Kaydedilmedi.")
        return

    anahtar = girilen.strip()
    if kalici:
        try:
            yol = env_file.set_values(
                {
                    "LLM_API_KEY": anahtar,
                    "LLM_MODEL": model.strip() or settings.LLM_MODEL,
                    "LLM_FALLBACK_ENABLED": "1",
                }
            )
        except (OSError, ValueError) as exc:
            st.error(f"`.env` yazılamadı: {exc}")
            return
        # Oturumluk anahtar kalıcı olanı gizlerdi: kullanıcı "kaydettim ama
        # eskisi kullanılıyor" durumuna düşerdi.
        st.session_state.pop(SESSION_KEY, None)
        settings.reload_from_env()
        st.success(
            f"Kaydedildi (`{yol.name}`). Panel hemen kullanmaya başladı. "
            "**Zamanlayıcı süreci** anahtarı bir sonraki koşusunda okuyacak — "
            "yeniden başlatmaya gerek yok."
        )
    else:
        st.session_state[SESSION_KEY] = anahtar
        settings.apply(
            LLM_API_KEY=anahtar,
            LLM_MODEL=model.strip() or settings.LLM_MODEL,
            LLM_FALLBACK_ENABLED=True,
        )
        st.success("Bu oturum için ayarlandı. Diske yazılmadı.")

    st.rerun()


# -------------------------------------------------------- çalıştır ----

def _render_run() -> None:
    st.markdown("#### Şimdi tazele")
    st.caption(
        "Agent'a bağlı bankaların kredi oranlarını **şu anda** çeker. Planlı "
        "koşu her gün 15:00'te (faiz kararları mesai saatinde açıklanıyor); "
        "bu düğme onu beklemeden bir kez çalıştırır."
    )

    if not settings.LLM_FALLBACK_ENABLED or not settings.LLM_API_KEY:
        st.button("Agent'ı çalıştır", disabled=True, help="Önce anahtar gir.")
        return

    if st.button("Agent'ı çalıştır", type="primary"):
        # İçeriden import: sekme açılır açılmaz toplayıcı modülünü yüklemek
        # (ve onunla httpx/yaml zincirini) panelin açılışını yavaşlatırdı.
        from collectors.loan_rates_llm import LlmLoanRateCollector

        with st.spinner("Bankalar çekiliyor ve modele soruluyor…"):
            onceki = queries.llm_token_totals(days=1)
            sonuc = LlmLoanRateCollector().run(trigger="manual")
            sonraki = queries.llm_token_totals(days=1)

        harcanan = sonraki["total_tokens"] - onceki["total_tokens"]
        if sonuc.ok:
            st.success(
                f"Tamam — **{sonuc.rows} oran** yazıldı, {harcanan} token harcandı. "
                "Kredi sekmesinde görebilirsin."
            )
        else:
            # Hatanın AŞAMASI önemli: 'fetch' banka sitesini, 'parse' modeli
            # ya da anahtarı işaret ediyor. İkisinde yapılacak şey farklı.
            st.error(
                f"Koşu başarısız (aşama: {sonuc.failure_kind or 'bilinmiyor'}). "
                f"{harcanan} token harcandı.\n\n```\n{sonuc.error}\n```"
            )
        _cached_llm_calls.clear()


# --------------------------------------------------------- kullanım ----

@st.cache_data(ttl=60)
def _cached_llm_calls() -> list[dict]:
    return queries.llm_calls(limit=25)


def _render_usage() -> None:
    st.markdown("#### Token ve maliyet")
    toplam = queries.llm_cost_totals(days=30)
    maliyet = toplam.get("cost_usd")

    c1, c2, c3 = st.columns(3)
    c1.metric("Çağrı (30 gün)", toplam["calls"])
    c2.metric("Token (30 gün)", f"{toplam['total_tokens']:,}".replace(",", "."))
    # "bilinmiyor" ile "sıfır" AYRI: birim fiyat girilmemişse maliyet
    # uydurulmaz (bkz. llm/settings.py::estimate_cost_usd).
    c3.metric(
        "Tahmini maliyet",
        f"${maliyet:.4f}" if maliyet is not None else "bilinmiyor",
        help="Birim fiyat `.env`'de tanımlı değilse hesaplanmaz — 0 yazmak "
             "'bedava' demek olurdu.",
    )

    rows = _cached_llm_calls()
    if not rows:
        st.caption("Henüz agent çağrısı yok.")
        return
    st.dataframe(
        pd.DataFrame([_call_row(r) for r in rows]),
        use_container_width=True,
        hide_index=True,
    )


def _call_row(row: dict) -> dict:
    ts = row.get("created_at")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return {
        "Zaman": f"{as_utc(ts).astimezone(ISTANBUL):%d.%m %H:%M}" if ts else "—",
        "Toplayıcı": row.get("collector") or "—",
        "Model": row.get("model") or "—",
        "Durum": row.get("status") or "—",
        "Girdi": row.get("prompt_tokens") or 0,
        "Çıktı": row.get("completion_tokens") or 0,
        "Kurtarılan satır": row.get("rows_recovered") or 0,
        "Hata": (row.get("error") or "")[:80],
    }
