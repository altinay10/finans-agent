"""Banka bazlı kredi faiz oranları.

Tasarım dokümanının orijinal kapsamında bu koleksiyoncu hiç yoktu (bkz.
ILERLEME.md, madde 1 — "kredi faiz oranı toplayıcısı hiç yok"). VakıfBank'ın
herkese açık API akışı (collectors/vakifbank_common.py) bunu da veriyor —
loanProductList çağrısı KKDF/BSMV ve aylık brüt oranı doğrudan döndürüyor.

Bu veri ayrıca config/taxes.yaml'daki İhtiyaç Kredisi BSMV oranını
düzeltmek için kullanıldı: eski değer %5 idi, VakıfBank'ın canlı verisi
%15 olduğunu gösterdi (bkz. taxes.yaml source notu, 2026-08-23).
"""
from __future__ import annotations

import html
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
from config.loader import resolve_loan_taxes
from core.loan import compare_installment
from store.clock import istanbul_today, utc_now
from store.db import SessionLocal
from store.observability import record_rate_changes
from store.models import LoanRate, LoanReferenceQuote

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "finans-agent/0.1", "Accept": "application/json"}
REQUEST_DELAY_SECONDS = 1.5  # bkz. config/sources.yaml: collection_etiquette

MAX_MONTHLY_RATE = 0.20  # %20 aylık — bunun üzeri neredeyse kesin bir ayrıştırma hatasıdır

# VakıfBank ürün adlarından bizim loan_type sözlüğümüze eşleme.
_PRODUCT_TYPE_MAP = {
    "Konut Kredisi": "housing",
    "TİK": "personal",
    "Taşıt Kredisi": "vehicle",
}


class LoanRateRecord(BaseModel):
    institution: str
    loan_type: str
    monthly_rate: float
    term_min: int | None = None
    term_max: int | None = None
    amount_max: float | None = None
    # Bu oran herkese değil belirli bir gruba mı açık (bkz.
    # store/models.py::LoanRate.is_campaign). API toplayıcıları bankanın
    # kendi tabela oranını çekiyor, bu yüzden varsayılan False; alanı
    # dolduran tek yer şimdilik `loan_rates_llm`.
    is_campaign: bool = False
    campaign_note: str | None = None

    @model_validator(mode="after")
    def _bounds(self):
        if not (0 <= self.monthly_rate <= MAX_MONTHLY_RATE):
            raise ValueError(f"{self.institution}: aylık oran bant dışında ({self.monthly_rate})")
        return self


class LoanReferenceQuoteRecord(BaseModel):
    """Bankanın KENDİ ilan ettiği taksit — hesabımızın denetçisi.

    Bkz. store/models.py::LoanReferenceQuote. Bu kayıt panelde gösterilmek
    için değil, bizim anüite + vergi modelimizi her gün bankanın rakamına
    karşı sınamak için toplanır.
    """

    institution: str
    loan_type: str
    principal: float
    term_months: int
    monthly_rate: float
    bank_installment: float
    bank_annual_cost_rate: float | None = None


class BankLoanParser(ABC):
    institution: str

    @abstractmethod
    def fetch(self) -> str: ...

    @abstractmethod
    def parse(self, raw: str) -> list[LoanRateRecord]: ...

    def reference_quotes(self, raw: str) -> list[LoanReferenceQuoteRecord]:
        """Banka kendi taksitini de döndürüyorsa burada çıkar.

        Varsayılan boş: çoğu banka yalnızca oran yayınlıyor. Akbank'ın
        GetCreditInfo yanıtı kontrol edildi (2026-08-24) — `urunTaksitTut`
        alanı var ama üç üründe de null geliyor, yani Akbank bu uç noktadan
        taksit vermiyor. Uydurulmadı.
        """
        return []


