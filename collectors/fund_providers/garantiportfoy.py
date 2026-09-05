"""Garanti BBVA Portföy — anonim bearer token + JSON seri uç noktası.

DİKKAT: dönen seri BİRİM PAY FİYATI DEĞİL, "1.000 TL yatırılsaydı"
endeksidir (bkz. base.py modül docstring'i, `price_is_unit_value`).
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
    epoch_ms_to_istanbul_date,
)

logger = logging.getLogger(__name__)


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

    def fetch(self, fund: FundSpec, *, known_dates: frozenset[date] = frozenset()) -> str:
        # `known_dates` yok sayılır: uç nokta tüm aralığı tek istekte veriyor.
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
