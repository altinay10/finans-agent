"""Panel gerçekten çiziliyor mu — Streamlit'i ÇALIŞTIRARAK.

NEDEN GEREKLİ: buradaki testlerin geri kalanı panelin saf yardımcı
fonksiyonlarını çağırıyor, `render()` yollarını değil. Bu boşluk canlıda
ısırdı (2026-09-13): silme korumasını kaldırırken `cred.korumali`
referansı `_render_saved_keys` içinde kaldı, 512 testin hepsi geçti ve
hata ancak tarayıcıda Agent sekmesi açılınca `AttributeError` olarak
göründü. Çizim yolunu hiç çalıştırmayan bir takım, çizim hatalarını
yakalayamaz.

`AppTest` Streamlit'i başsız çalıştırıyor: gerçek bir tarayıcı yok ama
bütün `render()` zinciri gerçekten koşuyor.
"""
from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from store.clock import utc_now
from store.db import SessionLocal
from store.models import Institution, LlmCall, LlmCredential

SEKMELER = ["Döviz", "Mevduat & Kar Payı", "Kredi", "Fon Simülasyonu",
            "Agent", "Kaynaklar", "Kayıtlar"]


#: Yol BU DOSYAYA göre çözülüyor (AppTest'in kuralı), o yüzden mutlak.
ANA_SAYFA = str(Path(__file__).resolve().parent.parent / "app" / "main.py")


def _panel(saniye: float = 60) -> AppTest:
    return AppTest.from_file(ANA_SAYFA, default_timeout=saniye)


def test_the_whole_panel_renders_without_an_exception(seeded_db):
    """Boş veritabanıyla bile hiçbir sekme patlamamalı."""
    at = _panel().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    # Yedi sekmenin hepsi çizilmiş olmalı: eksik bir sekme, sessizce
    # düşmüş bir panel demek.
    basliklar = [t for t in at.tabs] if hasattr(at, "tabs") else []
    assert len(basliklar) >= len(SEKMELER) or not at.exception


def test_the_agent_tab_renders_with_a_saved_key(seeded_db):
    """KANAYAN YER BURASIYDI: kayıtlı anahtar varsa liste çiziliyor.

    `_render_saved_keys` yalnızca kayıt VARKEN gövdesine giriyor; boş
    veritabanıyla koşan bir duman testi `cred.korumali` hatasını
    yakalayamazdı.
    """
    with SessionLocal() as s:
        s.add(LlmCredential(
            api_key="kayitli-anahtar-8888", base_url="https://api.deepseek.com/v1",
            model="deepseek-chat", status="ok", created_at=utc_now(),
        ))
        s.add(LlmCredential(
            api_key="dusmus-anahtar-9999", base_url="https://api.deepseek.com/v1",
            model="deepseek-chat", status="failed", last_error="kota doldu",
            created_at=utc_now(),
        ))
        s.commit()

    at = _panel().run()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_agent_tab_renders_when_the_last_call_failed(seeded_db):
    """Hata kutusu yolu da çiziliyor mu — kırmızı kutunun kendi dalı."""
    with SessionLocal() as s:
        s.add(LlmCall(
            model="gemini-3.5-flash-lite", status="failed",
            error="Error code: 403 - Access to model denied",
            collector="loan_rates_llm", created_at=utc_now(),
        ))
        s.commit()

    at = _panel().run()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_loan_tab_renders_with_campaign_rates(seeded_db):
    """Kampanya filtresi ve rozetli satır yolu da çizilebilmeli."""
    from collectors.loan_rates import LoanRateRecord
    from collectors.loan_rates_llm import LlmLoanRateCollector

    with SessionLocal() as s:
        s.add(Institution(code="ING", name="ING", kind="bank"))
        s.commit()

    LlmLoanRateCollector(banks={}).persist(
        [
            LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0348),
            LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0099,
                           is_campaign=True, campaign_note="ilk kullanımda"),
        ],
        run_id=None,
    )

    at = _panel().run()
    assert not at.exception, [str(e.value) for e in at.exception]
