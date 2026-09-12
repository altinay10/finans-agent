"""Panelden API anahtarı girme ve anında tazeleme.

Bu özelliğin tek amacı şu: anahtarı biten ya da hiç anahtarı olmayan bir
kullanıcı, sunucuya SSH'lamadan sistemi çalışır halde tutabilsin. Buradaki
testler o zincirin sessizce kopabileceği üç yeri kilitliyor — önbelleğe
takılan istemci, `.env`'i ezmeyen `load_dotenv`, ve ekrana düşen anahtar.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import llm.settings as settings
from config import env_file


# ------------------------------------------------ çalışma anında değişim ----

def test_new_key_takes_effect_without_restarting_the_process(monkeypatch):
    """Ayarlar süreç açılırken bir kez okunuyordu.

    Tüketicilerin hepsi `settings.X` diye modül üzerinden okuduğu için
    globalleri tazelemek yetiyor; bir yerde değeri kopyalayan
    `from llm.settings import LLM_API_KEY` olsaydı bu sessizce çalışmazdı.
    """
    settings.apply(LLM_API_KEY="anahtar-bir")
    assert settings.LLM_API_KEY == "anahtar-bir"
    settings.apply(LLM_API_KEY="anahtar-iki")
    assert settings.LLM_API_KEY == "anahtar-iki"


def test_changing_the_key_drops_the_cached_client(monkeypatch):
    """Sağlayıcı istemcisi lru_cache'li.

    Önbelleği temizlememek, yeni anahtarın SESSİZCE yok sayılmasına ve
    kullanıcının "girdim ama olmadı" demesine yol açardı — panelden anahtar
    girme özelliğinin tamamını işe yaramaz hale getiren tek hata bu.
    """
    from llm.provider import get_client

    settings.apply(LLM_API_KEY="birinci-anahtar")
    ilk = get_client()
    settings.apply(LLM_API_KEY="ikinci-anahtar")
    ikinci = get_client()

    assert ilk is not ikinci
    assert ikinci.api_key == "ikinci-anahtar"


def test_unknown_setting_is_refused():
    """Yazım hatası olan bir ad sessizce yeni bir global yaratırdı."""
    with pytest.raises(KeyError):
        settings.apply(LLM_APIKEY="x")


def test_reload_overrides_an_already_loaded_value(tmp_path, monkeypatch):
    """`load_dotenv` varsayılan olarak TANIMLI değişkeni EZMEZ.

    override=True olmasaydı, açılışta okunan eski anahtar dosyadaki yenisini
    bastırır ve panelden kaydedilen anahtar hiç devreye girmezdi.
    """
    monkeypatch.setenv("LLM_API_KEY", "acilistaki-eski")
    dosya = tmp_path / ".env"
    dosya.write_text("LLM_API_KEY=dosyadaki-yeni\n", encoding="utf-8")

    settings.reload_from_env(str(dosya))
    assert settings.LLM_API_KEY == "dosyadaki-yeni"


# ------------------------------------------------------ anahtar sızmasın ----

def test_key_is_never_shown_in_full():
    """Panel sunucuda açık duruyor olabilir.

    Ekranda duran bir anahtar, .env'i korumanın bütün anlamını götürür.
    """
    gizli = "AIzaSyC-cok-gizli-bir-anahtar-9f3b"
    maskeli = settings.masked_key(gizli)

    assert gizli not in maskeli
    assert maskeli == "…9f3b"
    assert settings.masked_key("") == "yok"
    assert settings.masked_key("kisa") == "…"


# --------------------------------------------------------- .env yazımı ----

def test_writing_the_key_keeps_every_other_setting(tmp_path):
    """`.env` yalnızca anahtarı değil DB_URL'i ve saklama sürelerini de taşıyor.

    Dosyayı baştan üretmek, kullanıcının elle yazdığı her şeyi sessizce
    silerdi.
    """
    dosya = tmp_path / ".env"
    dosya.write_text(
        "# elle yazılmış yorum\n"
        "DB_URL=postgresql://x\n"
        "LLM_API_KEY=eski\n"
        "RETAIN_HTTP_REQUEST_DAYS=7\n",
        encoding="utf-8",
    )

    env_file.set_values({"LLM_API_KEY": "yeni"}, dosya)
    icerik = dosya.read_text(encoding="utf-8")

    assert "LLM_API_KEY=yeni" in icerik
    assert "eski" not in icerik
    assert "# elle yazılmış yorum" in icerik
    assert "DB_URL=postgresql://x" in icerik
    assert "RETAIN_HTTP_REQUEST_DAYS=7" in icerik


def test_a_setting_that_is_not_in_the_file_is_appended(tmp_path):
    dosya = tmp_path / ".env"
    dosya.write_text("DB_URL=\n", encoding="utf-8")
    env_file.set_values({"LLM_FALLBACK_ENABLED": "1"}, dosya)
    assert env_file.read_values(dosya)["LLM_FALLBACK_ENABLED"] == "1"


def test_a_newline_in_the_value_is_refused(tmp_path):
    """Kaçan bir satır sonu dosyayı ikiye böler ve sonraki satırı yeni bir
    ayar gibi gösterirdi."""
    dosya = tmp_path / ".env"
    with pytest.raises(ValueError):
        env_file.set_values({"LLM_API_KEY": "abc\nDB_URL=kotu"}, dosya)


def test_a_new_env_file_is_not_world_readable(tmp_path):
    """Sunucuda `.env` gerçek bir sır taşıyor; varsayılan umask 0644 bırakır."""
    import stat

    dosya = tmp_path / ".env"
    env_file.set_values({"LLM_API_KEY": "x"}, dosya)
    mod = stat.S_IMODE(dosya.stat().st_mode)
    assert mod == 0o600, oct(mod)


# ------------------------------------------------------------- panel ----

def test_session_key_never_leaks_into_process_wide_settings(monkeypatch):
    """Ziyaretçinin anahtarı süreç geneline YAZILMAMALI.

    Eskiden yazılıyordu ve iki ayrı hata üretiyordu: zamanlayıcının planlı
    koşuları ziyaretçinin anahtarını kullanırdı, ve Streamlit tüm tarayıcı
    oturumlarını aynı süreçte koşturduğu için iki ziyaretçi birbirinin
    anahtarını görebilirdi.
    """
    from app.panels import agent

    monkeypatch.setattr(agent.settings, "reload_from_env", lambda *a: None)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sunucunun-anahtari")
    monkeypatch.setattr(agent.st, "session_state", {agent.SESSION_KEY: "ziyaretci"})

    assert agent.key_source() == "oturum"
    assert settings.LLM_API_KEY == "sunucunun-anahtari"
    assert settings.current_api_key() == "sunucunun-anahtari"


def test_source_is_reported_as_none_when_there_is_no_key(monkeypatch):
    from app.panels import agent

    monkeypatch.setattr(agent.settings, "reload_from_env", lambda *a: None)
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(agent.st, "session_state", {})
    assert agent.key_source() == "yok"


# ------------------------------------------ sunucunun anahtarı korunuyor ----

def test_manual_run_is_refused_without_the_users_own_key(monkeypatch):
    """Sunucudaki anahtar panelden HARCANAMAZ.

    `.env`'deki anahtar panel sahibinindir ve planlı koşular içindir. Paneli
    açan herkesin bir düğmeye basarak — üstelik sınırsız tekrarla — onu
    harcayabilmesi doğrudan bir fatura açığıdır.
    """
    from app.panels import agent

    monkeypatch.setattr(settings, "LLM_API_KEY", "sunucunun-anahtari")
    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", True)

    kosuldu = {"n": 0}
    monkeypatch.setattr(
        "collectors.loan_rates_llm.LlmLoanRateCollector",
        lambda *a, **k: kosuldu.__setitem__("n", 1),
    )

    for bos in (None, "", "   "):
        with pytest.raises(agent.AnahtarGerekli):
            agent.run_agent(bos)
    assert kosuldu["n"] == 0, "anahtarsız çağrıda toplayıcı hiç kurulmamalı"


def test_manual_run_uses_the_users_key_and_not_the_servers(monkeypatch):
    """Kullanıcının anahtarı YALNIZCA kendi koşusuna giriyor.

    `.env` anahtarına "düşmek" olmamalı: aksi halde geçersiz bir anahtar
    giren ziyaretçi, farkında olmadan sunucunun anahtarıyla koşardı.
    """
    from app.panels import agent

    monkeypatch.setattr(settings, "LLM_API_KEY", "sunucunun-anahtari")
    gorulen = {}

    class _Sahte:
        def run(self, trigger="manual"):
            gorulen["anahtar"] = settings.current_api_key()
            gorulen["izin"] = settings.fallback_enabled()
            return "bitti"

    monkeypatch.setattr("collectors.loan_rates_llm.LlmLoanRateCollector", _Sahte)

    assert agent.run_agent("kullanicinin-anahtari") == "bitti"
    assert gorulen["anahtar"] == "kullanicinin-anahtari"
    # Kullanıcı kendi anahtarıyla açıkça "çalıştır" dedi; LLM_FALLBACK_ENABLED
    # kazara token yakmaya karşı bir koruma ve bilerek basılan düğmenin önüne
    # konmasının anlamı yok.
    assert gorulen["izin"] is True


def test_the_context_key_is_released_after_the_run(monkeypatch):
    """Bağlam sızarsa, sonraki planlı koşu ziyaretçinin anahtarıyla giderdi."""
    monkeypatch.setattr(settings, "LLM_API_KEY", "sunucunun-anahtari")
    with settings.use_api_key("gecici"):
        assert settings.current_api_key() == "gecici"
    assert settings.current_api_key() == "sunucunun-anahtari"


def test_two_sessions_do_not_see_each_others_key(monkeypatch):
    """Streamlit her tarayıcı oturumunu AYNI SÜREÇTE ayrı iş parçacığında
    koşturuyor; modül globali kullanılsaydı biri diğerininkini görürdü."""
    import threading

    monkeypatch.setattr(settings, "LLM_API_KEY", "sunucunun-anahtari")
    gorulen: dict[str, str] = {}
    kapi = threading.Barrier(2)

    def _oturum(ad: str, anahtar: str) -> None:
        with settings.use_api_key(anahtar):
            kapi.wait()          # ikisi de bağlam AÇIKKEN buluşsun
            gorulen[ad] = settings.current_api_key()

    a = threading.Thread(target=_oturum, args=("a", "anahtar-A"))
    b = threading.Thread(target=_oturum, args=("b", "anahtar-B"))
    a.start(); b.start(); a.join(); b.join()

    assert gorulen == {"a": "anahtar-A", "b": "anahtar-B"}


def test_the_panel_no_longer_writes_the_key_to_env(monkeypatch):
    """Kalıcı anahtar `.env`'e DEĞİL veritabanına yazılır.

    `.env` `.dockerignore`'da olduğu için imaja hiç girmiyor; panel ile
    zamanlayıcı ayrı konteynerler ve panelin yazacağı dosyayı zamanlayıcı
    göremezdi. Üstelik `docker compose up --build` onu her yeniden
    kurulumda silerdi. Bu test o yolun geri gelmediğini bekçiliyor.
    """
    from app.panels import agent

    assert not hasattr(agent, "env_write_allowed")
    kaynak = Path(agent.__file__).read_text(encoding="utf-8")
    assert "env_file.set_values" not in kaynak


def test_the_test_suite_never_inherits_a_live_key():
    """Bu gerçekten yaşandı: `.env`'e çalışan bir anahtar konunca test paketi
    Google'a GERÇEK bir istek attı ve token harcadı. Testin sonucu makinede
    hangi dosyanın durduğuna göre değişiyordu (bkz. conftest._llm_kapali).
    """
    assert settings.LLM_FALLBACK_ENABLED is False
    assert settings.LLM_API_KEY == ""


def test_a_second_manual_run_is_not_silently_blocked(monkeypatch):
    """Panel süreci uzun ömürlü: çağrı sayacı koşular arasında birikiyordu.

    worker.py ve scheduler.py her koşudan önce sıfırlıyor, panel yapmıyordu.
    Sonuç: ilk koşu bütçeyi doldurup ikinci koşuyu sessizce hiçbir şey
    yapmayan bir "sınıra ulaşıldı"ya çeviriyordu.
    """
    from app.panels import agent
    from llm import extract as llm_extract

    class _Sahte:
        def run(self, trigger="manual"):
            return llm_extract.calls_made()

    monkeypatch.setattr("collectors.loan_rates_llm.LlmLoanRateCollector", _Sahte)

    llm_extract._calls_made.set(99)
    assert agent.run_agent("kullanicinin-anahtari") == 0


def test_two_sessions_do_not_share_the_call_budget():
    """Global sayaçla bir ziyaretçinin koşusu diğerininkini tüketirdi."""
    import threading

    from llm import extract as llm_extract

    gorulen: dict[str, int] = {}
    kapi = threading.Barrier(2)

    def _oturum(ad: str, kac: int) -> None:
        llm_extract.reset_budget()
        for _ in range(kac):
            llm_extract._calls_made.set(llm_extract.calls_made() + 1)
        kapi.wait()
        gorulen[ad] = llm_extract.calls_made()

    a = threading.Thread(target=_oturum, args=("a", 3))
    b = threading.Thread(target=_oturum, args=("b", 7))
    a.start(); b.start(); a.join(); b.join()

    assert gorulen == {"a": 3, "b": 7}


# ------------------------------------------- anahtar GERÇEKTEN çalışıyor mu ----

def _llm_call(model, status, error=None, minutes_ago=10):
    from datetime import datetime, timedelta, timezone

    from store.models import LlmCall

    return LlmCall(
        model=model, status=status, error=error, collector="loan_rates_llm",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(minutes=minutes_ago),
    )


def test_a_model_that_keeps_failing_is_visible_as_failing(db):
    """Agent kartının yeşil kutusu KANITA bakmalı.

    Canlıda `qwen-flash` her koşuda 403 "ModelAccessDenied" alırken panel
    18 saat boyunca "`.env`'deki anahtar kullanılıyor" diye yeşil kutu
    gösterdi (inceleme, 2026-09-08). Kutu hiçbir şeyi doğrulamıyordu;
    sessiz arızanın görünmemesinin sebebi buydu.
    """
    from store.db import SessionLocal
    from store.queries import llm_last_outcome

    with SessionLocal() as s:
        s.add_all([
            _llm_call("qwen-flash-2025-07-28", "ok", minutes_ago=5000),
            _llm_call("qwen-flash-2025-07-28", "failed",
                      error="Error code: 403 - Access to model denied", minutes_ago=30),
        ])
        s.commit()

    sonuc = llm_last_outcome("qwen-flash-2025-07-28")
    assert sonuc is not None
    assert sonuc["status"] == "failed"
    assert "403" in sonuc["error"]


def test_configuration_states_do_not_count_as_evidence(db):
    """'disabled' ve 'budget_exceeded' anahtar hakkında HİÇBİR ŞEY söylemez.

    İkisi de tek token harcamayan yapılandırma durumu (bkz.
    store/models.py::LlmCall). Bunları "son sonuç" sayan bir okuma, agent
    kapalıyken çalışan bir anahtarı bozuk, bozuk bir anahtarı da belirsiz
    gösterirdi.
    """
    from store.db import SessionLocal
    from store.queries import llm_last_outcome

    with SessionLocal() as s:
        s.add_all([
            _llm_call("gemini-3.5-flash-lite", "ok", minutes_ago=90),
            _llm_call("gemini-3.5-flash-lite", "disabled", minutes_ago=10),
            _llm_call("gemini-3.5-flash-lite", "budget_exceeded", minutes_ago=5),
        ])
        s.commit()

    sonuc = llm_last_outcome("gemini-3.5-flash-lite")
    assert sonuc["status"] == "ok", "yapılandırma durumları son GERÇEK çağrıyı gizlememeli"


def test_a_model_never_called_is_not_reported_as_working(db):
    """"Henüz denenmedi" ile "çalışıyor" aynı şey değil.

    Yeni kurulumda geçmiş yok; bunu "çalışıyor" saymak kullanıcıya
    doğrulanmamış bir güven verirdi. Panel bu durumda kullanıcıyı
    "Zinciri sına" düğmesine yönlendiriyor.
    """
    from store.queries import llm_last_outcome

    assert llm_last_outcome("hic-cagrilmamis-model") is None


# ------------------------------------------------- giriş formu davranışı ----

class _SahteDurum(dict):
    """`st.session_state` yerine geçen asgari sözlük."""

    def setdefault(self, k, v):  # dict.setdefault zaten var; açıklık için
        return super().setdefault(k, v)


@pytest.fixture
def form_durumu(monkeypatch, db):
    """Panelin oturum durumunu sahteyle değiştirir — Streamlit çalıştırmadan."""
    from app.panels import agent

    durum = _SahteDurum()
    monkeypatch.setattr(agent.st, "session_state", durum)
    return durum


def test_the_form_is_not_wiped_when_saving_fails(form_durumu):
    """Başarısız kayıttan sonra yazılanlar YERİNDE KALMALI.

    Form her gönderimde temizleniyordu; anahtar reddedildiğinde kullanıcı
    taban URL'yi, modeli ve ek gövdeyi baştan yazmak zorunda kalıyordu,
    oysa düzeltilecek tek şey genelde bir karakterdi (kullanıcı bildirimi,
    2026-09-12). Temizleme YALNIZCA başarıda çalışır.
    """
    from app.panels import agent

    form_durumu.update({
        agent.FORM_KEY: "yanlis-anahtar",
        agent.FORM_BASE_URL: "https://api.deepseek.com/v1",
        agent.FORM_MODEL: "deepseek-chat",
        agent.FORM_EXTRA: '{"a": 1}',
    })
    # Başarısızlık yolunda `_formu_temizle` ÇAĞRILMIYOR; çağrılsaydı bile
    # yalnızca anahtarı siler, sağlayıcı kutularına dokunmaz.
    agent._formu_temizle()

    assert form_durumu[agent.FORM_BASE_URL] == "https://api.deepseek.com/v1"
    assert form_durumu[agent.FORM_MODEL] == "deepseek-chat"
    assert form_durumu[agent.FORM_EXTRA] == '{"a": 1}'
    # Anahtar temizleniyor: kaydedilmiş bir anahtarın kutuda kalması gereksiz.
    assert form_durumu[agent.FORM_KEY] == ""


def test_choosing_a_provider_replaces_a_leftover_extra_body(form_durumu):
    """GEMINI SEÇİLİNCE QWEN'İN ALANI GİTMELİ — bildirilen hatanın tam kendisi.

    Kutu o an geçerli ayardan doldurulduğu için `.env`'deki Qwen alanı
    Gemini denemesine sızıyor ve istek reddediliyordu.
    """
    from app.panels import agent

    form_durumu[agent.FORM_EXTRA] = '{"enable_thinking": false}'
    form_durumu[agent.PROVIDER_KEY] = "Google Gemini"

    agent._saglayici_degisti()

    assert form_durumu[agent.FORM_EXTRA] == ""
    assert form_durumu[agent.FORM_BASE_URL].startswith("https://generativelanguage")
    assert form_durumu[agent.FORM_MODEL]


def test_choosing_qwen_fills_the_field_it_actually_needs(form_durumu):
    from app.panels import agent

    form_durumu[agent.PROVIDER_KEY] = "Qwen (Alibaba DashScope)"
    agent._saglayici_degisti()
    assert json.loads(form_durumu[agent.FORM_EXTRA]) == {"enable_thinking": False}


def test_custom_provider_keeps_what_the_user_typed(form_durumu):
    """'Diğer / elle' seçmek kullanıcının yazdıklarını silmemeli."""
    from app.panels import agent
    from llm import providers

    form_durumu.update({
        agent.FORM_BASE_URL: "http://localhost:11434/v1",
        agent.FORM_MODEL: "qwen3:8b",
        agent.FORM_EXTRA: '{"x": 1}',
        agent.PROVIDER_KEY: providers.CUSTOM,
    })
    agent._saglayici_degisti()

    assert form_durumu[agent.FORM_BASE_URL] == "http://localhost:11434/v1"
    assert form_durumu[agent.FORM_MODEL] == "qwen3:8b"
    assert form_durumu[agent.FORM_EXTRA] == '{"x": 1}'


def test_price_boxes_start_at_one_and_five(form_durumu):
    """Fiyat kutuları boş değil, makul bir varsayılanla açılmalı.

    Ayrı bir "Birim fiyat" bölümü vardı, kullanıcı onu açmayınca maliyet
    "bilinmiyor" kalıyordu (kullanıcı isteği, 2026-09-12).
    """
    from app.panels import agent

    agent._form_alanlarini_hazirla()
    assert form_durumu[agent.FORM_PRICE_IN] == 1.0
    assert form_durumu[agent.FORM_PRICE_OUT] == 5.0


def test_a_saved_price_is_not_clobbered_by_the_default(form_durumu):
    """Bilinçli girilmiş fiyat, varsayılanla sessizce geri alınmamalı."""
    from app.panels import agent
    from store import app_settings

    app_settings.set_price_rates(0.15, 0.60)
    agent._form_alanlarini_hazirla()

    assert form_durumu[agent.FORM_PRICE_IN] == 0.15
    assert form_durumu[agent.FORM_PRICE_OUT] == 0.60


def test_the_pricing_section_is_gone():
    """Ayrı "Birim fiyat" bölümü kaldırıldı; fiyat artık giriş formunda."""
    from app.panels import agent

    assert not hasattr(agent, "_render_pricing")
