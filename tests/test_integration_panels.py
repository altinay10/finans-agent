"""Panel (sunum katmanı) ve uçtan uca kullanıcı akışı entegrasyon testleri.

Streamlit'i çalıştırmadan panellerin saf yardımcı fonksiyonlarını ve
"anapara gir -> doğru rakam çık" akışını doğrular. Tarayıcı tarafındaki
görsel doğrulama ayrıca elle yapıldı (bkz. ILERLEME.md).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from config.loader import resolve_deposit_brackets, resolve_fund_withholding, resolve_loan_taxes
from core.deposit import DepositInput, resolve_withholding, single_term_return
from core.fund import nearest_prior_price, simulate
from core.loan import amortize
from core.models import FundSimInput, LoanInput
from store.db import SessionLocal
from pydantic import BaseModel

from store.models import DepositRate, FundPrice


class _DummySchema(BaseModel):
    """LLM testleri için asgari şema — ağa çıkmaz, sadece imza gerekli."""

    value: float = 0.0


def _add_deposit(term_days, rate, amount_min=0, amount_max=None, currency="TRY"):
    return DepositRate(
        institution="VAKIFBANK",
        currency=currency,
        term_days=term_days,
        amount_min=amount_min,
        amount_max=amount_max,
        annual_rate=rate,
        is_profit_share=False,
        valid_date=date.today(),
        fetched_at=datetime.now(timezone.utc),
    )


# --------------------------------------------- fon paneli: mevduat kıyası ----

def test_deposit_comparison_returns_none_without_data(seeded_db):
    from app.panels.fund import _deposit_comparison, _try_deposit_rates

    _try_deposit_rates.clear()
    assert _deposit_comparison(1_000_000, 365) is None


def test_deposit_comparison_uses_closest_term(seeded_db):
    from app.panels.fund import _deposit_comparison, _try_deposit_rates

    with SessionLocal() as s:
        s.add_all([_add_deposit(32, 0.40), _add_deposit(365, 0.45)])
        s.commit()

    _try_deposit_rates.clear()
    result = _deposit_comparison(1_000_000, 365)
    assert result is not None
    net_return, institution = result
    assert net_return > 0
    assert institution == "VAKIFBANK"

    # 365 gün ufku için 365 günlük vade seçilmeli (32 değil).
    brackets = resolve_deposit_brackets("TRY")
    wh = resolve_withholding(brackets, 365)
    expected_period = (0.45 / 365) * 365 * (1 - wh)
    assert net_return == pytest.approx(1_000_000 * expected_period, rel=1e-9)


def test_deposit_comparison_scales_with_principal(seeded_db):
    from app.panels.fund import _deposit_comparison, _try_deposit_rates

    with SessionLocal() as s:
        s.add(_add_deposit(365, 0.45))
        s.commit()

    _try_deposit_rates.clear()
    one, _ = _deposit_comparison(1_000_000, 365)
    _try_deposit_rates.clear()
    two, _ = _deposit_comparison(2_000_000, 365)
    assert two == pytest.approx(one * 2, rel=1e-9)


# -------------------------------------------------- uçtan uca kullanıcı akışı ----

def test_end_to_end_fund_scenario_equity_heavy_is_tax_exempt(seeded_db):
    """Kullanıcı 1M TL girer, hisse yoğun fon seçer -> stopaj kesilmemeli."""
    with SessionLocal() as s:
        for i in range(400):
            s.add(FundPrice(fund_code="AK3", price_date=date(2025, 8, 1) + timedelta(days=i),
                            price=10.0 + i * 0.05, fetched_at=datetime.now(timezone.utc)))
        s.commit()

    from store import queries

    prices = queries.fund_price_series("AK3", date(2025, 8, 1), date(2026, 9, 30))
    end_d, end_p = max(prices, key=lambda p: p[0])
    start_d, start_p = nearest_prior_price(prices, end_d - timedelta(days=365))

    result = simulate(FundSimInput(
        principal=1_000_000, price_start=start_p, price_end=end_p,
        date_start=start_d, date_end=end_d,
        withholding_rate=resolve_fund_withholding(True), is_equity_heavy=True,
    ))
    assert result.gross_return > 0
    assert result.net_return == pytest.approx(result.gross_return)
    assert result.withholding_applied is False


def test_end_to_end_fund_scenario_regular_fund_is_taxed(seeded_db):
    with SessionLocal() as s:
        for i in range(400):
            s.add(FundPrice(fund_code="AFA", price_date=date(2025, 8, 1) + timedelta(days=i),
                            price=1.0 + i * 0.002, fetched_at=datetime.now(timezone.utc)))
        s.commit()

    from store import queries

    prices = queries.fund_price_series("AFA", date(2025, 8, 1), date(2026, 9, 30))
    end_d, end_p = max(prices, key=lambda p: p[0])
    start_d, start_p = nearest_prior_price(prices, end_d - timedelta(days=365))

    wh = resolve_fund_withholding(False)
    result = simulate(FundSimInput(
        principal=1_000_000, price_start=start_p, price_end=end_p,
        date_start=start_d, date_end=end_d, withholding_rate=wh, is_equity_heavy=False,
    ))
    assert result.withholding_applied is True
    assert result.net_return == pytest.approx(result.gross_return * (1 - wh))


def test_end_to_end_loan_flow_uses_correct_taxes_per_type(seeded_db):
    """Konut kredisi vergisiz, ihtiyaç kredisi KKDF+BSMV'li olmalı."""
    housing_kkdf, housing_bsmv = resolve_loan_taxes("housing")
    personal_kkdf, personal_bsmv = resolve_loan_taxes("personal")

    assert (housing_kkdf, housing_bsmv) == (0.0, 0.0)
    assert personal_kkdf > 0 and personal_bsmv > 0

    housing = amortize(LoanInput(1_000_000, 0.0295, 12, housing_kkdf, housing_bsmv))
    personal = amortize(LoanInput(1_000_000, 0.0295, 12, personal_kkdf, personal_bsmv))

    assert housing.total_kkdf == 0 and housing.total_bsmv == 0
    assert personal.total_kkdf > 0 and personal.total_bsmv > 0
    # Aynı ilan edilen orana rağmen vergili kredi daha pahalı olmalı
    assert personal.installment > housing.installment
    # Her iki planda da bakiye sıfırlanmalı
    for r in (housing, personal):
        assert r.schedule[-1].remaining_balance == pytest.approx(0.0, abs=1e-6)


