"""Banka kur uç noktaları — dokümante edilmemiş iç servisler (bkz. §02).

TOPLAMA SINIRI (kullanıcı kararı, 2026-08-24): robots.txt kısıtları bu
projede uygulanmıyor. Gerekçe kayıtlı: kişisel kullanım, günde birkaç istek,
herkese açık ve kimlik doğrulaması olmayan veri. Bu kararla Akbank ve Ziraat
açıldı; ikisinin de uç noktası robots.txt'in `/_layouts*` yasağındaydı.
Her kaynağın `robots_override` alanı config/sources.yaml'da işaretli.

HÂLÂ AÇILMAYANLAR ve nedenleri — bunlar robots meselesi DEĞİL:
  İş Bankası : WAF, normal tarayıcı gezintisinde bile blok sayfası döndürüyor.
               Aşmak parmak izi taklidi gerektirir; bot-tespiti atlatma
               kapsamına girer, yapılmadı.
  Garanti    : Uç nokta doğru ama tam tarayıcı başlıklarıyla bile HTTP 500
               (nginx) dönüyor — cihaz/oturum çerezi gerektiriyor gibi.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import yaml
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from collectors import http, vakifbank_common
from collectors.base import Collector, ParseError, SanityCheckError
from store.db import REPO_ROOT, SessionLocal
from store.models import FxQuote

logger = logging.getLogger(__name__)

# Bankalar zaman damgalarını YEREL saatle (Europe/Istanbul) yayınlıyor ve
# hiçbirinde saat dilimi eki yok. Bunları doğrudan UTC diye etiketlemek —
# eski hâlin yaptığı buydu — kotasyonu üç saat kaydırır: yaz saatinde taze
# bir kur "3 saat sonrasından" gelmiş gibi görünür, tazelik hesabı negatif
# yaşlar üretir. Önce İstanbul saatiyle yorumla, sonra UTC'ye çevir.
ISTANBUL = ZoneInfo("Europe/Istanbul")

SANITY_BAND = {"USD": (10.0, 200.0), "EUR": (10.0, 220.0)}
MAX_DAILY_JUMP = 0.20
HEADERS = {"User-Agent": "finans-agent/0.1"}


class BankFxRecord(BaseModel):
    institution: str
    currency: str
    buy: float
    sell: float
    quoted_at: datetime
    quoted_at_is_estimated: bool = False

    @model_validator(mode="after")
    def _buy_lt_sell(self):
        if self.buy <= 0 or self.sell <= 0:
            raise ValueError(f"{self.institution}/{self.currency}: alış/satış pozitif olmalı")
        if self.buy > self.sell:
            raise ValueError(f"{self.institution}/{self.currency}: alış satıştan büyük olamaz")
        return self


def _load_active_endpoints() -> dict[str, dict]:
    with open(REPO_ROOT / "config" / "sources.yaml", encoding="utf-8") as f:
        sources = yaml.safe_load(f)
    endpoints = sources.get("fx_endpoints", {})
    return {k: v for k, v in endpoints.items() if v.get("status") == "active"}


def _istanbul_to_utc(naive: datetime) -> datetime:
    """Saat dilimi eki olmayan, İstanbul yerel saatiyle verilmiş damgayı UTC'ye çevirir."""
    return naive.replace(tzinfo=ISTANBUL).astimezone(timezone.utc)


def _tl_to_float(s: str) -> float:
    """'47,215000 TL' / '47,34217' -> 47.215 — TR ondalık virgülü, TL son eki."""
    s = s.replace("TL", "").strip()
    s = s.replace(".", "").replace(",", ".")
    return float(s)


