"""Kalıcı anahtar zinciri, oturum sağlayıcısı ve fiyat kaydı."""
from __future__ import annotations

import llm.settings as settings
from llm import credentials
from store import app_settings


def _kaydet(anahtar: str, *, model: str = "test-model", base_url: str = "https://a.example/v1"):
    return credentials.save(anahtar, base_url=base_url, model=model)


# ------------------------------------------------------------- zincir ----


def test_the_newest_key_is_used(db):
    """Kullanıcı yeni anahtar giriyorsa eskisi bitmiş demektir."""
    _kaydet("eski-anahtar-1111")
    yeni = _kaydet("yeni-anahtar-2222")
    assert credentials.active().id == yeni.id
    assert settings.current_api_key() == "yeni-anahtar-2222"


def test_a_failed_key_falls_back_to_the_previous_one(db):
    """En yeni anahtar kullanılamazsa bir öncekine düşülür."""
    onceki = _kaydet("onceki-anahtar-1111")
    sonraki = _kaydet("sonraki-anahtar-2222")

    credentials.mark_failed(sonraki.id, "401 invalid api key")
    assert credentials.active().id == onceki.id
    assert settings.current_api_key() == "onceki-anahtar-1111"


def test_a_failed_key_goes_to_the_end_not_to_the_bin(db):
    """Kotası yenilenebilir; kalıcı olarak dışlamak sistemi yarım bırakırdı."""
    tek = _kaydet("tek-anahtar-1111")
    credentials.mark_failed(tek.id, "429 quota")
    zincir = credentials.chain()
    assert [c.id for c in zincir] == [tek.id]
    assert zincir[0].status == "failed"
    # Başka anahtar yokken yine de kullanılır — yarım kalmaktansa denemek.
    assert settings.current_api_key() == "tek-anahtar-1111"


def test_every_key_is_tried_at_most_once(db):
    """REGRESYON: 'sonraki' konuma göre seçilirse aday atlanıyordu.

    Bir anahtar 'failed' işaretlendiği anda zincirin SONUNA kayıyor.
    Konum tabanlı bir "sonraki" mantığı, üç anahtarlı zincirde ilki
    düştüğünde ikinciyi hiç denemeden "yedek yok" diyordu.
    """
    birinci = _kaydet("bir-1111")
    ikinci = _kaydet("iki-2222")
    ucuncu = _kaydet("uc-3333")

    denenen: set[int] = set()
    secilen = []
    for _ in range(3):
        aday = credentials.first_untried(denenen)
        assert aday is not None
        secilen.append(aday.id)
        denenen.add(aday.id)
        credentials.mark_failed(aday.id, "401")

    assert sorted(secilen) == sorted([birinci.id, ikinci.id, ucuncu.id])
    assert credentials.first_untried(denenen) is None


def test_only_credential_errors_trigger_a_fallback():
    """Zaman aşımı ve 500 sağlayıcının arızası — sağlam anahtarı suçlama."""

    class Sahte(Exception):
        def __init__(self, kod):
            super().__init__(f"hata {kod}")
            self.status_code = kod

    for kod in (401, 403, 429):
        assert credentials.is_credential_error(Sahte(kod)) is True, kod
    for kod in (500, 502, 503):
        assert credentials.is_credential_error(Sahte(kod)) is False, kod
    assert credentials.is_credential_error(TimeoutError("zaman aşımı")) is False


def test_the_key_is_never_returned_in_full(db):
    cred = _kaydet("cok-gizli-anahtar-9f3b")
    assert cred.masked == "…9f3b"
    assert "cok-gizli" not in cred.masked


# --------------------------------------------- oturum > veritabanı > env --


def test_a_session_key_wins_over_a_saved_one(db, monkeypatch):
    """Ziyaretçinin anahtarı yalnızca kendi koşusunu beslemeli."""
    _kaydet("kayitli-anahtar-1111")
    with settings.use_api_key("oturum-anahtari-2222"):
        assert settings.current_api_key() == "oturum-anahtari-2222"
    assert settings.current_api_key() == "kayitli-anahtar-1111"


def test_a_saved_key_carries_its_own_provider(db):
    """Anahtar sağlayıcısından ayrılamaz: aynı anahtar Gemini'de geçerli,
    Qwen'de değil."""
    _kaydet("anahtar-1111", model="qwen-flash", base_url="https://qwen.example/v1")
    assert settings.current_model() == "qwen-flash"
    assert settings.current_base_url() == "https://qwen.example/v1"