def test_end_to_end_deposit_flow_selects_bracket_then_computes_net(seeded_db):
    """Anapara değişince kademe DEĞİŞMELİ ve net getiri buna göre hesaplanmalı."""
    from store import queries

    with SessionLocal() as s:
        s.add_all([
            _add_deposit(32, 0.42, amount_min=0, amount_max=1_000_000),
            _add_deposit(32, 0.38, amount_min=1_000_001, amount_max=None),
        ])
        s.commit()

    brackets = resolve_deposit_brackets("TRY")

    def net_for(principal):
        row = queries.deposit_rates_for_amount(principal)[0]
        wh = resolve_withholding(brackets, row["term_days"])
        return row["annual_rate"], single_term_return(
            DepositInput(principal, row["annual_rate"], row["term_days"], wh)
        ).net_return

    rate_small, net_small = net_for(500_000)
    rate_big, net_big = net_for(2_000_000)

    assert rate_small == pytest.approx(0.42)
    assert rate_big == pytest.approx(0.38)
    # 4x anapara ama daha düşük oran -> net getiri 4 katından AZ olmalı
    assert net_big < net_small * 4


def test_withholding_brackets_change_with_term(seeded_db):
    """Tasarım §04: stopaj vadeye göre değişir; panel bunu yansıtmalı."""
    brackets = resolve_deposit_brackets("TRY")
    short = resolve_withholding(brackets, 32)
    mid = resolve_withholding(brackets, 200)
    long = resolve_withholding(brackets, 400)
    assert short > mid > long

    nets = [
        single_term_return(DepositInput(1_000_000, 0.45, days, resolve_withholding(brackets, days))).net_return
        for days in (32, 200, 400)
    ]
    assert nets == sorted(nets)  # daha uzun vade -> daha çok gün + daha az stopaj


