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

import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st

import llm.settings as settings
from app.panels.common import ISTANBUL, as_utc, llm_call_description
from llm import credentials
from store import app_settings, queries

SESSION_KEY = "agent_api_key"
#: Oturumluk sağlayıcı ayarları. Anahtar tek başına yetmiyor: aynı anahtar
#: Gemini'de geçerli, Qwen'de değil. Eskiden panelde girilen model hiçbir
#: yere gitmiyordu ve oturum koşusu `.env`'deki sağlayıcıya istek atıyordu.
SESSION_BASE_URL = "agent_base_url"
SESSION_MODEL = "agent_model"
SESSION_EXTRA_BODY = "agent_extra_body"

# NOT: Panelden `.env`'e yazma yolu KALDIRILDI (2026-09-06). Yerini
# veritabanı aldı (bkz. llm/credentials.py). Sebebi teknikti, tercih değil:
# `.env` `.dockerignore`'da olduğu için imaja hiç girmiyor, panel ile
# zamanlayıcı ayrı konteynerler ve panelin yazdığı dosyayı zamanlayıcı
# göremiyordu; üstelik `docker compose up --build` her yeniden kurulumda
# o dosyayı siliyordu. `PANEL_ALLOW_ENV_WRITE` artık hiçbir şeyi
# değiştirmiyor, okuyan kod kalmadı.


class AnahtarGerekli(RuntimeError):
    """Elle koşu için kullanıcının kendi anahtarı yok."""


def session_key() -> str | None:
    """Bu tarayıcı oturumuna girilmiş anahtar (yoksa None)."""
    return (st.session_state.get(SESSION_KEY) or "").strip() or None


def session_provider() -> dict:
    """Oturuma girilmiş sağlayıcı ayarları (boşlar atlanır)."""
    return {
        "base_url": (st.session_state.get(SESSION_BASE_URL) or "").strip() or None,
        "model": (st.session_state.get(SESSION_MODEL) or "").strip() or None,
        "extra_body": st.session_state.get(SESSION_EXTRA_BODY),
    }


def key_source() -> str:
    """Anahtarın kaynağı: 'oturum' | '.env' | 'yok'.

    Oturum anahtarı ARTIK süreç geneline YAZILMIYOR. Eskiden yazılıyordu ve
    bu iki ayrı hata üretiyordu: ziyaretçinin anahtarı zamanlayıcının planlı
    koşularına da bulaşıyordu, ve Streamlit tüm tarayıcı oturumlarını aynı
    süreçte koşturduğu için iki ziyaretçi birbirinin anahtarını görebiliyordu.
    Oturum anahtarı artık yalnızca kendi tetiklediği koşuya, iş parçacığına
    bağlı bir bağlam üzerinden giriyor (bkz. llm/settings.use_api_key).
    """
    settings.reload_from_env()
    if session_key():
        return "oturum"
    # .ENV ÖNCE KONTROL EDİLİYOR (kullanıcı kararı, 2026-09-06): birleşik
    # zincirde `.env` her zaman baştadır (bkz. credentials.effective_chain).
    # `credentials.active()` artık .env varken onu döndürür; burada AYRICA
    # doğrudan `settings.LLM_API_KEY`e bakmak, ekranda "kayıtlı" değil
    # doğru kaynağı (".env") göstermek için gerekli.
    if settings.LLM_API_KEY:
        return ".env"
    if credentials.chain():
        return "kayıtlı"
    return "yok"


