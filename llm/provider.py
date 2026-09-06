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


@lru_cache(maxsize=4)
def _client(base_url: str, api_key: str, timeout: int) -> OpenAI:
    """Önbellek ANAHTARA GÖRE anahtarlanıyor.

    Eskiden `maxsize=1` ve parametresizdi: anahtar değişince önbellekteki
    istemci eski anahtarla dönmeye devam ediyordu, yani yeni anahtar sessizce
    yok sayılıyordu. Girdileri önbellek anahtarına koymak bunu yapısal olarak
    imkânsız kılıyor — ayrıca aynı anda farklı anahtarlarla koşan iş
    parçacıkları birbirinin istemcisini almıyor.
    """
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)


def get_client() -> OpenAI:
    key = settings.current_api_key()
    if not key:
        raise RuntimeError(
            "LLM anahtarı tanımlı değil — Agent sekmesinden kaydedebilir ya da "
            ".env dosyasına yazabilirsin (.env.example örnek alır). "
            "Fallback yalnızca parse() kırıldığında tetiklenir; anahtar olmadan çalışmaz."
        )
    # TABAN URL DE BAĞLAMDAN OKUNUYOR: anahtar sağlayıcısından ayrılamaz.
    # `_client` önbelleği zaten (base_url, key, timeout) üçlüsüyle
    # anahtarlandığı için farklı sağlayıcılar birbirinin istemcisini almaz.
    return _client(settings.current_base_url(), key, settings.LLM_TIMEOUT_SECONDS)


def clear_client_cache() -> None:
    _client.cache_clear()