class CepteTebParser:
    """teb: GET JSON, doğrulandı 2026-08-23. Alanlar: paraKodu, tebAlis, tebSatis, fiyatZaman."""

    institution = "TEB"
    currencies = ("USD", "EUR")

    def fetch(self) -> httpx.Response:
        return http.get(
            "https://www.cepteteb.com.tr/services/GetGunlukDovizKur",
            headers={**HEADERS, "Referer": "https://www.cepteteb.com.tr/"},
            timeout=15,
        )

    def parse(self, raw: str) -> list[BankFxRecord]:
        body = json.loads(raw)
        records = []
        for row in body.get("result", []):
            code = row.get("paraKodu")
            if code not in self.currencies:
                continue
            buy, sell = row.get("tebAlis"), row.get("tebSatis")
            if not buy or not sell:
                continue
            quoted_at = _istanbul_to_utc(
                datetime.strptime(row["fiyatZaman"], "%d/%m/%Y %H:%M:%S")
            )
            records.append(
                BankFxRecord(
                    institution=self.institution, currency=code, buy=buy, sell=sell, quoted_at=quoted_at
                )
            )
        return records


class VakifBankParser:
    """vakifbank: iki adımlı token akışı, doğrulandı 2026-08-23 (bkz. vakifbank_common.py).

    Alan adları kafa karıştırıcı: 'SaleRate' < 'PurchaseRate' — Ziraat'in aynı
    andaki ALIŞ/SATIŞ değerleriyle çapraz kontrol edilerek SaleRate=banka
    alış, PurchaseRate=banka satış olduğu doğrulandı.
    """

    institution = "VAKIFBANK"
    currencies = ("USD", "EUR")

    def fetch(self) -> httpx.Response:
        return vakifbank_common.call("/marketPrices", {}, scope="oob")

    def parse(self, raw: str) -> list[BankFxRecord]:
        # BOŞ/JSON OLMAYAN GÖVDE KORUMASI. Çıplak `json.loads` canlıda
        # `Expecting value: line 1 column 1 (char 0)` yazıyordu (source_runs,
        # 2026-09-08) ve bu mesaj hiçbir şey anlatmıyor: gövde boş mu geldi,
        # token akışı mı düştü, araya bir engel sayfası mı girdi — hepsi
        # aynı görünüyor. Asıl kazanç sağlamlık değil, TEŞHİS EDİLEBİLİRLİK;
        # gövdenin başı hataya yazılınca sebep tek bakışta anlaşılıyor.
        if not raw or not raw.strip():
            raise ParseError("VakıfBank boş gövde döndürdü")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"VakıfBank yanıtı JSON değil ({exc.msg}); ilk 80 karakter: {raw[:80]!r}"
            ) from exc
        records = []
        for row in body.get("Data", {}).get("Currency", []):
            code = row.get("CurrencyCode")
            if code not in self.currencies:
                continue
            buy, sell = float(row["SaleRate"]), float(row["PurchaseRate"])
            quoted_at = _istanbul_to_utc(
                datetime.strptime(row["RateDate"], "%Y-%m-%dT%H:%M:%S")
            )
            records.append(
                BankFxRecord(
                    institution=self.institution, currency=code, buy=buy, sell=sell, quoted_at=quoted_at
                )
            )
        return records


class EnparaParser:
    """enpara: GET HTML, sunucu tarafında render edilmiş tablo. Doğrulandı 2026-08-23.

    Sayfa kendi zaman damgasını vermiyor — quoted_at_is_estimated=True.
    """

    institution = "ENPARA"
    currencies = ("USD", "EUR")
    _row_re = re.compile(
        r'table-item (USD|EUR)"[^>]*>\s*<span>[^<]*</span>\s*<span>([^<]+)</span>\s*<span>([^<]+)</span>'
    )

    def fetch(self) -> httpx.Response:
        return http.get(
            "https://www.enpara.com/hesaplar/doviz-ve-altin-kurlari", headers=HEADERS, timeout=15
        )

    def parse(self, raw: str) -> list[BankFxRecord]:
        now = datetime.now(timezone.utc)
        records = []
        for code, buy_s, sell_s in self._row_re.findall(raw):
            records.append(
                BankFxRecord(
                    institution=self.institution,
                    currency=code,
                    buy=_tl_to_float(buy_s),
                    sell=_tl_to_float(sell_s),
                    quoted_at=now,
                    quoted_at_is_estimated=True,
                )
            )
        return records