def run_agent(key: str | None):
    """Agent'ı YALNIZCA verilen anahtarla çalıştırır.

    KARAR BURADA VERİLİYOR, düğmenin çiziminde değil. Bir düğmeyi `disabled`
    yapmak yalnızca görseldir: istemciden üretilmiş bir olay yine de sunucu
    tarafındaki bu yolu çağırabilir. Kontrol çalıştırma yolunun içinde olmak
    zorunda.

    Neden `.env` anahtarına düşmüyor: o anahtar PANEL SAHİBİNİNDİR ve planlı
    koşular içindir. Paneli açan herkesin bir düğmeye basarak onu
    harcayabilmesi — üstelik sınırsız tekrarla — doğrudan bir fatura açığıdır.
    """
    if not key or not key.strip():
        raise AnahtarGerekli(
            "Elle çalıştırma için kendi API anahtarını girmen gerekiyor. "
            "Sunucudaki anahtar planlı koşulara ayrılmıştır."
        )
    # İçeriden import: sekme açılır açılmaz toplayıcı modülünü (ve onunla
    # httpx/yaml zincirini) yüklemek panelin açılışını yavaşlatırdı.
    from collectors.loan_rates_llm import LlmLoanRateCollector
    from llm import extract as llm_extract

    # worker.py ve scheduler.py bunu her koşudan önce yapıyor; panel
    # yapmıyordu. Panel süreci UZUN ÖMÜRLÜ olduğu için sayaç birikiyordu:
    # ilk koşu bütçeyi doldurup ikinci koşuyu SESSİZCE hiçbir şey
    # yapmayan bir "sınıra ulaşıldı"ya çeviriyordu.
    llm_extract.reset_budget()

    saglayici = session_provider()
    with settings.use_api_key(
        key,
        base_url=saglayici["base_url"],
        model=saglayici["model"],
        extra_body=saglayici["extra_body"],
    ):
        return LlmLoanRateCollector().run(trigger="manual")


def render() -> None:
    kaynak = key_source()

    st.subheader("Agent (LLM) anahtarı")
    st.caption(
        "Bazı bankaların açık bir API'si yok; oranları sayfa metninden bir dil "
        "modeli çıkarıyor. Bunun için bir anahtar gerekiyor. **Kendi anahtarını "
        "buradan girebilirsin** — anahtarın biterse ya da hiç yoksa sistem bu "
        "sayede yarım kalmaz."
    )

    _render_state(kaynak)
    _render_key_form(kaynak)
    _render_saved_keys()
    _render_pricing()
    st.divider()
    _render_run()
    st.divider()
    _render_usage()


# ------------------------------------------------------------- durum ----

def _render_state(kaynak: str) -> None:
    # Gösterilen anahtar, o kaynağın anahtarı. Oturum anahtarı artık süreç
    # genelinde durmadığı için `masked_key()`'e AÇIKÇA veriliyor; parametresiz
    # çağrı `.env`'dekini gösterip kullanıcıya yanlış anahtarı işaret ederdi.
    if kaynak == "oturum":
        gosterilecek = session_key()
    elif kaynak == "kayıtlı":
        aktif = credentials.active()
        gosterilecek = aktif.api_key if aktif else ""
    else:
        gosterilecek = settings.LLM_API_KEY
    c1, c2, c3 = st.columns(3)
    c1.metric("Planlı koşular", "açık" if settings.fallback_enabled() else "kapalı")
    c2.metric("Anahtar", settings.masked_key(gosterilecek), help=f"kaynak: {kaynak}")
    # KART BAĞLAMDAN OKUR: eskiden `settings.LLM_MODEL` yazıyordu, yani
    # oturumluk model girildiğinde kart hâlâ .env'deki modeli gösteriyordu.
    c3.metric("Model", settings.current_model())

    if kaynak == "yok":
        st.warning(
            "**Kayıtlı anahtar yok.** Bu, panelin geri kalanını etkilemez: "
            "API'si olan bankaların oranları normal toplayıcılarla geliyor ve "
            "döviz/mevduat/fon tarafı LLM'e hiç dokunmuyor. Yalnızca agent'a "
            "bağlı banka sayfaları planlı koşularda güncellenmiyor.\n\n"
            "Ücretsiz anahtar: https://aistudio.google.com/apikey"
        )
    elif kaynak == ".env":
        yedek_sayisi = len(credentials.chain())
        yedek_notu = (
            f" **{yedek_sayisi} kayıtlı yedek anahtar** bekliyor; `.env`'deki "
            "kimlik hatasıyla düşerse otomatik olarak sıradakine geçilir."
            if yedek_sayisi else ""
        )
        st.success(
            f"**`.env`'deki anahtar** kullanılıyor (model `{settings.LLM_MODEL}`). "
            "Panelden kaydedilen bir anahtar varsa bile `.env` ÖNCELİKLİDİR — "
            "sunucu sahibinin doğrudan yapılandırdığı anahtar, panelden hiç "
            f"dokunulmamıştır.{yedek_notu}"
        )
    elif kaynak == "kayıtlı":
        aktif = credentials.active()
        st.success(
            f"Panelden kaydedilen anahtar kullanılıyor (**{aktif.masked}**, "
            f"model `{aktif.model}`) — `.env`'de anahtar tanımlı değil. "
            "Veritabanında tutuluyor, yani **zamanlayıcı da aynı anahtarı "
            "görüyor** ve konteyner yeniden kurulunca kaybolmuyor."
        )
    elif kaynak == "oturum":
        st.info(
            "Anahtar **yalnızca bu tarayıcı oturumunda** geçerli: diske "
            "yazılmadı, başka bir oturum onu göremez, zamanlayıcı da "
            "kullanmaz. Yalnızca aşağıdaki **Şimdi tazele** düğmesini besler."
        )
        # Geri dönüş yolu olmadan, yanlış anahtar giren kullanıcı tarayıcıyı
        # kapatana kadar sıkışıp kalıyordu.
        if st.button("Oturumluk anahtarı kaldır", help="Kalıcı kimliğe (varsa .env, yoksa kayıtlı anahtar) geri dön"):
            st.session_state.pop(SESSION_KEY, None)
            settings.reload_from_env()
            st.rerun()


