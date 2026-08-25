"""Katılım bankalarının YILLIK kâr payı oranları — mevduatla kıyaslanabilir sayı.

PLAN.md madde 2+3'ün çözümü. Bu modül, `collectors/profit_shares.py` ile
KARIŞTIRILMAMALI; ikisi bambaşka iki büyüklük topluyor:

    profit_shares.py     -> KÂR PAYLAŞIM ORANI (%93 = kârın %93'ü katılımcıya)
                            profit_share_ratios tablosuna yazar,
                            getiri hesabına ASLA girmez.
    participation_rates  -> YILLIK KÂR PAYI ORANI (%33,97 brüt yıllık)
                            deposit_rates tablosuna is_profit_share=True ile
                            yazar, faizle aynı boru hattından geçer.

Kullanıcının şikâyeti tam buydu: panel katılım bankalarında %93 gösteriyordu
ve bu, yanındaki bankaların %38'inin yanında **faiz gibi okunuyordu**. Veri
doğruydu, sunum yanıltıcıydı. Doğru çözüm paylaşım oranını getiriye
"çevirmek" (uydurma olurdu) değil, bankaların KENDİ yayınladığı yıllık oranı
bulmaktı. İkisi de yayınlıyormuş:

EMLAK KATILIM — `/Plugins/CalculateProfitShareRate`
    Sayfanın kendi hesaplama aracının uç noktası (data-service-url
    özniteliğinden bulundu). Tutar + vade + para birimi veriliyor,
    `GrossProfitShareYearly` / `NetProfitShareYearly` dönüyor. Tutar
    kademesi (SegmentName: Klasik/Altın/Platin/Platin+) oranı değiştiriyor.
    Bu bir BEKLENTİ: yeni açılacak hesap için bankanın öngörüsü.

KUVEYT TÜRK — `lastProfitShareRates`
    GERÇEKLEŞEN geçmiş oranlar (1/3/6/12 ay, TL/USD/EUR). Uç nokta adresi
    JS paketindeki `ApiEndpoints` nesnesinde `ck0d84?<HASH>` biçiminde
    duruyor ve hash dağıtımla birlikte değişiyor — bu yüzden adres KODA
    GÖMÜLMÜYOR, her koşuda sayfadan zincirleme keşfediliyor:
        sayfa -> magiclick.core.min.js -> ApiEndpoints -> uç nokta
    Hash sabitlenseydi banka bir kez yayın yaptığında toplayıcı sessizce
    404 almaya başlardı.

ORTAK SINIR — ikisi de TAAHHÜT DEĞİL. Emlak Katılım'ınki gelecek beklentisi,
Kuveyt Türk'ünki geçmiş gerçekleşme. Panel bunları `is_profit_share=True`
ile işaretliyor ve "Beklenen kar payı" diye etiketliyor; tasarım §07'nin
"katılım = beklenen, taahhüt değil" kuralı burada da geçerli.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime

from pydantic import BaseModel, model_validator
from sqlalchemy import select

from collectors import http
from collectors.base import Collector, ParseError, SanityCheckError
from store.clock import istanbul_today, utc_now
from store.db import SessionLocal
from store.models import DepositRate
from store.observability import record_rate_changes

logger = logging.getLogger(__name__)

# DİKKAT — HTTP başlıkları latin-1 ile kodlanır; Türkçe karakter içeren bir
# User-Agent, istek daha ağa çıkmadan UnicodeEncodeError ile patlar
# ("'ascii' codec can't encode character '\u015f'"). Bu hata canlı koşuda
# yakalandı (2026-08-25): config/sources.yaml'daki nezaket User-Agent'ı
# "kişisel kullanım" ifadesini taşıyor ve olduğu gibi kullanılamaz.
HEADERS = {
    "User-Agent": "finans-agent/0.1 (kisisel kullanim; efebarisaltinay@gmail.com)",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
REQUEST_DELAY_SECONDS = 1.5

# Bir katılım hesabının yıllık oranı için makul üst sınır. Bunun üzeri
# neredeyse kesin bir ayrıştırma hatasıdır (ondalık kayması gibi).
MAX_ANNUAL_RATE = 1.50   # %150


class ParticipationRateRecord(BaseModel):
    institution: str
    currency: str
    term_days: int
    amount_min: float
    amount_max: float | None
    annual_rate: float          # BRÜT yıllık, 0.3397 = %33,97
    segment: str | None = None

    @model_validator(mode="after")
    def _bounds(self):
        if not (0 <= self.annual_rate <= MAX_ANNUAL_RATE):
            raise ValueError(
                f"{self.institution}: yıllık oran bant dışında ({self.annual_rate})"
            )
        if self.amount_max is not None and self.amount_max < self.amount_min:
            raise ValueError(f"{self.institution}: amount_max < amount_min")
        return self


# ---------------------------------------------------------- Emlak Katılım --


class EmlakKatilimAnnualRateCollector(Collector):
    """Bankanın kendi kâr payı hesaplama aracından yıllık oran matrisi.

    KADEME SINIRLARI ÖRNEKLEMEYLE bulunuyor, çünkü uç nokta kademe
    tablosunu değil tek bir hesaplamayı döndürüyor. Sabit bir tutar
    merdiveni deneniyor ve her örnek KENDİ kademesi olarak yazılıyor:
    amount_min = örneklenen tutar, amount_max = bir sonraki örnek - 1.

    Bunun sonucu bilinçli olarak MUHAFAZAKÂR: iki örnek arasındaki bir
    tutar (ör. 750.000 TL) bir ALT örneğin oranını görür. Yani panel asla
    gerçekte alınamayacak kadar yüksek bir oran göstermez — ters yönde
    hata yapmak, kullanıcıyı yanlış bankaya yönlendirirdi.
    """

    name = "participation_rates"
    schema = ParticipationRateRecord
    default_source = "emlakkatilim"
    institution = "EMLAKKATILIM"

    URL = "https://www.emlakkatilim.com.tr/Plugins/CalculateProfitShareRate"
    REFERER = "https://www.emlakkatilim.com.tr/tr/hesaplama-araclari"

    # Sayfadaki vade seçicisinin kendi değerleri (GÜN cinsinden).
    TERMS = (31, 91, 180, 364)
    # Sayfadaki para birimi seçicisinin kendi kodları.
    CURRENCIES = {0: "TRY", 1: "USD", 19: "EUR"}
    # Tutar merdiveni: kademe geçişlerini yakalayacak kadar sık, siteyi
    # yormayacak kadar seyrek. Canlı doğrulamada segment sınırları bu
    # aralıklara düşüyor (Klasik -> Altın -> Platin -> Platin+).
    TL_AMOUNTS = (10_000, 100_000, 500_000, 1_000_000, 2_500_000)
    FX_AMOUNTS = (1_000, 50_000)

    def fetch(self) -> bytes:
        results: list[dict] = []
        for fec, currency in self.CURRENCIES.items():
            amounts = self.TL_AMOUNTS if currency == "TRY" else self.FX_AMOUNTS
            for amount in amounts:
                for term in self.TERMS:
                    time.sleep(REQUEST_DELAY_SECONDS)
                    payload = {
                        "LanguageId": 1,
                        "Money": amount,
                        "Fec": fec,
                        "MaturityTerm": term,
                    }
                    try:
                        resp = http.post(
                            self.URL,
                            headers={**HEADERS, "Referer": self.REFERER},
                            data=payload,
                            timeout=25,
                            follow_redirects=True,
                        )
                        resp.raise_for_status()
                        body = resp.json()
                    except Exception as exc:  # noqa: BLE001 - tek hücre tümünü düşürmesin
                        logger.warning(
                            "emlakkatilim %s/%s/%s gün: %s", currency, amount, term, exc
                        )
                        continue
                    results.append(
                        {"currency": currency, "amount": amount, "term": term, "body": body}
                    )
        return json.dumps(results).encode("utf-8")

    def parse(self, raw: bytes) -> list[ParticipationRateRecord]:
        cells = json.loads(raw)
        # (para birimi, vade) -> tutara göre sıralı sonuçlar
        grouped: dict[tuple[str, int], list[dict]] = {}
        for cell in cells:
            body = cell.get("body") or {}
            if not body.get("Success"):
                # Banka "bu kombinasyon için hesaplama yok" diyebiliyor;
                # uydurmak yerine atlanır.
                continue
            data = body.get("Data") or {}
            rate = data.get("GrossProfitShareYearly")
            if rate is None:
                continue
            grouped.setdefault((cell["currency"], cell["term"]), []).append(
                {
                    "amount": float(cell["amount"]),
                    "rate": float(rate) / 100,
                    "segment": data.get("SegmentName"),
                }
            )

        records: list[ParticipationRateRecord] = []
        for (currency, term), rows in grouped.items():
            rows.sort(key=lambda r: r["amount"])
            for index, row in enumerate(rows):
                # Üst sınır: bir sonraki örneğin bir altı. Son örnekte
                # sınır yok (daha büyük tutarlar da en az bu oranı alır).
                upper = (
                    rows[index + 1]["amount"] - 1 if index + 1 < len(rows) else None
                )
                records.append(
                    ParticipationRateRecord(
                        institution=self.institution,
                        currency=currency,
                        term_days=term,
                        amount_min=row["amount"],
                        amount_max=upper,
                        annual_rate=row["rate"],
                        segment=row["segment"],
                    )
                )
        if not records:
            raise ParseError("Emlak Katılım kâr payı hesaplayıcısından tek satır alınamadı")
        return records

    def sanity_check(self, records: list[ParticipationRateRecord]) -> None:
        _shared_sanity_check(records)

    def persist(self, records: list[ParticipationRateRecord], run_id: int) -> None:
        _persist_as_deposit_rates(records, run_id, self.name)


# ------------------------------------------------------------ Kuveyt Türk --


class KuveytTurkAnnualRateCollector(Collector):
    """GERÇEKLEŞEN kâr payı oranları — geçmiş, taahhüt değil.

    Uç nokta adresi keşfedilerek bulunuyor (bkz. modül docstring'i). Zincir
    üç adım: sayfa -> JS paketi -> ApiEndpoints. Her adım kendi hatasını
    verir ki bir gün kırıldığında hangi halkanın koptuğu belli olsun.
    """

    name = "participation_rates_kt"
    schema = ParticipationRateRecord
    default_source = "kuveytturk"
    institution = "KUVEYTTURK"

    PAGE = "https://www.kuveytturk.com.tr/hesaplama-araclari/kar-payi-hesaplama"
    BASE = "https://www.kuveytturk.com.tr/"
    ENDPOINT_KEY = "lastProfitShareRates"

    _BUNDLE_RE = re.compile(r'src="(/magiclick\.core\.min\.js[^"]*)"')
    _ENDPOINT_RE = re.compile(
        r'lastProfitShareRates\s*:\s*"([^"]+)"'
    )

    # Yanıttaki ay cinsinden vade -> projedeki gün ölçeği. Mevduat tarafıyla
    # aynı normalizasyon (bkz. profit_shares.TERM_DAYS_BY_LABEL).
    TERM_DAYS_BY_MONTH = {1: 31, 3: 92, 6: 182, 12: 365}
    CURRENCY_FIELDS = {"TRY": "GrossTL", "USD": "GrossUSD", "EUR": "GrossEUR"}

    def fetch(self) -> bytes:
        page = http.get(
            self.PAGE,
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=25,
            follow_redirects=True,
        )
        page.raise_for_status()
        bundle_match = self._BUNDLE_RE.search(page.text)
        if not bundle_match:
            raise ParseError(
                "Kuveyt Türk sayfasında magiclick.core.min.js bulunamadı — "
                "site altyapısı değişmiş olabilir"
            )

        time.sleep(REQUEST_DELAY_SECONDS)
        bundle = http.get(
            self.BASE.rstrip("/") + bundle_match.group(1),
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=40,
            follow_redirects=True,
        )
        bundle.raise_for_status()
        endpoint_match = self._ENDPOINT_RE.search(bundle.text)
        if not endpoint_match:
            raise ParseError(
                "JS paketinde ApiEndpoints.lastProfitShareRates anahtarı yok — "
                "uç nokta adı değişmiş olabilir"
            )
        endpoint = endpoint_match.group(1)

        time.sleep(REQUEST_DELAY_SECONDS)
        resp = http.get(
            self.BASE + endpoint,
            headers={**HEADERS, "Referer": self.PAGE},
            timeout=25,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return json.dumps({"endpoint": endpoint, "rates": resp.json()}).encode("utf-8")

    def parse(self, raw: bytes) -> list[ParticipationRateRecord]:
        body = json.loads(raw)
        rows = body.get("rates") or []
        records: list[ParticipationRateRecord] = []
        for row in rows:
            months = row.get("MaturityTerm")
            term_days = self.TERM_DAYS_BY_MONTH.get(months)
            if term_days is None:
                continue
            for currency, field in self.CURRENCY_FIELDS.items():
                rate = _tr_number(row.get(field))
                if rate is None:
                    continue
                records.append(
                    ParticipationRateRecord(
                        institution=self.institution,
                        currency=currency,
                        term_days=term_days,
                        # Uç nokta tutar kademesi vermiyor: gerçekleşen oran
                        # tüm bakiyeler için tek. Uydurma kademe yazılmıyor.
                        amount_min=0.0,
                        amount_max=None,
                        annual_rate=rate / 100,
                    )
                )
        if not records:
            raise ParseError("Kuveyt Türk gerçekleşen kâr payı oranı listesi boş")
        return records

    def sanity_check(self, records: list[ParticipationRateRecord]) -> None:
        _shared_sanity_check(records)

    def persist(self, records: list[ParticipationRateRecord], run_id: int) -> None:
        _persist_as_deposit_rates(records, run_id, self.name)


# ------------------------------------------------------------- ortak parça --


def _tr_number(raw) -> float | None:
    """"32,99" -> 32.99. Boş/None ise None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(".", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _shared_sanity_check(records: list[ParticipationRateRecord]) -> None:
    """TL oranı sıfırsa bu bir veri değil, boş bir yanıttır.

    Döviz tarafında %0'a yakın oranlar GERÇEK (canlı doğrulama: USD %0,70),
    o yüzden yalnızca TL için sıfır kontrolü yapılıyor — döviz satırlarını
    reddetmek doğru veriyi atmak olurdu.
    """
    try_rates = [r.annual_rate for r in records if r.currency == "TRY"]
    if try_rates and max(try_rates) <= 0:
        raise SanityCheckError("TL kâr payı oranlarının hepsi sıfır — yanıt boş olmalı")


def _persist_as_deposit_rates(
    records: list[ParticipationRateRecord], run_id: int, collector_name: str
) -> None:
    """deposit_rates tablosuna is_profit_share=True ile yazar.

    Neden aynı tabloya: bu artık faizle AYNI ölçekte bir yıllık orandır ve
    aynı hesaplama boru hattından (stopaj -> net getiri -> yıllık net %)
    geçmesi gerekir. Ayrı bir tabloda tutmak, panelde ikinci bir hesaplama
    yolu açardı ve iki yol kaçınılmaz olarak ayrışırdı.

    `is_profit_share` bayrağı ayrımı koruyor: panel bu satırları
    "Beklenen kar payı" diye etiketliyor ve taahhüt olmadığını yazıyor.
    """
    now = utc_now()
    today = istanbul_today()
    institutions = {r.institution for r in records}

    with SessionLocal() as session:
        # Bugüne ait ESKİ satırları temizle: bankanın kademe yapısı
        # değiştiyse (bir segment kalktıysa) eski satır ortada kalmasın.
        keys = {
            (r.institution, r.currency, r.term_days, r.amount_min) for r in records
        }
        existing = session.execute(
            select(DepositRate).where(
                DepositRate.institution.in_(institutions),
                DepositRate.valid_date == today,
                DepositRate.is_profit_share.is_(True),
            )
        ).scalars().all()
        for row in existing:
            if (row.institution, row.currency, row.term_days, float(row.amount_min)) not in keys:
                session.delete(row)

        for r in records:
            found = session.execute(
                select(DepositRate).where(
                    DepositRate.institution == r.institution,
                    DepositRate.currency == r.currency,
                    DepositRate.term_days == r.term_days,
                    DepositRate.amount_min == r.amount_min,
                    DepositRate.valid_date == today,
                )
            ).scalar_one_or_none()
            if found:
                found.annual_rate = r.annual_rate
                found.amount_max = r.amount_max
                found.is_profit_share = True
                found.fetched_at = now
                found.run_id = run_id
                continue
            session.add(
                DepositRate(
                    institution=r.institution,
                    currency=r.currency,
                    term_days=r.term_days,
                    amount_min=r.amount_min,
                    amount_max=r.amount_max,
                    annual_rate=r.annual_rate,
                    is_profit_share=True,
                    valid_date=today,
                    fetched_at=now,
                    run_id=run_id,
                )
            )
        session.commit()

    changed = record_rate_changes(
        "deposit",
        {
            (r.institution, f"{r.currency}/{r.term_days}/{r.amount_min:.0f}"): r.annual_rate
            for r in records
        },
        run_id=run_id,
    )
    if changed:
        logger.info("%s: %s kâr payı oranı değişimi kaydedildi", collector_name, changed)


if __name__ == "__main__":
    print(EmlakKatilimAnnualRateCollector().run())
    print(KuveytTurkAnnualRateCollector().run())
