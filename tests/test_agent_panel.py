"""Panelden API anahtarı girme ve anında tazeleme.

Bu özelliğin tek amacı şu: anahtarı biten ya da hiç anahtarı olmayan bir
kullanıcı, sunucuya SSH'lamadan sistemi çalışır halde tutabilsin. Buradaki
testler o zincirin sessizce kopabileceği üç yeri kilitliyor — önbelleğe
takılan istemci, `.env`'i ezmeyen `load_dotenv`, ve ekrana düşen anahtar.
"""
from __future__ import annotations

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

def test_session_key_wins_over_the_file(monkeypatch, tmp_path):
    """Sıralama önemli: önce dosya, sonra oturumluk anahtar.

    Ters sırada, dosyadaki eski anahtar kullanıcının az önce girdiğini
    sessizce geri alırdı.
    """
    from app.panels import agent

    monkeypatch.setattr(agent.settings, "reload_from_env", lambda *a: settings.apply(
        LLM_API_KEY="dosyadaki", LLM_FALLBACK_ENABLED=False))
    monkeypatch.setattr(agent.st, "session_state", {agent.SESSION_KEY: "oturumdaki"})

    assert agent.apply_session_key() == "oturum"
    assert settings.LLM_API_KEY == "oturumdaki"
    assert settings.LLM_FALLBACK_ENABLED is True


def test_source_is_reported_as_none_when_there_is_no_key(monkeypatch):
    from app.panels import agent

    monkeypatch.setattr(agent.settings, "reload_from_env", lambda *a: settings.apply(
        LLM_API_KEY=""))
    monkeypatch.setattr(agent.st, "session_state", {})
    assert agent.apply_session_key() == "yok"


# ----------------------------------------------------- testler .env'siz ----

def test_the_test_suite_never_inherits_a_live_key():
    """Bu gerçekten yaşandı: `.env`'e çalışan bir anahtar konunca test paketi
    Google'a GERÇEK bir istek attı ve token harcadı. Testin sonucu makinede
    hangi dosyanın durduğuna göre değişiyordu (bkz. conftest._llm_kapali).
    """
    assert settings.LLM_FALLBACK_ENABLED is False
    assert settings.LLM_API_KEY == ""
