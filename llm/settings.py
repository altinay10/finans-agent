"""Ortam değişkenlerinden LLM ayarları — hepsi .env üzerinden takas edilir.

Varsayılan sağlayıcı Google Gemini'nin OpenAI-uyumlu uç noktasıdır; böylece
llm/provider.py'deki tek istemci hiç değişmeden çalışır.

TOKEN BÜTÇESİ — bu dosyadaki sınırlar kasıtlı olarak DÜŞÜK. Fallback nadir
bir kurtarma yolu; pahalı bir döngüye dönüşmemeli. Sınırların hepsi .env'den
büyütülebilir ama varsayılanlar "takılırsa token yakmasın" ilkesine göre
seçildi (kullanıcı isteği, 2026-08-24).
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Google AI Studio'nun OpenAI-uyumlu uç noktası.
LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
)
# gemini-2.5-flash-lite YENİ KULLANICILARA KAPATILDI: API 404 ile
# "no longer available to new users" diyor (2026-08-27'de canlı doğrulandı).
# Varsayılan onun yerine geçen en ucuz modele alındı.
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")

# --- token korumaları ------------------------------------------------------

# Fallback varsayılan olarak KAPALI. Anahtar girilse bile açıkça
# LLM_FALLBACK_ENABLED=1 denmedikçe tek token harcanmaz.
LLM_FALLBACK_ENABLED = os.environ.get("LLM_FALLBACK_ENABLED", "").strip() in {"1", "true", "yes"}

# Modele gönderilecek HTML'in üst sınırı. ~8k karakter kabaca 2-3k token.
LLM_MAX_INPUT_CHARS = _int("LLM_MAX_INPUT_CHARS", 8_000)

# Yanıt üst sınırı. Çıkarım işi kısa JSON üretir; bunu aşan bir yanıt
# zaten hatalıdır, kesilmesi doğrudur.
LLM_MAX_OUTPUT_TOKENS = _int("LLM_MAX_OUTPUT_TOKENS", 2_000)

# Tek bir worker koşusunda izin verilen TOPLAM çağrı sayısı. Bir toplayıcı
# tekrar tekrar kırılırsa (ör. banka sayfasını komple değiştirdi) fallback
# her koşuda bir kez denenir, sonra susar.
LLM_MAX_CALLS_PER_RUN = _int("LLM_MAX_CALLS_PER_RUN", 1)

# AGENT toplayıcısı (collectors/loan_rates_llm.py) için AYRI ve daha yüksek
# sınır. Onarım fallback'i nadir bir kurtarma yolu olduğu için 1 çağrıyla
# sınırlı; agent toplayıcısı ise BANKA BAŞINA bir çağrı yapar ve tek çağrıyla
# işini bitiremez. İkisini aynı sayaca bağlamak, agent'ı ilk bankadan sonra
# susturur ve sessizce eksik veri üretirdi.
# VARSAYILAN 8 DEĞİL 60: envanterde şu an 12 aktif agent sayfası var ve
# toplayıcı banka BAŞINA bir çağrı yapıyor. 8'lik sınır, `.env` vermeden
# kurulan her sistemde ilk sekiz bankayı çekip GERİSİNİ SESSİZCE atıyordu —
# panelde hata da görünmüyordu, çünkü koşu "başarılı" sayılıyor. Sınırın
# amacı kaçak token değil, kaçak DÖNGÜ engellemek; kaynak sayısının üstünde
# bir değer bu amacı bozmuyor.
LLM_AGENT_MAX_CALLS_PER_RUN = _int("LLM_AGENT_MAX_CALLS_PER_RUN", 60)

# İstek zaman aşımı — asılı kalan bir çağrı hem koşuyu hem faturayı bekletir.
LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 45)


def _json_obj(name: str, default: dict) -> dict:
    """.env'den JSON nesne okur; bozuksa varsayılana düşer ve UYARIR.

    Sessizce boş sözlüğe düşmek en kötüsü olurdu: `enable_thinking` gibi bir
    anahtar kaybolduğunda hiçbir hata görünmez, yalnızca fatura kabarır.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return dict(default)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        logger.warning("%s ayrıştırılamadı (%s) — varsayılan kullanılıyor", name, exc)
        return dict(default)
    if not isinstance(value, dict):
        logger.warning("%s bir JSON nesnesi olmalı — varsayılan kullanılıyor", name)
        return dict(default)
    return value


