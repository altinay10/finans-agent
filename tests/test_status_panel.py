"""Üst şerit — panelin ilk bakışta verdiği hüküm.

Buradaki testler, eski `st.metric` şeridinin dört kusurunu kilitliyor:
iç isimler, sınırsız yaş, emekli toplayıcılar ve hiç koşmamış toplayıcının
görünmezliği. Dördü de sessiz hatalardı: panel yanlış bir şey söylemiyordu,
hiçbir şey söylemiyordu.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.panels import status


def _bosalt() -> None:
    """`st.cache_data` önbelleklerini boşalt — varsa.

    Bir test `_freshness`'i sade bir lambda ile değiştirmiş olabilir; o
    lambda'nın `clear`'ı yoktur. Fixture sıralaması monkeypatch'in geri
    almasından önce çalışabildiği için savunmacı olmak gerekiyor.
    """
    for fn in (status._freshness, status._limits):
        getattr(fn, "clear", lambda: None)()


@pytest.fixture(autouse=True)
def _no_cache():
    """st.cache_data testler arasında sızmasın."""
    _bosalt()
    yield
    _bosalt()


def _fake(monkeypatch, rows: list[dict], *, agent_on: bool = False) -> None:
    monkeypatch.setattr(status, "_freshness", lambda: rows)
    monkeypatch.setattr(status, "_agent_enabled", lambda: agent_on)


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ------------------------------------------------- yaş, sınırla birlikte ----

def test_same_age_is_fresh_for_one_collector_and_stale_for_another():
    """Eski şeridin asıl kusuru: "1,3 sa önce" tek başına hüküm vermiyor.

    fx_banks'in sınırı 6 saat, agent'ınki 30 gün. 8 saatlik veri birinde
    arıza, diğerinde gayet taze. Yaşı sınırsız göstermek, kullanıcıyı iyi
    ile kötüyü ayıramaz hale getiriyordu.
    """
    assert status._state(8, 6, "fx_banks", True) == "late"
    assert status._state(8, 24 * 30, "loan_rates_llm", True) == "fresh"


def test_one_missed_run_is_yellow_but_a_dead_source_is_red():
    """Hafta sonu kaçan tek koşuyu kırmızı yapmak, kırmızıyı değersizleştirir."""
    assert status._state(31, 30, "deposit_rates", True) == "late"
    assert status._state(61, 30, "deposit_rates", True) == "stale"
    assert status._state(None, 30, "deposit_rates", True) == "never"


def test_disabled_agent_is_grey_not_red(monkeypatch):
    """Anahtarsız kurulumda agent her koşuda hata verir — bu arıza DEĞİL.

    Kırmızı göstermek, düzeltilecek bir bozukluk sanılmasına yol açardı;
    oysa varsayılan davranış bu (bkz. llm/settings.py: LLM_FALLBACK_ENABLED).
    """
    assert status._state(None, 24 * 30, "loan_rates_llm", False) == "off"
    assert status._state(None, 24 * 30, "loan_rates_llm", True) == "never"


# ------------------------------------------------- eksik satır kalmasın ----

def test_a_collector_that_never_ran_still_gets_a_row(monkeypatch):
    """En tehlikeli arıza en görünmez olanıydı.

    Liste veritabanından gelseydi, hiç koşmamış bir toplayıcının satırı hiç
    olmaz ve panel onun yokluğunu sessizce geçerdi. Liste artık plandan
    başlatılıyor.
    """
    _fake(monkeypatch, [{"collector": "fx_banks", "last_ok": _iso(1), "fallback_count": 0}])
    rows = status._rows(datetime.now(timezone.utc))

    adlar = {r["collector"] for r in rows}
    assert adlar == {n for _, members in status.GROUPS for n in members}
    yok = next(r for r in rows if r["collector"] == "deposit_rates")
    assert yok["state"] == "never"


def test_retired_collectors_leave_the_strip_but_not_the_record(monkeypatch):
    """`v_freshness` scrape_runs'ı grupluyor: kayıttan çıkarılan bir toplayıcı
    orada sonsuza kadar yaşlanıp kalıcı kırmızı üretiyordu. Şeritten çıkıyor,
    ama tamamen saklanmıyor — bir toplayıcının gruptan DÜŞÜRÜLMESİ de gerçek
    bir arıza olurdu ve o zaman hiç fark edilmezdi.
    """
    _fake(
        monkeypatch,
        [
            {"collector": "fx_banks", "last_ok": _iso(1), "fallback_count": 0},
            {"collector": "akportfoy", "last_ok": _iso(50), "fallback_count": 0},
        ],
    )
    now = datetime.now(timezone.utc)
    assert "akportfoy" not in {r["collector"] for r in status._rows(now)}
    assert [r["collector"] for r in status.retired(now)] == ["akportfoy"]


# ------------------------------------------------------- grup hükmü ----

def test_a_group_is_as_healthy_as_its_weakest_member(monkeypatch):
    """Zayıf halka güveni belirler.

    Grubu en iyi üyesine göre yeşil göstermek, içinde 3 günlük ölü bir
    kaynak saklamak demek olurdu.
    """
    _fake(
        monkeypatch,
        [
            {"collector": "fx_banks", "last_ok": _iso(0.2), "fallback_count": 0},
            {"collector": "fx_tcmb", "last_ok": _iso(400), "fallback_count": 0},
        ],
    )
    rows = status._rows(datetime.now(timezone.utc))
    doviz = [r["state"] for r in rows if r["group"] == "Döviz"]
    assert status._worst(doviz) == "stale"


def test_groups_use_the_tab_names_not_internal_collector_names():
    """`participation_rates_kt` kullanıcının bildiği bir şey değil."""
    adlar = [ad for ad, _ in status.GROUPS]
    assert adlar == ["Döviz", "Mevduat & Kâr Payı", "Kredi", "Fon"]
    assert len(adlar) < len({n for _, m in status.GROUPS for n in m})


# ------------------------------------------------------------ biçim ----

@pytest.mark.parametrize(
    "hours, beklenen",
    [(0.5, "30 dk"), (2.25, "2,2 sa"), (40, "40 sa"), (100, "4 gün"), (None, "—")],
)
def test_age_is_short_enough_to_fit_a_chip(hours, beklenen):
    assert status.format_age(hours) == beklenen


def test_chip_escapes_its_tooltip():
    """Toplayıcı adı bir gün HTML'e benzerse rozet bozulmamalı."""
    chip = status._chip("#16a34a", "Döviz", "2 sa", '<b>"x"</b>')
    assert "<b>" not in chip.split("title=", 1)[1].split(">", 1)[0].replace("&lt;b&gt;", "")
    assert "&lt;b&gt;" in chip


