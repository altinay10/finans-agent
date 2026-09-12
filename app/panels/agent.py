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
from llm import credentials, providers
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
    _render_silme_kodu()
    _render_key_form(kaynak)
    _render_saved_keys()
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
        gecmis = _son_sonuc(settings.LLM_MODEL)
        # KUTUNUN RENGİ ARTIK KANITA BAĞLI. Eskiden koşulsuz yeşildi ve
        # "kullanılıyor" diyordu; anahtar çalışmasa bile. Canlıda
        # `qwen-flash` her koşuda 403 alırken panel 18 saat boyunca yeşil
        # kaldı (inceleme, 2026-09-08) — sessiz arızanın görünmemesinin
        # sebebi tam olarak buydu.
        if gecmis and gecmis["status"] == "failed":
            st.error(
                f"**`.env`'deki anahtar KULLANILAMIYOR.** `{settings.LLM_MODEL}` "
                f"ile yapılan son çağrı başarısız oldu "
                f"({_ne_zaman(gecmis['created_at'])}):\n\n"
                f"> {(gecmis['error'] or 'hata metni kaydedilmemiş')[:300]}\n\n"
                "Zincir `.env` ile başlıyor, yani her planlı koşu önce bunu "
                "deneyip hata alıyor. Düzeltmenin iki yolu var: `.env` "
                "dosyasındaki `LLM_MODEL`/`LLM_API_KEY` değerlerini "
                "geçerli bir sağlayıcıyla değiştirip konteyneri yeniden "
                "başlatmak, ya da aşağıdan çalışan bir anahtar kaydetmek."
                + (
                    f" Şu an **{yedek_sayisi} yedek anahtar** var; kimlik "
                    "hatasında (401/403/429) otomatik olarak onlara düşülür."
                    if yedek_sayisi else
                    " Şu an yedek anahtar YOK, yani agent hiç çalışmıyor."
                )
            )
        else:
            durum_notu = ""
            if gecmis:
                durum_notu = (
                    f" Son çağrı **başarılı** ({_ne_zaman(gecmis['created_at'])})."
                )
            else:
                # "Henüz denenmedi" ile "çalışıyor" aynı şey değil; ikisini
                # aynı cümleyle anlatmak yeni kurulumda yanlış güven verir.
                durum_notu = (
                    " Bu modelle henüz gerçek bir çağrı yapılmadı — aşağıdaki "
                    "**Zinciri sına** ile şimdi doğrulayabilirsin."
                )
            st.success(
                f"**`.env`'deki anahtar** kullanılıyor (model `{settings.LLM_MODEL}`). "
                "Panelden kaydedilen bir anahtar varsa bile `.env` ÖNCELİKLİDİR — "
                "sunucu sahibinin doğrudan yapılandırdığı anahtar, panelden hiç "
                f"dokunulmamıştır.{yedek_notu}{durum_notu}"
            )
        _render_chain_test()
    elif kaynak == "kayıtlı":
        aktif = credentials.active()
        st.success(
            f"Panelden kaydedilen anahtar kullanılıyor (**{aktif.masked}**, "
            f"model `{aktif.model}`) — `.env`'de anahtar tanımlı değil. "
            "Veritabanında tutuluyor, yani **zamanlayıcı da aynı anahtarı "
            "görüyor** ve konteyner yeniden kurulunca kaybolmuyor."
        )
        _render_chain_test()
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

#: Giriş formunun oturum durumu anahtarları. Kutular `key=` ile
#: bağlandığı için değerleri burada yaşıyor: form gönderildikten sonra da
#: yerinde kalmalarının (ve yalnızca BAŞARIDA temizlenmelerinin) yolu bu.
#: En son üretilen silme kodu — kaydettikten sonraki yeniden
#: çalıştırmada bir kez gösterilip düşürülüyor.
SON_SILME_KODU = "agent_son_silme_kodu"

PROVIDER_KEY = "agent_saglayici"
FORM_KEY = "agent_form_anahtar"
FORM_BASE_URL = "agent_form_base_url"
FORM_MODEL = "agent_form_model"
FORM_EXTRA = "agent_form_extra"
FORM_PRICE_IN = "agent_form_fiyat_girdi"
FORM_PRICE_OUT = "agent_form_fiyat_cikti"