# SAĞLAYICIYA ÖZEL EK GÖVDE ALANLARI — OpenAI şemasında olmayan parametreler.
#
# NEDEN VAR: Qwen'in (Alibaba Model Studio) OpenAI-uyumlu uç noktasında
# DÜŞÜNME MODU VARSAYILAN OLARAK AÇIK ve çıkarım işinde saf israf.
# 2026-09-02'de ölçüldü — aynı önemsiz çıkarım isteği:
#     qwen3.7-plus  düşünme açık : 1445 çıktı tokeni (1376'sı akıl yürütme)
#     qwen3.7-plus  düşünme kapalı:   41 çıktı tokeni
# Otuz küsur banka × her koşu ile bu, faturanın tamamını yakan fark.
#
# Üstelik sessiz bir BOZULMA riski de var: akıl yürütme tokenleri
# `max_tokens` bütçesinden yeniyor, yani düşünme açıkken model JSON'u
# yazmaya sıra gelmeden kesilebiliyor ve çağrı "yanıt ayrıştırılamadı" diye
# başarısız oluyor — token yanmış, sonuç yok.
#
# Ayar genel tutuldu (sağlayıcıya özel bayrak yerine serbest JSON) çünkü bu
# modülün tamamı "sağlayıcı .env'den takas edilir" ilkesi üzerine kurulu;
# Gemini'ye dönülürse bu alan boşaltılır, kod değişmez.
LLM_EXTRA_BODY = _json_obj("LLM_EXTRA_BODY", {})


# --- maliyet tahmini -------------------------------------------------------
#
# Token sayısı tek başına "ne kadar harcadı" sorusunu cevaplamıyor. Birim
# fiyat .env'den okunur çünkü sağlayıcı fiyatları değişir ve kodun içine
# gömülü bir fiyat sessizce yanlışlanır. BOŞSA maliyet NULL kalır — uydurma
# bir sayı yazmaktansa "bilinmiyor" demek dürüst.
def _float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# USD / 1.000.000 token. flash-lite için AI Studio ücretsiz
# katmanında 0'dır; ücretli katmana geçilirse .env'den girilir.
LLM_PRICE_INPUT_PER_1M = _float("LLM_PRICE_INPUT_PER_1M", None)
LLM_PRICE_OUTPUT_PER_1M = _float("LLM_PRICE_OUTPUT_PER_1M", None)


def estimate_cost_usd(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    input_per_1m: float | None = None,
    output_per_1m: float | None = None,
) -> float | None:
    """Fiyat tanımlı değilse None — 'bilinmiyor' ile 'sıfır' karıştırılmasın.

    Birim fiyat artık panelden de girilebiliyor ve orası veritabanına
    yazıyor (bkz. store/app_settings.py). Fonksiyon SAF tutuldu: fiyatı
    kendisi aramıyor, çağıran veriyor; verilmezse `.env`'deki geçerli.
    Böylece hem eski `.env` kurulumları hem panel aynı hesabı kullanıyor.
    """
    girdi = LLM_PRICE_INPUT_PER_1M if input_per_1m is None else input_per_1m
    cikti = LLM_PRICE_OUTPUT_PER_1M if output_per_1m is None else output_per_1m
    if girdi is None and cikti is None:
        return None
    cost = 0.0
    if prompt_tokens and girdi is not None:
        cost += prompt_tokens / 1_000_000 * girdi
    if completion_tokens and cikti is not None:
        cost += completion_tokens / 1_000_000 * cikti
    return cost


