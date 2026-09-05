"""Fon fiyat SAĞLAYICILARI — her portföy şirketi için bir adaptör.

PLAN.md madde 6. Asıl sorun "hangi site" değildi: fon listesi
`config/sources.yaml`'a gömülüydü ve tek bir sağlayıcıyı (Ak Portföy)
okuyabilen tek bir toplayıcı vardı. Yeni bir fon eklemek KOD DEĞİŞİKLİĞİ
gerektiriyordu — kullanıcının istediği ise "istediğim fonu ekleyebilmek".

Bu paket o bağı koparır: fon listesi `config/funds.yaml`'da bir satır,
sağlayıcı ise burada bir modül. Panelden fon eklemek yalnızca YAML'a satır
yazar (bkz. app/panels/fund.py).

TEFAS neden yok: `tefas.gov.tr` F5 Shape bot-challenge arkasında
(`window["bobcmn"]`, `/TSPD/`, `DOSL7.challenge.support_id`). Aşmak
obfuscated JS çalıştırıp çerez üretmek, yani bot tespiti atlatmak demek.
Kullanıcının robots.txt kararı bunu KAPSAMIYOR; bilinçli olarak yapılmıyor.

Ortak tipler ve sözleşme `base.py`'de; her sağlayıcı kendi modülünde.
Dışarıya görünen isimler bu dosyadan yeniden ihraç edilir, yani
`from collectors.fund_providers import PROVIDERS, FundSpec, ...` çalışmaya
devam eder.
"""
from __future__ import annotations

from collectors.fund_providers.akportfoy import AkPortfoyProvider
from collectors.fund_providers.base import (
    EQUITY_HEAVY_MARKER,
    HEADERS,
    HISTORY_DAYS,
    ISTANBUL,
    REQUEST_DELAY_SECONDS,
    FundPriceProvider,
    FundSeries,
    FundSpec,
    ResolvedFund,
    epoch_ms_to_istanbul_date,
)
from collectors.fund_providers.garantiportfoy import GarantiPortfoyProvider
from collectors.fund_providers.ykportfoy import YapiKrediPortfoyProvider


def build_providers() -> dict[str, FundPriceProvider]:
    """Sağlayıcı kayıt defterinin YENİ bir kopyası.

    Neden fabrika: sağlayıcılar örnek (instance) düzeyinde önbellek
    tutuyor — Yapı Kredi Portföy sitemap'ten çıkardığı fon dizinini bir
    kez okuyup saklıyor. Modül düzeyindeki tek bir örnek paylaşılsaydı bu
    dizin süreç ömrü boyunca bayat kalır, yeni açılan bir fon panelde
    hiçbir zaman bulunamazdı.
    """
    return {
        p.key: p
        for p in (
            AkPortfoyProvider(),
            GarantiPortfoyProvider(),
            YapiKrediPortfoyProvider(),
        )
    }


# Panel gibi yalnızca etiket/bayrak okuyan yerler için hazır kopya.
# TOPLAMA burada değil, `build_providers()` ile alınan taze kopyada yapılır
# (bkz. collectors/fund_prices.FundPriceCollector.__init__).
PROVIDERS: dict[str, FundPriceProvider] = build_providers()

__all__ = [
    "AkPortfoyProvider",
    "EQUITY_HEAVY_MARKER",
    "GarantiPortfoyProvider",
    "HEADERS",
    "HISTORY_DAYS",
    "ISTANBUL",
    "PROVIDERS",
    "REQUEST_DELAY_SECONDS",
    "FundPriceProvider",
    "FundSeries",
    "FundSpec",
    "ResolvedFund",
    "YapiKrediPortfoyProvider",
    "build_providers",
    "epoch_ms_to_istanbul_date",
]