# ------------------------------------------- zaman damgası gösterimi (4. tur) ----
#
# Veritabanındaki her zaman damgası UTC (naive) saklanıyor. Kullanıcıya
# İstanbul saati gösterilmezse "14:09'da çekildi" yerine "11:09" yazar ve
# veri üç saat bayat sanılır.


def test_timestamps_are_displayed_in_istanbul_time_not_utc():
    from app.panels.common import format_local, to_istanbul

    utc_naive = datetime(2026, 8, 23, 11, 9, 28)
    assert to_istanbul(utc_naive).hour == 14  # yaz saati: UTC+3
    assert format_local(utc_naive) == "23.08.2026 14:09"


def test_fetched_caption_reports_the_newest_stamp():
    from app.panels.common import fetched_caption

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    caption = fetched_caption([now - timedelta(hours=5), now])
    assert "dakika önce" in caption
    assert "Europe/Istanbul" in caption


def test_fetched_caption_handles_missing_timestamps():
    from app.panels.common import fetched_caption

    assert "bilinmiyor" in fetched_caption([None, None])
    assert "bilinmiyor" in fetched_caption([])


# ------------------------------------------------- anapara kutusu (5. tur) ----
#
# Kutu artık metin alanı: basamakları ayırabilmek için. Yazılanı sayıya
# çeviren `parse_amount` bütün sekmelerin hesabının girdisi; burada yanlış
# okunan bir tutar panelin tamamını sessizce yanıltır.


def test_parse_amount_accepts_every_separator_the_user_might_type():
    from app.panels.common import parse_amount

    for raw in ["2,500,000", "2.500.000", "2 500 000", "2500000"]:
        assert parse_amount(raw) == 2_500_000


def test_parse_amount_returns_none_when_there_is_no_digit():
    from app.panels.common import parse_amount

    # Kutu bunu son geçerli tutara döndürmek için kullanıyor; 0 dönseydi
    # boşa basan kullanıcı bütün hesapları sıfırlardı.
    assert parse_amount("") is None
    assert parse_amount("   ") is None
    assert parse_amount("abc") is None


def test_parse_amount_never_returns_a_negative_amount():
    from app.panels.common import parse_amount

    # Eski kutudaki min_value=0 sınırının karşılığı: eksi işareti eleniyor.
    assert parse_amount("-500000") == 500_000


def test_format_amount_groups_digits_like_the_tables():
    from app.panels.common import format_amount

    assert format_amount(2_500_000) == "2,500,000"
    assert format_amount(999) == "999"
    assert format_amount(0) == "0"


def test_amount_round_trips_through_the_box():
    from app.panels.common import format_amount, parse_amount

    for value in [0, 999, 50_000, 1_000_000, 12_345_678]:
        assert parse_amount(format_amount(value)) == value


# ----------------------------------------------- kredi vade sınırları (4. tur) ----
#
# Panel, bankanın ilan ettiği vade/tutar sınırının dışına çıkıldığında
# hesabı yapmayı sürdürür ama sayının gerçekte alınamayacağını söyler.


def test_loan_panel_warns_when_term_exceeds_bank_limit():
    from app.panels.loan import _limit_warning

    row = {"institution": "YAPIKREDI", "term_min": 3, "term_max": 36, "amount_max": None}
    warning = _limit_warning(row, term_months=240, principal=1_000_000)
    assert warning is not None
    assert "36 ay" in warning
    assert "Yapı Kredi" in warning


def test_loan_panel_warns_when_principal_exceeds_bank_limit():
    from app.panels.loan import _limit_warning

    row = {"institution": "VAKIFBANK", "term_min": 3, "term_max": 24, "amount_max": 250_000}
    warning = _limit_warning(row, term_months=12, principal=1_000_000)
    assert warning is not None and "250,000 TL" in warning