# ----------------------------------------------------------- giriş ----

def _render_key_form(kaynak: str) -> None:
    """Anahtar + SAĞLAYICI girişi ve iki kaydetme yolu.

    İKİ BUTON, İKİ AYRI ANLAM:
      * "Yalnızca bu oturumda kullan" — diske hiç yazılmaz, yalnızca bu
        tarayıcı oturumunun kendi koşusunu besler.
      * "Sürekli kullanmak için kaydet" — veritabanına yazılır; zamanlayıcı
        da okur, konteyner yeniden kurulunca kaybolmaz.

    NEDEN VERİTABANI, `.env` DEĞİL: `.env` imaja hiç girmiyor
    (`.dockerignore`) ve panel ile zamanlayıcı AYRI konteynerler. Panelden
    `.env`'e yazmak üç sebeple işe yaramazdı: dosya yalnızca panel
    konteynerinin geçici katmanında oluşur, zamanlayıcı onu göremez ve
    `docker compose up --build` ilk yeniden kurulumda siler.

    KAYDETMEDEN ÖNCE CANLI TEST: çalışmayan bir anahtarı kaydetmek sistemi
    "anahtar var ama hiçbir şey çalışmıyor" durumuna sokar; teşhisi zordur
    çünkü panel anahtarı gösterirken koşular sessizce başarısız olur.
    """
    with st.form("agent_key", clear_on_submit=True):
        girilen = st.text_input(
            "API anahtarı",
            type="password",
            placeholder="AIzaSy… veya sağlayıcının verdiği anahtar",
            help="Yazdığın anahtar ekranda görünmez ve hiçbir yere gönderilmez; "
                 "yalnızca seçtiğin LLM sağlayıcısına gider.",
        )
        c1, c2 = st.columns(2)
        base_url = c1.text_input(
            "Taban URL (OpenAI uyumlu uç nokta)",
            value=settings.current_base_url(),
            help="Sağlayıcıyı bu belirler. Gemini, DeepSeek, Qwen ve yerel "
                 "Ollama'nın hepsi OpenAI uyumlu uç nokta veriyor.",
        )
        model = c2.text_input(
            "Model",
            value=settings.current_model(),
            help="Model herhangi biri olabilir; sağlayıcının verdiği adı yaz.",
        )
        extra_ham = st.text_input(
            "Ek gövde alanları (isteğe bağlı, JSON)",
            value=json.dumps(settings.current_extra_body()) if settings.current_extra_body() else "",
            placeholder='{"enable_thinking": false}',
            help="Sağlayıcıya özel, OpenAI şemasında olmayan alanlar. "
                 "QWEN KULLANIYORSAN `{\"enable_thinking\": false}` yaz — düşünme "
                 "modu varsayılan açık ve çıkarım işinde token yakar.",
        )
        b1, b2 = st.columns(2)
        oturumluk = b1.form_submit_button(
            "Yalnızca bu oturumda kullan", use_container_width=True
        )
        kalici = b2.form_submit_button(
            "Sürekli kullanmak için kaydet", type="primary", use_container_width=True
        )

    if not (oturumluk or kalici):
        return
    if not girilen.strip():
        st.error("Anahtar boş. Kaydedilmedi.")
        return

    # Ek gövde JSON'u BURADA doğrulanıyor: bozuk bir JSON sessizce boş
    # sözlüğe düşseydi, Qwen kullanıcısı düşünme modu açık kalmış bir
    # anahtarı "kaydettim" sanıp token yakmaya devam ederdi.
    extra_body: dict = {}
    if extra_ham.strip():
        try:
            extra_body = json.loads(extra_ham)
            if not isinstance(extra_body, dict):
                raise ValueError("JSON nesnesi olmalı")
        except ValueError as exc:
            st.error(f"Ek gövde alanları geçerli bir JSON nesnesi değil: {exc}")
            return

    anahtar = girilen.strip()
    if kalici:
        with st.spinner("Anahtar sağlayıcıda deneniyor…"):
            calisti, hata = credentials.test_credential(
                anahtar, base_url=base_url, model=model, extra_body=extra_body
            )
        if not calisti:
            st.error(
                "**Anahtar çalışmadı, kaydedilmedi.** Sağlayıcının yanıtı:\n\n"
                f"```\n{hata}\n```\n"
                "Taban URL ile modelin birbirine uyduğundan emin ol — anahtar "
                "doğru olsa bile yanlış uç noktaya gönderilirse reddedilir."
            )
            return
        cred = credentials.save(
            anahtar, base_url=base_url, model=model, extra_body=extra_body
        )
        # Oturumluk anahtar kalıcı olanı gizlerdi: kullanıcı "kaydettim ama
        # eskisi kullanılıyor" durumuna düşerdi.
        for anahtar_adi in (SESSION_KEY, SESSION_BASE_URL, SESSION_MODEL, SESSION_EXTRA_BODY):
            st.session_state.pop(anahtar_adi, None)
        if settings.LLM_API_KEY:
            # .ENV DOLUYKEN bu anahtar HEMEN devreye girmez — yedek olarak
            # sıraya girer. Bunu söylemezsek kullanıcı "kaydettim, neden hâlâ
            # eski model kullanılıyor" diye şaşırırdı.
            st.success(
                f"**Test edildi ve kaydedildi** ({cred.masked}, `{cred.model}`) — "
                "**yedek olarak.** `.env`'de bir anahtar tanımlı olduğu için "
                "planlı koşular hâlâ ONU kullanıyor; bu anahtar yalnızca `.env`'in "
                "kimlik hatasıyla düşmesi durumunda devreye girer."
            )
        else:
            st.success(
                f"**Test edildi ve kaydedildi** ({cred.masked}, `{cred.model}`). "
                "Veritabanında tutuluyor: zamanlayıcı bir sonraki koşusunda "
                "kullanacak, konteyner yeniden kurulsa da kaybolmayacak. "
                "En son kaydedilen anahtar kullanılır; kullanılamaz hale gelirse "
                "bir öncekine düşülür."
            )
    else:
        # SÜREÇ GENELİNE YAZILMIYOR: modül globali tüm tarayıcı oturumlarınca
        # paylaşılıyor ve zamanlayıcının planlı koşuları da onu okurdu.
        st.session_state[SESSION_KEY] = anahtar
        st.session_state[SESSION_BASE_URL] = base_url.strip()
        st.session_state[SESSION_MODEL] = model.strip()
        st.session_state[SESSION_EXTRA_BODY] = extra_body
        st.success(
            "Bu oturum için ayarlandı. Diske yazılmadı, başka bir oturum "
            "göremez, zamanlayıcı kullanmaz. **Agent'ı çalıştır** düğmesi "
            "artık çalışıyor."
        )

    st.rerun()


