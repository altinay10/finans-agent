"""Yapı Kredi Portföy — 129 fon, sayfadaki sayısal kimlikle JSON seri ucu.

Kullanıcı isteği (2026-09-05): "denizbank veya yapı kredinin api sistemi
var, bu api ile fon simülasyonunu geliştir."

BANKANIN KENDİ SİTESİ YETMİYOR — 2026-09-05'te bakıldı:
`yapikredi.com.tr/yatirimci-kosesi/fon-bilgileri/` 42 fonu kod + ad +
GÜNCEL FİYAT olarak sunucu-render tabloda veriyor, geçmiş seri yok ve
listesi zaten Yapı Kredi Portföy'ün alt kümesi. Bu yüzden doğrudan
portföy şirketine gidiliyor.

AKIŞ (üç adım, hepsi login'siz)
  1. sitemap.xml  -> 129 fon sayfası; adresin SON parçası fon kodudur
     (.../yatirim-fonlari/hisse-senedi-stratejisi/yub -> YUB).
  2. fon sayfası  -> `<div data-ajax-fund-detail="/getFundDetail/1573">`
     Sayısal kimlik yalnızca sayfada; koddan türetilemiyor.
  3. POST /getFundDetail/<id> gövdesi:
        {startDate, endDate, currencyType:"TL", selectedYear:"price",
         selectValue:{label,value}}
     -> data[0].lineChartData.graphData = {labels:["06 Eylül 2024",...],
                                           datasets:[{label:"YUB",data:[...]}]}
     Ayrıca data[0].name resmî fon adını, "(Hisse Senedi Yoğun Fon)"
     ibaresiyle birlikte verir — stopaj kararı buna bağlı.

  robots.txt: `Allow: /`, yalnızca `/Search/SearchData/` yasak. Bu akış
  kapsam dışında.

  TUZAK — GET ÇALIŞMAZ: `/getFundDetail/<id>` adresine GET atınca 404
  HTML'i döner. Yöntem POST olmak zorunda; boş gövdeyle bile 200 döner
  ama seri BOŞ gelir (`lineChartData.graphData: null`). Yani "200 aldım,
  demek ki çalışıyor" kontrolü burada yanıltıcıdır; seri ayrıca aranır.

  TUZAK — 30 GÜNDEN UZUN ARALIK SERİYİ AYLIĞA SEYRELTİR. Ölçüldü
  (2026-09-05, YUB): 28 gün -> 20 nokta (günlük), 31 gün -> 2 nokta,
  90 gün -> 4 nokta, 1095 gün -> 37 nokta. Yani tek bir uzun istekle
  günlük seri ALINAMAZ. Bu yüzden aşağıdaki pencere düzeni var: uzun
  aralıktan AYLIK OMURGA, panelin senaryolarının denk geldiği yerlerden
  28 GÜNLÜK PENCERELERLE günlük çözünürlük.

  TUZAK — ÖZEL FONLAR KİLİTLİ: `ozel-fonlar` altındaki sayfalar şifre
  ister (`isLocked`), seri gelmez. Katalog bunları ve tasfiye edilmiş
  fonları dışarıda bırakır; yine de biri elle eklenirse parse ParseError
  fırlatır ve o fon o koşuda düşer, diğerleri toplanmaya devam eder.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

from collectors import http
from collectors.base import ParseError
from collectors.fund_providers.base import (
    EQUITY_HEAVY_MARKER,
    HEADERS,
    HISTORY_DAYS,
    REQUEST_DELAY_SECONDS,
    FundPriceProvider,
    FundSeries,
    FundSpec,
    ResolvedFund,
)

logger = logging.getLogger(__name__)

# 30 günü GEÇEN aralıkta uç nokta seriyi aylığa seyreltiyor (yukarıdaki
# ölçüm). 28 gün, hem güvenli tarafta hem de bir pencerede ~20 iş günü.
WINDOW_DAYS = 28

# Panelin uzak senaryoları (app/panels/fund.py: SCENARIOS). Bu tarihlerin
# ETRAFINA günlük pencere açılır ki "6 ay" ve "1 yıl" senaryolarının
# başlangıç tarihi aylık omurgaya yuvarlanmasın. "1 ay" burada YOK; onu
# iki ardışık günlük pencere zaten kapsıyor (bkz. _windows).
SCENARIO_ANCHORS = (182, 365)

# Bir çapa penceresinin "zaten var" sayılması için gereken kayıtlı gün
# sayısı. Pencere 28 günlük ve ~20 iş günü içeriyor; 10 kayıtlı gün,
# o bölgenin günlük çözünürlükte toplandığını gösterir.
COVERED_THRESHOLD = 10

TURKISH_MONTHS = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12,
}


class YapiKrediPortfoyProvider(FundPriceProvider):
    key = "ykportfoy"
    label = "Yapı Kredi Portföy"
    price_is_unit_value = True

    BASE = "https://www.yapikrediportfoy.com.tr"
    SITEMAP_URL = BASE + "/sitemap.xml"

    _DETAIL_ID_RE = re.compile(r'data-ajax-fund-detail="(/getFundDetail/\d+)"')
    _LOC_RE = re.compile(r"<loc>([^<]+)</loc>")

    # Kilitli/ölü fonların adres parçaları — katalogdan çıkarılır.
    _EXCLUDED_SEGMENTS = ("tasfiye-edilen-fonlar", "ozel-fonlar")

    def __init__(self) -> None:
        self._directory: dict[str, ResolvedFund] | None = None

    # ------------------------------------------------------------ dizin --

    def directory(self) -> dict[str, ResolvedFund]:
        """Kod -> fon eşlemesi, sitemap'ten (tek HTTP isteği, önbellekli).

        Fon adı BURADA YOK; adresten yalnızca kod ve yol çıkar. Gerçek ad
        `fetch` sırasında JSON yanıtından okunur — resmî ad orada ve
        "(Hisse Senedi Yoğun Fon)" ibaresi de orada.
        """
        if self._directory is not None:
            return self._directory
        resp = http.get(self.SITEMAP_URL, timeout=30, headers=HEADERS, follow_redirects=True)
        resp.raise_for_status()
        index: dict[str, ResolvedFund] = {}
        for url in self._LOC_RE.findall(resp.text):
            if "/yatirim-fonlari/" not in url:
                continue
            yol = url.split(self.BASE, 1)[-1].strip("/")
            kod = yol.rsplit("/", 1)[-1].strip().upper()
            if not (2 <= len(kod) <= 6 and kod.isalnum()):
                continue
            index.setdefault(kod, ResolvedFund(
                code=kod, provider=self.key, ref="/" + yol, name=None,
            ))
        self._directory = index
        return index

    def resolve(self, code: str) -> ResolvedFund | None:
        code = (code or "").strip().upper()
        try:
            return self.directory().get(code)
        except Exception as exc:  # noqa: BLE001 - çözümleme başarısızlığı hata değil
            logger.warning("Yapı Kredi Portföy sitemap'i okunamadı: %s", exc)
            return None

    def catalog(self) -> list[ResolvedFund]:
        try:
            kayitlar = self.directory().values()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Yapı Kredi Portföy kataloğu okunamadı: %s", exc)
            return []
        return [
            k for k in kayitlar
            if not any(parca in k.ref for parca in self._EXCLUDED_SEGMENTS)
        ]

    # ---------------------------------------------------------- pencere --

    def _windows(self, today: date, known: frozenset[date]) -> list[tuple[date, date]]:
        """İstenecek tarih aralıkları.

        İlk sıradaki HER ZAMAN son 28 gün: gece koşusunun asıl işi seriyi
        güncel tutmak ve bu tek istekle oluyor. Geri kalanlar yalnızca
        elimizde o bölge için yeterli gün yoksa isteniyor; yoksa her gece
        aynı iki yıllık geçmiş boşuna çekilirdi.

        İKİNCİ GÜNLÜK PENCERE NEDEN VAR (canlı ölçüm, 2026-09-05): tek
        pencere -28 günde bitiyordu, panelin "1 ay" senaryosu ise -30
        gününü hedefliyor. Aradaki iki günlük boşluk yüzünden
        `nearest_prior_price` aylık omurgaya düşüyor ve senaryo 59 GÜNLÜK
        bir dönemle hesaplanıyordu — YUB'da başlangıç tarihi 2026-08-05
        yerine 2026-07-07 çıktı. Senaryonun adı ile hesabı tutmuyordu.
        """
        pencereler: list[tuple[date, date]] = [
            (today - timedelta(days=WINDOW_DAYS), today)
        ]

        def eksik(bas: date, bit: date) -> bool:
            return sum(1 for g in known if bas <= g <= bit) < COVERED_THRESHOLD

        # "1 ay" çapasının GÜVENLE içinde kaldığı ikinci günlük pencere.
        ikinci_bas = today - timedelta(days=2 * WINDOW_DAYS)
        ikinci_bit = today - timedelta(days=WINDOW_DAYS)
        if eksik(ikinci_bas, ikinci_bit):
            pencereler.append((ikinci_bas, ikinci_bit))

        for geri in SCENARIO_ANCHORS:
            capa = today - timedelta(days=geri)
            bas = capa - timedelta(days=WINDOW_DAYS // 2)
            bit = capa + timedelta(days=WINDOW_DAYS // 2)
            if eksik(bas, bit):
                pencereler.append((bas, bit))

        # Aylık omurga: grafiğin iskeleti. Elde 760 günü kabaca kapsayan
        # bir dağılım varsa tekrar istenmez.
        if sum(1 for g in known if g < today - timedelta(days=SCENARIO_ANCHORS[-1])) < 12:
            pencereler.append((today - timedelta(days=HISTORY_DAYS), today))
        return pencereler

    # ------------------------------------------------------------ çekim --

    def fetch(self, fund: FundSpec, *, known_dates: frozenset[date] = frozenset()) -> str:
        yol = fund.ref if fund.ref.startswith("/") else "/" + fund.ref.strip("/")
        page_url = self.BASE + yol
        page = http.get(page_url, timeout=30, headers=HEADERS, follow_redirects=True)
        page.raise_for_status()

        eslesme = self._DETAIL_ID_RE.search(page.text)
        if eslesme is None:
            raise ParseError(
                f"{fund.code}: '{yol}' sayfasında data-ajax-fund-detail yok — "
                "sayfa yapısı değişmiş ya da adres yanlış olabilir"
            )
        detail_url = self.BASE + eslesme.group(1)

        today = datetime.now().date()
        parcalar: list[dict] = []
        for index, (bas, bit) in enumerate(self._windows(today, known_dates)):
            time.sleep(REQUEST_DELAY_SECONDS)
            resp = http.post(
                detail_url,
                timeout=40,
                headers={**HEADERS, "Referer": page_url, "Content-Type": "application/json"},
                content=json.dumps(
                    {
                        "startDate": _as_iso(bas),
                        "endDate": _as_iso(min(bit, today)),
                        "currencyType": "TL",
                        "selectedYear": "price",
                        "selectValue": {"label": "1 Yıl", "value": 1},
                    }
                ),
            )
            resp.raise_for_status()
            parcalar.append(resp.json())
            del index

        return json.dumps({"code": fund.code.upper(), "parts": parcalar})

    def parse(self, fund: FundSpec, raw: str) -> FundSeries:
        body = json.loads(raw)
        noktalar: dict[date, float] = {}
        name: str | None = None
        sayfa_kodu: str | None = None

        for parca in body.get("parts") or []:
            satirlar = (parca or {}).get("data") or []
            if not satirlar:
                continue
            satir = satirlar[0] or {}
            name = name or satir.get("name")
            sayfa_kodu = sayfa_kodu or (satir.get("code") or "").strip().upper() or None
            grafik = (satir.get("lineChartData") or {}).get("graphData") or {}
            etiketler = grafik.get("labels") or []
            veri_setleri = grafik.get("datasets") or []
            if not etiketler or not veri_setleri:
                continue
            degerler = (veri_setleri[0] or {}).get("data") or []
            for etiket, deger in zip(etiketler, degerler):
                gun = _parse_turkish_date(etiket)
                if gun is None or deger is None:
                    continue
                try:
                    noktalar[gun] = float(deger)
                except (TypeError, ValueError):
                    continue

        # KOD DOĞRULAMASI — Garanti Portföy'de canlıda yaşanan sessiz veri
        # bozulmasının aynısı burada da mümkün: kayıt defterindeki adres
        # başka bir fonun sayfasını gösterirse seri o fonundur ama bizim
        # kodumuzla saklanırdı. Bir yatırım aracında bu, hiçbir yerde
        # patlamadığı için sayfa şeması değişmesinden daha tehlikelidir.
        if sayfa_kodu and sayfa_kodu != fund.code.upper():
            raise ParseError(
                f"{fund.code}: '{fund.ref}' sayfasının fon kodu {sayfa_kodu}. "
                f"Kayıt defteri yanlış fonu gösteriyor — seri YAZILMADI. "
                f"Fonu koduyla yeniden ekle."
            )

        if not noktalar:
            raise ParseError(
                f"{fund.code}: Yapı Kredi Portföy serisi boş — fon kilitli "
                "(özel fon), tasfiye edilmiş ya da uç noktanın gövde biçimi "
                "değişmiş olabilir"
            )

        temiz_ad = " ".join(html.unescape(name).split()) if name else fund.name
        is_equity_heavy = (
            EQUITY_HEAVY_MARKER in temiz_ad.casefold() if temiz_ad else None
        )
        return FundSeries(
            fund.code.upper(), temiz_ad, is_equity_heavy, sorted(noktalar.items())
        )


def _as_iso(gun: date) -> str:
    """Sitenin kendi ön yüzünün gönderdiği biçim: saat 13:00'e sabitlenmiş ISO.

    Ön yüz `setHours(13, 0, 0, 0)` yapıyor (assets/js/fund-detail.js). Saati
    gece yarısına almak sınır günlerin aralığın dışında kalmasına yol açıyor;
    aynı değeri göndermek en güvenlisi.
    """
    return f"{gun.isoformat()}T13:00:00.000Z"


def _parse_turkish_date(etiket: str) -> date | None:
    """'06 Eylül 2024' -> date(2024, 9, 6).

    Uç nokta tarihi epoch değil, TÜRKÇE AY ADIYLA metin olarak veriyor.
    `datetime.strptime` yerel dil ayarına (locale) bağlı olurdu ve sunucuda
    Türkçe locale kurulu olmayabilir; bu yüzden ay adları elle eşleniyor.
    """
    parcalar = (etiket or "").strip().split()
    if len(parcalar) != 3:
        return None
    gun_s, ay_s, yil_s = parcalar
    ay = TURKISH_MONTHS.get(ay_s.casefold())
    if ay is None:
        return None
    try:
        return date(int(yil_s), ay, int(gun_s))
    except ValueError:
        return None