class VakifBankLoanParser(BankLoanParser):
    """vakifbank: doğrulandı 2026-08-23. /loanProductList tek çağrıda tüm ürünleri veriyor."""

    institution = "VAKIFBANK"

    def fetch(self) -> str:
        resp = vakifbank_common.call("/loanProductList", {}, scope="public")
        resp.raise_for_status()
        return resp.text

    def parse(self, raw: str) -> list[LoanRateRecord]:
        body = json.loads(raw)
        products = body.get("Data", {}).get("LoanProduct", [])
        seen: set[tuple[str, str]] = set()
        records: list[LoanRateRecord] = []
        for p in products:
            loan_type = _PRODUCT_TYPE_MAP.get(p.get("ProductName"))
            if loan_type is None:
                continue
            key = (loan_type, p.get("ProductCode"))
            if key in seen:
                continue  # aynı ürünün farklı kampanya varyantları — ilkini al
            seen.add(key)
            try:
                monthly_rate = float(p["InterestRate"]) / 100
            except (KeyError, TypeError, ValueError):
                continue
            records.append(
                LoanRateRecord(
                    institution=self.institution,
                    loan_type=loan_type,
                    monthly_rate=monthly_rate,
                    term_min=int(float(p["MinimumLoanTerm"])) if p.get("MinimumLoanTerm") else None,
                    term_max=int(float(p["MaximumLoanTerm"])) if p.get("MaximumLoanTerm") else None,
                    amount_max=float(p["MaksimumLoanAmount"]) if p.get("MaksimumLoanAmount") else None,
                )
            )
        return records


