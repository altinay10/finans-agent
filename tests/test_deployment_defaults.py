"""Depodan klonlayıp `.env` YAZMADAN çalıştıran ne alıyor?

NEDEN: sistemin GitHub'dan doğrudan çalıştırılabilir olması isteniyor
(kullanıcı isteği, 2026-09-13). Bu, varsayılanların üç ayrı yerde aynı
olmasını gerektiriyor: kodun kendisi, `docker-compose.yml`'deki
`${VAR:-varsayılan}` ve `.env.example`. Üçü kaydığında sonuç sessiz olur —
kod 3 gün derken konteyner 14 gün saklar ve kimse fark etmez.

Bu tam olarak yaşandı: snapshot penceresi kodda 3'e çekildi ama compose
hâlâ 14 geçiriyordu, yani `.env`'siz bir kurulum eski davranışı alıyordu.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

KOK = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((KOK / "docker-compose.yml").read_text(encoding="utf-8"))
ORNEK = (KOK / ".env.example").read_text(encoding="utf-8")


def _compose_varsayilani(degisken: str) -> str:
    """`${VAR:-x}` ifadesinden x'i çıkarır."""
    ham = COMPOSE["services"]["panel"]["environment"][degisken]
    m = re.fullmatch(r"\$\{" + degisken + r":-(.*)\}", str(ham))
    assert m, f"{degisken} beklenen biçimde değil: {ham!r}"
    return m.group(1)


def _ornek_degeri(degisken: str) -> str:
    m = re.search(rf"(?m)^{degisken}=(.*)$", ORNEK)
    assert m, f"{degisken} .env.example'da yok"
    return m.group(1).strip()


@pytest.mark.parametrize("degisken", ["RETAIN_SNAPSHOT_DAYS", "RETAIN_HTTP_REQUEST_DAYS"])
def test_compose_and_example_agree_on_retention(degisken):
    assert _compose_varsayilani(degisken) == _ornek_degeri(degisken)


def test_the_snapshot_window_is_three_everywhere():
    """Kod, compose ve örnek dosya AYNI pencereyi söylemeli."""
    from store.retention import _days

    assert _compose_varsayilani("RETAIN_SNAPSHOT_DAYS") == "3"
    assert _ornek_degeri("RETAIN_SNAPSHOT_DAYS") == "3"
    # Kodun varsayılanı (ortam değişkeni yokken) — çağrı yerindeki sayı.
    assert _days("HIC_OLMAYAN_DEGISKEN", 3) == 3


def test_every_long_running_service_caps_its_container_log():
    """Docker'ın `json-file` sürücüsü sınırsız yazar; tavan ŞART.

    Aynı Raspberry Pi'de başka bir projenin konteyner log'u 30 MB'a
    ulaşmış durumda (ölçüm, 2026-09-13). Kurulup unutulan bir sistemde
    bu sessizce büyümeye devam eder.
    """
    for ad, servis in COMPOSE["services"].items():
        log = servis.get("logging")
        assert log, f"{ad}: logging tanımlı değil"
        assert log["driver"] == "json-file", ad
        secenek = log["options"]
        assert secenek["max-size"].endswith("m"), f"{ad}: max-size yok"
        assert int(secenek["max-file"]) >= 1, f"{ad}: max-file yok"


def test_the_log_cap_is_bounded_and_small_enough_for_a_pi():
    """Toplam tavan makul olmalı — Pi'nin diski küçük."""
    toplam = 0
    for servis in COMPOSE["services"].values():
        s = servis["logging"]["options"]
        toplam += int(s["max-size"].rstrip("m")) * int(s["max-file"])
    assert toplam <= 200, f"toplam log tavanı {toplam} MB — Pi için fazla"