def test_loan_panel_is_silent_inside_bank_limits():
    from app.panels.loan import _limit_warning

    row = {"institution": "VAKIFBANK", "term_min": 3, "term_max": 24, "amount_max": 250_000}
    assert _limit_warning(row, term_months=12, principal=100_000) is None


def test_loan_panel_handles_unknown_limits():
    """term_min/term_max yoksa uyarı üretilmemeli — bilinmeyen sınır, ihlal değildir."""
    from app.panels.loan import _limit_warning

    row = {"institution": "TEB", "term_min": None, "term_max": None, "amount_max": None}
    assert _limit_warning(row, term_months=360, principal=10_000_000) is None


def test_loan_term_range_text_covers_open_ended_limits():
    from app.panels.loan import _term_range_text

    assert _term_range_text({"term_min": 3, "term_max": 36}) == "3 – 36"
    assert _term_range_text({"term_min": None, "term_max": 36}) == "≤ 36"
    assert _term_range_text({"term_min": 3, "term_max": None}) == "≥ 3"
    assert _term_range_text({"term_min": None, "term_max": None}) == "—"


# ------------------------------------------ mevduat paneli: kurum bazlı (4. tur) ----


def test_deposit_panel_tier_text_describes_the_selected_bracket():
    from app.panels.deposit import _tier_text

    rows = [{"amount_min": 750_000.0, "amount_max": 1_500_000.0}]
    assert _tier_text(rows, "TRY") == "750,000 – 1,500,000 TRY"

    open_ended = [{"amount_min": 1_500_000.0, "amount_max": None}]
    assert _tier_text(open_ended, "TRY") == "1,500,000 TRY ve üzeri"


def test_deposit_panel_groups_rows_per_institution(seeded_db):
    """Panel her kurumu kendi tablosunda gösterir; sorgu bunu besleyebilmeli."""
    from collections import defaultdict

    from store import queries

    with SessionLocal() as s:
        for institution in ("VAKIFBANK", "TEB", "ENPARA"):
            s.add(
                DepositRate(
                    institution=institution,
                    currency="TRY",
                    term_days=32,
                    amount_min=0,
                    amount_max=None,
                    annual_rate=0.40,
                    is_profit_share=False,
                    valid_date=date.today(),
                    fetched_at=datetime.now(timezone.utc),
                )
            )
        s.commit()

    rows = queries.deposit_rates_for_amount(1_000_000, "TRY")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["institution"]].append(row)
    assert set(grouped) == {"VAKIFBANK", "TEB", "ENPARA"}
    for row in rows:
        assert row["fetched_at"] is not None
        assert row["amount_min"] == 0


# ------------------------------------- mevduat sıralaması: yıllıklandırma (5. tur) ----
#
# Kurumları MUTLAK net getiriye göre sıralamak en uzun vadeyi sunan bankayı
# kayırıyordu: VakıfBank'ın 750 günlük %18'i (yıllık net %16,2) birinci,
# Yapı Kredi'nin 367 günlük %33,5'i (yıllık net %30,2) ikinci çıkıyordu.


def test_annualized_net_pct_normalizes_short_and_long_terms():
    from app.panels.deposit import _annualized_net_pct

    brackets = resolve_deposit_brackets("TRY")
    short = {"term_days": 32, "annual_rate": 0.3825}
    long_ = {"term_days": 750, "annual_rate": 0.18}

    short_pct = _annualized_net_pct(short, 1_000_000, brackets)
    long_pct = _annualized_net_pct(long_, 1_000_000, brackets)

    assert short_pct > long_pct, "kısa vadeli yüksek oran, uzun vadeli düşük orandan iyi olmalı"
    # %38,25 brüt, %17,5 stopaj -> yıllık net ~%31,6
    assert short_pct == pytest.approx(38.25 * (1 - 0.175), rel=1e-6)