#: Fiyat kutularının varsayılanı (USD / 1M token). Kullanıcı isteği
#: (2026-09-12): boş bırakmak yerine makul bir başlangıç göster, böylece
#: maliyet sütunu "bilinmiyor" olarak kalmasın.
VARSAYILAN_GIRDI_FIYAT = 1.0
VARSAYILAN_CIKTI_FIYAT = 5.0


def _render_silme_kodu() -> None:
    """Son kaydedilen anahtarın silme kodunu BİR KEZ gösterir.

    Sunucuda yalnızca kodun özeti duruyor (bkz. llm/credentials.py), yani
    bu kutu bir daha çizilmezse kod panelden geri okunamaz. Kullanıcı
    "Gördüm" deyip kapatana kadar ekranda kalıyor — yeniden çalıştırmalar
    arasında kaybolsaydı, kod da kaybolurdu.
    """
    kayit = st.session_state.get(SON_SILME_KODU)
    if not kayit:
        return
    _kimlik, kod = kayit
    st.warning(
        f"**Bu anahtarın silme kodu: `{kod}`**\n\n"
        "Bir yere kaydet. Bu kodu bilen dışında kimse bu anahtarı silemez — "
        "sunucuda yalnızca kodun özeti tutuluyor, panelden geri okunamaz. "
        "Kaybedersen sunucudaki `data/llm_credentials.json` dosyasından "
        "okuyabilirsin."
    )
    if st.button("Gördüm, kapat", key="silme_kodu_kapat"):
        st.session_state.pop(SON_SILME_KODU, None)
        st.rerun()


def _varsayilan_saglayici() -> str:
    """Form ilk açıldığında seçili gelecek sağlayıcı.

    O an geçerli olan taban URL'ye bakılıyor: kullanıcı zaten Gemini
    kullanıyorsa listeyi Gemini'de açmak, her seferinde yeniden seçmesini
    engelliyor.
    """
    return providers.guess_label(settings.current_base_url())


def _saglayici_degisti() -> None:
    """Sağlayıcı seçilince kutuları o sağlayıcının değerleriyle doldurur.

    EK GÖVDE DE SIFIRLANIYOR ve asıl düzeltme bu: kutu daha önce o an
    geçerli olan ayardan dolduruluyordu, yani `.env`'de Qwen'e ait
    `{"enable_thinking": false}` varken Gemini anahtarı denemek isteyen
    kullanıcı o alanı farkında olmadan Gemini'ye gönderiyor ve "anahtar
    çalışmadı" hatası alıyordu (kullanıcı bildirimi, 2026-09-12).

    'Diğer / elle' seçilirse kutulara dokunulmuyor: kullanıcı kendi
    yazdıklarını kaybetmesin.
    """
    onayar = providers.by_label(st.session_state.get(PROVIDER_KEY, providers.CUSTOM))
    if onayar.label == providers.CUSTOM:
        return
    st.session_state[FORM_BASE_URL] = onayar.base_url
    st.session_state[FORM_MODEL] = onayar.model
    st.session_state[FORM_EXTRA] = json.dumps(onayar.extra_body) if onayar.extra_body else ""


def _form_alanlarini_hazirla() -> None:
    """Kutuların oturum durumundaki ilk değerlerini kurar (bir kez)."""
    if PROVIDER_KEY not in st.session_state:
        st.session_state[PROVIDER_KEY] = _varsayilan_saglayici()
    varsayilanlar = {
        FORM_KEY: "",
        FORM_BASE_URL: settings.current_base_url(),
        FORM_MODEL: settings.current_model(),
        FORM_EXTRA: (
            json.dumps(settings.current_extra_body())
            if settings.current_extra_body() else ""
        ),
    }
    for ad, deger in varsayilanlar.items():
        st.session_state.setdefault(ad, deger)

    # Fiyat: kayıtlı değer varsa O, yoksa varsayılan. Kayıtlıyı ezmek,
    # bilinçli girilmiş bir fiyatı sessizce geri alırdı.
    kayitli_girdi, kayitli_cikti = app_settings.price_rates()
    st.session_state.setdefault(
        FORM_PRICE_IN, VARSAYILAN_GIRDI_FIYAT if kayitli_girdi is None else kayitli_girdi
    )
    st.session_state.setdefault(
        FORM_PRICE_OUT, VARSAYILAN_CIKTI_FIYAT if kayitli_cikti is None else kayitli_cikti
    )


