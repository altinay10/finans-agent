"""OpenAI-uyumlu sağlayıcı soyutlaması — tasarım dokümanı §06.

Tek bir istemci arayüzü; sağlayıcı .env üzerinden takas edilir. Gemini
(varsayılan), DeepSeek ve Qwen'in hepsi OpenAI-uyumlu uç nokta sunuyor,
bu yüzden sağlayıcı değiştirmek KOD DEĞİŞİKLİĞİ GEREKTİRMEZ.

`max_retries=0` bilinçli: OpenAI istemcisi varsayılan olarak başarısız
istekleri 2 kez tekrarlar. Bir çıkarım çağrısı hata alıyorsa tekrar denemek
çoğunlukla aynı hatayı ve iki kat token faturası üretir. Tek atış, sonra pes.
"""
from __future__ import annotations

from functools import lru_cache

from openai import OpenAI

from llm import settings


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    if not settings.LLM_API_KEY:
        raise RuntimeError(
            "LLM_API_KEY tanımlı değil — .env dosyasına bak (.env.example örnek alır). "
            "Fallback yalnızca parse() kırıldığında tetiklenir; anahtar olmadan çalışmaz."
        )
    return OpenAI(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=0,
    )
