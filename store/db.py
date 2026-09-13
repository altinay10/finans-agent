"""SQLAlchemy engine + session + şema bootstrap.

DB_URL ortam değişkeniyle takas edilir: varsayılan SQLite dosyası, ileride
tek satırla Postgres'e geçilir (bkz. tasarım dokümanı §03).
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

import yaml

from config.loader import funds_yaml_path
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from store.migrate import add_missing_columns
from store.models import Base, Fund, FundPrice, Institution

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = REPO_ROOT / "data" / "finans_agent.db"
SOURCES_YAML = REPO_ROOT / "config" / "sources.yaml"
# Fon kataloğu artık AYRI dosyada: panelden fon eklenebilmesi için
# (bkz. collectors/fund_prices.py::add_fund_to_registry).
FUNDS_YAML = REPO_ROOT / "config" / "funds.yaml"

FRESHNESS_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS v_freshness AS
SELECT collector,
       MAX(CASE WHEN status = 'ok' THEN finished_at END) AS last_ok,
       MAX(finished_at)                                  AS last_run,
       SUM(CASE WHEN status = 'llm_fallback' THEN 1 ELSE 0 END) AS fallback_count
FROM scrape_runs
WHERE started_at > datetime('now', '-7 days')
GROUP BY collector;
"""


def _db_url() -> str:
    url = os.environ.get("DB_URL")
    if url:
        return url
    DEFAULT_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_SQLITE_PATH}"


engine = create_engine(_db_url(), future=True)


#: Yazma kilidi beklerken vazgeçmeden önce beklenecek süre (ms).
#: SQLite'ın varsayılanı 0 — yani kilit görünce ANINDA hata verir.
BUSY_TIMEOUT_MS = 10_000


@event.listens_for(engine, "connect")
def _sqlite_pragmalari(dbapi_conn, _kayit) -> None:
    """Her yeni SQLite bağlantısında WAL ve bekleme süresi.

    SORUN: bu proje aynı SQLite dosyasına İKİ AYRI SÜREÇTEN yazıyor (panel
    ve zamanlayıcı, ayrı konteynerler, ortak volume). Varsayılan
    `journal_mode=delete` kipinde bir yazma, dosyanın TAMAMINI kilitliyor;
    o sırada okumaya çalışan diğer süreç `database is locked` alıyor.
    Canlıda bu, zamanlayıcının nabız yazımını düşürüyordu
    (`store.heartbeat: nabız yazılamadı`, 2026-09-13 ve öncesi).

    WAL (Write-Ahead Logging) NE YAPIYOR: yazmalar ana dosyaya değil, yanına
    konulan bir `-wal` dosyasına ekleniyor. Okuyucular bu sırada ana dosyanın
    tutarlı son hâlini okumaya devam ediyor. Yani TEK YAZAR + ÇOK OKUYUCU
    aynı anda çalışabiliyor ve birbirini bloke etmiyor. İki yazar hâlâ
    sıraya giriyor — WAL bunu kaldırmıyor, ama bizim çakışmamız
    yazar/okuyucu çakışmasıydı.

    NEDEN `synchronous` DEĞİŞTİRİLMİYOR: WAL ile birlikte `NORMAL` yaygın
    bir öneri ve daha hızlı, ama elektrik kesintisinde son işlemleri
    kaybettirebiliyor. Burası bir Raspberry Pi ve elektrik kesintisi gerçek
    bir olasılık; hız sorunumuz da yok. Dayanıklılığı istenmediği halde
    düşürmek doğru olmaz, varsayılan (FULL) kalıyor.

    BUSY_TIMEOUT AYRICA GEREKLİ: WAL iki YAZARIN çakışmasını çözmüyor.
    Varsayılan 0 ms ile ikinci yazar anında hata alır; 10 saniye beklemek,
    bu iş yükünde (saniyeler süren toplama turları değil, milisaniyelik
    yazmalar) çakışmayı fiilen görünmez kılıyor.

    KİP DOSYAYA KALICI, TIMEOUT BAĞLANTIYA ÖZEL — bu yüzden ikisi de her
    bağlantıda kuruluyor. `journal_mode` ikinci kez ayarlandığında SQLite
    hiçbir şey yapmıyor, yani tekrar maliyetsiz.
    """
    if engine.url.get_backend_name() != "sqlite":
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # :memory: ve geçici veritabanları WAL'ı desteklemiyor; PRAGMA
        # sessizce eski kipi döndürür, hata vermez. Yine de sonucu
        # kontrol etmiyoruz: WAL'a geçememek çalışmayı engellemez, yalnızca
        # eski (çakışmaya açık) davranışa döner.
        cur.execute("PRAGMA journal_mode=WAL")
    finally:
        cur.close()