# ---------------------------------------------------------------------------
# ÇALIŞMA ANINDA DEĞİŞTİRME — panelden anahtar girişi (kullanıcı isteği).
#
# Yukarıdaki sabitler süreç AÇILIRKEN bir kez hesaplanıyor. Anahtarı .env'e
# yazıp beklemek, "anlık yenileme" isteğini karşılamıyordu: panel yeni
# anahtarı görmüyor, zamanlayıcı ise ancak yeniden başlatılınca görüyordu.
#
# Tüm tüketiciler bu değerleri `settings.X` diye MODÜL ÜZERİNDEN okuyor
# (hiçbir yerde değeri kopyalayan `from llm.settings import LLM_API_KEY`
# yok) — bu yüzden modül globallerini tazelemek her yerde anında geçerli.
# ---------------------------------------------------------------------------

#: `apply()` ile değiştirilebilecek adlar. Beyaz liste bilinçli: yazım hatası
#: olan bir ad sessizce yeni bir global yaratıp hiçbir işe yaramazdı.
OVERRIDABLE = (
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_FALLBACK_ENABLED",
    "LLM_MAX_INPUT_CHARS",
    "LLM_MAX_OUTPUT_TOKENS",
    "LLM_MAX_CALLS_PER_RUN",
    "LLM_AGENT_MAX_CALLS_PER_RUN",
    "LLM_TIMEOUT_SECONDS",
    "LLM_EXTRA_BODY",
    "LLM_PRICE_INPUT_PER_1M",
    "LLM_PRICE_OUTPUT_PER_1M",
)


def _drop_client_cache() -> None:
    """Sağlayıcı istemcisi lru_cache'li.

    Anahtarı değiştirip önbelleği temizlememek, yeni anahtarın SESSİZCE yok
    sayılmasına ve kullanıcının "girdim ama olmadı" demesine yol açardı.
    İçeriden import: llm.provider zaten bu modülü import ediyor, döngüyü
    çağrı anına ertelemek gerekiyor.
    """
    from llm.provider import clear_client_cache

    clear_client_cache()


def apply(**overrides) -> None:
    """Süreç içinde ayar değiştirir (panelden girilen anahtar için)."""
    g = globals()
    for name, value in overrides.items():
        if name not in OVERRIDABLE:
            raise KeyError(f"değiştirilemez ayar: {name}")
        g[name] = value
    _drop_client_cache()


def reload_from_env(path: str | None = None) -> None:
    """.env'i yeniden okuyup sabitleri tazeler.

    `override=True` şart: `load_dotenv` varsayılan olarak ZATEN TANIMLI bir
    ortam değişkenini ezmez, yani ilk açılışta okunan eski anahtar dosyadaki
    yenisini bastırırdı — panelden kaydedilen anahtar hiç devreye girmezdi.
    """
    load_dotenv(path, override=True)
    apply(
        LLM_BASE_URL=os.environ.get(
            "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
        ),
        LLM_MODEL=os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite"),
        LLM_API_KEY=os.environ.get("LLM_API_KEY", ""),
        LLM_FALLBACK_ENABLED=os.environ.get("LLM_FALLBACK_ENABLED", "").strip()
        in {"1", "true", "yes"},
        LLM_MAX_INPUT_CHARS=_int("LLM_MAX_INPUT_CHARS", 8_000),
        LLM_MAX_OUTPUT_TOKENS=_int("LLM_MAX_OUTPUT_TOKENS", 2_000),
        LLM_MAX_CALLS_PER_RUN=_int("LLM_MAX_CALLS_PER_RUN", 1),
        LLM_AGENT_MAX_CALLS_PER_RUN=_int("LLM_AGENT_MAX_CALLS_PER_RUN", 60),
        LLM_TIMEOUT_SECONDS=_int("LLM_TIMEOUT_SECONDS", 45),
        LLM_EXTRA_BODY=_json_obj("LLM_EXTRA_BODY", {}),
        LLM_PRICE_INPUT_PER_1M=_float("LLM_PRICE_INPUT_PER_1M", None),
        LLM_PRICE_OUTPUT_PER_1M=_float("LLM_PRICE_OUTPUT_PER_1M", None),
    )


