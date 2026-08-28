"""Ortam değişkenlerinden LLM ayarları — hepsi .env üzerinden takas edilir.

Varsayılan sağlayıcı Google Gemini'nin OpenAI-uyumlu uç noktasıdır; böylece
llm/provider.py'deki tek istemci hiç değişmeden çalışır.

TOKEN BÜTÇESİ — bu dosyadaki sınırlar kasıtlı olarak DÜŞÜK. Fallback nadir
bir kurtarma yolu; pahalı bir döngüye dönüşmemeli. Sınırların hepsi .env'den
büyütülebilir ama varsayılanlar "takılırsa token yakmasın" ilkesine göre
seçildi (kullanıcı isteği, 2026-08-24).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

from dotenv import load_dotenv

load_dotenv()


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
LLM_AGENT_MAX_CALLS_PER_RUN = _int("LLM_AGENT_MAX_CALLS_PER_RUN", 8)

# İstek zaman aşımı — asılı kalan bir çağrı hem koşuyu hem faturayı bekletir.
LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 45)


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


def estimate_cost_usd(prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    """Fiyat tanımlı değilse None — 'bilinmiyor' ile 'sıfır' karıştırılmasın."""
    if LLM_PRICE_INPUT_PER_1M is None and LLM_PRICE_OUTPUT_PER_1M is None:
        return None
    cost = 0.0
    if prompt_tokens and LLM_PRICE_INPUT_PER_1M is not None:
        cost += prompt_tokens / 1_000_000 * LLM_PRICE_INPUT_PER_1M
    if completion_tokens and LLM_PRICE_OUTPUT_PER_1M is not None:
        cost += completion_tokens / 1_000_000 * LLM_PRICE_OUTPUT_PER_1M
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
        LLM_AGENT_MAX_CALLS_PER_RUN=_int("LLM_AGENT_MAX_CALLS_PER_RUN", 8),
        LLM_TIMEOUT_SECONDS=_int("LLM_TIMEOUT_SECONDS", 45),
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


def current_api_key() -> str:
    """O anki bağlamda kullanılacak anahtar."""
    return _api_key_override.get() or LLM_API_KEY


def fallback_enabled() -> bool:
    """Agent çağrısına izin var mı.

    Bağlama bir anahtar konulmuşsa izin de vardır: kullanıcı kendi anahtarıyla
    açıkça "çalıştır" dedi. `LLM_FALLBACK_ENABLED` kazara token yakmaya karşı
    bir koruma; bilerek basılan bir düğmenin önüne konmasının anlamı yok.
    """
    return bool(_api_key_override.get()) or LLM_FALLBACK_ENABLED


@contextmanager
def use_api_key(key: str):
    """Yalnızca bu blok içinde geçerli anahtar.

    Blok içindeki her LLM çağrısı BU anahtarı kullanır ve `.env`'dekine
    DÜŞMEZ — çıkarken bağlam eski haline döner.
    """
    if not key or not key.strip():
        raise ValueError("boş anahtarla koşu bağlamı açılamaz")
    token = _api_key_override.set(key.strip())
    try:
        yield
    finally:
        _api_key_override.reset(token)
