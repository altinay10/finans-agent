"""taxes.yaml okuma + tarih aralıklı çözümleme.

core/ saf kalsın diye YAML I/O'su burada yapılır; app/ ve worker.py bu
modülden çözülmüş oranları alıp core/ fonksiyonlarına parametre olarak verir.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXES_YAML = REPO_ROOT / "config" / "taxes.yaml"


@lru_cache(maxsize=1)
def _load_taxes() -> dict:
    with open(TAXES_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _pick_for_date(entries: list[dict], on_date: date) -> dict:
    applicable = [e for e in entries if e["valid_from"] <= on_date]
    if not applicable:
        raise ValueError(f"{on_date} için geçerli bir kayıt yok (en eski valid_from bundan sonra)")
    return max(applicable, key=lambda e: e["valid_from"])


def resolve_loan_taxes(loan_type: str, on_date: date | None = None) -> tuple[float, float]:
    """Returns (kkdf, bsmv) for the given loan type and date."""
    on_date = on_date or date.today()
    taxes = _load_taxes()
    entries = taxes["loans"][loan_type]
    entry = _pick_for_date(entries, on_date)
    return entry["kkdf"], entry["bsmv"]


def resolve_deposit_brackets(currency: str, on_date: date | None = None) -> list[dict]:
    on_date = on_date or date.today()
    taxes = _load_taxes()
    entries = taxes["deposits"][currency]
    entry = _pick_for_date(entries, on_date)
    return entry["brackets"]


def resolve_fund_withholding(is_equity_heavy: bool) -> float:
    taxes = _load_taxes()
    if is_equity_heavy:
        return taxes["funds"]["equity_heavy"]
    return taxes["funds"]["default"]


def clear_cache() -> None:
    """Testlerde veya YAML elle düzenlendikten sonra kullan.

    İKİSİ DE temizlenmeli. `load_sources` unutulduğunda panel, süreç
    yeniden başlatılana kadar eski envanteri gösterir — canlıda tam olarak
    bu yaşandı: sources.yaml'a yeni bölümler eklendi, panel görmedi.
    """
    _load_taxes.cache_clear()
    load_sources.cache_clear()


# --------------------------------------------------------------- sources --
#
# sources.yaml artık yalnızca geliştirici belgesi değil, PANELİN VERİSİ.
# "Bu sayı nereden geldi?" ve "bu bankada neden bu ürün yok?" sorularının
# cevabı buradan okunur. İkinci bir liste tutmak (README, sabit sözlük)
# kaçınılmaz olarak kayar; tek doğruluk kaynağı bu dosyadır.

SOURCES_YAML = REPO_ROOT / "config" / "sources.yaml"

LOAN_TYPES = ("personal", "housing", "vehicle")

# sources.yaml'daki veri kümesi anahtarı -> panelde görünen ad.
DATASET_LABELS = {
    "fx_endpoints": "Döviz kuru",
    "deposit_endpoints": "Mevduat / katılma hesabı",
    "loan_endpoints": "Kredi oranı",
    "loan_llm_endpoints": "Kredi oranı (agent)",
    "profit_share_endpoints": "Kâr paylaşım oranı",
    "fund_endpoints": "Fon fiyatı",
}


@lru_cache(maxsize=1)
def load_sources() -> dict:
    with open(SOURCES_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def loan_coverage() -> dict[str, dict]:
    """Kurum kodu -> {status, covers, missing, blocked_reason, url}.

    Panel bunu "İhtiyaç: 5 banka · Konut: 3 · Taşıt: 1" satırını ve eksik
    bankaların GEREKÇESİNİ basmak için kullanır. Kullanıcının "neden sadece
    3 banka var" sorusunun cevabı veride değil, bu gerekçelerdeydi ve
    yalnızca YAML yorumlarında duruyordu.
    """
    endpoints = load_sources().get("loan_endpoints", {}) or {}
    result: dict[str, dict] = {}
    for key, entry in endpoints.items():
        entry = entry or {}
        code = entry.get("institution")
        if not code:
            continue
        result[code] = {
            "key": key,
            "status": entry.get("status", "unverified"),
            "covers": list(entry.get("covers") or []),
            "missing": dict(entry.get("missing") or {}),
            "blocked_reason": _clean(entry.get("blocked_reason")),
            "url": entry.get("url") or entry.get("base_url"),
            "last_verified": entry.get("last_verified"),
        }
    return result


def source_inventory() -> list[dict]:
    """Panelin "Kaynaklar" sekmesini besleyen düz liste.

    Her satır bir (veri kümesi, kurum) çifti. `robots_override` bayrağı
    açıkça taşınır: kullanıcı 2026-08-24'te robots.txt kısıtlarını göz ardı
    etme kararı verdi ve hangi kaynakların bu karara dayandığı ekranda
    görünmelidir — karar geri alınırsa kapatılacak liste tam olarak budur.
    """
    sources = load_sources()
    rows: list[dict] = []
    for dataset, label in DATASET_LABELS.items():
        for key, entry in (sources.get(dataset) or {}).items():
            entry = entry or {}
            rows.append(
                {
                    "dataset": dataset,
                    "dataset_label": label,
                    "key": key,
                    "institution": _institution_of(dataset, key, entry),
                    "url": entry.get("url") or entry.get("base_url"),
                    "type": entry.get("type"),
                    "auth": entry.get("auth"),
                    "status": entry.get("status", "unverified"),
                    "robots_override": bool(entry.get("robots_override")),
                    "last_verified": entry.get("last_verified"),
                    "blocked_reason": _clean(entry.get("blocked_reason")),
                    "note": _clean(entry.get("note")),
                    "caveat": _clean(entry.get("caveat")),
                }
            )
    return rows


def _clean(text: str | None) -> str | None:
    """YAML blok skalarındaki satır sonlarını tek boşluğa indirger."""
    if not text:
        return None
    return " ".join(str(text).split())


# Fon sağlayıcıları banka değildir; institutions tablosunda karşılıkları yok.
_NON_INSTITUTION_KEYS = {"akportfoy", "tefas", "garantiportfoy", "isportfoy", "qnbportfoy"}


def _institution_of(dataset: str, key: str, entry: dict) -> str | None:
    """Uç nokta anahtarından kurum kodunu çözer.

    Anahtarlar kurum kodunun küçük harflisi ('akbank' -> 'AKBANK'), bu yüzden
    her girişe elle `institution:` yazmak gereksiz tekrar olurdu. Yine de
    açıkça yazılmışsa o kazanır — ileride anahtarla kodun ayrıştığı bir kaynak
    çıkarsa kırılmasın.
    """
    if entry.get("institution"):
        return entry["institution"]
    if dataset == "fund_endpoints" or key in _NON_INSTITUTION_KEYS:
        return None
    return key.upper()


def endpoint_for(dataset: str, institution: str) -> dict | None:
    """Bir tablonun altına "kaynak" satırı basmak için tek uç nokta kaydı."""
    for row in source_inventory():
        if row["dataset"] == dataset and row["institution"] == institution:
            return row
    return None


def source_summary(dataset: str, institution: str) -> str | None:
    """Panelde tablo altına düşen tek satırlık kaynak künyesi.

    "Bu sayı nereden geldi?" sorusunun cevabı sayının YANINDA olmalı;
    README'de olması sunucuya kurulduktan sonra hiçbir işe yaramıyor.
    """
    row = endpoint_for(dataset, institution)
    if not row or not row.get("url"):
        return None
    host_and_path = str(row["url"]).split("://", 1)[-1]
    parts = [f"Kaynak: `{host_and_path}`"]
    if row.get("type"):
        parts.append(str(row["type"]))
    if row.get("last_verified"):
        parts.append(f"son doğrulama {row['last_verified']:%d.%m.%Y}"
                     if hasattr(row["last_verified"], "strftime")
                     else f"son doğrulama {row['last_verified']}")
    if row.get("robots_override"):
        parts.append("robots.txt kısıtı kullanıcı kararıyla uygulanmıyor")
    return " · ".join(parts)


def source_caveat(dataset: str, institution: str) -> str | None:
    """Kaynağa özgü "bu sayı neyi ölçüyor" uyarısı — varsa tablonun altına.

    NEDEN AYRI ALAN: `note` geliştiriciye yazılmış bir envanter notudur ve
    panelde gösterilmez. `caveat` ise KULLANICIYA gösterilir ve yalnızca tek
    bir iş için vardır: aynı tabloda kıyaslanan sayılardan birinin ÖLÇÜM
    TABANI farklıysa bunu söylemek.

    İlk kullanımı CepteTEB kuru: paneldeki bütün bankalar döviz HESABI kuru
    yayınlarken TEB'in herkese açık ucu nakit/efektif kuru veriyor (makas %9
    vs %2). Sayı yanlış değil ama etiketsiz bırakılırsa "TEB en kötü kuru
    veriyor" diye okunur ki bu yanlış bir kıyas.
    """
    row = endpoint_for(dataset, institution)
    return row.get("caveat") if row else None