class EmlakKatilimParser:
    """emlakkatilim: GET HTML, /tr/tum-kurlarimiz tablosu. Doğrulandı 2026-08-23.

    Sayfa kendi zaman damgasını vermiyor — quoted_at_is_estimated=True.
    """

    institution = "EMLAKKATILIM"
    currencies = ("USD", "EUR")
    _row_re = re.compile(r"<tr><td>(USD|EUR)</td><td>([^<]+)</td><td>([^<]+)</td></tr>")

    def fetch(self) -> httpx.Response:
        return http.get(
            "https://www.emlakkatilim.com.tr/tr/tum-kurlarimiz", headers=HEADERS, timeout=15
        )

    def parse(self, raw: str) -> list[BankFxRecord]:
        now = datetime.now(timezone.utc)
        records = []
        for code, buy_s, sell_s in self._row_re.findall(raw):
            records.append(
                BankFxRecord(
                    institution=self.institution,
                    currency=code,
                    buy=_tl_to_float(buy_s),
                    sell=_tl_to_float(sell_s),
                    quoted_at=now,
                    quoted_at_is_estimated=True,
                )
            )
        return records


class YapiKrediParser:
    """yapikredi: POST JSON, doğrulandı 2026-08-23.

    Sayfanın kendi `$.Page.LoadMainCurrencies()` çağrısı — parametresiz,
    çerezsiz, token'sız. robots.txt 'Allow: /'. Kendi `lastUpdate` saatini
    verdiği için quoted_at TAHMİNİ DEĞİL (diğer HTML kaynaklarının aksine),
    ama saat İstanbul yerel saatidir — UTC'ye çevriliyor.

    Not: bu kurlar bankanın gişe/nakit kurudur, makas geniştir (canlı örnek
    USD 46,58 / 49,23). TCMB referansıyla kıyaslarken bu beklenen bir farktır.
    """

    institution = "YAPIKREDI"
    currencies = ("USD", "EUR")
    URL = "https://www.yapikredi.com.tr/_ajaxproxy/general.aspx/LoadMainCurrencies"
    REFERER = "https://www.yapikredi.com.tr/bireysel-bankacilik/hesaplama-araclari/doviz-hesaplama"

    def fetch(self) -> httpx.Response:
        return http.post(
            self.URL,
            headers={
                **HEADERS,
                "Content-Type": "application/json; charset=UTF-8",
                "Referer": self.REFERER,
                "X-Requested-With": "XMLHttpRequest",
            },
            json={},
            timeout=20,
        )

    def parse(self, raw: str) -> list[BankFxRecord]:
        body = json.loads(raw)
        records = []
        for row in body.get("d", []):
            code = row.get("code")
            if code not in self.currencies:
                continue  # XAU (altın) bu projede kapsam dışı
            try:
                buy, sell = float(row["buy"]), float(row["sell"])
                quoted_at = _istanbul_to_utc(
                    datetime.strptime(row["lastUpdate"], "%d.%m.%Y %H:%M:%S")
                )
            except (KeyError, TypeError, ValueError):
                continue
            records.append(
                BankFxRecord(
                    institution=self.institution,
                    currency=code,
                    buy=buy,
                    sell=sell,
                    quoted_at=quoted_at,
                )
            )
        return records


class AkbankParser:
    """akbank: POST JSON, doğrulandı 2026-08-24.

    Sayfanın kendi `exchangeRateCalculator.get()` çağrısı; tek parametre
    `kurTuru: '8'` (web kanalı). Kendi güncelleme saatini
    (`KurGuncellemeZamani`, İstanbul yereli) verdiği için quoted_at
    tahmini DEĞİL.

    `DovizAlis`/`DovizSatis` bankanın alış/satışıdır; `EfektifAlis`/
    `EfektifSatis` nakit (efektif) kurudur. Panelde döviz hesabı kuru
    kıyaslandığı için Doviz* alanları kullanılıyor.
    """

    institution = "AKBANK"
    currencies = ("USD", "EUR")
    URL = "https://www.akbank.com/_layouts/15/Akbank/CalcTools/Ajax.aspx/GetDovizKurlari"
    REFERER = "https://www.akbank.com/hesaplama-araclari"

    def fetch(self) -> httpx.Response:
        return http.post(
            self.URL,
            headers={
                **HEADERS,
                "Content-Type": "application/json; charset=UTF-8",
                "Referer": self.REFERER,
                "X-Requested-With": "XMLHttpRequest",
            },
            json={"kurTuru": "8"},
            timeout=25,
        )

    def parse(self, raw: str) -> list[BankFxRecord]:
        body = json.loads(raw)
        data = ((body.get("d") or {}).get("Data")) or {}
        records = []
        for row in data.get("DovizKurlari", []):
            code = row.get("AlfaKod")
            if code not in self.currencies:
                continue
            try:
                buy = float(str(row["DovizAlis"]).replace(",", "."))
                sell = float(str(row["DovizSatis"]).replace(",", "."))
                quoted_at = _istanbul_to_utc(
                    datetime.strptime(row["KurGuncellemeZamani"], "%d.%m.%Y %H:%M:%S")
                )
            except (KeyError, TypeError, ValueError):
                continue
            records.append(
                BankFxRecord(
                    institution=self.institution, currency=code, buy=buy, sell=sell,
                    quoted_at=quoted_at,
                )
            )
        return records