def test_absolute_return_would_rank_the_worse_offer_first():
    """Düzeltilen hatanın kendisini kilitler: mutlak getiri yanlış sıralar."""
    from app.panels.deposit import _annualized_net_pct, _net_return

    brackets = resolve_deposit_brackets("TRY")
    good = {"term_days": 367, "annual_rate": 0.335}   # yıllık net daha iyi
    bad = {"term_days": 750, "annual_rate": 0.18}     # mutlak getiri daha yüksek

    assert _net_return(bad, 1_000_000, brackets) > _net_return(good, 1_000_000, brackets)
    assert _annualized_net_pct(good, 1_000_000, brackets) > _annualized_net_pct(
        bad, 1_000_000, brackets
    )


def test_annualized_net_pct_is_safe_at_zero_principal():
    from app.panels.deposit import _annualized_net_pct

    brackets = resolve_deposit_brackets("TRY")
    assert _annualized_net_pct({"term_days": 32, "annual_rate": 0.38}, 0, brackets) == 0.0


# ------------------------------------------------- LLM token korumaları (5. tur) ----
#
# Kullanıcı isteği: "takılırsa token harcamasın". Bu testler o güvenceleri
# kilitler — hiçbiri ağa çıkmaz.


def test_llm_fallback_is_disabled_by_default():
    """Anahtar girilse bile açıkça açılmadıkça tek token harcanmamalı."""
    from llm import extract, settings

    assert settings.LLM_FALLBACK_ENABLED is False
    with pytest.raises(extract.LlmDisabled):
        extract.extract("<html>%38</html>", _DummySchema)


def test_llm_call_budget_stops_after_the_limit(monkeypatch):
    from llm import extract, settings

    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_MAX_CALLS_PER_RUN", 1)
    extract.reset_budget()

    calls = {"n": 0}

    def _fake_client():
        calls["n"] += 1
        raise RuntimeError("ağa çıkılmadı")

    monkeypatch.setattr(extract, "get_client", _fake_client)

    with pytest.raises(RuntimeError):
        extract.extract("<html>%38</html>", _DummySchema)
    # İkinci çağrı bütçeye takılmalı ve istemciye HİÇ ulaşmamalı.
    with pytest.raises(extract.LlmBudgetExceeded):
        extract.extract("<html>%38</html>", _DummySchema)
    assert calls["n"] == 1


def test_llm_input_is_condensed_to_the_rate_region():
    """Ham HTML'in ilk N karakteri <head> ve menüdür — token israfı.

    Girdi, oran işaretlerinin (%/gün/vade) geçtiği bölgeye daraltılmalı.
    """
    from llm import extract

    noise = "<head>" + ("x" * 5_000) + "</head>"
    payload = noise + "<table><tr><td>32 gün</td><td>%38,25</td></tr></table>"
    condensed = extract._condense(payload, 500)

    assert len(condensed) <= 500
    assert "38,25" in condensed


def test_llm_condense_strips_scripts_and_styles():
    from llm import extract

    payload = "<style>a{}</style><script>var x=1</script><p>%38,25</p>"
    condensed = extract._condense(payload, 1000)
    assert "script" not in condensed and "var x" not in condensed
    assert "%38,25" in condensed


def test_collector_reraises_parse_error_when_llm_is_off(db):
    """Fallback kapalıyken ASIL parse hatası kaybolmamalı."""
    from collectors.base import Collector, ParseError

    class Broken(Collector):
        name = "broken"
        schema = _DummySchema

        def fetch(self):
            return b"<html></html>"

        def parse(self, raw):
            raise ParseError("selector kırıldı")

        def sanity_check(self, records):
            pass

        def persist(self, records, run_id):
            pass

    result = Broken().run()
    assert result.ok is False
    assert "selector kırıldı" in (result.error or "")


# ---------------------------------- katılım kâr paylaşımı paneli (5. tur) ----


