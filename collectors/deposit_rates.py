"""Banka bazlı mevduat / kar payı oranları.

Tasarım dokümanı §02'nin kendi ifadesiyle: bu verinin genel bir API'si yok.
Her banka kendi faiz sayfasını TUTAR × VADE matrisi olarak sunuyor ve sayfa
yapısı bankadan bankaya bambaşka — bu yüzden PARSERS sözlüğü banka banka
dolduruluyor, körlemesine bir "genel" parser yazılmıyor (bkz. §10 risk
tablosu: "Kademe seçimi hatası").

Aktif kaynaklar (hepsi canlı doğrulandı, hiçbiri elle girilmiş sabit sayı
DEĞİL — her gün yeniden çekilir):

  VAKIFBANK  — sitenin herkese-açık API akışı (vakifbank_common.py), 3 adım
  TEB        — /services/GetMevduatFaizOranlari, tek JSON çağrısı
  ENPARA     — sunucu-render HTML, TL/USD/EUR sekmeleri tek sayfada
  YAPIKREDI  — /_ajaxproxy/.../GetCalculationTool, para birimi başına 1 çağrı

Yeni bir banka eklemek için adımlar:

  1. Bankanın faiz sayfasını tarayıcıda aç, DevTools > Network'ten JSON
     uç noktası var mı bak.
  2. Yoksa sayfanın HTML yapısını incele, tutar/vade tablosunun
     selector'ını çıkar.
  3. robots.txt'i kontrol et — Disallow edilen bir yolsa ATLA.
  4. Aşağıdaki BankDepositParser alt sınıflarından birini yaz, PARSERS
     sözlüğüne ekle, config/sources.yaml'da status: active yap.

Not — Emlak Katılım kasıtlı olarak burada YOK: /tr/tum-kurlarimiz sayfası
sadece döviz kuru veriyor (bkz. fx_banks.py), katılma hesabı sayfasındaki
sayılar ("86", "94", "95"...) doğrudan yıllık getiri ORANI değil, banka
kârının müşteriye dağıtım YÜZDESİ (paylaşım oranı). Gerçek yıllık getiriyi
hesaplamak için ayrıca yayınlanan "gerçekleşen kar payı" rakamıyla
çarpılması gerekiyor — o rakam bu araştırmada bulunamadı. Ratio'yu
annual_rate alanına doğrudan yazmak yanlış/yanıltıcı bir sayı üretir, bu
yüzden bilinçli olarak atlandı (bkz. ILERLEME.md).
"""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone

import httpx
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from collectors import http, vakifbank_common
from collectors.base import Collector, ParseError, SanityCheckError
from store.clock import istanbul_today, utc_now
from store.db import SessionLocal
from store.observability import record_rate_changes
from store.models import DepositRate

logger = logging.getLogger(__name__)

MAX_ANNUAL_RATE = 2.00  # %200 — tasarım dokümanı §02: "faiz oranı 0-%200 arasında mı"
REQUEST_DELAY_SECONDS = 1.5  # bkz. config/sources.yaml: collection_etiquette
HEADERS = {"User-Agent": "finans-agent/0.1", "Accept": "application/json, text/html"}


class DepositRateRecord(BaseModel):
    institution: str
    currency: str = "TRY"
    term_days: int
    amount_min: float
    amount_max: float | None
    annual_rate: float
    is_profit_share: bool

    @model_validator(mode="after")
    def _bounds(self):
        if not (0 <= self.annual_rate <= MAX_ANNUAL_RATE):
            raise ValueError(f"{self.institution}: oran bant dışında ({self.annual_rate})")
        if self.term_days <= 0:
            raise ValueError(f"{self.institution}: term_days pozitif olmalı")
        return self


class BankDepositParser(ABC):
    """Her banka bunu implement eder. Bkz. modül docstring'i."""

    institution: str

    @abstractmethod
    def fetch(self) -> str:
        """Bu banka için gereken tüm HTTP çağrılarını yapar, sonucu JSON string olarak döner."""

    @abstractmethod
    def parse(self, raw: str) -> list[DepositRateRecord]: ...


