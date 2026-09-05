"""Fon fiyat toplayıcısı — çok sağlayıcılı.

`config/funds.yaml`'daki her fon, kendi sağlayıcısı üzerinden çekilir
(bkz. collectors/fund_providers.py). Eski `collectors/akportfoy.py`'nin
yerini alır; tek sağlayıcıya bağlı olması, kullanıcının "istediğim fonu
ekleyebileyim" isteğinin önündeki asıl engeldi.

DAYANIKLILIK: bir fonun sayfası bozulduğunda diğerleri toplanmaya devam
eder (fx_banks/deposit_rates ile aynı desen). Her fon `source_runs`'a
kendi satırını yazar — "hangi fon düştü" sorusu ancak böyle cevaplanır.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel
from sqlalchemy import select

from collectors import http
from collectors.base import Collector, ParseError, SanityCheckError
from collectors.fund_providers import (
    PROVIDERS, REQUEST_DELAY_SECONDS, FundSpec, ResolvedFund, build_providers,
)
from config.loader import funds_yaml_path, writable_funds_yaml
from store.clock import istanbul_today, utc_now
from store.db import REPO_ROOT, SessionLocal
from store.models import Fund, FundPrice

logger = logging.getLogger(__name__)

# Geriye dönük uyum: bu isim başka modüllerde/testlerde kullanılıyor olabilir.
FUNDS_YAML = REPO_ROOT / "config" / "funds.yaml"

# Tek günde bundan fazla hareket, fiyat serisinde bir ölçek hatası
# (kuruş/lira karışması, bölünme) işaretidir. Fon fiyatları böyle
# sıçramaz; sıçrıyorsa veri yazılmaz.
SANITY_MAX_DAILY_MOVE = 0.30

# ARDIŞIK NOKTALAR ARASI eşiğin gün sayısıyla nasıl büyüyeceği.
#
# NEDEN GEREKLİ: Deniz Portföy ve Yapı Kredi Portföy serilerinin uzak
# geçmişi bilerek SEYREKTİR (haftalık/aylık noktalar; bkz. ilgili
# sağlayıcı modüllerinin ızgara açıklamaları). Eşik sabit %30 kalsaydı,
# iki aylık noktası arasındaki tamamen normal bir %35'lik hisse fonu
# yükselişi SanityCheckError fırlatır ve o koşuda HİÇBİR FON yazılmazdı —
# yani sağlam bir veri korumasını, veriyi topluca engelleyen bir arızaya
# çevirirdik.
#
# Eşik gün sayısının KAREKÖKÜYLE büyür (rassal yürüyüş: oynaklık ~ √t).
# Doğrusal büyütmek koruma bırakmazdı, sabit bırakmak yanlış alarm üretirdi.
SANITY_MAX_TOTAL_MOVE = 3.00   # üst sınır: %300'ü aşan sıçrama her hâlükârda şüpheli


def max_move_for_gap(gap_days: int) -> float:
    """`gap_days` gün arayla iki fiyat arasında kabul edilebilir en büyük oran.

    1 gün -> %30 (eski davranış aynen korunur), 7 gün -> ~%79,
    30 gün -> ~%164, 90+ gün -> tavan olan %300.
    """
    gap = max(1, gap_days)
    return min(SANITY_MAX_DAILY_MOVE * (gap ** 0.5), SANITY_MAX_TOTAL_MOVE)


class FundPriceRecord(BaseModel):
    fund_code: str
    price_date: date
    price: float
    fund_name: str | None = None
    is_equity_heavy: bool | None = None


def load_fund_registry(path: Path | None = None) -> list[FundSpec]:
    """config/funds.yaml -> FundSpec listesi.

    Bilinmeyen sağlayıcılı satırlar ATLANIR ve loglanır: panelden elle
    eklenen bir satırdaki yazım hatası tüm toplamayı düşürmemeli.
    """
    path = path or funds_yaml_path()
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        body = yaml.safe_load(f) or {}
    specs: list[FundSpec] = []
    for row in body.get("funds", []) or []:
        provider = row.get("provider")
        if provider not in PROVIDERS:
            logger.warning(
                "funds.yaml: '%s' fonunun sağlayıcısı tanınmıyor (%s) — atlanıyor",
                row.get("code"), provider,
            )
            continue
        specs.append(
            FundSpec(
                code=row["code"],
                provider=provider,
                ref=str(row.get("ref") or row["code"]),
                name=row.get("name"),
                benchmark=row.get("benchmark"),
            )
        )
    return specs


def resolve_fund(code: str) -> ResolvedFund | None:
    """Fon KODUNDAN sağlayıcıyı ve adres parçasını bul.

    Kullanıcı isteği (2026-09-04): "sadece fon koduyla, url olmadan".
    Sağlayıcılar sırayla denenir; ilk tanıyan kazanır. Fon kodları
    sağlayıcılar arasında çakışmıyor (her portföy şirketinin kodu kendi ön
    ekiyle başlıyor), o yüzden sıra sonucu değiştirmiyor.

    Bulunamazsa None döner — çağıran kullanıcıya "bu kod tanınmadı" der.
    Sessizce yanlış bir sağlayıcıya yazmaktansa hiç yazmamak doğrusu.
    """
    code = (code or "").strip().upper()
    if not code:
        return None
    # Taze kopya: sağlayıcıların dizin önbelleği örnek ömrü boyunca yaşıyor.
    # Modül düzeyindeki PROVIDERS kullanılsaydı, panel açıkken bir kez
    # okunan fon dizini süreç kapanana kadar bayat kalırdı.
    for provider in build_providers().values():
        try:
            bulunan = provider.resolve(code)
        except Exception as exc:  # noqa: BLE001 - bir sağlayıcı diğerini engellemesin
            logger.warning("fon çözümleme (%s/%s) başarısız: %s", provider.key, code, exc)
            continue
        if bulunan is not None:
            return bulunan
    return None


def add_fund_by_code(code: str, *, benchmark: str | None = None) -> ResolvedFund:
    """Fonu YALNIZCA koduyla kayıt defterine ekle.

    Adres parçasını kullanıcıya sordurmak hem zahmetliydi hem de yanlış
    yapıştırmaya açıktı; canlıda tam olarak o oldu (bkz.
    GarantiPortfoyProvider.fetch'teki kod doğrulaması). Adres artık
    sağlayıcının kendi dizininden okunuyor, elle girilmiyor.
    """
    bulunan = resolve_fund(code)
    if bulunan is None:
        raise ValueError(
            f"{(code or '').strip().upper()} hiçbir sağlayıcıda bulunamadı. "
            f"Desteklenen sağlayıcılar: "
            + ", ".join(p.label for p in PROVIDERS.values())
        )
    add_fund_to_registry(
        code=bulunan.code, provider=bulunan.provider, ref=bulunan.ref,
        name=bulunan.name, benchmark=benchmark,
    )
    return bulunan


def add_provider_catalog(provider_key: str) -> list[ResolvedFund]:
    """Bir sağlayıcının TÜM fonlarını kayıt defterine ekle.

    NEDEN VAR: kullanıcının şikâyeti "fon sayısı çok az"dı ve fonları
    tek tek koduyla eklemek, 76 fonluk bir katalog için 76 ayrı işlem
    demekti. Katalog sağlayıcının kendi dizininden tek istekte okunuyor,
    yani ekleme ucuz; fiyat toplama maliyeti ise gece koşusuna dağılıyor.

    Zaten kayıtlı olan kodlar sessizce atlanır — çağrı yeniden
    çalıştırılabilir olsun diye. Dönen liste GERÇEKTEN EKLENENLERDİR.
    """
    provider = build_providers().get(provider_key)
    if provider is None:
        raise ValueError(f"{provider_key}: böyle bir sağlayıcı yok")
    mevcut = {spec.code.upper() for spec in load_fund_registry()}
    eklenen: list[ResolvedFund] = []
    for kayit in provider.catalog():
        if kayit.code.upper() in mevcut:
            continue
        add_fund_to_registry(
            code=kayit.code, provider=kayit.provider, ref=kayit.ref, name=kayit.name,
        )
        mevcut.add(kayit.code.upper())
        eklenen.append(kayit)
    return eklenen


def collect_one(code: str):
    """TEK fonun fiyat serisini hemen çek.

    NEDEN: panelden fon eklenince ne grafik ne fiyat geliyordu; kayıt
    defterine satır yazılıyor ama seri bir sonraki planlı koşuya (21:00)
    kadar boş kalıyordu. Kullanıcı için bu "eklendi ama çalışmıyor" demek.
    Ekleme kullanıcının BAŞLATTIĞI bir eylem olduğu için tek fonluk bir
    çekim burada meşru — panelin kendi kendine toplama yapmaması kuralı
    (tasarım §01) planlı/otomatik trafiği kastediyor.
    """
    spec = next((s for s in load_fund_registry() if s.code == code.strip().upper()), None)
    if spec is None:
        raise ValueError(f"{code}: kayıt defterinde yok")
    return FundPriceCollector(specs=[spec]).run(trigger="manual")


def add_fund_to_registry(
    *, code: str, provider: str, ref: str, name: str | None = None,
    benchmark: str | None = None, path: Path | None = None,
) -> None:
    """Panelden fon ekleme — YAML'a bir satır yazar.

    Neden YAML'a yazıyoruz da doğrudan veritabanına değil: kayıt defteri
    tek doğruluk kaynağı olmalı. Fon yalnızca veritabanına eklenseydi,
    `seed_reference_data` bir sonraki açılışta onu "kayıt defterinde yok"
    diye silerdi.
    """
    path = path or writable_funds_yaml()
    body = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            body = yaml.safe_load(f) or {}
    funds = body.get("funds") or []
    if any((row.get("code") or "").upper() == code.upper() for row in funds):
        raise ValueError(f"{code} zaten kayıtlı")
    entry = {"code": code.upper(), "provider": provider, "ref": ref}
    if name:
        entry["name"] = name
    if benchmark:
        entry["benchmark"] = benchmark
    funds.append(entry)
    body["funds"] = funds

    # Dosyanın BAŞLIK YORUMLARI korunur. yaml.safe_dump yorumları atar;
    # ilk panelden ekleme, alanların ne anlama geldiğini ve
    # `is_equity_heavy`in neden burada tutulmadığını anlatan açıklamaları
    # sessizce silmişti. Bir sonraki okuyan (insan ya da model) o bilgiyi
    # kaybetmesin diye `funds:` anahtarından önceki blok aynen geri yazılır.
    header = ""
    if path.exists():
        original = path.read_text(encoding="utf-8")
        marker = original.find("funds:")
        if marker > 0:
            header = original[:marker]

    with open(path, "w", encoding="utf-8") as f:
        if header:
            f.write(header)
        yaml.safe_dump(body, f, allow_unicode=True, sort_keys=False)


class FundPriceCollector(Collector):
    name = "fund_prices"
    schema = FundPriceRecord

    def __init__(self, specs: list[FundSpec] | None = None) -> None:
        super().__init__()
        self.specs = specs if specs is not None else load_fund_registry()
        # KOŞUYA ÖZEL sağlayıcı kopyaları. Modül düzeyindeki paylaşılan
        # PROVIDERS sözlüğü kullanılamaz: sağlayıcılar örnek düzeyinde
        # önbellek tutuyor (Yapı Kredi Portföy fon dizinini sitemap'ten bir
        # kez okuyup saklıyor). Paylaşılan örnekte bu dizin süreç ömrü
        # boyunca bayat kalır, yeni açılan bir fon hiç bulunamazdı.
        self.providers = build_providers()

    def _known_dates(self) -> dict[str, frozenset[date]]:
        """SAĞLAYICI başına, veritabanında zaten olan fiyat tarihleri.

        Fon başına değil SAĞLAYICI başına: Deniz Portföy'de tek bir istek
        97 fonun o günkü fiyatını birden getiriyor, yani "bu gün elimizde
        var mı" sorusunun cevabı fon değil sağlayıcı ölçeğinde anlamlı.
        Bu küme olmadan seyrek seri toplayan sağlayıcılar her gece aynı
        yüzlerce isteği baştan atardı.
        """
        by_provider: dict[str, set[str]] = {}
        for spec in self.specs:
            by_provider.setdefault(spec.provider, set()).add(spec.code)
        sonuc: dict[str, frozenset[date]] = {}
        with SessionLocal() as session:
            for provider_key, codes in by_provider.items():
                tarihler = session.execute(
                    select(FundPrice.price_date)
                    .where(FundPrice.fund_code.in_(codes))
                    .distinct()
                ).scalars().all()
                sonuc[provider_key] = frozenset(tarihler)
        return sonuc

    def fetch(self) -> bytes:
        payloads: dict[str, dict] = {}
        known = self._known_dates()
        for index, spec in enumerate(self.specs):
            if index:
                time.sleep(REQUEST_DELAY_SECONDS)
            provider = self.providers[spec.provider]
            started = time.monotonic()
            with http.source(spec.code):
                try:
                    payloads[spec.code] = {
                        "ok": True,
                        "provider": spec.provider,
                        "ref": spec.ref,
                        "body": provider.fetch(
                            spec, known_dates=known.get(spec.provider, frozenset())
                        ),
                    }
                    self.record_source(
                        spec.code, phase="fetch", status="ok",
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                except Exception as exc:  # noqa: BLE001 - tek fon hepsini düşürmesin
                    logger.warning("fund_prices/%s: fetch başarısız: %s", spec.code, exc)
                    payloads[spec.code] = {"ok": False, "error": str(exc)}
                    self.record_source(
                        spec.code, phase="fetch", status="failed", error=str(exc),
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
        self._record_provider_summary(payloads)
        return json.dumps(payloads).encode("utf-8")

    def _record_provider_summary(self, payloads: dict[str, dict]) -> None:
        """Fon başına satırın YANINDA, SAĞLAYICI başına bir özet satır.

        İki farklı soruya iki farklı kayıt gerekiyor:
          * "hangi FON düştü"       -> source = fon kodu (yukarıdaki döngü)
          * "SAĞLAYICI ayakta mı"   -> source = sağlayıcı anahtarı (burası)
        İkincisi olmadan Kaynaklar sekmesi, `config/sources.yaml`'daki
        `fund_endpoints.akportfoy` satırını hiçbir koşuyla eşleştiremez ve
        çalışan bir kaynağı "başarılı koşu yok" diye işaretler.
        """
        by_provider: dict[str, list[bool]] = {}
        for spec in self.specs:
            entry = payloads.get(spec.code) or {}
            by_provider.setdefault(spec.provider, []).append(bool(entry.get("ok")))
        for provider, results in by_provider.items():
            ok_count = sum(results)
            self.record_source(
                provider,
                phase="fetch",
                status="ok" if ok_count else "failed",
                rows=ok_count,
                error=(
                    None if ok_count == len(results)
                    else f"{len(results) - ok_count}/{len(results)} fon çekilemedi"
                ),
            )

    def parse(self, raw: bytes) -> list[FundPriceRecord]:
        payloads: dict[str, dict] = json.loads(raw)
        by_code = {spec.code: spec for spec in self.specs}
        records: list[FundPriceRecord] = []

        for code, entry in payloads.items():
            if not entry.get("ok"):
                continue
            spec = by_code.get(code) or FundSpec(
                code=code, provider=entry.get("provider", ""), ref=entry.get("ref", code)
            )
            provider = self.providers.get(spec.provider)
            if provider is None:
                self.record_source(
                    code, phase="parse", status="failed",
                    error=f"sağlayıcı tanınmıyor: {spec.provider}",
                )
                continue
            try:
                series = provider.parse(spec, entry["body"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("fund_prices/%s: parse başarısız: %s", code, exc)
                self.record_source(code, phase="parse", status="failed", error=str(exc))
                continue

            if not series.points:
                self.record_source(
                    code, phase="parse", status="empty",
                    error="sıfır fiyat noktası — sayfa şeması değişmiş olabilir",
                )
                continue
            self.record_source(code, phase="parse", status="ok", rows=len(series.points))
            for price_date, price in series.points:
                records.append(
                    FundPriceRecord(
                        fund_code=series.code,
                        price_date=price_date,
                        price=price,
                        fund_name=series.name,
                        is_equity_heavy=series.is_equity_heavy,
                    )
                )

        if not records:
            raise ParseError("Hiçbir fon için fiyat satırı bulunamadı")
        return records

    def sanity_check(self, records: list[FundPriceRecord]) -> None:
        today = istanbul_today()
        by_fund: dict[str, list[FundPriceRecord]] = {}
        for r in records:
            if r.price <= 0:
                raise SanityCheckError(f"{r.fund_code} {r.price_date}: fiyat pozitif olmalı")
            if r.price_date > today:
                raise SanityCheckError(f"{r.fund_code}: gelecek tarihli fiyat ({r.price_date})")
            by_fund.setdefault(r.fund_code, []).append(r)

        for code, rows in by_fund.items():
            rows.sort(key=lambda r: r.price_date)
            dates = [r.price_date for r in rows]
            if len(dates) != len(set(dates)):
                raise SanityCheckError(f"{code}: aynı tarih için birden fazla fiyat var")
            for prev, cur in zip(rows, rows[1:]):
                move = abs(cur.price - prev.price) / prev.price
                gap = (cur.price_date - prev.price_date).days
                limit = max_move_for_gap(gap)
                if move > limit:
                    raise SanityCheckError(
                        f"{code}: {prev.price_date}->{cur.price_date} arası "
                        f"({gap} gün) %{move*100:.1f} hareket — sınır %{limit*100:.1f}"
                    )

    def persist(self, records: list[FundPriceRecord], run_id: int) -> None:
        now = utc_now()
        benchmarks = {spec.code: spec.benchmark for spec in self.specs}
        with SessionLocal() as session:
            # Fon kataloğunu sağlayıcıdan okunan GERÇEK ad/sınıflandırmayla hizala.
            meta: dict[str, FundPriceRecord] = {}
            for r in records:
                if r.fund_code not in meta and r.fund_name:
                    meta[r.fund_code] = r
            for code, r in meta.items():
                fund = session.get(Fund, code)
                if fund is None:
                    session.add(
                        Fund(
                            code=code,
                            name=r.fund_name,
                            is_equity_heavy=bool(r.is_equity_heavy),
                            benchmark=benchmarks.get(code),
                        )
                    )
                else:
                    fund.name = r.fund_name or fund.name
                    if r.is_equity_heavy is not None:
                        fund.is_equity_heavy = r.is_equity_heavy
                    if benchmarks.get(code):
                        fund.benchmark = benchmarks[code]

            existing = {
                (fp.fund_code, fp.price_date): fp
                for fp in session.execute(
                    select(FundPrice).where(
                        FundPrice.fund_code.in_({r.fund_code for r in records})
                    )
                ).scalars()
            }
            for r in records:
                found = existing.get((r.fund_code, r.price_date))
                if found is not None:
                    found.price = r.price
                    found.fetched_at = now
                else:
                    session.add(
                        FundPrice(
                            fund_code=r.fund_code,
                            price_date=r.price_date,
                            price=r.price,
                            fetched_at=now,
                        )
                    )
            session.commit()


if __name__ == "__main__":
    print(FundPriceCollector().run())