class ZiraatParser:
    """ziraat: ana sayfa "Ziraat Verileri" widget'ı. Doğrulandı 2026-08-24.

    Yanıt JSON ama içeriği HTML parçası: her para birimi bir <li> ve içinde
    "BANKA ALIŞ" / "BANKA SATIŞ" başlıklı iki <span>. Sayfa kendi kotasyon
    saatini vermiyor -> quoted_at_is_estimated=True.
    """

    institution = "ZIRAAT"
    currencies = ("USD", "EUR")
    URL = "https://www.ziraatbank.com.tr/tr/_layouts/15/Ziraat/HomePage/Ajax.aspx/GetZiraatVerileri"
    REFERER = "https://www.ziraatbank.com.tr/tr"

    # Widget para birimini Türkçe adıyla yazıyor.
    _NAME_MAP = {"AMERIKAN DOLARI": "USD", "EURO": "EUR"}
    _BLOCK_RE = re.compile(r"<li>(.*?)</li>", re.DOTALL)
    _TITLE_RE = re.compile(r"<h2>([^<]+)</h2>")
    _VALUE_RE = re.compile(r"<h3>([^<]+)</h3>\s*<span>([^<]+)</span>")

    def fetch(self) -> httpx.Response:
        return http.post(
            self.URL,
            headers={
                **HEADERS,
                "Content-Type": "application/json; charset=UTF-8",
                "Referer": self.REFERER,
                "X-Requested-With": "XMLHttpRequest",
            },
            json={},
            timeout=25,
        )

    def parse(self, raw: str) -> list[BankFxRecord]:
        body = json.loads(raw)
        markup = ((body.get("d") or {}).get("Data") or {}).get("Html", "")
        now = datetime.now(timezone.utc)
        records = []
        for block in self._BLOCK_RE.findall(markup):
            title = self._TITLE_RE.search(block)
            if not title:
                continue
            code = self._NAME_MAP.get(title.group(1).strip().upper())
            if code not in self.currencies:
                continue
            values = {
                label.strip().upper(): value.strip()
                for label, value in self._VALUE_RE.findall(block)
            }
            if "BANKA ALIŞ" not in values or "BANKA SATIŞ" not in values:
                continue
            records.append(
                BankFxRecord(
                    institution=self.institution,
                    currency=code,
                    buy=_tl_to_float(values["BANKA ALIŞ"]),
                    sell=_tl_to_float(values["BANKA SATIŞ"]),
                    quoted_at=now,
                    quoted_at_is_estimated=True,
                )
            )
        return records