def test_the_session_provider_overrides_the_model(db):
    """REGRESYON: panelde girilen model hiçbir yere gitmiyordu.

    Eskiden yalnızca anahtar bağlama giriyordu; kullanıcı kendi
    sağlayıcısının anahtarını girdiğinde istek `.env`'deki uç noktaya
    gidiyor ve reddediliyordu — ekranda ise hâlâ eski model yazıyordu.
    """
    _kaydet("kayitli-1111", model="gemini-3.5-flash-lite")
    with settings.use_api_key(
        "oturum-2222", base_url="https://ollama.local/v1", model="qwen3:8b"
    ):
        assert settings.current_model() == "qwen3:8b"
        assert settings.current_base_url() == "https://ollama.local/v1"
    assert settings.current_model() == "gemini-3.5-flash-lite"


def test_a_saved_key_enables_scheduled_runs(db, monkeypatch):
    """Panelden test edilip kaydedilen anahtar planlı koşuları da açar.

    Bunu ayrıca `.env`'de bir bayrağa bağlamak, panelden kaydetmeyi
    işlevsiz bırakırdı: kullanıcı anahtarı test ettirip "sürekli kullan"
    diyor, niyeti açık.
    """
    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    assert settings.fallback_enabled() is False
    _kaydet("kayitli-1111")
    assert settings.fallback_enabled() is True


# -------------------------------------------------------------- fiyat ----


def test_price_is_read_from_the_database(db, monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICE_INPUT_PER_1M", None)
    monkeypatch.setattr(settings, "LLM_PRICE_OUTPUT_PER_1M", None)
    assert app_settings.price_rates() == (None, None)

    app_settings.set_price_rates(0.15, 0.60)
    assert app_settings.price_rates() == (0.15, 0.60)


def test_zero_price_is_kept_as_zero_not_unknown(db, monkeypatch):
    """Ücretsiz katmanda fiyat GERÇEKTEN sıfır; 'bilinmiyor' değil."""
    monkeypatch.setattr(settings, "LLM_PRICE_INPUT_PER_1M", None)
    monkeypatch.setattr(settings, "LLM_PRICE_OUTPUT_PER_1M", None)
    app_settings.set_price_rates(0.0, 0.0)
    assert app_settings.price_rates() == (0.0, 0.0)
    assert settings.estimate_cost_usd(1000, 500, 0.0, 0.0) == 0.0


def test_cost_is_unknown_when_no_price_is_set(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICE_INPUT_PER_1M", None)
    monkeypatch.setattr(settings, "LLM_PRICE_OUTPUT_PER_1M", None)
    assert settings.estimate_cost_usd(1000, 500) is None


def test_explicit_rates_beat_the_env_defaults(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICE_INPUT_PER_1M", 99.0)
    monkeypatch.setattr(settings, "LLM_PRICE_OUTPUT_PER_1M", 99.0)
    maliyet = settings.estimate_cost_usd(1_000_000, 1_000_000, 1.0, 2.0)
    assert maliyet == 3.0


# ------------------------------------------------------------ açıklama ----


def test_every_call_says_which_data_it_fetched():
    """Tabloda "loan_rates_llm" yazması yetmiyordu: aynı toplayıcı 12 ayrı
    banka sayfasına gidiyor ve her satır ayrı bir çağrı."""
    from app.panels.common import llm_call_description

    assert llm_call_description("loan_rates_llm", "halkbank") == "Halkbank ihtiyaç kredisi"
    assert llm_call_description("loan_rates_llm", "denizbank_tasit") == "DenizBank taşıt kredisi"
    assert llm_call_description("loan_rates_llm", "qnb_konut") == "QNB Bank konut kredisi"
    # Onarım yedeğinde kaynak yok: kırılan toplayıcının ne topladığı yazılır.
    assert "döviz" in llm_call_description("fx_banks", None).lower()
    assert "yedeği" in llm_call_description("fx_banks", None)


def test_institution_names_come_from_the_registry():
    """REGRESYON: elle tutulan kopya sözlükte 7 banka eksikti.

    DenizBank, ING, QNB, Odeabank, Fibabanka, Anadolubank ve Burgan
    panelde ham kod olarak ("DENIZBANK") görünüyordu.
    """
    from app.panels.common import institution_label

    for kod, ad in (
        ("DENIZBANK", "DenizBank"),
        ("ING", "ING Bank"),
        ("QNB", "QNB Bank"),
        ("BURGAN", "Burgan Bank"),
        ("ANADOLUBANK", "Anadolubank"),
    ):
        assert institution_label(kod) == ad, kod