def _formu_temizle() -> None:
    """Kutuları BAŞARILI kayıttan sonra temizler.

    Yalnızca anahtar siliniyor; taban URL, model ve ek gövde duruyor çünkü
    kullanıcı büyük olasılıkla aynı sağlayıcıyla devam edecek. Fiyat da
    duruyor: az önce kaydedilen değerin kutuda kalması doğru geri bildirim.
    """
    st.session_state[FORM_KEY] = ""


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
    _form_alanlarini_hazirla()

    # SAĞLAYICI SEÇİMİ FORMUN DIŞINDA: form içindeki bir seçim ancak
    # gönderildiğinde okunur, yani seçtiğin anda diğer kutuları dolduramaz.
    # Dışarıda olunca seçim anında yeniden çalıştırma tetikliyor ve kutular
    # o sağlayıcının değerleriyle geliyor.
    secili = st.selectbox(
        "Sağlayıcı",
        providers.LABELS,
        key=PROVIDER_KEY,
        on_change=_saglayici_degisti,
        help="Seçince taban URL, örnek model ve ek gövde alanı o "
             "sağlayıcıya göre doldurulur. Hepsi düzenlenebilir.",
    )
    onayar = providers.by_label(secili)
    if onayar.note:
        st.caption(onayar.note)

    # clear_on_submit=False — BU BİR HATA DÜZELTMESİ. Form her gönderimde
    # temizleniyordu; anahtar reddedildiğinde kullanıcı taban URL'yi,
    # modeli ve ek gövdeyi BAŞTAN yazmak zorunda kalıyordu, oysa
    # düzeltilecek tek şey genelde bir karakterdi. Artık yazılanlar yerinde
    # kalıyor; kutular yalnızca BAŞARILI kayıttan sonra temizleniyor
    # (bkz. aşağıdaki `_formu_temizle`).
    with st.form("agent_key", clear_on_submit=False):
        girilen = st.text_input(
            "API anahtarı",
            type="password",
            key=FORM_KEY,
            placeholder="AIzaSy… veya sağlayıcının verdiği anahtar",
            help="Yazdığın anahtar ekranda görünmez ve hiçbir yere gönderilmez; "
                 "yalnızca seçtiğin LLM sağlayıcısına gider.",
        )
        c1, c2 = st.columns(2)
        base_url = c1.text_input(
            "Taban URL (OpenAI uyumlu uç nokta)",
            key=FORM_BASE_URL,
            help="Sağlayıcıyı bu belirler. Gemini, OpenAI, Claude, Qwen, "
                 "DeepSeek ve yerel Ollama'nın hepsi OpenAI uyumlu uç nokta veriyor.",
        )
        model = c2.text_input(
            "Model",
            key=FORM_MODEL,
            help="Model herhangi biri olabilir; sağlayıcının verdiği adı yaz.",
        )
        extra_ham = st.text_input(
            "Ek gövde alanları (isteğe bağlı, JSON)",
            key=FORM_EXTRA,
            placeholder="{}",
            help="Sağlayıcıya özel, OpenAI şemasında olmayan alanlar. "
                 "ÇOĞU SAĞLAYICIDA BOŞ OLMALI — listedeki sağlayıcılar arasında "
                 "yalnızca Qwen bir alan istiyor. Buraya Qwen'e özel alanı "
                 "bırakıp Gemini anahtarı denemek, anahtar doğru olsa bile "
                 "isteğin reddedilmesine yol açar.",
        )

        # FİYAT ARTIK BURADA. Ayrı bir "Birim fiyat" bölümü vardı ve
        # kullanıcı anahtarı girdikten sonra onu ayrıca açıp doldurmak
        # zorundaydı; doldurmayınca maliyet "bilinmiyor" kalıyordu. Fiyat
        # anahtarın sağlayıcısına ait bir bilgi, o yüzden anahtarla birlikte
        # soruluyor (kullanıcı isteği, 2026-09-12).
        f1, f2 = st.columns(2)
        girdi_fiyat = f1.number_input(
            "Girdi fiyatı (USD / 1M token)",
            min_value=0.0, step=0.10, format="%.4f", key=FORM_PRICE_IN,
            help="Sağlayıcının fiyat sayfasındaki değer. 0 yazmak 'bedava' "
                 "demektir ve ücretsiz katmanda bu doğrudur.",
        )
        cikti_fiyat = f2.number_input(
            "Çıktı fiyatı (USD / 1M token)",
            min_value=0.0, step=0.10, format="%.4f", key=FORM_PRICE_OUT,
        )

        b1, b2 = st.columns(2)
        oturumluk = b1.form_submit_button(
            "Yalnızca bu oturumda kullan", width="stretch"
        )
        kalici = b2.form_submit_button(
            "Sürekli kullanmak için kaydet", type="primary", width="stretch"
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
        cred, silme_kodu = credentials.save(
            anahtar, base_url=base_url, model=model, extra_body=extra_body
        )
        # Fiyat anahtarla birlikte kaydediliyor (bkz. formdaki not).
        app_settings.set_price_rates(float(girdi_fiyat), float(cikti_fiyat))
        # SİLME KODU BİR KEZ GÖSTERİLİYOR. Sunucuda yalnızca özeti var,
        # yani bu kutu kapandıktan sonra kodu hiçbir yerden geri okunamaz
        # (kurtarma yolu: data/llm_credentials.json).
        st.session_state[SON_SILME_KODU] = (cred.id, silme_kodu)
        _formu_temizle()
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
        app_settings.set_price_rates(float(girdi_fiyat), float(cikti_fiyat))
        _formu_temizle()
        st.success(
            "Bu oturum için ayarlandı. Diske yazılmadı, başka bir oturum "
            "göremez, zamanlayıcı kullanmaz. **Agent'ı çalıştır** düğmesi "
            "artık çalışıyor."
        )

    st.rerun()


#: Zincir sınamasının sonucu — düğmeye basıldıktan sonraki yeniden
#: çalıştırmalarda da ekranda kalsın diye oturum durumunda tutuluyor.
CHAIN_TEST_KEY = "zincir_sinama"


@st.cache_data(ttl=60)
def _son_sonuc(model: str) -> dict | None:
    return queries.llm_last_outcome(model)


def _ne_zaman(ts: datetime | None) -> str:
    """'3,2 saat önce' — kutu metnine sığacak kadar kısa."""
    if ts is None:
        return "zamanı bilinmiyor"
    fark = (datetime.now(timezone.utc) - as_utc(ts)).total_seconds()
    if fark < 3600:
        return f"{fark / 60:.0f} dk önce"
    if fark < 86400:
        return f"{fark / 3600:.1f} saat önce"
    return f"{fark / 86400:.1f} gün önce"


def _render_chain_test() -> None:
    """Zincirdeki HER anahtarı tek tek sınar ve sonucu gösterir.

    NEDEN AÇILIŞTA DEĞİL, DÜĞMEYLE: açılışta koşan bir sınama, paneli her
    açan ziyaretçi için sağlayıcıya istek atardı — panelin sunucunun
    anahtarını harcamaması kuralına (bkz. store/llm_backoff.py) arkadan
    dolaşmak olurdu. Düğme, sınamayı kullanıcının bilinçli kararı yapıyor.

    NEDEN GEREKLİ: `llm_calls` geçmişi yalnızca DAHA ÖNCE denenmiş modeller
    hakkında konuşabiliyor. Yeni kurulumda, ya da `.env` az önce
    değiştirildiğinde geçmiş yok ve "çalışıyor mu" sorusunun tek dürüst
    cevabı canlı bir deneme.

    ZİNCİRİN TAMAMI sınanıyor, yalnızca aktif anahtar değil: asıl soru
    "anahtarım çalışıyor mu" değil, "`.env` düşerse arkasında çalışan bir
    şey var mı". Bu, canlıda 18 saat cevapsız kalan soruydu.
    """
    with st.expander("Zinciri sına — her anahtara birer küçük istek", expanded=False):
        st.caption(
            "Her anahtara `max_tokens=1` ile tek bir *ping* atılır; amaç "
            "yanıtın içeriği değil, sağlayıcının anahtarı kabul edip "
            "etmediği. Maliyeti ihmal edilebilir ama **sıfır değildir**, "
            "bu yüzden kendiliğinden çalışmaz."
        )
        if st.button("Zinciri sına", key="zinciri_sina"):
            zincir = credentials.effective_chain()
            sonuclar = []
            for cred in zincir:
                kaynak_adi = (
                    "`.env`" if cred.id == credentials.ENV_CREDENTIAL_ID
                    else f"kayıtlı #{cred.id}"
                )
                ok, hata = credentials.test_credential(
                    cred.api_key,
                    base_url=cred.base_url,
                    model=cred.model,
                    extra_body=cred.extra_body,
                )
                sonuclar.append(
                    {"kaynak": kaynak_adi, "model": cred.model,
                     "anahtar": cred.masked, "ok": ok, "hata": hata}
                )
            st.session_state[CHAIN_TEST_KEY] = sonuclar

        sonuclar = st.session_state.get(CHAIN_TEST_KEY)
        if not sonuclar:
            return
        for s in sonuclar:
            if s["ok"]:
                st.success(f"🟢 {s['kaynak']} · `{s['model']}` · {s['anahtar']} — çalışıyor")
            else:
                st.error(
                    f"🔴 {s['kaynak']} · `{s['model']}` · {s['anahtar']}\n\n"
                    f"> {(s['hata'] or 'hata metni yok')[:300]}"
                )
        if not any(s["ok"] for s in sonuclar):
            st.warning(
                "**Zincirde çalışan anahtar yok** — agent'a bağlı banka "
                "sayfaları hiç güncellenmiyor. Panelin geri kalanı (döviz, "
                "mevduat, fon ve API'si olan bankaların kredi oranları) "
                "bundan etkilenmez."
            )


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
        st.caption(
            "🔒 **Bir anahtarı yalnızca onu ekleyen kişi silebilir.** Silmek "
            "için kaydederken gösterilen kod gerekiyor; sunucuda o kodun "
            "yalnızca özeti duruyor. Kodunu kaybettiysen sunucudaki "
            "`data/llm_credentials.json` dosyasından okunabilir."
        )
        for index, cred in enumerate(kayitlilar):
            c1, c2, c3 = st.columns([3, 3, 2])
            if cred.status == "failed":
                etiket = "🔴 düştü"
            elif index == 0 and not env_oncelikli:
                etiket = "🟢 kullanılıyor"
            else:
                etiket = "⚪ yedek"
            c1.write(f"{etiket} · **{cred.masked}**" + (" 🔒" if cred.korumali else ""))
            c2.write(f"`{cred.model}`")
            c3.caption(cred.base_url.replace("https://", "")[:28])
            _render_delete(cred)
            if cred.status == "failed" and cred.last_error:
                st.caption(f"↳ {cred.last_error[:160]}")


def _render_delete(cred) -> None:
    """Tek bir anahtarın silme denetimi.

    KODU BİLEN AYNI OTURUMDA İKİ KEZ YAZMASIN: kaydeden kişinin kodu
    oturum durumunda duruyor ve kutuya önceden dolduruluyor. Başka bir
    tarayıcıdan gelen biri boş kutu görür ve kodu bilmiyorsa silemez —
    korumanın bütün noktası bu.
    """
    if not cred.korumali:
        # Bu koruma eklenmeden ÖNCE kaydedilmiş satır: sahibi bilinmiyor,
        # kod sorulamaz. Aksi halde kimse silemez ve panelde kalıcı olarak
        # takılı kalırdı.
        if st.button("Sil", key=f"sil_{cred.id}"):
            credentials.delete(cred.id)
            st.rerun()
        return

    d1, d2 = st.columns([3, 1])
    kayitli = st.session_state.get(SON_SILME_KODU)
    onceden = kayitli[1] if kayitli and kayitli[0] == cred.id else ""
    kod = d1.text_input(
        "Silme kodu",
        value=onceden,
        key=f"kod_{cred.id}",
        label_visibility="collapsed",
        placeholder="silme kodu",
    )
    if d2.button("Sil", key=f"sil_{cred.id}"):
        try:
            credentials.delete(cred.id, kod)
        except credentials.NotAuthorized as exc:
            st.error(f"{exc}")
            return
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
        width="stretch",
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
