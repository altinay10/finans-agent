"""Sağlayıcı hazır ayarları — `llm/providers.py`.

NEDEN VAR: anahtar giriş formu `extra_body` kutusunu o an geçerli olan
ayardan dolduruyordu. `.env`'de Qwen için `{"enable_thinking": false}`
yazdığı için Gemini anahtarı denemek isteyen kullanıcı o alanı farkında
olmadan Gemini'ye gönderiyor ve anahtar doğru olsa bile "çalışmadı"
hatası alıyordu (kullanıcı bildirimi, 2026-09-12).
"""
from __future__ import annotations

import pytest

from llm import providers


def test_only_qwen_needs_an_extra_body():
    """ASIL DÜZELTME: diğer sağlayıcılara ek gövde GÖNDERİLMEMELİ.

    OpenAI uyumlu uç noktalar standart şemayı kabul ediyor; şemada
    karşılığı olmayan bir alan eklemek isteği reddettiriyor.
    """
    dolu = {p.label: p.extra_body for p in providers.PROVIDERS if p.extra_body}
    assert list(dolu) == ["Qwen (Alibaba DashScope)"], dolu
    assert dolu["Qwen (Alibaba DashScope)"] == {"enable_thinking": False}


def test_every_listed_provider_has_a_usable_endpoint_and_model():
    for p in providers.PROVIDERS:
        if p.label == providers.CUSTOM:
            continue
        assert p.base_url.startswith("https://"), p.label
        assert p.model, p.label
        assert p.note, f"{p.label}: gerekçe yazılmamış"


def test_the_custom_option_fills_nothing():
    """'Diğer / elle' kullanıcının yazdıklarını EZMEMELİ."""
    ozel = providers.by_label(providers.CUSTOM)
    assert ozel.base_url == "" and ozel.model == "" and ozel.extra_body == {}


@pytest.mark.parametrize(
    "base_url, beklenen",
    [
        ("https://generativelanguage.googleapis.com/v1beta/openai/", "Google Gemini"),
        ("https://api.openai.com/v1", "OpenAI (ChatGPT)"),
        ("https://api.anthropic.com/v1/", "Anthropic (Claude)"),
        ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "Qwen (Alibaba DashScope)"),
        ("https://api.deepseek.com/v1", "DeepSeek"),
        # Sürüm eki değişse bile ana bilgisayar adı sağlayıcıyı belirler.
        ("https://api.deepseek.com/v3", "DeepSeek"),
        ("http://localhost:11434/v1", providers.CUSTOM),
        ("", providers.CUSTOM),
    ],
)
def test_the_form_opens_on_the_provider_already_in_use(base_url, beklenen):
    """Form açılırken doğru sağlayıcı seçili gelsin.

    Yanlış bir sağlayıcıyı seçili göstermek, kullanıcının farkında olmadan
    başka bir uç noktaya anahtar göndermesine yol açardı — eşleşme yoksa
    'Diğer / elle' doğru cevap.
    """
    assert providers.guess_label(base_url) == beklenen


def test_an_unknown_label_falls_back_to_custom():
    """Bilinmeyen etikette patlamak yerine elle moda düşmeli."""
    assert providers.by_label("Olmayan Sağlayıcı").label == providers.CUSTOM
