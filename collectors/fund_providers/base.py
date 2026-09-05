"""Fon fiyat sağlayıcılarının ORTAK sözleşmesi.

Her portföy şirketi için bir adaptör; adaptörler bu modüldeki tipleri
paylaşır. Sağlayıcı sınıflarının kendisi kardeş modüllerdedir
(`akportfoy.py`, `garantiportfoy.py`, `denizportfoy.py`, `ykportfoy.py`)
ve `__init__.py` hepsini `PROVIDERS` altında toplar.

NEDEN PAKET: eskiden hepsi tek `fund_providers.py` dosyasındaydı. İki
sağlayıcı daha eklenince dosya 900+ satıra çıkıyordu; her sağlayıcının
kendi tuzakları ve kendi uzun açıklaması var, tek dosyada birbirine
karışıyorlardı. Dışarıya görünen isimler DEĞİŞMEDİ — `from
collectors.fund_providers import PROVIDERS, FundSpec, ...` aynen çalışır.

SERİ TÜRÜ FARKI — önemli:
  * Ak Portföy      -> BİRİM PAY FİYATI (mutlak TL)
  * Yapı Kredi Prtf -> BİRİM PAY FİYATI (mutlak TL)
  * Garanti Portföy -> 1.000 TL'nin zaman içindeki DEĞERİ (endeks)
Hepsi simülasyon için yeterlidir çünkü `core/fund.py` yalnızca
başlangıç/bitiş ORANINI kullanır (units = anapara/başlangıç fiyatı).
Ama panelde "birim pay fiyatı" diye gösterilirse Garanti fonlarında
yanıltıcı olur; bu yüzden sağlayıcı `price_is_unit_value` bayrağını taşır
ve panel etiketini ona göre seçer.

SERİ SIKLIĞI FARKI — bu da önemli:
Ak/Garanti tek istekte tüm geçmişi GÜNLÜK veriyor. Yapı Kredi Portföy ise
30 günden uzun aralıkta seriyi AYLIĞA seyreltiyor; bu yüzden orada seri
uzak geçmişte bilerek SEYREKTİR (bkz. ykportfoy.py'deki pencere
açıklaması). Panelin
`nearest_prior_price` çağrısı bunu kaldırır — hedef tarihte fiyat yoksa
ondan önceki en yakın fiyatı kullanır ve panel gerçek başlangıç tarihini
yazar. Buna karşılık `fund_prices.sanity_check` ardışık noktalar arası
hareketi ölçerken aradaki GÜN SAYISINI hesaba katmak zorundadır.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ISTANBUL = ZoneInfo("Europe/Istanbul")
REQUEST_DELAY_SECONDS = 2.0  # bkz. config/sources.yaml: collection_etiquette

# HTTP başlıkları latin-1 kodlanır; Türkçe karakter kullanma (bkz.
# collectors/participation_rates.py'deki aynı tuzak).
HEADERS = {"User-Agent": "finans-agent/0.1"}

EQUITY_HEAVY_MARKER = "hisse senedi yoğun fon"

# Panelin ihtiyacı ~400 gün; 2 yıl istemek hem yeterli hem de sunucuyu
# gereksiz yormaz. Ak Portföy zaten tüm geçmişi tek sayfada veriyor.
HISTORY_DAYS = 760


def epoch_ms_to_istanbul_date(epoch_ms: int) -> date:
    """Epoch ms -> İSTANBUL takvim günü.

    UTC kullanmak tarihleri bir gün geriye kaydırır: Ak Portföy'ün damgaları
    İstanbul gece yarısına denk geliyor (1514840400000 = 2018-01-02 00:00 +03)
    ve UTC olarak yorumlanınca 2018-01-01 olur. Fon simülasyonu o zaman
    yanlış günün fiyatını kullanır.
    """
    return datetime.fromtimestamp(epoch_ms / 1000, ISTANBUL).date()


@dataclass(frozen=True)
class FundSpec:
    """config/funds.yaml'daki bir satır."""

    code: str                    # TEFAS kodu: 'AK3', 'GTA'
    provider: str                # 'akportfoy' | 'garantiportfoy' | ...
    ref: str                     # sağlayıcıya özgü adres parçası
    name: str | None = None
    benchmark: str | None = None
    # Elle girilebilir ama SAĞLAYICI ÜZERİNE YAZAR: stopaj kararı resmî
    # fon adındaki "(Hisse Senedi Yoğun Fon)" ibaresine bağlıdır, elle
    # girilen bir bayrağa değil.
    is_equity_heavy: bool | None = None


@dataclass(frozen=True)
class ResolvedFund:
    """Fon kodundan çözülen kayıt defteri satırı."""

    code: str
    provider: str
    ref: str
    name: str | None = None


@dataclass(frozen=True)
class FundSeries:
    """Bir sağlayıcının bir fon için döndürdüğü sonuç."""

    code: str
    name: str | None
    is_equity_heavy: bool | None
    points: list[tuple[date, float]]


class FundPriceProvider(ABC):
    key: str
    label: str
    # Seri birim pay fiyatı mı, yoksa endeks mi (bkz. modül docstring'i).
    price_is_unit_value: bool = True

    @abstractmethod
    def fetch(self, fund: FundSpec, *, known_dates: frozenset[date] = frozenset()) -> str:
        """Ham yanıtı döndürür. HTTP çağrıları collectors/http üzerinden.

        `known_dates` — bu sağlayıcının fonları için VERİTABANINDA ZATEN
        olan fiyat tarihleri. Ak/Garanti bunu yok sayar (tek istekte tüm
        geçmişi veriyorlar). Deniz Portföy ve Yapı Kredi Portföy ise seriyi
        parça parça toplamak zorunda; bu küme olmadan her gece aynı 100+
        isteği baştan atarlar. Boş küme = "hiç geçmiş yok, tam tarama yap".
        """

    @abstractmethod
    def parse(self, fund: FundSpec, raw: str) -> FundSeries:
        """Ham yanıtı tarih->fiyat serisine çevirir."""

    def resolve(self, code: str) -> ResolvedFund | None:
        """Fon KODUNDAN adres parçasını bul — kullanıcı URL girmesin.

        Kullanıcı isteği (2026-09-04): "sadece fon koduyla, url olmadan".
        Panelden fon eklemek, sağlayıcının sitesine gidip fonun sayfa kısa
        adını elle bulup kopyalamayı gerektiriyordu; bu hem zahmetli hem de
        yanlış yapıştırmaya çok açık — nitekim öyle de oldu (bkz.
        GarantiPortfoyProvider.fetch'teki kod doğrulaması).

        Bulamazsa None döner; çağıran diğer sağlayıcıları dener.
        """
        return None

    def catalog(self) -> list[ResolvedFund]:
        """Sağlayıcının TÜM fonları — "hepsini ekle" akışı için.

        Boş liste = bu sağlayıcı toplu listeleme desteklemiyor; kullanıcı
        fonları tek tek koduyla ekler.
        """
        return []