def test_sub_minute_age_reads_as_words_not_zero():
    """"0 dk" bir an için "hiç veri yok" diye okunuyor."""
    assert status.format_age(0.005) == "az önce"


# --------------------------------------------------------------- alarm ----

class _FakeSt:
    """`st.error` çağrılarını yakalayan minik yerine.

    Uzun kurtarma talimatının YALNIZCA gerçekten bozukken çıktığını
    doğrulamanın tek yolu bu: her açılışta tepede duran beş satırlık docker
    komutunu kullanıcı okumayı bırakır ve gerçekten bozulduğunda da okumaz.
    """

    def __init__(self):
        self.errors: list[str] = []

    def error(self, text):
        self.errors.append(text)


def _alarm(monkeypatch, rows, beat) -> list[str]:
    fake = _FakeSt()
    monkeypatch.setattr(status, "st", fake)
    status._render_alarm(rows, beat)
    return fake.errors


def test_healthy_system_shows_no_recovery_instructions(monkeypatch):
    rows = [{"collector": "fx_banks", "state": "fresh", "age_hours": 1.0}]
    beat = {"alive": True, "age_seconds": 9, "host": "h", "pid": 1}
    assert _alarm(monkeypatch, rows, beat) == []


def test_a_single_late_source_does_not_trigger_the_alarm(monkeypatch):
    """Hafta sonu kaçan tek koşu için kırmızı kutu açmak, kutuyu değersizleştirir."""
    rows = [{"collector": "fx_banks", "state": "late", "age_hours": 8.0}]
    beat = {"alive": True, "age_seconds": 9, "host": "h", "pid": 1}
    assert _alarm(monkeypatch, rows, beat) == []


def test_dead_scheduler_sends_the_user_to_the_process_not_the_sources(monkeypatch):
    rows = [{"collector": "fx_banks", "state": "stale", "age_hours": 99.0}]
    beat = {"alive": False, "age_seconds": 7200, "host": "h", "pid": 1}
    (mesaj,) = _alarm(monkeypatch, rows, beat)
    assert "Zamanlayıcı durmuş" in mesaj
    assert "docker compose up -d scheduler" in mesaj


def test_live_scheduler_with_dead_sources_sends_the_user_to_the_logs(monkeypatch):
    """Süreç ayaktayken "süreci başlat" demek kullanıcıyı yanlış tarafa gönderir."""
    rows = [{"collector": "fx_banks", "state": "stale", "age_hours": 99.0}]
    beat = {"alive": True, "age_seconds": 9, "host": "h", "pid": 1}
    (mesaj,) = _alarm(monkeypatch, rows, beat)
    assert "Kayıtlar" in mesaj
    assert "docker" not in mesaj
    assert "`fx_banks`" in mesaj


def test_missing_heartbeat_explains_how_to_start_a_collector_at_all(monkeypatch):
    rows = [{"collector": "fx_banks", "state": "never", "age_hours": None}]
    (mesaj,) = _alarm(monkeypatch, rows, None)
    assert "hiç çalışmamış" in mesaj
    assert "python worker.py all" in mesaj


# ---------------------------------------------------------- durum sözlüğü ----

def test_every_status_is_explained_in_the_legend():
    """Bir renk adı tek başına ne arızayı ne tercihi anlatıyor.

    Yeni bir durum eklenip sözlükte anlatılmadan kalırsa kullanıcı tabloda
    açıklaması olmayan bir kelime görür — "kapalı" tam olarak böyleydi.
    """
    for etiket in status.STATE_LABEL.values():
        assert etiket in status.LEGEND, f"'{etiket}' durumu sözlükte anlatılmamış"


def test_disabled_agent_is_named_not_just_greyed():
    """"kapalı" belirsizdi: neyin kapalı olduğunu söylemiyordu.

    Anahtarsız kurulumda bu satır KALICI olarak görünüyor; düzeltilecek bir
    bozukluk sanılırsa kullanıcı olmayan bir arızayı kovalar. Bu yüzden hem
    adı hem de açılış yolu yazılı.
    """
    assert status.STATE_LABEL["off"] == "agent kapalı"
    assert "LLM_API_KEY" in status.LEGEND
    assert "LLM_FALLBACK_ENABLED=1" in status.LEGEND
    assert "arıza değil" in status.LEGEND.lower()


def test_legend_stays_short_enough_to_read():
    """Açıklama tablodan uzun olursa kimse okumaz."""
    assert len(status.LEGEND) < 800


def test_ago_phrasing_does_not_stutter():
    """"az önce" zaten bir zarf: cümleye ikinci bir "önce" eklenmemeli."""
    assert status.format_ago(0.005) == "az önce"
    assert status.format_ago(2.25) == "2,2 sa önce"
    assert status.format_ago(None) == "—"
