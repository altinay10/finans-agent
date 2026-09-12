"""ORM tabloları — tasarım dokümanı §03'teki şemanın SQLAlchemy karşılığı."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Institution(Base):
    __tablename__ = "institutions"

    code: Mapped[str] = mapped_column(String, primary_key=True)   # 'ZIRAAT', 'EMLAKKATILIM'
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)     # 'bank' | 'participation' | 'reference'


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    collector: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)   # 'ok' | 'failed' | 'llm_fallback'
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rows_written: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bu koşuyu NE başlattı: 'schedule' (planlanmış saat) | 'startup'
    # (zamanlayıcı ayağa kalktı) | 'catchup' (veri bayattı, telafi) |
    # 'manual' (worker.py elle). Telafi mekanizmasının gerçekten çalışıp
    # çalışmadığı yalnızca bu alanla doğrulanabilir.
    trigger: Mapped[str | None] = mapped_column(String, nullable=True)
    # Başarısızlık HANGİ aşamada oldu: 'fetch' | 'parse' | 'sanity' |
    # 'persist'. 'sanity' ayrı tutuluyor çünkü anlamı bambaşka: veri geldi,
    # ayrıştırıldı, ama bant dışıydı ve BİLEREK yazılmadı. Bu bir arıza
    # değil, korumanın çalıştığının kanıtıdır — hata metnine gömülü kalırsa
    # "kaç kez saçma veri geldi" sorusu sayılamaz.
    failure_kind: Mapped[str | None] = mapped_column(String, nullable=True)


class FxQuote(Base):
    __tablename__ = "fx_quotes"
    __table_args__ = (
        UniqueConstraint("institution", "currency", "quoted_at", name="uq_fx_quote"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    institution: Mapped[str] = mapped_column(ForeignKey("institutions.code"), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)          # 'USD' | 'EUR'
    buy: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    sell: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    quoted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"), nullable=True)
    quoted_at_is_estimated: Mapped[bool] = mapped_column(Boolean, default=False)


class DepositRate(Base):
    __tablename__ = "deposit_rates"
    __table_args__ = (
        UniqueConstraint(
            "institution", "currency", "term_days", "amount_min", "valid_date",
            name="uq_deposit_rate",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    institution: Mapped[str] = mapped_column(ForeignKey("institutions.code"), nullable=False)
    currency: Mapped[str] = mapped_column(String, default="TRY")
    term_days: Mapped[int] = mapped_column(nullable=False)                 # 32, 92, 181, 365
    amount_min: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    amount_max: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)  # NULL = üst sınır yok
    annual_rate: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)        # brüt yıllık
    is_profit_share: Mapped[bool] = mapped_column(Boolean, nullable=False)           # katılım = beklenen
    valid_date: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"), nullable=True)


class LoanRate(Base):
    __tablename__ = "loan_rates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    institution: Mapped[str] = mapped_column(ForeignKey("institutions.code"), nullable=False)
    loan_type: Mapped[str] = mapped_column(String, nullable=False)         # 'vehicle'|'housing'|'personal'
    monthly_rate: Mapped[float] = mapped_column(Numeric(8, 5), nullable=False)  # AYLIK, brüt
    term_min: Mapped[int | None] = mapped_column(nullable=True)
    term_max: Mapped[int | None] = mapped_column(nullable=True)
    amount_max: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    valid_date: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # KAMPANYA ORANI MI? Yani herkesin değil, yalnızca belirli bir grubun
    # (yeni müşteri, ön onaylı, emekli, kampanyanın ilk kullanımı)
    # alabildiği bir oran mı.
    #
    # NEDEN SÜTUN OLARAK VAR: bu oranlar eskiden TAMAMEN ELENİYORDU
    # (`loan_rates_llm._ground`) ve bedeli ağırdı — Odeabank'ın sekiz,
    # QNB'nin altı oranının hepsi kampanyalı olduğu için o iki banka
    # panelde HİÇ görünmüyordu ve kaynak "bozuk" damgası yiyordu
    # (inceleme, 2026-09-08). Oysa sayfa çalışıyordu, model doğru
    # davranıyordu; elenen şey gerçek ve kullanıcıyı ilgilendiren veriydi.
    #
    # ELEMENİN ASIL SEBEBİ BAŞKA YERDEYDİ: `persist` aynı (kurum, tür) için
    # EN DÜŞÜK oranı saklıyor ve kampanya oranı hep en düşük olduğu için
    # "en iyi oran" sistematik olarak "en koşullu oran" oluyordu (ING
    # %0,99, canlı hata 2026-08-30). Bu sütun o seçimi ikiye ayırıyor:
    # herkese açık oranların en düşüğü ve kampanyalıların en düşüğü AYRI
    # satırlar. Böylece kampanya oranı asla genel oranın yerine geçmiyor
    # ama veri de kaybolmuyor.
    is_campaign: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: Oranın kimlere açık olduğunu anlatan gerekçe (modelin `why` alanı).
    #: Panelde rozetin ipucu olarak gösteriliyor; kampanyanın NEDEN
    #: kampanya sayıldığı denetlenebilir kalsın diye saklanıyor.
    campaign_note: Mapped[str | None] = mapped_column(String, nullable=True)


class Fund(Base):
    __tablename__ = "funds"

    code: Mapped[str] = mapped_column(String, primary_key=True)    # 'TI2', 'GAF'
    name: Mapped[str] = mapped_column(String, nullable=False)
    umbrella: Mapped[str | None] = mapped_column(String, nullable=True)
    is_equity_heavy: Mapped[bool] = mapped_column(Boolean, default=False)
    benchmark: Mapped[str | None] = mapped_column(String, nullable=True)  # 'BIST100'


class FundPrice(Base):
    __tablename__ = "fund_prices"

    fund_code: Mapped[str] = mapped_column(ForeignKey("funds.code"), primary_key=True)
    price_date: Mapped[date] = mapped_column(Date, primary_key=True)
    price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ProfitShareRatio(Base):
    """Katılım bankalarının yayınladığı KÂR PAYLAŞIM ORANI — faiz DEĞİL.

    Neden ayrı tablo: bu sayı yıllık getiri değil, bankanın elde ettiği kârın
    müşteriye düşen YÜZDESİ (ör. 92 = kârın %92'si katılımcıya). DepositRate
    tablosuna `annual_rate` diye yazmak, panelin bunu %92 faiz sanıp devasa
    ve tamamen uydurma bir getiri hesaplamasına yol açardı. Gerçek getiriye
    çevirmek için bankanın gerçekleşen kâr rakamı gerekir; katılım bankaları
    bunu web'de yayınlamıyor.

    Bu yüzden ayrı tabloda, ayrı panel bloğunda ve hesaplamaya sokulmadan
    gösterilir (tasarım §07: "katılım = beklenen, taahhüt değil").
    """

    __tablename__ = "profit_share_ratios"
    __table_args__ = (
        UniqueConstraint(
            "institution", "currency", "term_days", "amount_min", "valid_date",
            name="uq_profit_share_ratio",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    institution: Mapped[str] = mapped_column(ForeignKey("institutions.code"), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)      # TRY | USD | EUR | XAU | XAG
    term_label: Mapped[str] = mapped_column(String, nullable=False)    # "3 Aylık" — bankanın kendi ifadesi
    term_days: Mapped[int] = mapped_column(nullable=False)             # kıyas için normalize edilmiş
    amount_min: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    amount_max: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    share_ratio: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)   # 0.92 = kârın %92'si
    withholding_rate: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    valid_date: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SourceRun(Base):
    """TEK BİR KAYNAĞIN (banka/kurum) tek bir koşudaki sonucu.

    `scrape_runs` koşunun TAMAMINI özetler: "deposit_rates ok, 801 satır".
    Ama o koşuda beş bankadan biri düşmüşse bu satır yine 'ok' der ve hangi
    bankanın kaybolduğu yalnızca stdout log'unda kalır — sunucuda konteyner
    yeniden başlayınca o log gider. Bu tablo o boşluğu kapatır: banka banka
    ne oldu, kaç satır geldi, ne kadar sürdü, hata neydi.

    Neden önemli: bir bankanın sayfası değiştiğinde koşu 'ok' görünmeye devam
    eder (diğer dördü çalıştığı için) ve panel o bankayı sessizce eksik
    gösterir. Buradaki 'failed' satırı o sessiz bozulmanın tek kanıtıdır.
    """

    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"), nullable=True)
    collector: Mapped[str] = mapped_column(String, nullable=False)   # 'fx_banks'
    source: Mapped[str] = mapped_column(String, nullable=False)      # 'akbank'
    phase: Mapped[str] = mapped_column(String, nullable=False)       # 'fetch' | 'parse'
    status: Mapped[str] = mapped_column(String, nullable=False)      # 'ok' | 'failed' | 'empty'
    rows: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LlmCall(Base):
    """Her LLM çağrısının token muhasebesi.

    Kullanıcı isteği: "şu kadar token harcadı" görülebilsin. Token harcaması
    log satırında kalırsa aylık maliyet asla toplanamaz; burada toplanabilir.

    `status` alanı çağrının hiç YAPILMADIĞI durumları da kaydeder
    ('disabled', 'budget_exceeded') — böylece "fallback neden devreye
    girmedi" sorusu da cevaplanabilir olur ve sıfır token harcandığı
    görünür.
    """

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"), nullable=True)
    collector: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    # 'ok' | 'failed' | 'disabled' | 'budget_exceeded'
    prompt_tokens: Mapped[int | None] = mapped_column(nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(nullable=True)
    input_chars: Mapped[int | None] = mapped_column(nullable=True)
    rows_recovered: Mapped[int | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fallback'i TETİKLEYEN hata. "Agent ne zaman hangi durumda devreye
    # girdi" sorusunun cevabı burasıdır: collector adı tek başına yetmez,
    # asıl bilgi hangi bankanın hangi hatasının modeli çağırttığıdır.
    trigger_source: Mapped[str | None] = mapped_column(String, nullable=True)
    trigger_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Tahmini maliyet (USD). Fiyat .env'den okunur; bilinmiyorsa NULL kalır —
    # uydurma bir sayı yazmaktansa boş bırakmak dürüst.
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LoanReferenceQuote(Base):
    """Bankanın KENDİ hesapladığı taksit — bizim anüite hesabımızın denetçisi.

    Panelin ödeme planı bankadan gelmez; `core/loan.py::amortize()` bankanın
    ilan ettiği aylık orandan üretir. Bu doğru bir yaklaşım ama denetimsiz:
    KKDF/BSMV oranlarımız eskirse ya da gün sayımımız kayarsa taksit sessizce
    yanlışlanır ve bunu fark ettirecek hiçbir sinyal olmaz.

    Bazı bankalar (Yapı Kredi) hesaplama uç noktasında kendi taksit tutarını
    da döndürüyor. Bu tablo o tutarı saklar; her koşuda bizim hesabımızla
    karşılaştırılır ve sapma eşiği aşarsa `source_runs`'a 'validate'
    aşamasında bir uyarı düşer. Yani vergi modelimiz her gün bankanın kendi
    rakamıyla sınanır.

    `bank_annual_cost_rate` bankanın ilan ettiği yıllık maliyet oranıdır
    (Yapı Kredi: YearlyCustomerCostRate). Faiz oranından farklıdır: vergiler
    dahil, bileşik bazda yıllık maliyet.
    """

    __tablename__ = "loan_reference_quotes"
    __table_args__ = (
        UniqueConstraint(
            "institution", "loan_type", "principal", "term_months", "valid_date",
            name="uq_loan_reference_quote",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    institution: Mapped[str] = mapped_column(ForeignKey("institutions.code"), nullable=False)
    loan_type: Mapped[str] = mapped_column(String, nullable=False)
    principal: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    term_months: Mapped[int] = mapped_column(nullable=False)
    monthly_rate: Mapped[float] = mapped_column(Numeric(8, 5), nullable=False)
    bank_installment: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    bank_annual_cost_rate: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    valid_date: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"), nullable=True)


class HttpRequest(Base):
    """TEK BİR HTTP isteğinin kaydı — "hangi istek ne zaman dönmedi"in cevabı.

    `source_runs` bir bankanın SONUCUNU söyler ("akbank/fetch failed").
    Ama bir banka için birden çok istek atılıyor (Akbank'ta üç ürün kodu,
    VakıfBank'ta önce token sonra veri, Yapı Kredi'de dört adımlık zincir).
    Zincirin hangi halkasının koptuğu, kaç ms sürdüğü, HTTP kodunun ne olduğu
    yalnızca burada durur.

    Neden ayrı tablo ve neden saklama süresi var: bu tablo projedeki EN
    GÜRÜLTÜLÜ tablodur (koşu başına onlarca satır). `store/retention.py`
    onu düzenli olarak budar; aksi halde tek başına veritabanını büyütür.
    """

    __tablename__ = "http_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"), nullable=True)
    collector: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)   # 'akbank'
    method: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(nullable=True)      # NULL = yanıt hiç gelmedi
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    response_bytes: Mapped[int | None] = mapped_column(nullable=True)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt: Mapped[int] = mapped_column(default=1)
    # 'ok' | 'http_error' | 'timeout' | 'network_error'
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RateChange(Base):
    """Bir oranın DEĞİŞTİĞİ an — sessiz donmayı yakalayan kayıt.

    Neden gerekli: bir uç nokta HTTP 200 dönmeye devam edebilir ama arkasındaki
    besleme durmuş olabilir. `source_runs` bunu 'ok' diye kaydeder, panel
    veriyi taze gösterir, oysa sayı haftalardır aynıdır. 'empty' kadar sinsi
    bir bozulma türüdür ve tek tespiti "bu oran en son ne zaman değişti"
    sorusudur.

    Yalnızca DEĞİŞİM yazılır (aynı değer tekrar gelirse satır eklenmez), bu
    yüzden tablo yavaş büyür ve `MAX(changed_at)` doğrudan "son değişim"i
    verir.
    """

    __tablename__ = "rate_changes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String, nullable=False)     # 'deposit' | 'loan'
    institution: Mapped[str] = mapped_column(String, nullable=False)
    series_key: Mapped[str] = mapped_column(String, nullable=False)  # 'TRY/92/500000' | 'housing'
    old_value: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    new_value: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"), nullable=True)


class SchedulerHeartbeat(Base):
    """Zamanlayıcı süreci YAŞIYOR MU — panelin tek güvenilir cevabı.

    NEDEN GEREKLİ: panel bugüne kadar yalnızca "veri kaç saat eski"ye
    bakıyordu ve eski veriyi görünce "zamanlayıcı çalışmıyor gibi" diyordu.
    "Gibi" kelimesi boşuna değildi — panel bunu BİLMİYORDU. İki bambaşka
    durum aynı mesajı üretiyordu:

      1. Zamanlayıcı hiç başlatılmamış (Docker'da yalnızca panel servisi
         ayakta, ya da `docker run` ile tek konteyner çalıştırılmış).
      2. Zamanlayıcı çalışıyor ama bankalardan veri alamıyor.

    Birincisinde çözüm "süreci başlat", ikincisinde "kaynak arızasına bak".
    Kullanıcıya yanlış tarafı gösteren bir teşhis, teşhis değildir.

    Tek satırlık tablo (id sabit 1): süreç her turda `last_beat`i tazeler.
    `host`/`pid` hangi konteynerin sahiplendiğini söyler — iki zamanlayıcının
    aynı anda koşmasını önlemek de bu satıra bakılarak yapılıyor
    (bkz. store/heartbeat.py).
    """

    __tablename__ = "scheduler_heartbeat"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    host: Mapped[str] = mapped_column(String, nullable=False)
    pid: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_beat: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LlmCredential(Base):
    """Panelden kaydedilen LLM kimlik bilgileri — SIRAYLA denenir.

    NEDEN VERİTABANI, `.env` DEĞİL: bu proje Docker'da iki ayrı konteyner
    olarak koşuyor (panel + zamanlayıcı) ve `.env` imaja hiç girmiyor
    (`.dockerignore`). Panelden `.env`'e yazmak üç ayrı sebeple işe yaramaz:
    dosya yalnızca panel konteynerinin yazılabilir katmanında oluşur,
    zamanlayıcı onu HİÇ göremez, ve `docker compose up --build` konteyneri
    yeniden yarattığında dosya kaybolur. Veritabanı ise `finans-data` adlı
    kalıcı volume'de ve İKİ konteyner de aynı dosyayı açıyor.

    NEDEN TEK SATIR DEĞİL: anahtar biter, kotası dolar, iptal edilir. Tek
    satır tutulsaydı o an sistem yine yarım kalırdı — panelden anahtar
    girilebilmesinin bütün amacı buydu. Birden fazla satır tutulup EN SON
    girilen kullanılıyor; o kullanılamaz hale gelirse bir öncekine
    düşülüyor (bkz. llm/credentials.py).

    NEDEN base_url/model DE BURADA: anahtar tek başına anlamsız. Aynı
    anahtar Gemini'de geçerli, Qwen'de değil; `extra_body` de sağlayıcıya
    özel (Qwen'de `enable_thinking`). Anahtarı sağlayıcısından ayırmak,
    "kaydettim ama çalışmıyor" durumunun en sık sebebi olurdu.
    """

    __tablename__ = "llm_credentials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    #: Sağlayıcıya özel ek gövde alanları, JSON metni. Boşsa gönderilmez.
    extra_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 'ok' | 'failed' — kaydedilirken CANLI test edilir, sonra her
    #: kullanılamama durumunda 'failed'e çekilir.
    status: Mapped[str] = mapped_column(String, nullable=False, default="ok")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    #: SİLME KODUNUN SHA-256 ÖZETİ. Kodun kendisi burada DURMAZ.
    #
    # NEDEN VAR: panelde kimlik doğrulama yok ve "Sil" düğmesi herkese
    # açıktı; paneli açabilen biri bütün anahtarları silip agent'ı tamamen
    # durdurabiliyordu (kullanıcı bildirimi, 2026-09-12).
    #
    # NEDEN ÇEREZ DEĞİL: çerez istemcide duruyor ve istemci onu kendi
    # yazabilir. "Ben sahibim" diyen bir çerez, kötü niyetli birini
    # engellemez — yalnızca kazara silmeyi engeller. Burada tutulan şey bir
    # TAŞIYICI JETON özeti: anahtarı kaydeden kişiye rastgele bir kod
    # gösteriliyor, sunucu yalnızca özetini saklıyor, silmek için o kodu
    # sunmak gerekiyor. Kodu olmayan kimse o satırı silemez ve kod
    # sunucudan geri okunamaz.
    #
    # NULL = ESKİ SATIR. Bu sütun eklenmeden önce kaydedilmiş anahtarların
    # sahibi bilinmiyor; onlar için kod sorulmuyor (aksi halde kimse
    # silemezdi). Yeni kayıtların hepsi kod taşır.
    #
    # SINIR — BUNUN ÇÖZMEDİĞİ ŞEY: bu, panelin önüne kimlik doğrulama
    # koymanın YERİNE GEÇMEZ. Paneli açabilen biri hâlâ anahtar EKLEYEBİLİR
    # ve agent'ı çalıştırıp token harcatabilir. Gerçek koruma ters vekil +
    # kimlik doğrulamadır (bkz. docker-compose.yml'deki port notu).
    owner_hash: Mapped[str | None] = mapped_column(String, nullable=True)


class LlmFallbackState(Base):
    """Ayrıştırıcısı kırılan toplayıcı için GERİ ÇEKİLME (backoff) sayacı.

    NEDEN GEREKLİ: tazelik telafisi son BAŞARILI koşuya bakıyor. Bir
    ayrıştırıcı kalıcı olarak kırılırsa o koşu bir daha asla başarılı
    olmaz, yani telafi 30 dakikada bir sonsuza kadar yeniden dener ve her
    denemede bir LLM çağrısı yakar. Günlerce fark edilmeyen bir hafta sonu
    arızası, sessizce fatura üretirdi.

    Sayaç YALNIZCA gerçekten LLM çağrısı yapılıp başarısız olduğunda artar.
    "Agent kapalı" ya da "bütçe doldu" durumlarında hiç çağrı yapılmıyor,
    dolayısıyla geri çekilecek bir şey de yok — bunlar sayacı kirletseydi
    anahtarı olmayan bir kurulum 12 banka sayfasını 5 dakikada bir çekmeye
    başlardı (bkz. collectors/base.py).
    """

    __tablename__ = "llm_fallback_state"

    collector: Mapped[str] = mapped_column(String, primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSetting(Base):
    """Sır OLMAYAN, panelden değiştirilebilen ayarlar (anahtar/değer).

    Şimdilik tek kullanıcısı token birim fiyatları. `.env`'e yazılmıyor
    çünkü (a) konteynerde `.env` yok, (b) fiyat bir kimlik bilgisi değil;
    onu `PANEL_ALLOW_ENV_WRITE` iznine bağlamak, maliyetini görmek isteyen
    kullanıcıyı sır yazma iznine muhtaç bırakırdı.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