def _render_saved_keys() -> None:
    """Kayıtlı anahtarlar — hangisi kullanılıyor, hangisi yedek, hangisi düştü."""
    kayitlilar = credentials.listele()
    if not kayitlilar:
        return
    # .ENV VARKEN HİÇBİR DB SATIRI FİİLEN KULLANILMIYOR — birleşik zincirde
    # `.env` her zaman baştadır. Rozeti buna göre çizmezsek liste "üstteki
    # kullanılıyor" derken .env zaten onu geride bırakmış olurdu.
    env_oncelikli = bool(settings.LLM_API_KEY)
    with st.expander(f"Kayıtlı anahtarlar ({len(kayitlilar)})", expanded=False):
        if env_oncelikli:
            st.caption(
                "**`.env`'deki anahtar öncelikli** — buradakiler yalnızca o "
                "kimlik hatasıyla düşerse sırayla devreye girer. `.env` boşsa "
                "en üstteki doğrudan kullanılır."
            )
        else:
            st.caption(
                "**En üstteki kullanılır.** Bir anahtar kimlik hatası verirse "
                "(kota doldu, iptal edildi) otomatik olarak `düştü` işaretlenir ve "
                "sıradakine geçilir. Kotası yenilenirse ilk başarılı koşuda tekrar "
                "`çalışıyor` olur — bu yüzden düşen anahtar silinmiyor, sona atılıyor."
            )
        for index, cred in enumerate(kayitlilar):
            c1, c2, c3, c4 = st.columns([2, 3, 2, 1])
            if cred.status == "failed":
                etiket = "🔴 düştü"
            elif index == 0 and not env_oncelikli:
                etiket = "🟢 kullanılıyor"
            else:
                etiket = "⚪ yedek"
            c1.write(f"{etiket} · **{cred.masked}**")
            c2.write(f"`{cred.model}`")
            c3.caption(cred.base_url.replace("https://", "")[:28])
            if c4.button("Sil", key=f"sil_{cred.id}"):
                credentials.delete(cred.id)
                st.rerun()
            if cred.status == "failed" and cred.last_error:
                st.caption(f"↳ {cred.last_error[:160]}")