def masked_key(key: str | None = None) -> str:
    """Anahtarı ASLA tam göstermeme kuralı tek yerde.

    Panel sunucuda açık duruyor olabilir; ekranda duran bir anahtar, .env'i
    korumanın bütün anlamını götürür. Son dört hane "hangi anahtar takılı"
    sorusunu cevaplamaya yetiyor.
    """
    key = LLM_API_KEY if key is None else key
    if not key:
        return "yok"
    return f"…{key[-4:]}" if len(key) > 4 else "…"


# ---------------------------------------------------------------------------
# KOŞU BAŞINA ANAHTAR — panelden elle çalıştırma için.
#
# Yukarıdaki `LLM_API_KEY` süreç geneli: `.env`'den gelir ve PANEL SAHİBİNE
# aittir; planlı koşuları o besler. Paneli açan bir ziyaretçinin girdiği
# anahtar ise yalnızca kendi tetiklediği koşuyu beslemeli. İkisini aynı
# modül globaline yazmak İKİ ayrı hata üretiyordu:
#
#   * Ziyaretçinin anahtarı süreç geneline yazılınca zamanlayıcının planlı
#     koşuları da onu kullanırdı.
#   * Streamlit her tarayıcı oturumunu AYNI SÜREÇTE ayrı bir iş parçacığında
#     koşturuyor. Modül globali paylaşıldığı için iki ziyaretçi birbirinin
#     anahtarını görebilir, biri diğerininkiyle çağrı yapabilirdi.
#
# ContextVar iş parçacığı başına ayrı değer tutar; sızıntı kapanıyor.
# ---------------------------------------------------------------------------

_api_key_override: ContextVar[str | None] = ContextVar("llm_api_key_override", default=None)

# ANAHTARIN YANINDA SAĞLAYICISI DA GEÇERSİZ KILINABİLMELİ.
#
# Eskiden yalnızca anahtar bağlama giriyordu; panelde girilen "Model" alanı
# ise hiçbir yere gitmiyordu. Sonuç sessiz bir hataydı: kullanıcı kendi
# OpenAI anahtarını girip "çalıştır" dediğinde istek `.env`'deki Gemini uç
# noktasına gidiyor, anahtar oraya ait olmadığı için reddediliyordu — ve
# ekranda görünen model adı hâlâ Gemini'ninkiydi. Anahtar sağlayıcısından
# ayrılamaz: aynı anahtar Gemini'de geçerli, Qwen'de değil.
_base_url_override: ContextVar[str | None] = ContextVar("llm_base_url_override", default=None)
_model_override: ContextVar[str | None] = ContextVar("llm_model_override", default=None)
_extra_body_override: ContextVar[dict | None] = ContextVar("llm_extra_body_override", default=None)

#: Koşu boyunca kullanılan veritabanı kimliğinin id'si. Çağrı kimlik
#: hatasıyla düşerse hangi satırın 'failed' işaretleneceğini bu söyler.
_credential_id: ContextVar[int | None] = ContextVar("llm_credential_id", default=None)


def _db_credential():
    """Veritabanındaki en güncel kullanılabilir kimlik (yoksa None).

    Gecikmeli import: `llm` -> `store` bağını modül yüklenme zamanına
    taşımak döngüsel import üretir (store.observability zaten llm.settings
    okuyor).
    """
    try:
        from llm import credentials

        return credentials.active()
    except Exception as exc:  # noqa: BLE001 - veritabanı yoksa .env geçerli
        logger.debug("veritabanı kimliği okunamadı: %s", exc)
        return None