# Bankalar TL'yi üç farklı kodla yazıyor: VakıfBank/TEB "TL", Yapı Kredi "YTL".
_CURRENCY_MAP = {"TL": "TRY", "YTL": "TRY", "TRY": "TRY", "USD": "USD", "EUR": "EUR"}


class VakifBankDepositParser(BankDepositParser):
    """vakifbank: 3 adımlı akış, doğrulandı 2026-08-23 (bkz. vakifbank_common.py).

    1) /calculator/depositProductList  -> ürün listesi (ProductCode, CampaignId, para birimleri)
    2) her ürün × para birimi için /timeDepositRates -> InterestRates (TUTAR × VADE matrisi)
    """

    institution = "VAKIFBANK"

    def fetch(self) -> str:
        products_resp = vakifbank_common.call(
            "/calculator/depositProductList", {"ProductName": "vadeli-hesap"}, scope="public"
        )
        products_resp.raise_for_status()
        products = products_resp.json().get("Data", {}).get("DepositProduct", [])

        rate_payloads = []
        for product in products:
            product_code = product.get("ProductCode")
            campaign_id = product.get("CampaignId")
            for cc in product.get("CurrencyCodes", []):
                currency = cc.get("CurrencyCode")
                time.sleep(REQUEST_DELAY_SECONDS)
                resp = vakifbank_common.call(
                    "/timeDepositRates",
                    {"CurrencyCode": currency, "ProductCode": product_code, "CampaignId": campaign_id},
                    scope="public",
                )
                if resp.status_code != 200:
                    continue  # bazı kampanya/para birimi kombinasyonları geçersiz olabilir
                rate_payloads.append(
                    {
                        "currency": currency,
                        "amount_min": cc.get("MinAmount"),
                        "amount_max": cc.get("MaxAmount"),
                        "body": resp.json(),
                    }
                )
        return json.dumps(rate_payloads)

    def parse(self, raw: str) -> list[DepositRateRecord]:
        rate_payloads = json.loads(raw)
        records: list[DepositRateRecord] = []
        for entry in rate_payloads:
            currency = _CURRENCY_MAP.get(entry["currency"], entry["currency"])
            interest_rates = entry["body"].get("Data", {}).get("DepositInfo", {}).get("InterestRates", [])
            for row in interest_rates:
                try:
                    term_days = int(float(row["TermDaysEnd"]))
                    annual_rate = float(row["CurrentInterestRate"]) / 100
                    amount_min = float(row.get("AmountStart", entry["amount_min"] or 0))
                    amount_max_raw = row.get("AmountEnd")
                    amount_max = float(amount_max_raw) if amount_max_raw not in (None, "") else None
                except (KeyError, TypeError, ValueError):
                    continue
                records.append(
                    DepositRateRecord(
                        institution=self.institution,
                        currency=currency,
                        term_days=term_days,
                        amount_min=amount_min,
                        amount_max=amount_max,
                        annual_rate=annual_rate,
                        is_profit_share=False,
                    )
                )
        return records