def _render_pricing() -> None:
    """Token birim fiyatı — maliyetin hesaplanabilmesi için."""
    girdi_fiyat, cikti_fiyat = app_settings.price_rates()
    with st.expander("Birim fiyat (tahmini maliyet için)", expanded=girdi_fiyat is None):
        st.caption(
            "Sağlayıcının fiyat sayfasındaki değerleri gir: **USD / 1.000.000 "
            "token**. Boş bırakılırsa maliyet *bilinmiyor* olarak gösterilir — "
            "0 yazmak 'bedava' demektir ve ücretsiz katmanda bu doğrudur. "
            "Kaydedilen fiyat veritabanında tutulur ve **bundan sonraki her "
            "çağrının** maliyeti onunla hesaplanır."
        )
        with st.form("fiyat"):
            c1, c2 = st.columns(2)
            girdi = c1.number_input(
                "Girdi (USD / 1M token)",
                min_value=0.0, step=0.01, format="%.4f",
                value=float(girdi_fiyat) if girdi_fiyat is not None else 0.0,
            )
            cikti = c2.number_input(
                "Çıktı (USD / 1M token)",
                min_value=0.0, step=0.01, format="%.4f",
                value=float(cikti_fiyat) if cikti_fiyat is not None else 0.0,
            )
            k1, k2 = st.columns(2)
            kaydet = k1.form_submit_button("Fiyatı kaydet", use_container_width=True)
            temizle = k2.form_submit_button("Fiyatı temizle", use_container_width=True)
        if kaydet:
            app_settings.set_price_rates(girdi, cikti)
            st.success(
                f"Kaydedildi: girdi ${girdi:.4f} / çıktı ${cikti:.4f} (1M token). "
                "Bundan sonraki çağrılar bu fiyatla hesaplanacak; **daha önceki "
                "çağrıların maliyeti bilinmiyor olarak kalır** çünkü o an geçerli "
                "fiyat kayıtlı değildi."
            )
            st.rerun()
        if temizle:
            app_settings.set_price_rates(None, None)
            st.success("Fiyat temizlendi; maliyet yeniden *bilinmiyor* olarak gösterilecek.")
            st.rerun()


# -------------------------------------------------------- çalıştır ----


