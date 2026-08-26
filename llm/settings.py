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
# "Gemini 3.5 Flash Lite" diye bir model YOK; en ucuz uygun olan bu.
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.5-flash-lite")
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


# USD / 1.000.000 token. gemini-2.5-flash-lite için AI Studio ücretsiz
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