class KuveytTurkParser:
    """kuveytturk: Finans Portalı'nın kendi JSON ucu. Doğrulandı 2026-09-04.

    Uç noktanın adresi sayfada YAZMIYOR; `magiclick.core.min.js` paketindeki
    `ApiEndpoints` nesnesinde `exchangeRates: "ck0d84?<hash>"` olarak duruyor.
    Aynı keşif deseni collectors/participation_rates.py'de kâr payı ucu için
    zaten kullanılıyor ve orada 2026-08-25'ten beri çalışıyor.

    NEDEN ÖNCE BİLİNEN ADRES DENENİYOR: paket 750 KB ve bu toplayıcı mesai
    saatlerinde SAATTE BİR koşuyor. Her koşuda paketi indirmek günde ~7,5 MB
    gereksiz trafik demekti (bkz. config/sources.yaml collection_etiquette).
    Bu yüzden son bilinen adres doğrudan denenir; yalnızca o düşerse paket
    indirilip adres yeniden keşfedilir. Hash döndüğünde kaynak kendi kendini
    onarır, normal günde tek istek atar.

    Kotasyon saati yayınlanmıyor -> quoted_at_is_estimated=True.
    """

    institution = "KUVEYTTURK"
    currencies = ("USD", "EUR")
    BASE = "https://www.kuveytturk.com.tr"
    PAGE = f"{BASE}/finans-portali/"
    # 2026-09-04'te keşfedilen adres. Kırıldığında _discover() devreye girer.
    KNOWN_ENDPOINT = "/ck0d84?C24AD4C0FDA76C73081889B634A8C039"

    _BUNDLE_RE = re.compile(r'src="(/magiclick\.core\.min\.js[^"]*)"')
    _ENDPOINT_RE = re.compile(r'exchangeRates\s*:\s*"([^"]+)"')

    def _discover(self) -> str:
        page = http.get(self.PAGE, headers=HEADERS, timeout=25, follow_redirects=True)
        page.raise_for_status()
        bundle_match = self._BUNDLE_RE.search(page.text)
        if not bundle_match:
            raise ParseError("Kuveyt Türk sayfasında magiclick.core.min.js bulunamadı")
        bundle = http.get(self.BASE + bundle_match.group(1), headers=HEADERS,
                          timeout=40, follow_redirects=True)
        bundle.raise_for_status()
        endpoint_match = self._ENDPOINT_RE.search(bundle.text)
        if not endpoint_match:
            raise ParseError("JS paketinde ApiEndpoints.exchangeRates anahtarı yok")
        return "/" + endpoint_match.group(1).lstrip("/")

    def fetch(self) -> httpx.Response:
        headers = {**HEADERS, "Referer": self.PAGE}
        try:
            resp = http.get(self.BASE + self.KNOWN_ENDPOINT, headers=headers,
                            timeout=25, follow_redirects=True)
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 - adres dönmüş olabilir, keşfe düş
            logger.info("fx_banks/kuveytturk: bilinen adres düştü (%s) — paketten aranıyor", exc)
        resp = http.get(self.BASE + self._discover(), headers=headers,
                        timeout=25, follow_redirects=True)
        resp.raise_for_status()
        return resp

    def parse(self, raw: str) -> list[BankFxRecord]:
        now = datetime.now(timezone.utc)
        records = []
        for row in json.loads(raw):
            code = row.get("CurrencyCode")
            if code not in self.currencies:
                continue
            buy, sell = row.get("BuyRate"), row.get("SellRate")
            if not buy or not sell:
                continue
            records.append(
                BankFxRecord(
                    institution=self.institution, currency=code,
                    buy=float(buy), sell=float(sell),
                    quoted_at=now, quoted_at_is_estimated=True,
                )
            )
        return records


PARSERS = {
    "teb": CepteTebParser(),
    "vakifbank": VakifBankParser(),
    "enpara": EnparaParser(),
    "emlakkatilim": EmlakKatilimParser(),
    "yapikredi": YapiKrediParser(),
    "akbank": AkbankParser(),
    "ziraat": ZiraatParser(),
    "kuveytturk": KuveytTurkParser(),
}


