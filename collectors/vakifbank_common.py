"""VakıfBank'ın kendi public web sitesinin kullandığı, anonim (login'siz)
API akışı — DevTools'ta değil, `endpoints.js`/`modules.min.js` içindeki
istemci kodunun okunmasıyla bulundu, canlı doğrulandı (2026-08-23).

Akış, sitenin kendi tarayıcı istemcisinin yaptığının birebir aynısı: önce
`/plugins/getTokenCreditCard`'dan kısa ömürlü, anonim bir "oob"/"public"
bearer token alınır (CAPTCHA yok, giriş yok — herkese açık veri için), sonra
bu token'la `inbound.apigateway.vakifbank.com.tr` üzerindeki ilgili uç nokta
çağrılır. Bot-koruması bypass edilmiyor; sitenin kendi genel API sözleşmesi
kullanılıyor.
"""
from __future__ import annotations

import httpx

from collectors import http

TOKEN_URL = "https://www.vakifbank.com.tr/plugins/getTokenCreditCard"
API_BASE = "https://inbound.apigateway.vakifbank.com.tr:8443"

_HEADERS_COMMON = {
    "Origin": "https://www.vakifbank.com.tr",
    "Referer": "https://www.vakifbank.com.tr/tr",
    "User-Agent": "finans-agent/0.1",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def get_token(scope: str = "public") -> str:
    resp = http.post(
        TOKEN_URL,
        headers={**_HEADERS_COMMON, "X-INTERNAL-REQ": "1", "scope": scope},
        content="{}",
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def call(path: str, payload: dict, scope: str = "public", timeout: float = 15) -> httpx.Response:
    token = get_token(scope)
    return http.post(
        f"{API_BASE}{path}",
        headers={**_HEADERS_COMMON, "Authorization": f"Bearer {token}", "scope": scope},
        json=payload,
        timeout=timeout,
    )
