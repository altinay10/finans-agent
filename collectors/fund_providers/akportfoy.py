"""Ak Portföy — fiyat serisi sayfaya gömülü (`var fundVals = {...}`).

API çağrısı, token veya CSRF yok; sitede robots.txt de yok (HTTP 404).
2018'den bugüne ~2160 günlük kapanış fiyatı, tek istekte.
"""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import date

from collectors import http
from collectors.base import ParseError
from collectors.fund_providers.base import (
    EQUITY_HEAVY_MARKER,
    HEADERS,
    FundPriceProvider,
    FundSeries,
    FundSpec,
    ResolvedFund,
    epoch_ms_to_istanbul_date,
)

logger = logging.getLogger(__name__)


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

    def fetch(self, fund: FundSpec, *, known_dates: frozenset[date] = frozenset()) -> str:
        # `known_dates` yok sayılır: sayfa tüm geçmişi tek istekte veriyor.
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