def _render_run() -> None:
    st.markdown("#### Şimdi tazele")
    st.caption(
        "Agent'a bağlı bankaların kredi oranlarını **şu anda** çeker. Planlı "
        "koşu **Pazartesi ve Perşembe 15:00**'te (faiz kararları mesai "
        "saatinde açıklanıyor); bu düğme onu beklemeden bir kez çalıştırır."
    )

    # Düğmenin `disabled` olması yalnızca GÖRSELDİR ve tek başına bir güvence
    # değildir; asıl kontrol `run_agent()` içinde, koşu yolunun kendisinde.
    if not session_key():
        st.button("Agent'ı çalıştır", disabled=True)
        st.caption(
            "Bu düğme **yalnızca kendi anahtarınla** çalışır. Kayıtlı anahtar "
            "planlı koşulara ayrılmıştır; panelden harcanamaz. Yukarıdan "
            "*Yalnızca bu oturumda kullan* ile kendi anahtarını gir."
        )
        return

    if st.button("Agent'ı çalıştır", type="primary"):
        with st.spinner("Bankalar çekiliyor ve modele soruluyor…"):
            onceki = queries.llm_cost_totals(days=1)
            try:
                # Anahtar burada TEKRAR okunuyor: karar çizim anındaki
                # duruma değil, koşu anındaki duruma göre veriliyor.
                sonuc = run_agent(session_key())
            except AnahtarGerekli as exc:
                st.error(str(exc))
                return
            sonraki = queries.llm_cost_totals(days=1)

        _render_run_usage(onceki, sonraki, sonuc)
        _cached_llm_calls.clear()


def _fark(onceki: dict, sonraki: dict, alan: str) -> float:
    """İki ölçüm arasındaki fark — None'ı sıfır sayar."""
    return (sonraki.get(alan) or 0) - (onceki.get(alan) or 0)


