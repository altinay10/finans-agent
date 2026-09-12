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

import html as html_lib
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
    # DEMİRLEME: oranın geçtiği cümlenin birebir kopyası. Modeli, kararını
    # oranın KENDİ cümlesine dayandırmaya zorluyor; sayfanın başka bir
    # yerindeki koşulu bu orana yapıştırmasını engelleyen tek şey bu.
    rate_sentence: str = ""
    term_min_months: int | None = None
    term_max_months: int | None = None
    # Bu oran HERKESE mi açık? Modelin cevaplayabileceği kadar dar bir soru;
    # kararı Python'da vermek mümkün değil (bkz. _ground docstring'i).
    # Varsayılanı YOK: model alanı atlarsa bu görünür bir ayrıştırma hatası
    # olsun, sessizce boşalan bir tablo değil.
    available_to_all: bool
    why: str = ""                      # gerekçe — kayda geçer, denetlenebilir

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

    SAYI SINIRI ŞART (2026-09-05'te canlıda yakalandı). Kontrol düz bir
    `alt dizge var mı` idi ve YUVARLAK ORANLARDA neredeyse hiçbir şey
    doğrulamıyordu: "%5,00" -> `"5.00".rstrip("0").rstrip(".")` -> "5",
    yani tek karakterlik bir arama. "5" ise sayfadaki 5.014 TL taksitinin,
    100.000 TL tutarının, 24 aylık vadenin ya da 04.09.2026 tarihinin
    içinde bulunuyordu. Yani %2,00 gibi bir oran "vade 24 ay" yazan bir
    sayfada bile "zeminlenmiş" sayılıyordu.

    Somut sonuç: qwen-flash Burgan'ın konut ve ihtiyaç oranlarını %5,00
    olarak döndürdü (sayfa 3,25% ve 3,75% yazıyor; 5,014 ve 5,084 TL
    TAKSİT tutarlarıydı) ve zeminleme bunu geçirdi. Yanlış faiz oranı
    veritabanına yazıldı — eksik veriden çok daha kötüsü.

    Artık aday sayı, daha uzun bir sayının PARÇASI olamaz: iki yanında
    rakam, nokta veya virgül bulunmamalı. "%5 faiz" hâlâ geçerli, "5.014
    TL" içindeki 5 değil.
    """
    formatted = f"{rate_percent:.2f}".rstrip("0").rstrip(".")
    candidates = {formatted, formatted.replace(".", ",")}
    # 2.9 -> "2,90" biçimi de sayfada geçebilir
    two_dp = f"{rate_percent:.2f}"
    candidates |= {two_dp, two_dp.replace(".", ",")}
    return any(
        re.search(r"(?<![\d.,])" + re.escape(c) + r"(?![\d.,])", text)
        for c in candidates
    )


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
        "Her oran için SIRAYLA şunu yap:\n"
        "1) rate_sentence: oranın GEÇTİĞİ cümleyi sayfadan BİREBİR kopyala. "
        "Kopyaladığın metin o oranın rakamını İÇERMEK ZORUNDA. Oran bir "
        "TABLODA ise cümle arama: oranın bulunduğu tablo satırını (ve varsa "
        "üstündeki başlığı) kopyala. Cümle bulamamak, oranı atlamak için "
        "gerekçe DEĞİLDİR.\n"
        "2) available_to_all: kararını ÖNCE bu cümleye, sonra onu İZLEYEN "
        "cümlelere bakarak ver. Sayfanın başka bir yerindeki, BAŞKA BİR "
        "ÜRÜNE ait koşulları bu orana UYGULAMA. '### ' ile başlayan satırlar "
        "bölüm başlığıdır; farklı bölümdeki koşul bu oranı bağlamaz.\n"
        "   Soru KİMİN başvurabileceğiyle ilgilidir, NE KADAR alabileceğiyle "
        "değil. TUTAR ve VADE kademeleri ('20.000 TL'ye kadar', '36 ay "
        "vadede') KISITLAMA DEĞİLDİR; bunlar yüzünden false YAZMA.\n"
        "   false yaz: yalnızca YENİ/ilk kez müşteri olanlara; yalnızca "
        "kampanyanın ilk kullanımında; belirli bir MÜŞTERİ GRUBUNA özel ürün "
        "(emekli, esnaf, öğretmen, kamu çalışanı); ön onaylı/davetli "
        "müşterilere; tarihli tanıtım oranı.\n"
        "   true yaz: mevcut müşteri dahil herkesin başvurabileceği genel "
        "oran — tutar/vade kademesi olsa bile.\n"
        "   DİKKAT: 'MEVCUT MÜŞTERİ' BİR KISIT DEĞİLDİR. Bankanın kendi "
        "müşterilerine açık olan oran, o bankanın GENEL orandır (herkes "
        "müşteri olabilir) — bunun için false YAZMA. Kısıt olan şey tersidir: "
        "oranın YALNIZCA yeni müşterilere verilmesi, çünkü o zaman mevcut "
        "müşteri o oranı alamaz.\n"
        "   Türkçede 'de/da' KAPSAYICIDIR: 'Mevcut müşteriler DE "
        "kullanabilir' cümlesi oranı sınırlamaz, tam tersine kapsamı "
        "genişletir. Bunu dışlayıcı okuma.\n"
        "3) why: gerekçen, tek cümle.\n"
        "\n"
        "DİĞER KURALLAR:\n"
        "- Yalnızca sayfada AÇIKÇA YAZAN oranları döndür. Hesaplama yapma, "
        "ortalama alma, tahmin etme.\n"
        # EKSİKSİZLİK: sayfalarda oran İKİ yerde birden duruyor — tanıtım
        # cümlesinde ("aylık %3,25 faiz ile") ve kampanya tablosunda
        # ("5,00 %"). Model yalnızca birini döndürürse persist'in "en
        # düşüğü sakla" kuralı seçemiyor ve kullanıcı, bankanın ilan
        # ettiğinden yüksek bir oran görüyor. Burgan'da tam olarak bu oldu
        # (2026-09-05): tablodaki %5,00 geldi, cümledeki %3,25 gelmedi.
        "- Sayfadaki TÜM oranları döndür, ilkini bulunca durma. Aynı ürün "
        "için hem tanıtım cümlesinde hem tabloda oran varsa İKİSİNİ DE ayrı "
        "kayıt olarak yaz; hangisinin geçerli olduğuna sen karar verme.\n"
        "- Oran AYLIK yüzde olmalı (2.99 gibi). Yıllık maliyet oranını "
        "(%68 gibi) DÖNDÜRME.\n"
        "- loan_type: ihtiyaç/tüketici -> personal, konut/mortgage -> "
        "housing, taşıt/araç -> vehicle.\n"
        "- Kredi kartı, nakit avans, KMH, ticari kredi oranlarını ALMA.\n"
        "- Faiz oranı %0 ise DÖNDÜRME: sıfır bir faiz oranı değil, tanıtım "
        "teklifidir.\n"
        "- Emin olmadığın hiçbir kaydı üretme. Hiç oran yoksa boş liste "
        "döndür.\n"
        "\n"
        "SAYFA:\n{html}\n"
    )

    def _prompt_for(self, code: str) -> str:
        """Sayfanın ÜRÜN TÜRÜ ipucunu prompt'a gömer.

        NEDEN: bir oran tabloda geçtiğinde satırda ürün adı olmuyor
        ("100.000 TL | 48 Ay | 4.14%") ve model türü tahmin edemeyip
        varsayılan olarak `personal` yazıyor. DenizBank'ın TAŞIT sayfası ve
        Anadolubank'ın KONUT sayfası böyle yanlış sınıflanıp ihtiyaç
        kredisi tablosuna düşüyordu (2026-09-02'de gözlendi).

        İpucu envanterden geliyor — hangi sayfayı çektiğimizi zaten
        biliyoruz, bunu modelden gizlemenin bir anlamı yok. Yine de EMİR
        değil ipucu: sayfada açıkça başka bir tür yazıyorsa model onu
        yazmalı, çünkü bazı sayfalar birden fazla ürünü listeliyor.
        """
        hint = (self.banks.get(code) or {}).get("loan_type_hint")
        if not hint:
            return self.PROMPT
        etiket = LOAN_TYPE_LABELS.get(hint, hint)
        satir = (
            f"- BU SAYFA ağırlıklı olarak {etiket.upper()} kredisi sayfasıdır; "
            f"tablodaki oranlar aksi YAZMIYORSA loan_type='{hint}' yaz. "
            f"Sayfada açıkça başka bir ürün belirtiliyorsa onu kullan.\n"
            "\n"
        )
        # Yer tutucu ({hint} gibi) KULLANILMIYOR: prompt'u doğrudan
        # `.format(html=...)` ile kullanan her çağrı yolu, bilmediği bir
        # yer tutucuda KeyError ile patlardı. Ekleme yapmak güvenli.
        return self.PROMPT.replace("SAYFA:\n{html}", satir + "SAYFA:\n{html}", 1)

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

        if not llm_settings.fallback_enabled():
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
                    prompt=self._prompt_for(code),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("loan_rates_llm/%s: agent başarısız: %s", code, exc)
                self.record_source(code.lower(), phase="parse", status="failed", error=str(exc))
                continue

            kept, dropped = _ground(rows, text, code)
            if dropped:
                logger.warning("loan_rates_llm/%s: %s", code, dropped.aciklama())
            if not kept:
                # Buraya artık YALNIZCA zeminleme tutmadığında düşülüyor.
                # Eskiden kampanya elemesi de buraya düşürüyordu ve sonuç
                # yanlıştı: sayfa çalışıyor, model doğru davranıyor, ama
                # kaynak "bozuk" görünüyordu (odeabank ve qnb, 18 saattir
                # bozuk damgası — inceleme 2026-09-08). Kampanya oranları
                # artık saklandığı için o durumda `kept` dolu ve koşu 'ok'.
                self.record_source(
                    code.lower(), phase="parse", status="empty",
                    error=(
                        f"agent {len(rows)} kayıt döndürdü — "
                        f"{dropped.aciklama() or 'hiçbiri sayfada doğrulanamadı'}"
                    ),
                )
                continue

            # İPUCUYLA UYUŞMAYAN TÜR — sessizce yutulmasın.
            #
            # persist() aynı (kurum, tür) için EN DÜŞÜK oranı bırakıyor. Bir
            # KONUT sayfasından `personal` satır dönerse o satır, aynı
            # bankanın ihtiyaç satırıyla aynı anahtara düşüp sessizce
            # kayboluyor: kaynak her gün "ok, 1 satır" diyor, tabloda konut
            # kredisi hiç görünmüyor ve hiçbir yerde iz kalmıyor.
            #
            # Canlıda tam olarak bu oldu (2026-09-04): anadolubank_konut
            # günlerdir "ok" dönüyordu ama Anadolubank'ın konut satırı hiç
            # oluşmadı. Sebebi sonradan anlaşıldı — o sayfanın çekilen
            # metni, sitenin her sayfasında duran "%3,09 İHTİYAÇ kredisi"
            # banner'ından ibaret; yani model DOĞRU davranıyordu, kaynak
            # yanlıştı. Bunu görebilmek için kaydın kalması şart.
            hint = (self.banks.get(code) or {}).get("loan_type_hint")
            if hint:
                uymayan = [r for r in kept if r.loan_type != hint]
                if uymayan and len(uymayan) == len(kept):
                    turler = sorted({r.loan_type for r in uymayan})
                    logger.warning(
                        "loan_rates_llm/%s: %s bekleniyordu, sayfadan %s geldi — "
                        "kaynak yanlış ürünü gösteriyor olabilir",
                        code, hint, ", ".join(turler),
                    )
                    self.record_source(
                        code.lower(), phase="parse", status="empty",
                        error=(
                            f"{hint} kredisi bekleniyordu ama sayfadan yalnızca "
                            f"{', '.join(turler)} oranı çıktı — sayfa o ürünün "
                            f"oranını yayınlamıyor olabilir"
                        ),
                    )
                    continue

            # NOT, 'dropped' TRUTHY OLMASA DA YAZILIYOR: kampanya işareti
            # artık eleme değil, ama koşu kaydında görünmesi şart —
            # kullanıcının "bu bankanın genel oranı neden yok" sorusunun
            # cevabı tam olarak bu satır.
            self.record_source(
                code.lower(), phase="parse", status="ok", rows=len(kept),
                error=(dropped.aciklama() or None),
            )
            kurum = self.banks[code].get("institution", code)
            for row in kept:
                records.append(
                    LoanRateRecord(
                        institution=kurum,
                        loan_type=row.loan_type,
                        monthly_rate=row.monthly_rate_percent / 100,
                        term_min=row.term_min_months,
                        term_max=row.term_max_months,
                        is_campaign=not row.available_to_all,
                        # Gerekçe yalnızca kampanyalı satırda anlamlı;
                        # genel oranda "kısıt yok" yazmak gürültü olurdu.
                        campaign_note=(row.why or None) if not row.available_to_all else None,
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

        # Aynı (kurum, tür, kampanya) için birden fazla oran gelirse EN
        # DÜŞÜĞÜ tutulur; bunu burada değil persist'te yapıyoruz, burada
        # yalnızca çelişkiyi yakalıyoruz.
        #
        # ANAHTARDA KAMPANYA DA VAR: aynı sayfada kampanya oranı ile tabela
        # oranı yan yana durabiliyor ve bunlar TANIM GEREĞİ farklı olmalı.
        # İkisini aynı kovaya koymak, aralarındaki normal farkı "agent
        # çelişkili iki oran döndürdü" sanmaya götürürdü.
        seen: dict[tuple[str, str, bool], float] = {}
        for r in records:
            key = (r.institution, r.loan_type, r.is_campaign)
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

        # EN DÜŞÜK ORAN, HER GRUP İÇİNDE AYRI SEÇİLİR.
        #
        # Anahtarın üçüncü alanı (`is_campaign`) bu düzeltmenin can alıcı
        # noktası. Eskiden anahtar (kurum, tür) idi ve kampanya oranı hep
        # en düşük olduğu için "en iyi oran" sistematik olarak "en koşullu
        # oran" oluyordu — ING %0,99 (canlı hata, 2026-08-30). Buna karşı
        # kampanyalı satırlar TAMAMEN eleniyordu ve bu sefer de Odeabank
        # ile QNB panelden tümden kayboluyordu (inceleme, 2026-09-08).
        #
        # Grubu anahtara koymak ikisini birden çözüyor: herkese açık
        # oranların en düşüğü ile kampanyalıların en düşüğü AYRI satırlar.
        # Kampanya oranı genel oranın yerine geçemiyor (farklı anahtar),
        # ama veri de kaybolmuyor.
        best: dict[tuple[str, str, bool], LoanRateRecord] = {}
        for r in records:
            key = (r.institution, r.loan_type, r.is_campaign)
            if key not in best or r.monthly_rate < best[key].monthly_rate:
                best[key] = r

        with SessionLocal() as session:
            for r in best.values():
                exists = session.execute(
                    select(LoanRate).where(
                        LoanRate.institution == r.institution,
                        LoanRate.loan_type == r.loan_type,
                        LoanRate.valid_date == today,
                        LoanRate.is_campaign == r.is_campaign,
                    )
                ).scalar_one_or_none()
                if exists:
                    exists.monthly_rate = r.monthly_rate
                    exists.term_min = r.term_min
                    exists.term_max = r.term_max
                    exists.campaign_note = r.campaign_note
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
                        is_campaign=r.is_campaign,
                        campaign_note=r.campaign_note,
                    )
                )
            session.commit()

        # ORAN DEĞİŞİM İZİ YALNIZCA GENEL ORANLAR İÇİN. Kampanya oranları
        # tanım gereği gelip geçici; onları da bu seriye katmak "faiz
        # değişti" izini kampanya başlangıç/bitişleriyle doldurur ve asıl
        # sinyali (bankanın tabela oranı kımıldadı mı) boğardı.
        changed = record_rate_changes(
            "loan",
            {
                (r.institution, r.loan_type): r.monthly_rate
                for r in best.values()
                if not r.is_campaign
            },
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


_HEADING_RE = re.compile(r"<(h[1-6])[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_BLOCK_RE = re.compile(r"</?(p|div|br|li|tr|td|th|section|article)[^>]*>", re.IGNORECASE)

#: Başlıkların metindeki işareti. Modelin "burada yeni bir bölüm başlıyor"
#: diyebilmesi için görünür olmalı; ZEMİNLEME de bu metin üzerinde çalıştığı
#: için işaretin oran rakamlarına benzememesi şart.
HEADING_MARK = "\n\n### "


def _visible_text(html: str) -> str:
    """HTML'i düz metne indirger AMA BÖLÜM SINIRLARINI KORUR.

    Modele ham HTML göndermek token'ın büyük kısmını etiketlere harcar.
    Düz metin hem ucuz hem de ZEMİNLEME kontrolünün üzerinde çalıştığı
    yüzey: "oran sayfada geçiyor mu" sorusu metin üzerinde sorulmalı,
    çünkü model de metni görüyor.

    BAŞLIKLAR NEDEN KORUNUYOR: eskiden bütün etiketler boşluğa çevriliyordu
    ve sayfanın bölüm yapısı yok oluyordu. DenizBank sayfasında bu, somut
    bir VERİ HATASI üretti (2026-09-02'de altı ayrı modelle doğrulandı):

        ...emekli maaşını DenizBank aracılığıyla alan müşterilere özel bir
        ihtiyaç kredisidir. Özellikleri %2,99'dan başlayan faiz oranları...

    Düzleştirilmiş metinde "emekli" cümlesi ile genel %2,99 oranı yan yana
    düşüyordu; denenen ALTI modelin ALTISI da oranı emeklilere özel sanıp
    eledi. Oysa sayfanın kendi yapısında "Özellikleri" bir <h2>, yani yeni
    bir bölümün başlangıcı ve %2,99 "Oran ve Fiyatlar" tablosunun resmî
    satırı. Yani hata modelde değil, modele verdiğimiz girdideydi.
    """
    cleaned = _SCRIPT_RE.sub(" ", html)
    # Başlıkları önce işaretle — etiketler silinmeden önce yapılmalı.
    cleaned = _HEADING_RE.sub(
        lambda m: HEADING_MARK + _TAG_RE.sub(" ", m.group(2)).strip() + "\n", cleaned
    )
    # Blok etiketleri satır sonuna çevir: tablo satırları ve maddeler
    # birbirine yapışmasın.
    cleaned = _BLOCK_RE.sub("\n", cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    # TÜM HTML varlıklarını çöz, elle iki tanesini değil.
    # CepteTEB sayfası bunu somut olarak kırdı: metin modele
    # "Hen&uuml;z TEB ya da CEPTETEB m&uuml;şterisi değilseniz" diye
    # gidiyordu ve model bu bozuk Türkçeden hiçbir oran çıkaramıyordu
    # (2026-09-02'de gözlendi). Elle kurulmuş iki varlıklı liste, geri
    # kalan yüzlerce varlığı sessizce metinde bırakıyordu.
    cleaned = html_lib.unescape(cleaned)
    cleaned = cleaned.replace("\xa0", " ")
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
        start = _snap_back(text, match.start() - CONTEXT_CHARS, match.start())
        end = _snap_forward(text, min(len(text), match.end() + CONTEXT_CHARS))
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


# Pencere sınırlarını taşımaya izin verilen mesafe. Küçük tutuldu: cümle
# bulunamazsa pencereyi büyütmek yerine ham sınırda kalmak daha ucuz.
SNAP_CHARS = 200
_SENT_END = re.compile(r"[.!?…]\s|\n")


class _Eleme(int):
    """ELENEN kayıt sayısı — ama işaretlenenleri de ayrıca sayar.

    Düz bir sayı yetmiyordu: koşu kaydına "N kayıt sayfada bulunamadı"
    yazılıyordu ve bu, erişilebilirlik elemesini UYDURMA gibi gösteriyordu.
    Odeabank'ta yedi oranın yedisi de sayfada gerçekten yazıyordu; hepsi
    "ön onaylı müşterilere" özel olduğu için elenmişti, ki bu doğru
    davranış. Yanlış etiket beni olmayan bir hatayı kovalamaya gönderdi.

    İKİ SAYI, İKİ FARKLI ŞEY (2026-09-12): `yok_sayfada` gerçekten ATILAN
    kayıt — zeminleme tutmadı, veri şüpheli. `kosullu` ise ATILMAYAN,
    yalnızca kampanya diye İŞARETLENEN kayıt (bkz. `_ground`). int değeri
    yalnızca ATILANLARI sayar, çünkü `if dropped:` kontrolleri "bir şey
    kaybettik mi" diye soruyor ve işaretlemek kayıp değil.
    """

    def __new__(cls, yok_sayfada: int, kosullu: int):
        self = super().__new__(cls, yok_sayfada)
        self.yok_sayfada = yok_sayfada
        self.kosullu = kosullu
        return self

    def aciklama(self) -> str:
        """Koşu kaydına yazılacak not; söylenecek bir şey yoksa boş dizge."""
        parcalar = []
        if self.yok_sayfada:
            parcalar.append(f"{self.yok_sayfada} kayıt sayfada bulunamadı")
        if self.kosullu:
            parcalar.append(f"{self.kosullu} kayıt kampanyalı (herkese açık değil)")
        return ", ".join(parcalar)


def _snap_back(text: str, pos: int, rate_pos: int) -> int:
    """Pencere başlangıcını oranın KENDİ bölümüne çeker.

    İki aşamalı, çünkü iki ayrı hata vardı:

    1. BÖLÜM SINIRI (asıl olan): `pos` ile oranın kendisi arasında bir
       başlık varsa pencere ORADAN başlar. Önceki bölüm başka bir ürünü
       anlatıyor olabilir ve modele onu göstermek yanlış cevap ürettiriyor
       — DenizBank'ta "emeklilere yönelik borç transfer kredisi" paragrafı
       genel %2,99 oranının hemen üstünde duruyordu ve denenen altı modelin
       altısı da oranı bu paragrafa bağlayıp eledi.
       Geriye değil İLERİ bakmak şart: başlık, pencerenin başlangıcı ile
       oran arasında; pencere başından geriye bakmak onu ıskalıyordu.

    2. CÜMLE SINIRI: başlık yoksa en azından cümle ortasından başlama.
    """
    if pos <= 0 and rate_pos <= 0:
        return 0
    pos = max(0, pos)
    # 1) Oran ile pencere başlangıcı ARASINDAKİ son başlık.
    ara = text.rfind(HEADING_MARK.strip("\n"), pos, rate_pos)
    if ara != -1:
        return ara
    # 2) Pencere başlangıcından hemen önceki cümle sonu.
    bas = max(0, pos - SNAP_CHARS)
    pencere = text[bas:pos]
    son = None
    for m in _SENT_END.finditer(pencere):
        son = m.end()
    return bas + son if son is not None else pos


def _snap_forward(text: str, pos: int) -> int:
    """Pencere sonunu bir SONRAKİ cümle sınırına uzatır.

    Oranı diskalifiye eden cümle çoğu zaman oranın HEMEN ARDINDAN gelir
    (ING'de %0,99'u kampanyaya bağlayan cümle böyleydi); yarım bırakılan
    bir cümle o kanıtı kesip atardı.
    """
    if pos >= len(text):
        return len(text)
    sinir = min(len(text), pos + SNAP_CHARS)
    # Bir sonraki başlığa DEĞME: sonraki bölüm bu oranı ilgilendirmiyor.
    baslik = text.find(HEADING_MARK.strip("\n"), pos, sinir)
    if baslik != -1:
        return baslik
    m = _SENT_END.search(text, pos, sinir)
    return m.end() if m else pos


def _ground(rows, text: str, code: str) -> tuple[list, int]:
    """Sayfada GEÇEN oranları bırakır; koşullu olanları İŞARETLER.

    ZEMİNLEME oranın uydurma olmadığını garanti eder — ELEYEN tek kontrol
    budur. Model bir sayı uydurursa kaynakta bulunmaz ve kayıt atılır.

    ERİŞİLEBİLİRLİK ise ELEME DEĞİL, ETİKET. Ayrım önemli, çünkü ikisi
    farklı şeyler söylüyor: zeminleme "bu sayı gerçek mi", erişilebilirlik
    "bu sayı KİME açık" diye soruyor. İkincisinin cevabı "herkese değil"
    olduğunda veri yanlış olmuyor, yalnızca koşullu oluyor.

    NEDEN ÖNCE ELENİYORDU: `persist` aynı (kurum, tür) için en düşük oranı
    saklıyordu ve kampanya oranı hep en düşük olduğu için "en iyi oran"
    sistematik olarak "en koşullu oran" oluyordu. Canlı gözlem
    (2026-08-30): panel ING'yi aylık %0,99 gösteriyordu, DenizBank'ın
    %2,99'unun üçte biri. Sayı sayfada gerçekten yazıyordu — zeminleme
    kusursuz çalışmıştı; %0,99 "Turuncu Ekstra Avantajlı Kredi"nin oranıydı
    ve yalnızca kampanyanın ilk kullanımında geçerliydi.

    NEDEN ARTIK ELENMİYOR: elemenin bedeli, çözdüğü sorundan büyük çıktı.
    Odeabank'ın sekiz, QNB'nin altı oranının hepsi kampanyalıydı; ikisi de
    panelde HİÇ görünmedi ve kaynakları "18 saattir bozuk" damgası yedi
    (inceleme, 2026-09-08). Asıl sorun `persist`'in SEÇİMİYDİ ve düzeltme
    oraya taşındı: kampanyalı ve herkese açık oranlar ayrı satırlar olarak
    saklanıyor, "en düşük" seçimi her grup içinde ayrı yapılıyor. Kampanya
    oranı hâlâ genel oranın yerine geçemiyor, ama artık kaybolmuyor da.

    Kararı MODEL veriyor. Python'da anahtar kelimeyle denendi ve başarısız
    oldu: %0,99'u diskalifiye eden cümle, oranın geçtiği cümlenin bir
    SONRAKİSİ; üstelik Halkbank'ın gerekçesi "yeni müşterilere özel
    olduğuna dair kısıt YOK" diyor — "yeni müşteri" kelimesini arayan bir
    filtre onu da elerdi. Modele sorulan soru bilinçli olarak dar: yorum
    değil, tek bir evet/hayır.
    """
    kept, dropped, yok_sayfada, kosullu = [], 0, 0, 0
    for row in rows:
        if not _appears_in_text(row.monthly_rate_percent, text):
            dropped += 1
            yok_sayfada += 1
            logger.warning(
                "loan_rates_llm/%s: %%%.2f sayfada bulunamadı — kayıt atıldı",
                code, row.monthly_rate_percent,
            )
            continue
        if not row.available_to_all:
            # ATILMIYOR, SAYILIYOR: koşu kaydına "n oran kampanyalı" diye
            # geçsin ki kullanıcı panelde neden o bankanın genel oranını
            # görmediğini anlayabilsin.
            kosullu += 1
            logger.info(
                "loan_rates_llm/%s: %%%.2f herkese açık değil, kampanya olarak "
                "işaretlendi — %s",
                code, row.monthly_rate_percent, (row.why or "gerekçe yok")[:100],
            )
        # Demirleme DENETİMİ — eleme değil, uyarı. Model oranı kendi
        # cümlesine bağlayamadıysa erişilebilirlik kararı da şüphelidir.
        # Kaydı burada ATMIYORUZ: alıntı boş gelmesi (şema varsayılanı)
        # tek başına oranın yanlış olduğunu göstermez ve sessizce veri
        # kaybetmektense görünür bir uyarı bırakmak doğru.
        if row.rate_sentence and not _appears_in_text(
            row.monthly_rate_percent, row.rate_sentence
        ):
            logger.warning(
                "loan_rates_llm/%s: %%%.2f alıntılanan cümlede geçmiyor — "
                "erişilebilirlik kararı şüpheli: %r",
                code, row.monthly_rate_percent, row.rate_sentence[:120],
            )
        kept.append(row)
    return kept, _Eleme(yok_sayfada, kosullu)


def _load_banks() -> dict[str, dict]:
    """Envanterden aktif sayfaları okur.

    ANAHTAR YAML ANAHTARIDIR, kurum kodu DEĞİL. Eskiden kurum koduyla
    anahtarlanıyordu ve bu, aynı bankaya ikinci bir sayfa eklemeyi sessizce
    imkânsız kılıyordu: `denizbank_ihtiyac` ile `denizbank_tasit` aynı
    "DENIZBANK" anahtarına yazıldığı için ikincisi birincisini eziyordu —
    yani konut/taşıt sayfaları hiç çekilmiyordu ve bunu gösteren bir hata
    da yoktu. Kurum kodu artık kaydın İÇİNDE taşınıyor.
    """
    from config.loader import load_sources

    out: dict[str, dict] = {}
    for key, entry in (load_sources().get("loan_llm_endpoints") or {}).items():
        entry = entry or {}
        if entry.get("status") != "active" or not entry.get("url"):
            continue
        out[key] = {
            "key": key,
            "url": entry["url"],
            "institution": entry.get("institution", key.upper()),
            "loan_type_hint": entry.get("loan_type_hint"),
        }
    return out


if __name__ == "__main__":
    print(LlmLoanRateCollector().run())