class BankFxCollector(Collector):
    name = "fx_banks"
    schema = BankFxRecord

    def __init__(self) -> None:
        super().__init__()
        self.active = _load_active_endpoints()

    def fetch(self) -> bytes:
        """Beş kurum tek koşuda çekiliyor; biri düşerse diğerleri yazılmaya devam eder.

        (Aynı desen collectors/deposit_rates.py ve loan_rates.py'de de var.)
        """
        payloads: dict[str, dict] = {}
        for key in self.active:
            parser = PARSERS.get(key)
            if parser is None:
                continue  # sources.yaml'da active ama henüz parser yazılmamış
            started = time.monotonic()
            try:
                # Bu bloktaki her HTTP isteği bu bankayla etiketlenir; çok
                # adımlı akışlarda (VakıfBank token -> veri) hangi halkanın
                # koptuğu ancak böyle görünür.
                with http.source(key):
                    resp = parser.fetch()
                    resp.raise_for_status()
                payloads[key] = {"ok": True, "body": resp.text}
                self.record_source(key, phase="fetch", status="ok",
                                   duration_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:  # noqa: BLE001 - tek kurum tüm koşuyu düşürmemeli
                logger.warning("fx_banks/%s: fetch başarısız: %s", key, exc)
                payloads[key] = {"ok": False, "error": str(exc)}
                self.record_source(key, phase="fetch", status="failed", error=str(exc),
                                   duration_ms=int((time.monotonic() - started) * 1000))
        return json.dumps(payloads).encode("utf-8")

    def parse(self, raw: bytes) -> list[BankFxRecord]:
        try:
            payloads: dict[str, dict] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError(str(exc)) from exc

        records: list[BankFxRecord] = []
        for key, entry in payloads.items():
            if not entry.get("ok"):
                continue
            try:
                parsed = PARSERS[key].parse(entry["body"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("fx_banks/%s: parse başarısız: %s", key, exc)
                self.record_source(key, phase="parse", status="failed", error=str(exc))
                continue
            if not parsed:
                logger.warning("fx_banks/%s: kayıt çıkmadı — sayfa şeması değişmiş olabilir", key)
                self.record_source(key, phase="parse", status="empty",
                                   error="sıfır kayıt — sayfa şeması değişmiş olabilir")
                continue
            self.record_source(key, phase="parse", status="ok", rows=len(parsed))
            records.extend(parsed)
        if not records:
            raise ParseError("Hiçbir kurumdan kur kaydı çıkarılamadı")
        return records

    def sanity_check(self, records: list[BankFxRecord]) -> None:
        with SessionLocal() as session:
            for r in records:
                low, high = SANITY_BAND.get(r.currency, (0, float("inf")))
                if not (low <= r.buy <= high and low <= r.sell <= high):
                    raise SanityCheckError(f"{r.institution}/{r.currency}: bant dışında")

                prev = session.execute(
                    select(FxQuote)
                    .where(FxQuote.institution == r.institution, FxQuote.currency == r.currency)
                    .order_by(FxQuote.quoted_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if prev is not None:
                    change = abs(r.sell - float(prev.sell)) / float(prev.sell)
                    if change > MAX_DAILY_JUMP:
                        raise SanityCheckError(f"{r.institution}/{r.currency}: günlük sıçrama > %20")

    def persist(self, records: list[BankFxRecord], run_id: int) -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal() as session:
            for r in records:
                exists = session.execute(
                    select(FxQuote).where(
                        FxQuote.institution == r.institution,
                        FxQuote.currency == r.currency,
                        FxQuote.quoted_at == r.quoted_at,
                    )
                ).scalar_one_or_none()
                if exists:
                    # Aynı kotasyon anı yeniden görüldü. Satırı silmiyoruz ama
                    # fetched_at'i tazeliyoruz: bu alan "bu değeri kaynakta EN SON
                    # ne zaman gördük" demek ve panelde "veri çekilme zamanı"
                    # olarak gösteriliyor. Güncellenmezse, kaynağı beş dakika önce
                    # kontrol etmiş olmamıza rağmen veri saatler öncesinden
                    # kalmış gibi görünür. buy/sell de yazılıyor ki kaynak aynı
                    # zaman damgasıyla bir düzeltme yayınlarsa yakalansın.
                    exists.buy = r.buy
                    exists.sell = r.sell
                    exists.fetched_at = now
                    exists.run_id = run_id
                    continue
                session.add(
                    FxQuote(
                        institution=r.institution,
                        currency=r.currency,
                        buy=r.buy,
                        sell=r.sell,
                        quoted_at=r.quoted_at,
                        fetched_at=now,
                        run_id=run_id,
                        quoted_at_is_estimated=r.quoted_at_is_estimated,
                    )
                )
            session.commit()


if __name__ == "__main__":
    result = BankFxCollector().run()
    print(result)
