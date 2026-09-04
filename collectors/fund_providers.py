"""Fon fiyat SAĞLAYICILARI — her portföy şirketi için bir adaptör.

PLAN.md madde 6. Asıl sorun "hangi site" değildi: fon listesi
`config/sources.yaml`'a gömülüydü ve tek bir sağlayıcıyı (Ak Portföy)
okuyabilen tek bir toplayıcı vardı. Yeni bir fon eklemek KOD DEĞİŞİKLİĞİ
gerektiriyordu — kullanıcının istediği ise "istediğim fonu ekleyebilmek".

Bu modül o bağı koparır: fon listesi `config/funds.yaml`'da bir satır,
sağlayıcı ise burada bir sınıf. Panelden fon eklemek yalnızca YAML'a satır
yazar (bkz. app/panels/fund.py).

TEFAS neden yok: `tefas.gov.tr` F5 Shape bot-challenge arkasında
(`window["bobcmn"]`, `/TSPD/`, `DOSL7.challenge.support_id`). Aşmak
obfuscated JS çalıştırıp çerez üretmek, yani bot tespiti atlatmak demek.
Kullanıcının robots.txt kararı bunu KAPSAMIYOR; bilinçli olarak yapılmıyor.

SERİ TÜRÜ FARKI — önemli:
  * Ak Portföy    -> BİRİM PAY FİYATI (mutlak TL)
  * Garanti Portföy -> 1.000 TL'nin zaman içindeki DEĞERİ (endeks)
İkisi de simülasyon için yeterlidir çünkü `core/fund.py` yalnızca
başlangıç/bitiş ORANINI kullanır (units = anapara/başlangıç fiyatı).
Ama panelde "birim pay fiyatı" diye gösterilirse Garanti fonlarında
yanıltıcı olur; bu yüzden sağlayıcı `price_is_unit_value` bayrağını taşır
ve panel etiketini ona göre seçer.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from collectors import http
from collectors.base import ParseError

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
    provider: str                # 'akportfoy' | 'garantiportfoy'
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
    def fetch(self, fund: FundSpec) -> str:
        """Ham yanıtı döndürür. HTTP çağrıları collectors/http üzerinden."""

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


# ------------------------------------------------------------ Ak Portföy --


class AkPortfoyProvider(FundPriceProvider):
    """Fiyat serisi sayfaya gömülü: `var fundVals = {...}`.

    API çağrısı, token veya CSRF yok; sitede robots.txt de yok (HTTP 404).
    2018'den bugüne ~2160 günlük kapanış fiyatı — panelin ihtiyacından
    çok fazlası.
    """

    key = "akportfoy"
    label = "Ak Portföy"
    price_is_unit_value = True

    URL_TEMPLATE = "https://www.akportfoy.com.tr/tr/fon/{ref}"

    _FUNDVALS_RE = re.compile(r"var\s+fundVals\s*=\s*(\{.*?\});", re.DOTALL)
    _TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)

    def fetch(self, fund: FundSpec) -> str:
        resp = http.get(
            self.URL_TEMPLATE.format(ref=fund.ref),
            timeout=25,
            headers=HEADERS,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text

    def resolve(self, code: str) -> ResolvedFund | None:
        """Ak Portföy'de adres parçası fon kodunun kendisi: /tr/fon/AK3.

        Yine de sayfa GERÇEKTEN açılıyor mu diye bakılır; yoksa var olmayan
        bir kod kayıt defterine yazılır ve her koşuda sessizce düşerdi.
        """
        code = code.strip().upper()
        try:
            resp = http.get(
                self.URL_TEMPLATE.format(ref=code),
                timeout=25, headers=HEADERS, follow_redirects=True,
            )
        except Exception:  # noqa: BLE001 - çözümleme başarısızlığı hata değil
            return None
        if resp.status_code != 200 or self._FUNDVALS_RE.search(resp.text) is None:
            return None
        name = None
        m = self._TITLE_RE.search(resp.text)
        if m:
            name = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split())
        return ResolvedFund(code=code, provider=self.key, ref=code, name=name or None)

    def parse(self, fund: FundSpec, raw: str) -> FundSeries:
        match = self._FUNDVALS_RE.search(raw)
        if match is None:
            raise ParseError(
                f"{fund.code}: 'var fundVals' bulunamadı — sayfa yapısı değişmiş olabilir"
            )
        try:
            fund_vals = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ParseError(f"{fund.code}: fundVals JSON'u ayrıştırılamadı ({exc})") from exc

        name = None
        is_equity_heavy = None
        title_match = self._TITLE_RE.search(raw)
        if title_match:
            name = html.unescape(title_match.group(1)).replace("| Ak Portföy", "").strip()
            # Başlık "AK3 - Ak Portföy ..." biçiminde; kodu ada tekrar gömme.
            name = re.sub(rf"^{re.escape(fund.code)}\s*[-–]\s*", "", name).strip()
            is_equity_heavy = EQUITY_HEAVY_MARKER in name.casefold()

        series = fund_vals.get(fund.ref) or fund_vals.get(fund.code)
        if not series:
            raise ParseError(f"{fund.code}: fundVals içinde bu koda ait seri yok")

        points: list[tuple[date, float]] = []
        for point in series:
            try:
                points.append(
                    (epoch_ms_to_istanbul_date(point["Date"]), float(point["Close"]))
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ParseError(f"{fund.code}: fiyat noktası ayrıştırılamadı ({exc})") from exc
        return FundSeries(fund.code, name, is_equity_heavy, points)


# ------------------------------------------------------ Garanti Portföy --


class GarantiPortfoyProvider(FundPriceProvider):
    """Anonim bearer token + JSON seri uç noktası. Doğrulandı 2026-08-25.

    AKIŞ (üç adım, hepsi login'siz):
      1. `/webservice/gettoken` -> anonim `access_token`. Bu bir KULLANICI
         kimliği değil, sitenin kendi ön yüzünün herkese açık istemci
         belirtecidir (client_id: "garantiportfoy:frontend"). VakıfBank
         akışıyla aynı desen (collectors/vakifbank_common.py).
      2. `/webservice/lastdate` -> serinin son iş günü.
      3. `/webservice/fundsinddailyds` -> {lang, code, firstDate, lastDate,
         rollBack:"T", value:1000} ile [[epoch_ms, değer, yüzde], ...]

    DİKKAT — DÖNEN SERİ BİRİM PAY FİYATI DEĞİL: "1.000 TL yatırılsaydı bugün
    ne olurdu" endeksidir. Simülasyon için sorun değil (yalnızca oran
    kullanılıyor) ama panelde "fiyat" diye gösterilirse yanıltır; bu yüzden
    `price_is_unit_value = False`.

    Fon adı ve "(Hisse Senedi Yoğun Fon)" ibaresi fon SAYFASINDAN okunuyor;
    uç nokta adı döndürmüyor.
    """

    key = "garantiportfoy"
    label = "Garanti BBVA Portföy"
    price_is_unit_value = False

    BASE = "https://www.garantibbvaportfoy.com.tr"
    PAGE_TEMPLATE = BASE + "/{ref}"
    TOKEN_URL = BASE + "/webservice/gettoken"
    LAST_DATE_URL = BASE + "/webservice/lastdate"
    SERIES_URL = BASE + "/webservice/fundsinddailyds"

    _CODE_RE = re.compile(r"window\.fundCode\s*=\s*'([A-Z0-9]+)'")
    # Ana sayfadaki GİZLİ fon dizini: her fon için <a href="/slug">'ın içinde
    # önce `d-none` sınıflı kod, sonra görünen ad geliyor. Tek istekle 70+
    # fonun kod -> slug -> ad eşlemesi çıkıyor; fonu koddan bulmanın en ucuz
    # yolu bu (fon sayfalarını tek tek gezmek 70+ istek ederdi).
    _INDEX_RE = re.compile(
        r'<a\s+href="/([a-z0-9][a-z0-9\-]*)"\s*>\s*'
        r'<span class="d-none">([A-Z0-9]{2,6})</span>\s*'
        r"<span>(.*?)</span>",
        re.DOTALL,
    )
    _NAME_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
    _TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)

    def fetch(self, fund: FundSpec) -> str:
        page_url = self.PAGE_TEMPLATE.format(ref=fund.ref.strip("/"))
        page = http.get(page_url, timeout=30, headers=HEADERS, follow_redirects=True)
        page.raise_for_status()

        # KOD DOĞRULAMASI — sessiz veri bozulmasına karşı.
        #
        # Burada eskiden `code = sayfadaki_kod or fund.code` yazıyordu: yani
        # sayfanın kodu kayıt defterindekinden FARKLIYSA sayfanınki
        # kullanılıp seri çekiliyor, sonra persist onu KAYIT DEFTERİNDEKİ
        # kodla saklıyordu. Yanlış bir slug girildiğinde sonuç sessizce
        # BAŞKA BİR FONUN getirisi oluyordu.
        #
        # Canlıda gerçekleşti (2026-09-04): GPB kaydı
        # `birinci-para-piyasasi-fonu` sayfasını gösteriyordu, o sayfanın
        # kodu ise GTL. Panelde GPB seçen kullanıcı 530 günlük GTL serisini
        # görüyordu. Bir yatırım aracında bu, sayfa yapısı değişmesinden çok
        # daha tehlikeli bir hata: hiçbir yerde patlamıyor.
        sayfa_kodu = self._code_from_page(page.text)
        if sayfa_kodu and sayfa_kodu != fund.code:
            raise ParseError(
                f"{fund.code}: '{fund.ref}' sayfasının fon kodu {sayfa_kodu}. "
                f"Kayıt defteri yanlış fonu gösteriyor — seri ÇEKİLMEDİ. "
                f"Doğru adresi bulmak için fonu koduyla yeniden ekle."
            )
        code = sayfa_kodu or fund.code

        time.sleep(REQUEST_DELAY_SECONDS)
        token_resp = http.get(
            self.TOKEN_URL,
            timeout=25,
            headers={**HEADERS, "Referer": page_url},
            follow_redirects=True,
        )
        token_resp.raise_for_status()
        token = token_resp.json().get("access_token")
        if not token:
            raise ParseError("Garanti Portföy anonim belirteci alınamadı")

        auth = {
            **HEADERS,
            "Referer": page_url,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

        time.sleep(REQUEST_DELAY_SECONDS)
        last_resp = http.post(
            self.LAST_DATE_URL, timeout=25, headers=auth, content=json.dumps({"lang": "tr"})
        )
        last_resp.raise_for_status()
        last_date = _unwrap(last_resp.json())
        if not isinstance(last_date, str):
            raise ParseError(f"Garanti Portföy son tarih beklenmedik biçimde: {last_date!r}")
        first_date = (
            datetime.strptime(last_date, "%Y-%m-%d").date() - timedelta(days=HISTORY_DAYS)
        ).isoformat()

        time.sleep(REQUEST_DELAY_SECONDS)
        series_resp = http.post(
            self.SERIES_URL,
            timeout=40,
            headers=auth,
            content=json.dumps(
                {
                    "lang": "tr",
                    "code": code,
                    "firstDate": first_date,
                    "lastDate": last_date,
                    "rollBack": "T",
                    # Endeksin başlangıç değeri; oran kullanıldığı için
                    # seçimi sonucu etkilemez.
                    "value": 1000,
                }
            ),
        )
        series_resp.raise_for_status()
        return json.dumps(
            {"code": code, "page_title": self._name_from_page(page.text),
             "series": _unwrap(series_resp.json())}
        )

    def fund_index(self) -> dict[str, ResolvedFund]:
        """Ana sayfadaki dizinden kod -> fon eşlemesi (tek HTTP isteği)."""
        resp = http.get(self.BASE + "/", timeout=30, headers=HEADERS, follow_redirects=True)
        resp.raise_for_status()
        index: dict[str, ResolvedFund] = {}
        for slug, code, name in self._INDEX_RE.findall(resp.text):
            index[code] = ResolvedFund(
                code=code, provider=self.key, ref=slug,
                name=" ".join(html.unescape(re.sub(r"<[^>]+>", " ", name)).split()) or None,
            )
        return index

    def resolve(self, code: str) -> ResolvedFund | None:
        code = code.strip().upper()
        try:
            return self.fund_index().get(code)
        except Exception:  # noqa: BLE001 - çözümleme başarısızlığı hata değil
            return None

    def _code_from_page(self, page: str) -> str | None:
        match = self._CODE_RE.search(page)
        return match.group(1) if match else None

    def _name_from_page(self, page: str) -> str | None:
        for pattern in (self._NAME_RE, self._TITLE_RE):
            match = pattern.search(page)
            if not match:
                continue
            text = html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
            text = " ".join(text.split()).replace("| Garanti BBVA Portföy", "").strip()
            if text and text.lower() != "garanti bbva portföy":
                return text
        return None

    def parse(self, fund: FundSpec, raw: str) -> FundSeries:
        body = json.loads(raw)
        series = body.get("series")
        if isinstance(series, list) and series:
            data = (series[0] or {}).get("Data") or []
        else:
            data = []
        if not data:
            raise ParseError(
                f"{fund.code}: Garanti Portföy serisi boş — fon kodu ya da sayfa adresi "
                "değişmiş olabilir"
            )

        points: list[tuple[date, float]] = []
        for point in data:
            try:
                points.append((epoch_ms_to_istanbul_date(point[0]), float(point[1])))
            except (IndexError, TypeError, ValueError) as exc:
                raise ParseError(f"{fund.code}: seri noktası ayrıştırılamadı ({exc})") from exc

        name = body.get("page_title") or fund.name
        is_equity_heavy = (
            EQUITY_HEAVY_MARKER in name.casefold() if name else None
        )
        return FundSeries(fund.code, name, is_equity_heavy, points)


def _unwrap(payload):
    """Garanti uç noktaları bazen JSON'u STRING olarak sarmalayıp döndürüyor.

    `"{\\"data\\": ...}"` biçimi; iki kez çözmek gerekiyor. Tek seferlik
    çözüm varsayan bir okuma, yanıtın biçimi değiştiğinde sessizce boş seri
    üretirdi.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return payload
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


PROVIDERS: dict[str, FundPriceProvider] = {
    p.key: p for p in (AkPortfoyProvider(), GarantiPortfoyProvider())
}