def _render_run_usage(onceki: dict, sonraki: dict, sonuc) -> None:
    """Düğmenin ALTINDA: bu koşu ne harcadı.

    Girdi ve çıktı AYRI gösteriliyor: ikisinin birim fiyatı farklı ve
    "çıktı token'ı neden bu kadar yüksek" sorusu (Qwen'de düşünme modu)
    ancak ayrıldığında görülüyor.
    """
    girdi = _fark(onceki, sonraki, "prompt_tokens")
    cikti = _fark(onceki, sonraki, "completion_tokens")
    cagri = _fark(onceki, sonraki, "calls")
    maliyet = None
    if onceki.get("cost_usd") is not None or sonraki.get("cost_usd") is not None:
        maliyet = (sonraki.get("cost_usd") or 0) - (onceki.get("cost_usd") or 0)

    if sonuc.ok:
        st.success(f"Tamam — **{sonuc.rows} oran** yazıldı. Kredi sekmesinde görebilirsin.")
    else:
        # Hatanın AŞAMASI önemli: 'fetch' banka sitesini, 'parse' modeli
        # ya da anahtarı işaret ediyor. İkisinde yapılacak şey farklı.
        st.error(
            f"Koşu başarısız (aşama: {sonuc.failure_kind or 'bilinmiyor'}).\n\n"
            f"```\n{sonuc.error}\n```"
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Çağrı", int(cagri))
    c2.metric("Girdi tokeni", f"{int(girdi):,}".replace(",", "."))
    c3.metric("Çıktı tokeni", f"{int(cikti):,}".replace(",", "."))
    # "bilinmiyor" ile "sıfır" AYRI: fiyat girilmemişse maliyet uydurulmaz.
    c4.metric("Maliyet", f"${maliyet:.5f}" if maliyet is not None else "bilinmiyor")
    if maliyet is None:
        st.caption(
            "Maliyet **bilinmiyor** (sıfır değil): birim fiyat tanımlı değil. "
            "Yukarıdaki *Birim fiyat* bölümünden girebilirsin."
        )
    st.caption("Bu koşu **kendi anahtarınla** yapıldı; kayıtlı anahtar harcanmadı.")


# --------------------------------------------------------- kullanım ----


@st.cache_data(ttl=60)
def _cached_llm_calls(limit: int, since: date, until: date) -> list[dict]:
    # Önbellek anahtarı parametreleri İÇERMEK ZORUNDA: parametresiz bir
    # önbellek, tarih değiştirildiğinde eski günün satırlarını döndürürdü.
    return queries.llm_calls(limit=limit, since=since, until=until)


def _render_usage() -> None:
    st.markdown("#### Token ve maliyet")

    bugun = datetime.now(ISTANBUL).date()
    ilk_kayit, _ = queries.llm_calls_span()

    c1, c2 = st.columns([3, 1])
    # VARSAYILAN GÜNLÜK: her açılışta bugünün çağrıları. Geçmiş silinmiyor,
    # yalnızca gösterilmiyor — aralığı genişletmek tek tıklık.
    aralik = c1.date_input(
        "Tarih aralığı",
        value=(bugun, bugun),
        min_value=ilk_kayit or bugun,
        max_value=bugun,
        help="Varsayılan bugün. Geçmiş kayıtların hiçbiri silinmiyor "
             "(llm_calls asla budanmaz); aralığı geriye çekerek hepsini görebilirsin.",
    )
    limit = c2.selectbox("Satır", [50, 200, 1000, 5000], index=1)

    # date_input tek tarih de döndürebiliyor (kullanıcı ilk günü seçip
    # ikinciyi seçmeden). Tek değeri aralığa çevirmezsek tablo patlardı.
    if isinstance(aralik, (list, tuple)):
        since = aralik[0]
        until = aralik[1] if len(aralik) > 1 else aralik[0]
    else:
        since = until = aralik

    toplam = queries.llm_cost_totals_between(since, until)
    kayit_sayisi = queries.llm_calls_count(since, until)
    maliyet = toplam.get("cost_usd")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Çağrı", toplam["calls"])
    m2.metric("Girdi tokeni", f"{toplam['prompt_tokens']:,}".replace(",", "."))
    m3.metric("Çıktı tokeni", f"{toplam['completion_tokens']:,}".replace(",", "."))
    m4.metric(
        "Tahmini maliyet",
        # `is not None` ŞART: 0.0 geçerli bir maliyettir (ücretsiz katman).
        # `if maliyet else` yazmak sıfırı "bilinmiyor" gibi gösterirdi.
        f"${maliyet:.5f}" if maliyet is not None else "bilinmiyor",
        help="Birim fiyat tanımlı değilse hesaplanmaz — 0 yazmak "
             "'bedava' demek olurdu.",
    )

    rows = _cached_llm_calls(limit, since, until)
    if not rows:
        st.caption(
            f"Seçilen aralıkta çağrı yok. Kayıtlı en eski çağrı: "
            f"{ilk_kayit:%d.%m.%Y}" if ilk_kayit else "Henüz hiç agent çağrısı yok."
        )
        return

    st.dataframe(
        pd.DataFrame([_call_row(r) for r in rows]),
        use_container_width=True,
        hide_index=True,
        height=420,
    )
    st.caption(
        f"Aralıkta **{kayit_sayisi}** çağrı var, **{len(rows)}** tanesi "
        f"gösteriliyor. Kayıtların hiçbiri silinmiyor; satır sayısını ya da "
        "tarih aralığını değiştirerek tamamına ulaşabilirsin."
    )


def _call_row(row: dict) -> dict:
    ts = row.get("created_at")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    maliyet = row.get("cost_usd")
    return {
        "Zaman": f"{as_utc(ts).astimezone(ISTANBUL):%d.%m %H:%M}" if ts else "—",
        # TOPLAYICI ADI TEK BAŞINA YETMİYORDU: "loan_rates_llm" hangi veriyi
        # çektiğini söylemiyor. Açıklama `trigger_source`tan üretiliyor.
        "Çekilen veri": llm_call_description(row.get("collector"), row.get("trigger_source")),
        "Toplayıcı": row.get("collector") or "—",
        "Model": row.get("model") or "—",
        "Durum": row.get("status") or "—",
        "Girdi": row.get("prompt_tokens") or 0,
        "Çıktı": row.get("completion_tokens") or 0,
        "Maliyet": f"${maliyet:.6f}" if maliyet is not None else "—",
        "Kurtarılan satır": row.get("rows_recovered") or 0,
        "Süre (ms)": row.get("duration_ms") or 0,
        "Hata": (row.get("error") or "")[:80],
    }