def test_profit_share_query_picks_the_matching_balance_tier(seeded_db):
    from datetime import date as _date

    from store.models import ProfitShareRatio

    with SessionLocal() as s:
        s.add_all([
            ProfitShareRatio(
                institution="EMLAKKATILIM", currency="TRY", term_label="Yıllık",
                term_days=365, amount_min=0, amount_max=99_999, share_ratio=0.88,
                withholding_rate=0.15, valid_date=_date.today(),
                fetched_at=datetime.now(timezone.utc),
            ),
            ProfitShareRatio(
                institution="EMLAKKATILIM", currency="TRY", term_label="Yıllık",
                term_days=365, amount_min=100_000, amount_max=None, share_ratio=0.92,
                withholding_rate=0.15, valid_date=_date.today(),
                fetched_at=datetime.now(timezone.utc),
            ),
        ])
        s.commit()

    from store import queries

    rows = queries.profit_share_ratios(1_000_000, "TRY")
    assert len(rows) == 1
    assert rows[0]["share_ratio"] == pytest.approx(0.92)

    small = queries.profit_share_ratios(50_000, "TRY")
    assert small[0]["share_ratio"] == pytest.approx(0.88)


def test_profit_share_ratios_are_never_treated_as_interest(seeded_db):
    """Kâr paylaşım oranı deposit_rates'e SIZMAMALI.

    %92'lik bir paylaşım oranı faiz sanılırsa panel 1M TL için ~920.000 TL
    getiri hesaplar — tamamen uydurma bir sayı.
    """
    from datetime import date as _date

    from store.models import ProfitShareRatio

    with SessionLocal() as s:
        s.add(ProfitShareRatio(
            institution="EMLAKKATILIM", currency="TRY", term_label="Yıllık",
            term_days=365, amount_min=0, amount_max=None, share_ratio=0.92,
            withholding_rate=0.15, valid_date=_date.today(),
            fetched_at=datetime.now(timezone.utc),
        ))
        s.commit()

    from store import queries

    assert queries.profit_share_ratios(1_000_000, "TRY")
    assert queries.deposit_rates_for_amount(1_000_000, "TRY") == []


# --------------------------------------------------- banka günü saati (5. tur) ----


def test_business_date_is_istanbul_not_machine_local():
    from datetime import timezone as _tz

    from store.clock import ISTANBUL, istanbul_today, utc_now

    assert utc_now().tzinfo is _tz.utc
    assert istanbul_today() == datetime.now(ISTANBUL).date()


def test_business_date_diverges_from_utc_after_midnight_in_istanbul():
    """Hatanın kendisini kilitler — duvar saatine bağlı OLMADAN.

    Docker konteyneri varsayılan olarak UTC koşar. Türkiye'de 25 Ağustos
    00:30 iken UTC hâlâ 24 Ağustos 21:30'dur. Eski kod (`date.today()`)
    konteynerde o anda 24 Ağustos yazardı: o saatlerde çekilen BÜTÜN oranlar
    bir gün geriye kaydedilir, "bugünün eski satırlarını temizle" mantığı
    yanlış günü hedefler ve panel dünkü oranı bugünkü sanardı.
    """
    from datetime import timezone as _tz

    from store.clock import ISTANBUL

    # Türkiye'de gece yarısından hemen sonraki bir an.
    instant = datetime(2026, 8, 25, 0, 30, tzinfo=ISTANBUL)

    assert instant.astimezone(_tz.utc).date() == date(2026, 8, 24)  # UTC dünü
    assert instant.date() == date(2026, 8, 25)  # banka günü bugünü
    # İkisi GERÇEKTEN farklı; test tautoloji değil.
    assert instant.astimezone(_tz.utc).date() != instant.date()


# ------------------------------------------------- seyrek seri uyarısı ----

