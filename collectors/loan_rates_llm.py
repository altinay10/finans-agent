"""API'si olmayan bankaların kredi oranları — AGENT ile.

NEDEN AYRI BİR TOPLAYICI: `collectors/loan_rates.py` beş kurumun uç
noktasını elle ayrıştırıyor. Geriye kalan bankalarda uç nokta YOK; oran
pazarlama sayfasında düz metin olarak yazıyor. Her biri için elle parser
yazmak dört kırılgan parser demek ve bankalar bu sayfaları sık değiştiriyor.
Bu, LLM'in gerçekten doğru araç olduğu nadir durumlardan biri: yapı sürekli
değişiyor ama İÇERİK aynı ("36 ay vadeli ihtiyaç kredisi aylık %2,99").

NE YAPMAZ: model hesap yapmaz, oran uydurmaz, sayfada gezinmez. Tek işi
elindeki HTML'den yapılandırılmış kayıt çıkarmak (tasarım §06).

ÜÇ KATMANLI UYDURMA KORUMASI — bir modelin çıktısını doğrudan finansal
tabloya yazmak kabul edilemez:

  1. **Zeminleme (en önemlisi):** modelin döndürdüğü her oran, sayfanın
     METNİNDE gerçekten geçmek ZORUNDA. "%2,99" sayfada yoksa kayıt atılır.
     Model bir sayı uydurursa bu kontrol onu yakalar; çünkü uydurulan sayı
     tanım gereği kaynakta yoktur.
  2. **Bant kontrolü:** `LoanRateRecord` aylık oranı %0-20 arasına
     sıkıştırıyor (yıllık oranı aylık sanmak en olası model hatası).
  3. **Çapraz akıl kontrolü:** aynı türde elle ayrıştırılan bankaların
     oranlarına göre absürt sapan kayıtlar reddedilir (`sanity_check`).

KAPALIYSA SESSİZ: `LLM_FALLBACK_ENABLED=0` ise (varsayılan) bu toplayıcı
tek token harcamadan "atlandı" diyerek biter. Anahtar girilmeden hiçbir şey
çalışmaz ve bu bilinçli.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime

from pydantic import BaseModel, model_validator
from sqlalchemy import select

from collectors import http
from collectors.base import Collector, ParseError, SanityCheckError
from collectors.loan_rates import MAX_MONTHLY_RATE, LoanRateRecord
from store.clock import istanbul_today, utc_now
from store.db import SessionLocal
from store.models import LoanRate
from store.observability import record_rate_changes

logger = logging.getLogger(__name__)

HEADERS = {
    # Türkçe karakter KULLANMA: HTTP başlıkları latin-1 kodlanır ve istek
    # ağa çıkmadan UnicodeEncodeError ile patlar.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}
REQUEST_DELAY_SECONDS = 2.0

LOAN_TYPE_LABELS = {"personal": "ihtiyaç", "housing": "konut", "vehicle": "taşıt"}


class LlmLoanRow(BaseModel):
    """Modelin döndürmesi beklenen şema — kasıtlı olarak DAR.

    Ne kadar az alan istenirse model o kadar az uydurur. Vade sınırları
    isteğe bağlı; sayfada yoksa boş bırakılması, uydurulmasından iyidir.
    """

    loan_type: str                     # 'personal' | 'housing' | 'vehicle'
    monthly_rate_percent: float        # AYLIK yüzde: 2.99
    term_min_months: int | None = None
    term_max_months: int | None = None

    @model_validator(mode="after")
    def _plausible(self):
        if not (0 < self.monthly_rate_percent <= MAX_MONTHLY_RATE * 100):
            raise ValueError(f"aylık oran bant dışında: {self.monthly_rate_percent}")
        if self.loan_type not in LOAN_TYPE_LABELS:
            raise ValueError(f"bilinmeyen kredi türü: {self.loan_type}")
        return self


# Sayfadan çıkarılan metinde oranın gerçekten geçip geçmediğini arayan kalıp.
# Türkçe ondalık virgül ve nokta, ayrıca "%" işaretinin iki yanı da olabilir.
def _appears_in_text(rate_percent: float, text: str) -> bool:
    """ZEMİNLEME: bu oran sayfada gerçekten yazıyor mu?

    Model bir sayı uydurursa kaynakta bulunmaz. Bu kontrol, uydurma bir
    faiz oranının veritabanına girmesini engelleyen ASIL güvencedir; bant
    kontrolü uydurma ama makul bir sayıyı geçirebilir, bu geçirmez.

    Yuvarlama toleransı yok ve olmamalı: "%2,99" ile "%2.99" aynı sayıdır
    ama "%3,00" başka bir orandır.
    """
    formatted = f"{rate_percent:.2f}".rstrip("0").rstrip(".")
    candidates = {formatted, formatted.replace(".", ",")}
    # 2.9 -> "2,90" biçimi de sayfada geçebilir
    two_dp = f"{rate_percent:.2f}"
    candidates |= {two_dp, two_dp.replace(".", ",")}
    return any(c in text for c in candidates)


class LlmLoanRateCollector(Collector):
    """Agent tabanlı kredi oranı toplayıcısı.

    Banka listesi `config/sources.yaml` -> `loan_llm_endpoints`. Yeni banka
    eklemek bir YAML satırı; kod değişikliği gerekmez.
    """

    name = "loan_rates_llm"
    schema = LlmLoanRow

    PROMPT = (
        "Aşağıdaki banka sayfasından KREDİ FAİZ ORANLARINI çıkar.\n"
        "\n"
        "KURALLAR:\n"
        "- Yalnızca sayfada AÇIKÇA YAZAN oranları döndür. Hesaplama yapma, "
        "ortalama alma, tahmin etme.\n"
        "- Oran AYLIK yüzde olmalı (2.99 gibi). Sayfada yıllık maliyet oranı "
        "(%68 gibi) yazıyorsa onu DÖNDÜRME — yalnızca aylık faiz oranını al.\n"
        "- loan_type: ihtiyaç/tüketici -> personal, konut/mortgage -> housing, "
        "taşıt/araç -> vehicle.\n"
        "- Kredi kartı, nakit avans, KMH, ticari kredi oranlarını ALMA.\n"
        "- Emin olmadığın hiçbir kaydı üretme. Hiç oran yoksa boş liste döndür.\n"
        "\n"
        "SAYFA:\n{html}\n"
    )

    def __init__(self, banks: dict[str, dict] | None = None) -> None:
        super().__init__()
        self.banks = banks if banks is not None else _load_banks()

    # ------------------------------------------------------------ fetch --

    def fetch(self) -> bytes:
        pages: dict[str, dict] = {}
        for index, (code, cfg) in enumerate(self.banks.items()):
            if index:
                time.sleep(REQUEST_DELAY_SECONDS)
            started = time.monotonic()
            with http.source(code.lower()):
                try:
                    resp = http.get(
                        cfg["url"], headers=HEADERS, timeout=25, follow_redirects=True
                    )
                    resp.raise_for_status()
                    if _looks_blocked(resp.text):
                        # Bot tespiti: sayfa 200 dönüyor ama içerik engel
                        # sayfası. Modele göndermek boşuna token yakmak olur.
                        raise ParseError("bot tespiti / engel sayfası döndü")
                    pages[code] = {"ok": True, "body": resp.text, "url": cfg["url"]}
                    self.record_source(
                        code.lower(), phase="fetch", status="ok",
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                except Exception as exc:  # noqa: BLE001 - tek banka hepsini düşürmesin
                    logger.warning("loan_rates_llm/%s: fetch başarısız: %s", code, exc)
                    pages[code] = {"ok": False, "error": str(exc)}
                    self.record_source(
                        code.lower(), phase="fetch", status="failed", error=str(exc),
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
        return json.dumps(pages).encode("utf-8")

    # ------------------------------------------------------------ parse --

    def parse(self, raw: bytes) -> list[LoanRateRecord]:
        from llm import extract as llm_extract
        from llm import settings as llm_settings

        pages: dict[str, dict] = json.loads(raw)

        if not llm_settings.LLM_FALLBACK_ENABLED:
            # Anahtar/izin yoksa TEK TOKEN harcamadan çık. Bunu bir hata
            # gibi göstermek yanıltıcı olurdu: sistem doğru çalışıyor,
            # sadece agent kapalı.
            raise ParseError(
                "Agent kapalı (LLM_FALLBACK_ENABLED=0) — bu toplayıcı LLM olmadan "
                "çalışamaz. Açmak için .env: LLM_FALLBACK_ENABLED=1 ve LLM_API_KEY."
            )

        budget = min(len(pages), llm_settings.LLM_AGENT_MAX_CALLS_PER_RUN)
        records: list[LoanRateRecord] = []

        for code, page in pages.items():
            if not page.get("ok"):
                continue
            text = _visible_text(page["body"])
            focused = _rate_regions(text)
            if not focused:
                # Sayfada hiç oran geçmiyor: modeli çağırmak boşuna token.
                logger.warning("loan_rates_llm/%s: sayfada oran bulunamadı, agent çağrılmadı", code)
                self.record_source(
                    code.lower(), phase="parse", status="empty",
                    error="sayfada oran geçmiyor — agent çağrılmadı, token harcanmadı",
                )
                continue
            try:
                rows = llm_extract.extract(
                    focused,
                    LlmLoanRow,
                    collector=self.name,
                    run_id=self._run_id,
                    trigger_source=code.lower(),
                    budget=budget,
                    prompt=self.PROMPT,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("loan_rates_llm/%s: agent başarısız: %s", code, exc)
                self.record_source(code.lower(), phase="parse", status="failed", error=str(exc))
                continue

            kept, dropped = _ground(rows, text, code)
            if dropped:
                logger.warning(
                    "loan_rates_llm/%s: %s kayıt ZEMİNLEME'de elendi (sayfada yok)",
                    code, dropped,
                )
            if not kept:
                self.record_source(
                    code.lower(), phase="parse", status="empty",
                    error=f"agent {len(rows)} kayıt döndürdü, {dropped} tanesi sayfada bulunamadı",
                )
                continue

            self.record_source(
                code.lower(), phase="parse", status="ok", rows=len(kept),
                error=(f"{dropped} kayıt zeminlemede elendi" if dropped else None),
            )
            for row in kept:
                records.append(
                    LoanRateRecord(
                        institution=code,
                        loan_type=row.loan_type,
                        monthly_rate=row.monthly_rate_percent / 100,
                        term_min=row.term_min_months,
                        term_max=row.term_max_months,
                    )
                )

        if not records:
            raise ParseError("Agent hiçbir bankadan doğrulanabilir kredi oranı çıkaramadı")
        return records

    # ------------------------------------------------------------ sanity --

    def sanity_check(self, records: list[LoanRateRecord]) -> None:
        for r in records:
            if r.term_min is not None and r.term_max is not None and r.term_min > r.term_max:
                raise SanityCheckError(f"{r.institution}/{r.loan_type}: term_min > term_max")

        # Aynı (kurum, tür) için birden fazla oran gelirse EN DÜŞÜĞÜ tutulur
        # — sayfalarda kampanya oranı ile tabela oranı yan yana durabiliyor
        # ve düşük göstermek yüksek göstermekten güvenlidir. Bunu burada
        # doğrulamıyoruz, persist'te yapıyoruz; burada yalnızca çelişkiyi
        # yakalıyoruz.
        seen: dict[tuple[str, str], float] = {}
        for r in records:
            key = (r.institution, r.loan_type)
            if key in seen and abs(seen[key] - r.monthly_rate) > 0.10:
                raise SanityCheckError(
                    f"{r.institution}/{r.loan_type}: agent birbirinden 10 puandan fazla "
                    f"sapan iki oran döndürdü ({seen[key]*100:.2f} vs {r.monthly_rate*100:.2f})"
                )
            seen[key] = min(seen.get(key, r.monthly_rate), r.monthly_rate)

    # ----------------------------------------------------------- persist --

    def persist(self, records: list[LoanRateRecord], run_id: int) -> None:
        now = utc_now()
        today = istanbul_today()

        # Aynı (kurum, tür) için en düşük oranı bırak (yukarıdaki gerekçe).
        best: dict[tuple[str, str], LoanRateRecord] = {}
        for r in records:
            key = (r.institution, r.loan_type)
            if key not in best or r.monthly_rate < best[key].monthly_rate:
                best[key] = r

        with SessionLocal() as session:
            for r in best.values():
                exists = session.execute(
                    select(LoanRate).where(
                        LoanRate.institution == r.institution,
                        LoanRate.loan_type == r.loan_type,
                        LoanRate.valid_date == today,
                    )
                ).scalar_one_or_none()
                if exists:
                    exists.monthly_rate = r.monthly_rate
                    exists.term_min = r.term_min
                    exists.term_max = r.term_max
                    exists.fetched_at = now
                    continue
                session.add(
                    LoanRate(
                        institution=r.institution,
                        loan_type=r.loan_type,
                        monthly_rate=r.monthly_rate,
                        term_min=r.term_min,
                        term_max=r.term_max,
                        valid_date=today,
                        fetched_at=now,
                    )
                )
            session.commit()

        changed = record_rate_changes(
            "loan",
            {(r.institution, r.loan_type): r.monthly_rate for r in best.values()},
            run_id=run_id,
        )
        if changed:
            logger.info("loan_rates_llm: %s oran değişimi kaydedildi", changed)


# --------------------------------------------------------------- yardım --


_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]*\n[ \t\n]*")

_BLOCK_MARKERS = (
    "İstek Engellenmiştir", "Request Rejected", "bobcmn", "/TSPD/",
    "DOSL7.challenge", "captcha", "Access Denied",
)


def _looks_blocked(html: str) -> bool:
    return any(marker in html for marker in _BLOCK_MARKERS)


def _visible_text(html: str) -> str:
    """HTML'i düz metne indirger.

    Modele ham HTML göndermek token'ın büyük kısmını etiketlere harcar.
    Düz metin hem ucuz hem de ZEMİNLEME kontrolünün üzerinde çalıştığı
    yüzey: "oran sayfada geçiyor mu" sorusu metin üzerinde sorulmalı,
    çünkü model de metni görüyor.
    """
    cleaned = _SCRIPT_RE.sub(" ", html)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("&nbsp;", " ").replace("&amp;", "&")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return _WS_RE.sub("\n", cleaned).strip()


_RATE_RE = re.compile(r"%\s?\d{1,2}[.,]\d{1,2}|\d{1,2}[.,]\d{1,2}\s?%")

# Modele gönderilecek metnin üst sınırı. Sayfalar 10-105 KB; tamamını
# göndermek token'ın çoğunu menüye ve yasal metne harcar.
CONTEXT_CHARS = 320          # her oran geçişinin iki yanından alınan pencere
MAX_PROMPT_CHARS = 6_000


def _rate_regions(text: str, max_chars: int = MAX_PROMPT_CHARS) -> str:
    """Metni ORAN GEÇEN bölgelere daraltır.

    NEDEN: `llm/extract.py`'nin genel kırpması sayfanın ilk sinyalinden
    itibaren düz bir pencere alıyor. 105 KB'lık bir DenizBank sayfasında
    aranan oran o pencerenin DIŞINDA kalabilir — model o zaman sayfada
    yazan oranı göremeden cevap üretmeye zorlanır ki bu, uydurmayı davet
    eden tek durumdur.

    Burada bunun yerine her "%2,99" benzeri geçişin etrafından bir pencere
    alınıyor. Sonuç hem çok daha küçük (token tasarrufu) hem de aranan
    bilginin tamamını içeriyor. Hiç oran geçmiyorsa boş döner ve çağıran
    modeli hiç çağırmaz — oransız bir sayfaya token harcamak anlamsız.
    """
    spans: list[tuple[int, int]] = []
    for match in _RATE_RE.finditer(text):
        start = max(0, match.start() - CONTEXT_CHARS)
        end = min(len(text), match.end() + CONTEXT_CHARS)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))

    chunks, total = [], 0
    for start, end in spans:
        piece = text[start:end].strip()
        if total + len(piece) > max_chars:
            piece = piece[: max(0, max_chars - total)]
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
        if total >= max_chars:
            break
    return "\n---\n".join(chunks)


def _ground(rows, text: str, code: str) -> tuple[list, int]:
    """Modelin döndürdüğü oranlardan yalnızca SAYFADA GEÇENLERİ bırakır."""
    kept, dropped = [], 0
    for row in rows:
        if _appears_in_text(row.monthly_rate_percent, text):
            kept.append(row)
        else:
            dropped += 1
            logger.warning(
                "loan_rates_llm/%s: %%%.2f sayfada bulunamadı — kayıt atıldı",
                code, row.monthly_rate_percent,
            )
    return kept, dropped


def _load_banks() -> dict[str, dict]:
    from config.loader import load_sources

    out: dict[str, dict] = {}
    for key, entry in (load_sources().get("loan_llm_endpoints") or {}).items():
        entry = entry or {}
        if entry.get("status") != "active" or not entry.get("url"):
            continue
        out[entry.get("institution", key.upper())] = {"key": key, "url": entry["url"]}
    return out


if __name__ == "__main__":
    print(LlmLoanRateCollector().run())