def current_api_key() -> str:
    """O anki bağlamda kullanılacak anahtar.

    SIRA: oturum (panelde girilen) -> veritabanı (kalıcı kaydedilen) ->
    `.env`. Oturum en üstte çünkü ziyaretçinin kendi anahtarı yalnızca
    kendi koşusunu beslemeli; `.env` en altta çünkü artık kalıcı kayıt
    veritabanında tutuluyor.
    """
    oturum = _api_key_override.get()
    if oturum:
        return oturum
    cred = _db_credential()
    if cred and cred.api_key:
        return cred.api_key
    return LLM_API_KEY


def current_base_url() -> str:
    oturum = _base_url_override.get()
    if oturum:
        return oturum
    if not _api_key_override.get():
        cred = _db_credential()
        if cred and cred.base_url:
            return cred.base_url
    return LLM_BASE_URL


def current_model() -> str:
    oturum = _model_override.get()
    if oturum:
        return oturum
    if not _api_key_override.get():
        cred = _db_credential()
        if cred and cred.model:
            return cred.model
    return LLM_MODEL


def current_extra_body() -> dict:
    oturum = _extra_body_override.get()
    if oturum is not None:
        return oturum
    if not _api_key_override.get():
        cred = _db_credential()
        if cred:
            return cred.extra_body
    return LLM_EXTRA_BODY


def current_credential_id() -> int | None:
    """Koşuda kullanılan veritabanı kimliğinin id'si (oturum anahtarında None)."""
    acik = _credential_id.get()
    if acik is not None:
        return acik
    if _api_key_override.get():
        return None
    cred = _db_credential()
    return cred.id if cred else None


def fallback_enabled() -> bool:
    """Agent çağrısına izin var mı.

    Bağlama bir anahtar konulmuşsa izin de vardır: kullanıcı kendi anahtarıyla
    açıkça "çalıştır" dedi. `LLM_FALLBACK_ENABLED` kazara token yakmaya karşı
    bir koruma; bilerek basılan bir düğmenin önüne konmasının anlamı yok.

    Panelden KALICI olarak kaydedilen bir anahtar da aynı anlama geliyor:
    kullanıcı anahtarı test ettirip "sürekli kullan" dedi. Bunu ayrıca
    `.env`'de bir bayrağa bağlamak, panelden kaydetmeyi işlevsiz bırakırdı.
    """
    if _api_key_override.get():
        return True
    if LLM_FALLBACK_ENABLED:
        return True
    return _db_credential() is not None


@contextmanager
def use_api_key(
    key: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    extra_body: dict | None = None,
):
    """Yalnızca bu blok içinde geçerli kimlik.

    Blok içindeki her LLM çağrısı BU anahtarı (ve verildiyse bu sağlayıcıyı)
    kullanır, `.env`'dekine ya da veritabanındakine DÜŞMEZ — çıkarken bağlam
    eski haline döner.
    """
    if not key or not key.strip():
        raise ValueError("boş anahtarla koşu bağlamı açılamaz")
    tokens = [(_api_key_override, _api_key_override.set(key.strip()))]
    if base_url and base_url.strip():
        tokens.append((_base_url_override, _base_url_override.set(base_url.strip())))
    if model and model.strip():
        tokens.append((_model_override, _model_override.set(model.strip())))
    if extra_body is not None:
        tokens.append((_extra_body_override, _extra_body_override.set(extra_body)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


@contextmanager
def use_credential(cred):
    """Belirli bir veritabanı kimliğiyle koşmak — düşme (fallback) için.

    `use_api_key`'ten farkı: kullanılan satırın id'si de bağlama giriyor,
    böylece çağrı kimlik hatasıyla düşerse DOĞRU satır 'failed'
    işaretlenebiliyor.
    """
    tokens = [
        (_api_key_override, _api_key_override.set(cred.api_key)),
        (_base_url_override, _base_url_override.set(cred.base_url)),
        (_model_override, _model_override.set(cred.model)),
        (_extra_body_override, _extra_body_override.set(cred.extra_body)),
        (_credential_id, _credential_id.set(cred.id)),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