def test_daily_coverage_flags_a_sparse_series_but_not_a_daily_one():
    """Panel uyarısı SAĞLAYICI ADINA değil VERİYE bakmalı.

    Yapı Kredi Portföy uç noktası uzak geçmişi aylık veriyor; grafiğe
    bakan biri "veri eksik mi?" diye düşünmesin diye panel sebebi yazıyor.
    Kontrol veriye bakınca, yeni bir sağlayıcı eklendiğinde uyarı
    kendiliğinden doğru çalışır.

    REGRESYON (tarayıcıda yakalandı, 2026-09-05): ölçüm SON 90 GÜNE
    bakıyordu ve uyarı hiç çıkmıyordu — çünkü serinin son iki ayı zaten
    günlük; seyrek olan uzak geçmişi. Ölçüm serinin tamamına bakmalı.
    """
    from datetime import date, timedelta

    from app.panels.fund import _daily_coverage

    son = date(2026, 9, 4)
    # Kesintisiz günlük seri (hafta sonları kapalı) ~0,71'i geçemez.
    gunluk = [(son - timedelta(days=k), 1.0 + k) for k in range(0, 400) if (son - timedelta(days=k)).weekday() < 5]
    # Yapı Kredi'nin gerçek deseni: son 56 gün günlük, öncesi aylık.
    ykp = [(son - timedelta(days=k), 1.0 + k) for k in range(0, 56)]
    ykp += [(son - timedelta(days=30 * k), 1.0 + k) for k in range(2, 26)]

    assert _daily_coverage(gunluk) > 0.5
    assert _daily_coverage(ykp) < 0.5, "seyrek uzak geçmiş uyarıyı tetiklemeli"
    assert _daily_coverage([]) is None
    assert _daily_coverage([(son, 1.0)]) is None


# ------------------------------------------------------- anapara kutusu ----


def test_principal_input_label_has_no_currency_unit():
    """Etiket birimsiz kalmalı.

    Kilitlenen şey gerçek: etiket seçilen para birimine bağlanamaz, çünkü
    tutar bütün sekmelerde ORTAK ve Kredi ile Fon sekmeleri onu her koşulda
    TL sayıyor; para birimi seçicisi yalnızca Mevduat sekmesinin içinde
    (bkz. app/panels/deposit.py).
    """
    from app.panels.common import PRINCIPAL_LABEL

    assert PRINCIPAL_LABEL == "Anapara"


def test_principal_boxes_share_one_amount(monkeypatch):
    """Bir sekmede girilen tutar diğer sekmelerin kutusuna da yansır.

    Sekmeler ayrı `key` taşımak zorunda (Streamlit hepsini aynı
    çalıştırmada render eder), bu yüzden ortaklık kanonik tutar üzerinden
    kuruluyor. Test o zinciri taklit ediyor: Döviz kutusuna ham "2500000"
    yazılıyor, sonra Mevduat kutusu çiziliyor ve orada ayrılmış gösterimi
    okuması bekleniyor.
    """
    from app.panels import common

    class _SahteStreamlit:
        def __init__(self):
            self.session_state = {}
            self.cizilen = []

        def text_input(self, label, key=None, on_change=None, args=()):
            self.cizilen.append((label, key, self.session_state[key]))

    sahte = _SahteStreamlit()
    monkeypatch.setattr(common, "st", sahte)

    assert common.principal_input("doviz") == common.DEFAULT_PRINCIPAL

    # Kullanıcı Döviz kutusuna ayırıcısız yazıyor; kutunun on_change'i budur.
    sahte.session_state["anapara_doviz"] = "2500000"
    common._principal_changed("anapara_doviz")

    assert common.principal_input("mevduat") == 2_500_000
    assert sahte.cizilen[-1] == ("Anapara", "anapara_mevduat", "2,500,000")
    # Aynı çalıştırmada Döviz kutusu da kanonik gösterime dönüyor.
    common.principal_input("doviz")
    assert sahte.session_state["anapara_doviz"] == "2,500,000"


def test_principal_input_keeps_last_amount_when_box_emptied():
    """Kutu boşaltılırsa tutar sıfıra düşmez, son geçerli değer kalır.

    Sessizce sıfıra düşmek bütün sekmelerin hesabını fark edilmeden
    bozardı; boş kutu bir tutar değil, yarım kalmış bir düzenlemedir.
    """
    from app.panels import common

    class _SahteStreamlit:
        def __init__(self):
            self.session_state = {}

        def text_input(self, label, key=None, on_change=None, args=()):
            pass

    import pytest as _pytest

    sahte = _SahteStreamlit()
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(common, "st", sahte)
        common.principal_input("kredi")
        sahte.session_state["anapara_kredi"] = "750000"
        common._principal_changed("anapara_kredi")
        sahte.session_state["anapara_kredi"] = "   "
        common._principal_changed("anapara_kredi")
        assert common.principal_input("kredi") == 750_000