class YapiKrediLoanParser(BankLoanParser):
    """Yapı Kredi: kredi hesaplama aracının kendi uç noktaları. Doğrulandı 2026-08-23.

    İki farklı uç nokta gerekiyor, çünkü banka ihtiyaç kredisini kampanya
    üzerinden, konut/taşıtı ise kategori kodu üzerinden fiyatlıyor:

      ihtiyaç : GetSubProduct -> GetCampaign -> GetPersonalCreditPaymentPlan
                (vade başına ayrı InterestRate döner; en kısa vadeninkini
                alıyoruz — tabela oranı odur, uzun vadede indirim uygulanıyor)
      konut   : GetCategoryCodeListByCreditClass -> CalculateByCreditAmount

    TUZAK: GetPersonalCreditPaymentPlan'da `installmentAmount` alanı 0
    gönderilirse yanıt sessizce BOŞ döner (PaymentPlanList: null, HTTP 200).
    null gönderilmesi şart. Regresyon değeri: canlı doğrulamada 3 ay vadede
    %5,99, KKDF 15, BSMV 15.

    Taşıt (kategori A1 "KASKOLU") kasıtlı olarak yok: uç nokta her vade ve
    tutar denemesinde PaymentList=null dönüyor, yani banka bu araç üzerinden
    taşıt fiyatı yayınlamıyor. Uydurulmuş bir oran yazmaktansa boş bırakıldı.
    """

    institution = "YAPIKREDI"
    BASE = "https://www.yapikredi.com.tr/_ajaxproxy/calculate-tools/retail-credits.aspx"
    REFERER = "https://www.yapikredi.com.tr/bireysel-bankacilik/hesaplama-araclari/kredi-hesaplama"

    PERSONAL_CREDIT_TYPE = "1"      # GetSubProduct: "Standart Bireysel İhtiyaç Kredisi"
    HOUSING_CREDIT_CLASS = "1"      # GetCategoryCodeListByCreditClass: "KONUT KREDİSİ"
    SAMPLE_PRINCIPAL = 100_000      # oran tutara bağlı değil; temsili bir tutar yeter
    SAMPLE_HOUSING_PRINCIPAL = 1_000_000
    SAMPLE_HOUSING_MATURITY = 120

    def _post(self, method: str, payload: dict) -> dict:
        time.sleep(REQUEST_DELAY_SECONDS)
        resp = http.post(
            f"{self.BASE}/{method}",
            headers={
                **HEADERS,
                "Content-Type": "application/json; charset=UTF-8",
                "Referer": self.REFERER,
                "X-Requested-With": "XMLHttpRequest",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch(self) -> str:
        sub_products = self._post("GetSubProduct", {"creditType": self.PERSONAL_CREDIT_TYPE})
        sub_list = ((sub_products.get("d") or {}).get("Data") or {}).get("SubProductList") or []
        sub_id = sub_list[0]["Id"] if sub_list else None

        personal_plan: dict = {}
        if sub_id is not None:
            campaigns = self._post("GetCampaign", {"subProductIdList": [sub_id]})
            campaign_list = ((campaigns.get("d") or {}).get("Data") or {}).get("CampaignList") or []
            if campaign_list:
                personal_plan = self._post(
                    "GetPersonalCreditPaymentPlan",
                    {
                        "campaignId": campaign_list[0]["CampaignId"],
                        "subProductId": sub_id,
                        "deferredMonthCount": 0,
                        "principalAmount": self.SAMPLE_PRINCIPAL,
                        "installmentAmount": None,  # 0 GÖNDERME - boş yanıt döner
                    },
                )

        categories = self._post(
            "GetCategoryCodeListByCreditClass", {"creditClass": self.HOUSING_CREDIT_CLASS}
        )
        category_list = ((categories.get("d") or {}).get("Data")) or []
        housing_plan: dict = {}
        if category_list:
            housing_plan = self._post(
                "CalculateByCreditAmount",
                {
                    "categoryCode": category_list[0]["CategoryCode"],
                    "maturity": self.SAMPLE_HOUSING_MATURITY,
                    "principal": self.SAMPLE_HOUSING_PRINCIPAL,
                    "wantedOfferedMaturities": True,
                },
            )

        return json.dumps({"personal": personal_plan, "housing": housing_plan})

    @staticmethod
    def _tr_number(raw: str | float) -> float:
        if isinstance(raw, (int, float)):
            return float(raw)
        return float(str(raw).replace(".", "").replace(",", "."))

    def parse(self, raw: str) -> list[LoanRateRecord]:
        body = json.loads(raw)
        records: list[LoanRateRecord] = []

        plans = ((body.get("personal") or {}).get("d") or {}).get("Data") or {}
        plan_list = plans.get("PaymentPlanList") or []
        if plan_list:
            shortest = min(plan_list, key=lambda p: p["Maturity"])
            maturities = [p["Maturity"] for p in plan_list]
            records.append(
                LoanRateRecord(
                    institution=self.institution,
                    loan_type="personal",
                    monthly_rate=self._tr_number(shortest["InterestRate"]) / 100,
                    term_min=min(maturities),
                    term_max=max(maturities),
                )
            )

        housing = ((body.get("housing") or {}).get("d") or {}).get("Data") or {}
        payment_list = housing.get("PaymentList") or []
        if payment_list:
            first = payment_list[0]["Value"]
            maturities = housing.get("MaturityList") or []
            records.append(
                LoanRateRecord(
                    institution=self.institution,
                    loan_type="housing",
                    monthly_rate=self._tr_number(first["InterestRate"]) / 100,
                    term_min=min(maturities) if maturities else None,
                    term_max=max(maturities) if maturities else None,
                )
            )

        return records

    def reference_quotes(self, raw: str) -> list[LoanReferenceQuoteRecord]:
        """Yapı Kredi kendi taksitini de döndürüyor — projedeki tek denetim çapası.

        Bu yüzden değerli: taksit tutarı bizim üç varsayımımızın ortak
        çıktısıdır (anüite formülü, KKDF/BSMV oranları, verginin faize
        eklenme biçimi). Üçünden biri bozulursa tutar tutmaz. Canlı
        doğrulama (2026-08-24): ihtiyaç 100.000 TL / 3 ay -> banka 38.654,31,
        bizim hesap 38.654,31; konut 1.000.000 TL / 36 ay -> banka 48.156,85,
        bizim 48.156,85. Sapma %0,00001 mertebesinde.

        Karşılaştırma OKUNAN ORANLA aynı satırdan yapılır (ihtiyaçta en kısa
        vade) — farklı vadenin taksitiyle kıyaslamak sahte bir sapma üretir.
        """
        body = json.loads(raw)
        quotes: list[LoanReferenceQuoteRecord] = []

        plans = ((body.get("personal") or {}).get("d") or {}).get("Data") or {}
        plan_list = plans.get("PaymentPlanList") or []
        if plan_list:
            shortest = min(plan_list, key=lambda p: p["Maturity"])
            installment = shortest.get("MonthlyInstallmentAmount")
            if installment:
                quotes.append(
                    LoanReferenceQuoteRecord(
                        institution=self.institution,
                        loan_type="personal",
                        principal=self.SAMPLE_PRINCIPAL,
                        term_months=int(shortest["Maturity"]),
                        monthly_rate=self._tr_number(shortest["InterestRate"]) / 100,
                        bank_installment=self._tr_number(installment),
                        bank_annual_cost_rate=(
                            self._tr_number(shortest["YearlyCustomerCostRate"]) / 100
                            if shortest.get("YearlyCustomerCostRate") is not None
                            else None
                        ),
                    )
                )

        housing = ((body.get("housing") or {}).get("d") or {}).get("Data") or {}
        payment_list = housing.get("PaymentList") or []
        if payment_list:
            first = payment_list[0]["Value"]
            installment = first.get("MonthlyInstallmentAmount")
            if installment and first.get("PrincipalAmount"):
                quotes.append(
                    LoanReferenceQuoteRecord(
                        institution=self.institution,
                        loan_type="housing",
                        principal=self._tr_number(first["PrincipalAmount"]),
                        term_months=int(first["Maturity"]),
                        monthly_rate=self._tr_number(first["InterestRate"]) / 100,
                        bank_installment=self._tr_number(installment),
                        # YearlyEffectiveInterestRate, MonthlyCostRate'in
                        # yıllıklandırılmışıdır ve KOMİSYONU içerir (3,34 faiz
                        # -> 3,52 maliyet -> %51,43 yıllık). Bizim yıllık
                        # maliyetimiz yalnızca faiz+vergi; panel bu farkı
                        # açıkça yazar, sayı sessizce eşitlenmez.
                        bank_annual_cost_rate=(
                            self._tr_number(first["YearlyEffectiveInterestRate"]) / 100
                            if first.get("YearlyEffectiveInterestRate") is not None
                            else None
                        ),
                    )
                )

        return quotes


class EmlakKatilimLoanParser(BankLoanParser):
    """Emlak Katılım: ihtiyaç finansmanı kâr oranı. Doğrulandı 2026-08-23.

    Katılım bankası olduğu için bu bir FAİZ oranı değil, **kâr oranı**; panel
    bunu kurumun `kind` alanından (institutions tablosu) okuyup ayrı
    etiketliyor. Hesaplama matematiği aynı (aylık orana KKDF/BSMV eklenip
    anüite kuruluyor) ama isimlendirme aynı değil — tasarım §07.

    KAYNAĞIN SINIRI, açıkça: sayfadaki tablonun başlığı "**Örnek** İhtiyaç
    Finansmanı Tablosu" ve tek satırı var (30.000 ₺ / 12 ay / %1,69). Yani
    bu bir tutar x vade matrisi değil, bankanın yayınladığı tek örnek. Farklı
    tutar/vadede oran değişebilir. term_min/term_max = 12 yazılıyor ki panel,
    başka bir vade seçildiğinde "bu oran o vade için ilan edilmedi" uyarısını
    versin — sessizce 60 aya uygulamaktan iyidir.

    Konut ve taşıt finansmanı sayfaları KASITLI olarak okunmuyor: oradaki
    tablolar oran değil, teminat oranı (taşıt değerine oranı) ve azami vade
    yayınlıyor. Oran yok, uydurulmadı.

    NOT — `data-title` özniteliklerine GÜVENME: sayfada bozuklar (tutar
    hücresi data-title="Vade", oran hücresi data-title="Yabancı Para").
    Sütun eşlemesi thead başlıklarından yapılıyor.
    """

    institution = "EMLAKKATILIM"
    URL = "https://www.emlakkatilim.com.tr/tr/bireysel/finansmanlar/ihtiyac-finansmani"
    EXAMPLE_TERM_MONTHS = 12

    _TABLE_RE = re.compile(r"<table>\s*<thead>.*?</table>", re.DOTALL)
    _CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL)
    _ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
    _RATE_RE = re.compile(r"([\d.,]+)\s*%")

    def fetch(self) -> str:
        resp = http.get(self.URL, headers=HEADERS, timeout=25, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _text(fragment: str) -> str:
        return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()

    def parse(self, raw: str) -> list[LoanRateRecord]:
        for table in self._TABLE_RE.findall(raw):
            rows = self._ROW_RE.findall(table)
            if len(rows) < 2:
                continue
            headers = [self._text(c).lower() for c in self._CELL_RE.findall(rows[0])]
            try:
                rate_index = next(i for i, h in enumerate(headers) if "oran" in h)
            except StopIteration:
                continue
            for row in rows[1:]:
                cells = self._CELL_RE.findall(row)
                if rate_index >= len(cells):
                    continue
                match = self._RATE_RE.search(self._text(cells[rate_index]))
                if not match:
                    continue
                monthly_rate = float(match.group(1).replace(".", "").replace(",", ".")) / 100
                return [
                    LoanRateRecord(
                        institution=self.institution,
                        loan_type="personal",
                        monthly_rate=monthly_rate,
                        term_min=self.EXAMPLE_TERM_MONTHS,
                        term_max=self.EXAMPLE_TERM_MONTHS,
                    )
                ]
        return []


class AkbankLoanParser(BankLoanParser):
    """Akbank: üç kredi türü de tek uç noktadan. Doğrulandı 2026-08-24.

    `GetCreditInfo` ürün kodu başına çağrılıyor; kodlar hesaplama
    sayfalarındaki ürün seçicisinden alındı. Yanıttaki
    `ServisData.ucretOut.urunFaizListesi` gerçek bir VADE x TUTAR matrisi
    (faizMinVade/faizMaxVade, faizMinTutar/faizMaxTutar, faizOranTL).

    Bu kaynak projede TAŞIT kredisini nihayet dolduran tek yer — VakıfBank
    kataloğunda taşıt ürünü dönmüyor, Yapı Kredi'nin taşıt kategorisi boş
    yanıt veriyor.

    Şemamız kredi türü başına tek oran tuttuğu için EN KISA vadenin oranı
    yazılıyor: tabela oranı odur, uzun vadede indirim uygulanıyor
    (YapiKrediLoanParser ile aynı politika).

    NOT — vergi oranları: yanıt `urunFonOrn` (KKDF) ve `urunVrgOrn` (BSMV)
    döndürüyor ama konut için BSMV'yi 15 diyor. Konut kredisi 6802 s. Kanun
    m.29 ile BSMV'den istisnadır ve VakıfBank'ın kendi verisi 0 diyor;
    bu yüzden vergi oranları BURADAN OKUNMUYOR, config/taxes.yaml
    yetkili kaynak olarak kalıyor.
    """

    institution = "AKBANK"
    URL = "https://www.akbank.com/_layouts/15/Akbank/CalcTools/Ajax.aspx/GetCreditInfo"
    REFERER = "https://www.akbank.com/krediler/ihtiyac-kredisi-hesaplama"

    # Ürün kodu -> bizim loan_type. Kodlar hesaplama sayfalarının
    # KrediUrunTipiTab* seçicisinden alındı (2026-08-24).
    PRODUCTS = {
        "10-10-10-8207": "personal",
        "30-10-10-8398": "housing",
        "20-10-10-9991": "vehicle",
    }

    def fetch(self) -> str:
        payloads = {}
        for code in self.PRODUCTS:
            time.sleep(REQUEST_DELAY_SECONDS)
            resp = http.post(
                self.URL,
                headers={
                    **HEADERS,
                    "Content-Type": "application/json; charset=UTF-8",
                    "Referer": self.REFERER,
                    "X-Requested-With": "XMLHttpRequest",
                },
                json={"KrediKodu": code},
                timeout=30,
            )
            resp.raise_for_status()
            payloads[code] = resp.json()
        return json.dumps(payloads)

    def parse(self, raw: str) -> list[LoanRateRecord]:
        payloads = json.loads(raw)
        records: list[LoanRateRecord] = []
        for code, body in payloads.items():
            loan_type = self.PRODUCTS.get(code)
            if loan_type is None:
                continue
            entries = (body.get("d") or {}).get("Data") or []
            if not entries:
                continue
            fees = (entries[0].get("ServisData") or {}).get("ucretOut") or {}
            rate_rows = fees.get("urunFaizListesi") or []
            if not rate_rows:
                continue
            shortest = min(rate_rows, key=lambda r: r.get("faizMinVade", 10**6))
            try:
                monthly_rate = float(shortest["faizOranTL"]) / 100
            except (KeyError, TypeError, ValueError):
                continue
            records.append(
                LoanRateRecord(
                    institution=self.institution,
                    loan_type=loan_type,
                    monthly_rate=monthly_rate,
                    term_min=fees.get("urunMinVadeTL"),
                    term_max=fees.get("urunMaxVadeTL"),
                    amount_max=float(fees["urunMaxTut"]) if fees.get("urunMaxTut") else None,
                )
            )
        return records


class EnparaLoanParser(BankLoanParser):
    """Enpara ihtiyaç kredisi. Doğrulandı 2026-08-24.

    ÖNCEKİ TESPİT YANLIŞTI ve düzeltildi: bu kaynak "CSRF token gerektiriyor"
    diye kapalı bırakılmıştı. Sayfadaki `-` yer tutucusu JS ile dolduğu için
    öyle görünüyordu. Gerçekte oran tablosu sayfaya SUNUCU TARAFINDA gömülü:

        var loanInterestRates = JSON.parse('[{"Title":"0-12 Ay", ...}]')

    Yani tek GET yeterli; token, POST, tarayıcı gerekmiyor. Ders: "değer
    ekranda JS ile geliyor" ile "veri sayfada yok" aynı şey değil.

    Şemamız kredi türü başına tek oran tuttuğu için EN KISA vade dilimi
    yazılıyor (diğer parser'larla aynı politika).
    """

    institution = "ENPARA"
    URL = "https://www.enpara.com/krediler/ihtiyac-kredisi"
    # `= JSON.parse('[...]')` biçimi; tek tırnak içinde JSON.
    _RATES_RE = re.compile(r"loanInterestRates\s*=\s*JSON\.parse\('(\[.*?\])'\)", re.DOTALL)

    def fetch(self) -> str:
        resp = http.get(self.URL, headers=HEADERS, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    def parse(self, raw: str) -> list[LoanRateRecord]:
        match = self._RATES_RE.search(raw)
        if not match:
            return []
        rows = json.loads(match.group(1))
        if not rows:
            return []
        shortest = min(rows, key=lambda r: r.get("MinInstalment", 10**6))
        maturities = [
            r[k] for r in rows for k in ("MinInstalment", "MaxInstalment") if r.get(k) is not None
        ]
        try:
            monthly_rate = float(shortest["InstalmentRate"]) / 100
        except (KeyError, TypeError, ValueError):
            return []
        return [
            LoanRateRecord(
                institution=self.institution,
                loan_type="personal",
                monthly_rate=monthly_rate,
                term_min=min(maturities) if maturities else None,
                term_max=max(maturities) if maturities else None,
                # Sayfa metninde ilan edilen üst sınır (hesaplama aracının
                # kaydırıcı üst değeri).
                amount_max=750_000,
            )
        ]


PARSERS: dict[str, BankLoanParser] = {
    "vakifbank": VakifBankLoanParser(),
    "enpara": EnparaLoanParser(),
    "akbank": AkbankLoanParser(),
    "yapikredi": YapiKrediLoanParser(),
    "emlakkatilim": EmlakKatilimLoanParser(),
}


class LoanRateCollector(Collector):
    name = "loan_rates"
    schema = LoanRateRecord

    def __init__(self) -> None:
        super().__init__()
        # parse() ile persist() arasında taşınır. Ayrı bir alan olmasının
        # sebebi: bunlar oran KAYDI değil, oranın DOĞRULAMASI. sanity_check
        # ve LLM fallback yalnızca LoanRateRecord'la ilgilenir; referans
        # taksitin oraya karışması şemayı bulandırırdı.
        self._reference_quotes: list[LoanReferenceQuoteRecord] = []

    def fetch(self) -> bytes:
        """Bir bankanın uç noktası düşerse diğerinin verisi kaybolmasın
        diye hata banka bazında yakalanır (bkz. deposit_rates.py aynı desen)."""
        payloads: dict[str, dict] = {}
        for key, parser in PARSERS.items():
            started = time.monotonic()
            try:
                with http.source(key):
                    payloads[key] = {"ok": True, "body": parser.fetch()}
                self.record_source(key, phase="fetch", status="ok",
                                   duration_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:  # noqa: BLE001
                logger.warning("loan_rates/%s: fetch başarısız: %s", key, exc)
                payloads[key] = {"ok": False, "error": str(exc)}
                self.record_source(key, phase="fetch", status="failed", error=str(exc),
                                   duration_ms=int((time.monotonic() - started) * 1000))
        return json.dumps(payloads).encode("utf-8")

    def parse(self, raw: bytes) -> list[LoanRateRecord]:
        payloads: dict[str, dict] = json.loads(raw)
        records: list[LoanRateRecord] = []
        for key, entry in payloads.items():
            if not entry.get("ok"):
                continue
            try:
                parsed = PARSERS[key].parse(entry["body"])
                if parsed:
                    self.record_source(key, phase="parse", status="ok", rows=len(parsed))
                else:
                    self.record_source(key, phase="parse", status="empty",
                                       error="sıfır kayıt — sayfa şeması değişmiş olabilir")
                records.extend(parsed)
            except Exception as exc:  # noqa: BLE001
                logger.warning("loan_rates/%s: parse başarısız: %s", key, exc)
                self.record_source(key, phase="parse", status="failed", error=str(exc))
            try:
                self._reference_quotes.extend(PARSERS[key].reference_quotes(entry["body"]))
            except Exception as exc:  # noqa: BLE001 - denetim verisi asıl veriyi düşürmemeli
                logger.warning("loan_rates/%s: referans taksit okunamadı: %s", key, exc)
                self.record_source(key, phase="reference", status="failed", error=str(exc))
        if not records:
            raise ParseError("Hiçbir bankadan kredi oranı satırı çıkarılamadı")
        return records

    def sanity_check(self, records: list[LoanRateRecord]) -> None:
        for r in records:
            if r.term_min is not None and r.term_max is not None and r.term_min > r.term_max:
                raise SanityCheckError(f"{r.institution}/{r.loan_type}: term_min > term_max")

    def persist(self, records: list[LoanRateRecord], run_id: int) -> None:
        now = utc_now()
        # valid_date TÜRKİYE takvim günüdür — bkz. store/clock.py
        today = istanbul_today()
        with SessionLocal() as session:
            for r in records:
                # is_campaign ANAHTARDA: bu toplayıcı yalnızca genel oran
                # yazıyor (hepsinde False), ama aynı satıra `loan_rates_llm`
                # de yazabiliyor ve o kampanyalı satır da üretiyor. Koşul
                # olmadan buradaki sorgu bir bankanın kampanya satırını
                # bulup üzerine genel oranı yazabilirdi.
                exists = session.execute(
                    select(LoanRate).where(
                        LoanRate.institution == r.institution,
                        LoanRate.loan_type == r.loan_type,
                        LoanRate.valid_date == today,
                        LoanRate.is_campaign == r.is_campaign,
                    )
                ).scalar_one_or_none()
                if exists:
                    exists.monthly_rate = r.monthly_rate
                    exists.term_min = r.term_min
                    exists.term_max = r.term_max
                    exists.amount_max = r.amount_max
                    exists.fetched_at = now
                    continue
                session.add(
                    LoanRate(
                        institution=r.institution,
                        loan_type=r.loan_type,
                        monthly_rate=r.monthly_rate,
                        term_min=r.term_min,
                        term_max=r.term_max,
                        amount_max=r.amount_max,
                        valid_date=today,
                        fetched_at=now,
                        is_campaign=r.is_campaign,
                        campaign_note=r.campaign_note,
                    )
                )
            session.commit()

        changed = record_rate_changes(
            "loan",
            {(r.institution, r.loan_type): r.monthly_rate for r in records},
            run_id=run_id,
        )
        if changed:
            logger.info("loan_rates: %s oran değişimi kaydedildi", changed)
        self._persist_reference_quotes(run_id, today, now)

    def _persist_reference_quotes(self, run_id: int, today: date, now: datetime) -> None:
        """Bankanın kendi taksitini yazar VE bizim hesabımızla karşılaştırır.

        Karşılaştırmanın sonucu `source_runs`'a 'validate' aşaması olarak
        düşer. Sapma eşiği aşarsa status='failed' — koşu yine de başarılıdır
        (oranlar toplandı), ama Kayıtlar sekmesinde kırmızı bir satır kalır.
        Bu bilinçli: yanlış bir vergi oranı yüzünden veri toplamayı durdurmak
        aşırı tepki olur, ama sessiz geçmek de kabul edilemez.
        """
        if not self._reference_quotes:
            return
        with SessionLocal() as session:
            for q in self._reference_quotes:
                exists = session.execute(
                    select(LoanReferenceQuote).where(
                        LoanReferenceQuote.institution == q.institution,
                        LoanReferenceQuote.loan_type == q.loan_type,
                        LoanReferenceQuote.principal == q.principal,
                        LoanReferenceQuote.term_months == q.term_months,
                        LoanReferenceQuote.valid_date == today,
                    )
                ).scalar_one_or_none()
                if exists:
                    exists.monthly_rate = q.monthly_rate
                    exists.bank_installment = q.bank_installment
                    exists.bank_annual_cost_rate = q.bank_annual_cost_rate
                    exists.fetched_at = now
                    exists.run_id = run_id
                else:
                    session.add(
                        LoanReferenceQuote(
                            institution=q.institution,
                            loan_type=q.loan_type,
                            principal=q.principal,
                            term_months=q.term_months,
                            monthly_rate=q.monthly_rate,
                            bank_installment=q.bank_installment,
                            bank_annual_cost_rate=q.bank_annual_cost_rate,
                            valid_date=today,
                            fetched_at=now,
                            run_id=run_id,
                        )
                    )
            session.commit()

        for q in self._reference_quotes:
            try:
                kkdf, bsmv = resolve_loan_taxes(q.loan_type, today)
                cmp = compare_installment(
                    principal=q.principal,
                    monthly_rate=q.monthly_rate,
                    term_months=q.term_months,
                    kkdf=kkdf,
                    bsmv=bsmv,
                    bank_installment=q.bank_installment,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("referans karşılaştırma yapılamadı: %s", exc)
                continue
            detail = (
                f"{q.loan_type}: {q.principal:,.0f} TL / {q.term_months} ay -> "
                f"bizim {cmp.ours:,.2f} TL, bankanın {cmp.theirs:,.2f} TL "
                f"(fark %{cmp.deviation_pct:.4f})"
            )
            source = q.institution.lower()
            if cmp.within_tolerance:
                logger.info("loan_rates/%s doğrulama: %s", source, detail)
                self.record_source(source, phase="validate", status="ok", rows=1, error=detail)
            else:
                logger.error("loan_rates/%s DOĞRULAMA SAPMASI: %s", source, detail)
                self.record_source(
                    source,
                    phase="validate",
                    status="failed",
                    error=(
                        "Taksit hesabımız bankanın kendi tutarından sapıyor. "
                        "Muhtemel sebep: config/taxes.yaml'daki KKDF/BSMV oranı "
                        f"eskimiş olabilir. {detail}"
                    ),
                )


if __name__ == "__main__":
    result = LoanRateCollector().run()
    print(result)