class CepteTebDepositParser(BankDepositParser):
    """CepteTEB: para birimi başına tek POST, tam TUTAR x VADE matrisi.

    Doğrulandı 2026-08-23. robots.txt yalnızca `*.pdf`'i yasaklıyor.

    UÇ NOKTA SEÇİMİ — bu bir tuzaktı ve yakalandı:
    `/services/GetMevduatFaizOranlari` daha kolay bulunuyor (tek GET, kimlik
    doğrulaması yok) ama TL için her vadeye %3 döndürüyor; bu, dijital
    CepteTEB müşterisinin aldığı oran DEĞİL. Sitenin kendi hesaplama aracı
    aynı gün aynı vade için %37,5 gösteriyordu. Yani o uç nokta panele
    konsaydı TEB, piyasanın onda biri faiz veren bir banka gibi görünecekti.
    Gerçek matris burada: sayfanın `cepteTebVadeliOzet()` fonksiyonunun
    çağırdığı `/Services/VadeliHesapFaizOranList`, `ceptetebEH=E`
    (CepteTEB müşterisi) parametresiyle.

    Kalan bilinen fark: hesaplama aracı (`MevduatGetirisiHesaplaProf`) aynı
    tutar ve vade için bu listeden yaklaşık 1 puan YÜKSEK dönüyor — büyük
    olasılıkla yürürlükteki bir kampanya farkı. Listedeki tabela oranı
    kullanılıyor: eksik göstermek, fazla göstermekten daha güvenli.

    Vade olarak aralığın başlangıcı (`vadeMin`) yazılıyor — Yapı Kredi
    parser'ıyla aynı gerekçe, bkz. YapiKrediDepositParser.
    """

    institution = "TEB"
    URL = "https://www.cepteteb.com.tr/Services/VadeliHesapFaizOranList"
    REFERER = "https://www.cepteteb.com.tr/hesaplama/mevduat-faizi-hesaplama"
    CURRENCIES = ("TL", "USD", "EUR")
    # Listede üst sınır bu devasa sentinel ile "sınırsız" anlamında geliyor.
    UPPER_LIMIT_SENTINEL = 999_999_999_999

    def fetch(self) -> str:
        payloads = {}
        for currency in self.CURRENCIES:
            time.sleep(REQUEST_DELAY_SECONDS)
            resp = http.post(
                self.URL,
                headers={
                    **HEADERS,
                    "Referer": self.REFERER,
                    "X-Requested-With": "XMLHttpRequest",
                },
                data={"paraKod": currency, "ceptetebEH": "E"},
                timeout=25,
            )
            resp.raise_for_status()
            payloads[currency] = resp.json()
        return json.dumps(payloads)

    def parse(self, raw: str) -> list[DepositRateRecord]:
        payloads = json.loads(raw)
        records: list[DepositRateRecord] = []
        for body in payloads.values():
            for row in body:
                currency = _CURRENCY_MAP.get(row.get("paraKodu"))
                if currency is None:
                    continue  # GBP vb. — bu projede kapsam dışı
                try:
                    upper = float(row["tutarMax"])
                    records.append(
                        DepositRateRecord(
                            institution=self.institution,
                            currency=currency,
                            term_days=int(row["vadeMin"]),
                            amount_min=float(row["tutarMin"]),
                            amount_max=None if upper >= self.UPPER_LIMIT_SENTINEL else upper,
                            annual_rate=float(row["faizOran"]) / 100,
                            is_profit_share=False,
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        return records


class EnparaDepositParser(BankDepositParser):
    """Enpara: sunucu-render HTML, üç para birimi de aynı sayfada. Doğrulandı 2026-08-23.

    Sayfa yapısı: para birimi başına bir kapsayıcı
    `<div class="... flex-table TRY_S">`, içinde `<hr>` ile ayrılmış satırlar;
    her satırın ilk hücresi tutar aralığı ("150.000 - 750.000 TL"), kalan
    hücreler `<div class="...flex-table-head">32 gün</div>` +
    `<div class="...flex-table-value">%36,75</div>` ikilisi.

    UYARI — tutar kademesinin anlamı: Enpara kademeyi "tüm hesaplarınızın TL
    KARŞILIĞI toplam bakiyesi" üzerinden belirliyor. Yani USD/EUR tablosundaki
    "150.000 - 750.000 TL" sınırı, yatırılan döviz tutarıyla aynı birimde
    değil. Bugün pratikte etkisi yok (Enpara tüm döviz kademelerine aynı
    %0,01'i veriyor), ama banka kademeleri farklılaştırırsa döviz için
    seçilen kademe yanlış olabilir. Bkz. ILERLEME.md.
    """

    institution = "ENPARA"
    URL = "https://www.enpara.com/hesaplar/mevduat-faiz-oranlari"

    _TABLE_RE = re.compile(
        r'flex-table\s+(?P<currency>[A-Z]{3})_[A-Z]"[^>]*>(?P<body>.*?)(?=flex-table\s+[A-Z]{3}_[A-Z]"|\Z)',
        re.DOTALL,
    )
    _AMOUNT_RE = re.compile(r'flex-table-value fixed-right">([^<]+)<')
    _CELL_RE = re.compile(
        r'flex-table-head">\s*(\d+)\s*g(?:&#252;|ü)n\s*</div>\s*'
        r'<div class="[^"]*flex-table-value">\s*%\s*([\d.,]+)\s*</div>',
        re.DOTALL,
    )
    _RANGE_RE = re.compile(r"([\d.]+)(?:\s*-\s*([\d.]+))?")

    def fetch(self) -> str:
        resp = http.get(self.URL, headers=HEADERS, timeout=25, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _tr_number(raw: str) -> float:
        return float(raw.replace(".", "").replace(",", "."))

    def _amount_bounds(self, label: str) -> tuple[float, float | None]:
        """'0 - 150.000 TL' -> (0, 150000);  '1.500.000 TL ve üzeri' -> (1500000, None)."""
        m = self._RANGE_RE.search(label)
        if not m:
            raise ValueError(f"tutar aralığı okunamadı: {label!r}")
        low = self._tr_number(m.group(1))
        high = self._tr_number(m.group(2)) if m.group(2) else None
        return low, high

    def parse(self, raw: str) -> list[DepositRateRecord]:
        records: list[DepositRateRecord] = []
        for table in self._TABLE_RE.finditer(raw):
            currency = _CURRENCY_MAP.get(table.group("currency"), table.group("currency"))
            # Satırlar <hr> ile ayrılıyor; ilk parça yalnızca başlık satırı.
            for chunk in re.split(r"<hr[^>]*>", table.group("body")):
                amount_match = self._AMOUNT_RE.search(chunk)
                if not amount_match:
                    continue  # başlık satırı — tutar hücresi yok
                amount_min, amount_max = self._amount_bounds(amount_match.group(1))
                for term_raw, rate_raw in self._CELL_RE.findall(chunk):
                    records.append(
                        DepositRateRecord(
                            institution=self.institution,
                            currency=currency,
                            term_days=int(term_raw),
                            amount_min=amount_min,
                            amount_max=amount_max,
                            annual_rate=self._tr_number(rate_raw) / 100,
                            is_profit_share=False,
                        )
                    )
        return records


class YapiKrediDepositParser(BankDepositParser):
    """Yapı Kredi: e-mevduat hesaplama aracının kendi uç noktası. Doğrulandı 2026-08-23.

    Sayfadaki `$.Page.GetCalculationTool(eDeposite, currency)` çağrısının
    birebir aynısı — çerez, token veya oturum gerektirmiyor, robots.txt
    `Allow: /`. Para birimi başına bir POST.

    Yanıt iki listeyi çaprazlıyor:
      RateLevelList[i]  -> tutar kademesi (MinAmount / MaxAmount)
      GroupedRateList[] -> vade aralığı (StartTenor..EndTenor) + Rates[i]
    Rates dizisinin i'inci elemanı, RateLevelList'in i'inci kademesine aittir.

    Vade olarak StartTenor yazılıyor: "32-35 gün" kovası için garanti edilen
    en kısa vade odur ve diğer bankaların standart vadeleriyle (32/92/181)
    hizalanır. EndTenor kullanmak 31/35/45 gibi kıyaslanamaz vadeler üretirdi.
    """

    institution = "YAPIKREDI"
    URL = (
        "https://www.yapikredi.com.tr/_ajaxproxy/calculate-tools/"
        "calculate-flexible-interest.aspx/GetCalculationTool"
    )
    REFERER = (
        "https://www.yapikredi.com.tr/bireysel-bankacilik/hesaplama-araclari/"
        "e-mevduat-faizi-hesaplama"
    )
    CURRENCIES = ("YTL", "USD", "EUR")

    def fetch(self) -> str:
        payloads = {}
        for currency in self.CURRENCIES:
            time.sleep(REQUEST_DELAY_SECONDS)
            resp = http.post(
                self.URL,
                headers={
                    **HEADERS,
                    "Content-Type": "application/json; charset=UTF-8",
                    "Referer": self.REFERER,
                    "X-Requested-With": "XMLHttpRequest",
                },
                json={"eDeposite": False, "currency": currency},
                timeout=25,
            )
            resp.raise_for_status()
            payloads[currency] = resp.json()
        return json.dumps(payloads)

    def parse(self, raw: str) -> list[DepositRateRecord]:
        payloads = json.loads(raw)
        records: list[DepositRateRecord] = []
        for body in payloads.values():
            data = (body.get("d") or {}).get("Data") or {}
            for rate_block in data.get("RateList", []):
                currency = _CURRENCY_MAP.get(rate_block.get("Currency"), rate_block.get("Currency"))
                levels = rate_block.get("RateLevelList", [])
                for group in rate_block.get("GroupedRateList", []):
                    rates = group.get("Rates", [])
                    if len(rates) != len(levels):
                        # Şema değişmiş demektir; sessizce yanlış kademe
                        # eşlemektense bu grubu atla.
                        continue
                    for level, rate in zip(levels, rates):
                        try:
                            records.append(
                                DepositRateRecord(
                                    institution=self.institution,
                                    currency=currency,
                                    term_days=int(group["StartTenor"]),
                                    amount_min=float(level["MinAmount"]),
                                    amount_max=float(level["MaxAmount"]),
                                    annual_rate=float(rate) / 100,
                                    is_profit_share=False,
                                )
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
        return records


class AkbankDepositParser(BankDepositParser):
    """Akbank: tek POST, tam TUTAR x VADE matrisi. Doğrulandı 2026-08-24.

    Sayfanın `depositRateCalculator.get()` çağrısı. Parametreler ürün
    seçicisinin `value` alanından ayrışıyor: "97-0-8" -> faizTipi=97,
    faizTuru=0, kanalKodu=8 ("Tanışma" ürünü, web kanalı).

    Yanıt iki listeyi çaprazlıyor:
      Headers[i]   -> tutar kademesi metni ("100.000 - 249.999")
      GrossRates[] -> vade aralığı ("32 - 40") + GRates[i].Rate
    GRates dizisinin i'inci elemanı Headers'ın i'inci kademesine aittir.

    Oran "-" olabilir (o kademede o vade açık değil) — atlanır.
    Yalnızca TL: ürün seçicisinde tek para birimi var (dovizKodu 888) ve
    840/978 denemeleri boş dönüyor.

    robots.txt `/_layouts*` yasağı kullanıcı kararıyla uygulanmıyor
    (bkz. config/sources.yaml robots_override).
    """

    institution = "AKBANK"
    URL = "https://www.akbank.com/_layouts/15/Akbank/CalcTools/Ajax.aspx/GetMevduatFaiz"
    REFERER = "https://www.akbank.com/mevduat-yatirim/mevduat/vadeli-mevduat-faiz-hesaplama"
    PAYLOAD = {"dovizKodu": "888", "faizTipi": "97", "faizTuru": "0", "kanalKodu": "8"}

    _RANGE_RE = re.compile(r"([\d.]+)\s*-\s*([\d.]+)")

    def fetch(self) -> str:
        resp = http.post(
            self.URL,
            headers={
                **HEADERS,
                "Content-Type": "application/json; charset=UTF-8",
                "Referer": self.REFERER,
                "X-Requested-With": "XMLHttpRequest",
            },
            json=self.PAYLOAD,
            timeout=25,
        )
        resp.raise_for_status()
        return resp.text

    @classmethod
    def _bounds(cls, label: str) -> tuple[float, float | None]:
        m = cls._RANGE_RE.search(label)
        if not m:
            raise ValueError(f"aralık okunamadı: {label!r}")
        low = float(m.group(1).replace(".", ""))
        high = float(m.group(2).replace(".", ""))
        # En üst kademe "500.000 - 999.999.999.999.999" gibi devasa bir
        # sentinel ile geliyor; bunu "üst sınır yok" diye yaz.
        return low, None if high >= 1e12 else high

    def parse(self, raw: str) -> list[DepositRateRecord]:
        body = json.loads(raw)
        service = (
            ((body.get("d") or {}).get("Data") or {}).get("ServiceData") or {}
        )
        headers = service.get("Headers") or []
        records: list[DepositRateRecord] = []
        for group in service.get("GrossRates") or []:
            try:
                term_days, _ = self._bounds(group["Period"])
            except (KeyError, ValueError):
                continue
            rates = group.get("GRates") or []
            if len(rates) != len(headers):
                continue  # şema kaymış — yanlış kademeye oran yazmaktansa atla
            for header, cell in zip(headers, rates):
                raw_rate = str(cell.get("Rate", "")).strip()
                if not raw_rate or raw_rate == "-":
                    continue  # o kademede bu vade açık değil
                try:
                    amount_min, amount_max = self._bounds(header)
                    annual_rate = float(raw_rate.replace(".", "").replace(",", ".")) / 100
                except ValueError:
                    continue
                records.append(
                    DepositRateRecord(
                        institution=self.institution,
                        currency="TRY",
                        term_days=int(term_days),
                        amount_min=amount_min,
                        amount_max=amount_max,
                        annual_rate=annual_rate,
                        is_profit_share=False,
                    )
                )
        return records


PARSERS: dict[str, BankDepositParser] = {
    "vakifbank": VakifBankDepositParser(),
    "akbank": AkbankDepositParser(),
    "teb": CepteTebDepositParser(),
    "enpara": EnparaDepositParser(),
    "yapikredi": YapiKrediDepositParser(),
}


class DepositRateCollector(Collector):
    name = "deposit_rates"
    schema = DepositRateRecord

    def fetch(self) -> bytes:
        """Her bankayı ayrı ayrı dener.

        Dört banka tek koleksiyoncuda toplandığı için bir bankanın uç noktası
        düştüğünde diğer üçünün verisi de kaybolmamalı. Bu yüzden hata
        yutulmuyor ama yayılmıyor da: yükün içine yazılıyor, parse() atlıyor,
        log'a düşüyor.
        """
        payloads: dict[str, dict] = {}
        for key, parser in PARSERS.items():
            started = time.monotonic()
            try:
                with http.source(key):
                    payloads[key] = {"ok": True, "body": parser.fetch()}
                self.record_source(key, phase="fetch", status="ok",
                                   duration_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:  # noqa: BLE001 - tek banka tüm koşuyu düşürmemeli
                logger.warning("deposit_rates/%s: fetch başarısız: %s", key, exc)
                payloads[key] = {"ok": False, "error": str(exc)}
                self.record_source(key, phase="fetch", status="failed", error=str(exc),
                                   duration_ms=int((time.monotonic() - started) * 1000))
        return json.dumps(payloads).encode("utf-8")

    def parse(self, raw: bytes) -> list[DepositRateRecord]:
        payloads: dict[str, dict] = json.loads(raw)
        records: list[DepositRateRecord] = []
        for key, entry in payloads.items():
            if not entry.get("ok"):
                continue
            try:
                parsed = PARSERS[key].parse(entry["body"])
            except Exception as exc:  # noqa: BLE001 - aynı gerekçe, parse tarafında
                logger.warning("deposit_rates/%s: parse başarısız: %s", key, exc)
                self.record_source(key, phase="parse", status="failed", error=str(exc))
                continue
            if not parsed:
                logger.warning("deposit_rates/%s: sıfır satır çıktı", key)
                self.record_source(key, phase="parse", status="empty",
                                   error="sıfır kayıt — sayfa şeması değişmiş olabilir")
            else:
                self.record_source(key, phase="parse", status="ok", rows=len(parsed))
            records.extend(parsed)
        if not records:
            raise ParseError("Hiçbir bankadan mevduat oranı satırı çıkarılamadı")
        return records

    def sanity_check(self, records: list[DepositRateRecord]) -> None:
        for r in records:
            if r.amount_max is not None and r.amount_max < r.amount_min:
                raise SanityCheckError(f"{r.institution}: amount_max < amount_min")

    def persist(self, records: list[DepositRateRecord], run_id: int) -> None:
        now = utc_now()
        # valid_date TÜRKİYE takvim günüdür — bkz. store/clock.py
        today = istanbul_today()
        with SessionLocal() as session:
            # Bugüne ait ESKİ satırları temizle.
            #
            # Yalnızca upsert yapmak yetmiyor: bir banka vade setini veya tutar
            # kademelerini değiştirdiğinde (ya da biz yanlış bir uç noktadan
            # doğrusuna geçtiğimizde) eski anahtarlar hiç güncellenmeden aynı
            # valid_date altında kalır ve panelde artık geçerli olmayan
            # oranlarla YAN YANA görünür. Bu yüzden her koşuda, o gün için
            # ilgili kurumun yeni sette bulunmayan satırları siliniyor.
            # Sadece bu koşuda verisi gelen kurumlara dokunuluyor — çeken
            # bankası düşen bir kurumun dünkü verisi korunuyor.
            fresh_keys: dict[str, set[tuple[str, int, float]]] = {}
            for r in records:
                fresh_keys.setdefault(r.institution, set()).add(
                    (r.currency, r.term_days, float(r.amount_min))
                )
            for institution, keys in fresh_keys.items():
                existing = session.execute(
                    select(DepositRate).where(
                        DepositRate.institution == institution,
                        DepositRate.valid_date == today,
                    )
                ).scalars().all()
                for row in existing:
                    if (row.currency, row.term_days, float(row.amount_min)) not in keys:
                        session.delete(row)
                session.flush()

            for r in records:
                exists = session.execute(
                    select(DepositRate).where(
                        DepositRate.institution == r.institution,
                        DepositRate.currency == r.currency,
                        DepositRate.term_days == r.term_days,
                        DepositRate.amount_min == r.amount_min,
                        DepositRate.valid_date == today,
                    )
                ).scalar_one_or_none()
                if exists:
                    exists.annual_rate = r.annual_rate
                    exists.fetched_at = now
                    continue
                session.add(
                    DepositRate(
                        institution=r.institution,
                        currency=r.currency,
                        term_days=r.term_days,
                        amount_min=r.amount_min,
                        amount_max=r.amount_max,
                        annual_rate=r.annual_rate,
                        is_profit_share=r.is_profit_share,
                        valid_date=today,
                        fetched_at=now,
                        run_id=run_id,
                    )
                )
            session.commit()

        # Sessiz donma tespiti: uç nokta 200 dönüyor ama sayı haftalardır
        # aynıysa besleme durmuş olabilir. Yalnızca DEĞİŞENLER yazılır.
        changed = record_rate_changes(
            "deposit",
            {
                (r.institution, f"{r.currency}/{r.term_days}/{r.amount_min:.0f}"): r.annual_rate
                for r in records
            },
            run_id=run_id,
        )
        if changed:
            logger.info("deposit_rates: %s oran değişimi kaydedildi", changed)


if __name__ == "__main__":
    result = DepositRateCollector().run()
    print(result)