SessionLocal: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(seed: bool = True) -> None:
    """Tabloları ve v_freshness view'ini oluşturur; institutions/funds'ı seed eder.

    create_all YALNIZCA olmayan tabloyu yaratır; var olan bir tabloya sütun
    eklemez. Sunucuda veritabanı dosyası kalıcı olduğu için şemaya sonradan
    eklenen her sütun `no such column` ile patlardı — add_missing_columns o
    boşluğu kapatır (bkz. store/migrate.py, kapsamı orada yazılı).
    """
    Base.metadata.create_all(engine)
    added = add_missing_columns(engine)
    if added:
        logger.info("şema güncellendi: %s", ", ".join(added))
    if engine.url.get_backend_name() == "sqlite":
        with engine.begin() as conn:
            conn.execute(text(FRESHNESS_VIEW_SQL))
    if seed:
        seed_reference_data()


def seed_reference_data() -> None:
    if not SOURCES_YAML.exists():
        return
    with open(SOURCES_YAML, encoding="utf-8") as f:
        sources = yaml.safe_load(f)

    with SessionLocal() as session:
        for row in sources.get("institutions", []):
            if session.get(Institution, row["code"]) is None:
                session.add(Institution(code=row["code"], name=row["name"], kind=row["kind"]))
        configured_funds = _configured_funds()
        for row in configured_funds:
            if session.get(Fund, row["code"]) is None:
                session.add(
                    Fund(
                        code=row["code"],
                        # `name` ZORUNLU DEĞİL. Panelden fon eklerken ad
                        # alanı isteğe bağlı ve boş bırakılabiliyor; burada
                        # row["name"] demek, uygulamanın AÇILIŞTA KeyError
                        # ile patlaması demekti (canlı akış testinde
                        # yakalandı, 2026-08-25). Gerçek adı zaten toplayıcı
                        # sağlayıcı sayfasından okuyup üzerine yazıyor.
                        name=row.get("name") or row["code"],
                        is_equity_heavy=row.get("is_equity_heavy", False),
                        benchmark=row.get("benchmark"),
                    )
                )

        # sources.yaml'dan çıkarılmış fonların artık katalogda durmaması gerekiyor,
        # yoksa panelin fon seçicisinde hiç fiyatı olmayan ölü kayıtlar görünür.
        # Güvenlik için YALNIZCA hiç fiyat verisi olmayanlar silinir — gerçek
        # zaman serisi olan bir fon asla otomatik silinmez.
        configured_codes = {row["code"] for row in configured_funds}
        if configured_codes:
            for fund in session.execute(select(Fund)).scalars().all():
                if fund.code in configured_codes:
                    continue
                has_prices = session.execute(
                    select(func.count()).select_from(FundPrice).where(
                        FundPrice.fund_code == fund.code
                    )
                ).scalar_one()
                if not has_prices:
                    session.delete(fund)
        session.commit()


def _configured_funds() -> list[dict]:
    """config/funds.yaml -> fon kataloğu satırları.

    Geriye dönük uyum: dosya yoksa eski yer olan sources.yaml'daki `funds`
    bölümüne düşülür. Sunucuda eski bir kopya üzerine güncelleme yapıldığında
    fon listesinin bir anda boşalmaması için.
    """
    # DİKKAT — YOLU BURADA HESAPLAMA. `config/funds.yaml` sabitini okumak,
    # Docker'da panelden eklenen fonların SESSİZCE SİLİNMESİNE yol açıyordu:
    # panel kayıt defterini `data/funds.yaml`'a (volume) yazıyor, buradaki
    # okuma ise imaj katmanındaki BAYAT kopyayı görüyordu; fonu bulamayınca
    # da (henüz fiyatı yoksa) katalogdan siliyordu. Tek çözümleyici:
    # config.loader.funds_yaml_path().
    kayit_defteri = funds_yaml_path()
    if kayit_defteri.exists():
        with open(kayit_defteri, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("funds", []) or []
    if SOURCES_YAML.exists():
        with open(SOURCES_YAML, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("funds", []) or []
    return []


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
