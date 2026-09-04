"""Agent tabanlı kredi oranı toplayıcısı.

Bir modelin çıktısını doğrudan finansal tabloya yazmak kabul edilemez.
Buradaki testler, uydurma bir faiz oranının veritabanına girmesini engelleyen
üç katmanı kilitliyor — en önemlisi ZEMİNLEME: modelin döndürdüğü her oran
sayfanın metninde gerçekten geçmek zorunda.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from collectors.base import ParseError, SanityCheckError
from collectors.loan_rates import LoanRateRecord
from collectors.loan_rates_llm import (
    LlmLoanRateCollector,
    LlmLoanRow,
    _appears_in_text,
    _looks_blocked,
    _rate_regions,
    _visible_text,
)
from store.db import SessionLocal
from store.models import Institution, LoanRate


# ------------------------------------------------------------ zeminleme ----

@pytest.mark.parametrize(
    "rate, text, beklenen",
    [
        (2.99, "36 ay vadede aylık %2,99 faiz", True),     # Türkçe virgül
        (2.99, "monthly rate 2.99%", True),                # nokta
        (3.19, "%3,19 oranıyla", True),
        (1.70, "aylık %1,70", True),                       # sondaki sıfır
        (3.00, "aylık %2,99", False),                      # YAKIN ama başka oran
        (4.19, "hiç oran yok", False),
    ],
)
def test_grounding_accepts_only_rates_that_really_appear(rate, text, beklenen):
    """Model bir sayı uydurursa kaynakta bulunmaz — asıl güvence bu.

    Yuvarlama toleransı YOK ve olmamalı: %2,99 ile %3,00 aynı oran değildir
    ve aradaki fark 1.000.000 TL'lik bir kredide binlerce lira eder.
    """
    assert _appears_in_text(rate, text) is beklenen


def test_hallucinated_rate_is_dropped_and_counted(db, caplog):
    """Sayfada olmayan oran ATILIR, atıldığı da kayda geçer."""
    collector = LlmLoanRateCollector(banks={})
    rows = [
        LlmLoanRow(loan_type="personal", monthly_rate_percent=2.99, available_to_all=True),
        LlmLoanRow(loan_type="housing", monthly_rate_percent=9.99,
                   available_to_all=True),                            # uydurma
    ]
    from collectors.loan_rates_llm import _ground

    kept, dropped = _ground(rows, "ihtiyaç kredisi aylık %2,99", "TEST")
    assert [r.loan_type for r in kept] == ["personal"]
    assert dropped == 1


# ------------------------------------------------------- bant kontrolü ----

def test_annual_rate_mistaken_for_monthly_is_rejected():
    """En olası model hatası: yıllık maliyet oranını aylık sanmak.

    QNB sayfasında hem %3,19 (aylık) hem %71,24 (yıllık maliyet) yazıyor;
    ikisi de sayfada geçtiği için zeminleme bunu yakalayamaz. Bandı aşan
    oranı reddeden katman burası.
    """
    with pytest.raises(ValueError):
        LlmLoanRow(loan_type="personal", monthly_rate_percent=71.24)


def test_zero_and_negative_rates_are_rejected():
    for bad in (0.0, -1.5):
        with pytest.raises(ValueError):
            LlmLoanRow(loan_type="personal", monthly_rate_percent=bad)


def test_unknown_loan_type_is_rejected():
    with pytest.raises(ValueError):
        LlmLoanRow(loan_type="kredi_karti", monthly_rate_percent=2.99)


# --------------------------------------------------------- token disiplini ----

def test_rate_regions_shrink_the_page_but_keep_the_rate():
    """Modele 105 KB göndermek token'ın çoğunu menüye harcar.

    Daha kötüsü: genel kırpma aranan oranı pencerenin dışında bırakabilir
    ve model sayfada yazan oranı GÖREMEDEN cevap üretmeye zorlanır — bu,
    uydurmayı davet eden tek durumdur.
    """
    text = ("menü " * 3000) + "ihtiyaç kredisi aylık %2,99 faiz" + (" yasal metin" * 3000)
    focused = _rate_regions(text)
    assert "%2,99" in focused
    assert len(focused) < len(text) / 10


def test_page_without_any_rate_yields_empty_so_the_model_is_not_called():
    """Oransız sayfaya token harcamak anlamsız — çağrı hiç yapılmamalı."""
    assert _rate_regions("bu sayfada hiç oran yok, sadece metin") == ""


def test_rate_regions_respects_the_character_budget():
    text = " ".join(f"aylık %{i % 9 + 1},50 faiz oranı" for i in range(2000))
    assert len(_rate_regions(text)) <= 6_000


def test_collector_skips_cleanly_when_the_agent_is_disabled(db, monkeypatch):
    """Anahtar yoksa TEK TOKEN harcanmadan, açık bir mesajla bitmeli."""
    import llm.settings as settings

    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", False)
    collector = LlmLoanRateCollector(banks={})
    payload = json.dumps({"HALKBANK": {"ok": True, "body": "aylık %2,99"}}).encode()
    with pytest.raises(ParseError, match="Agent kapalı"):
        collector.parse(payload)


# ----------------------------------------------------------- bot tespiti ----

def test_waf_block_page_is_detected_and_never_sent_to_the_model():
    """İş Bankası HTTP 200 dönüyor ama içerik engel sayfası.

    Modele göndermek hem token yakar hem de engel sayfasından oran
    "çıkarmaya" zorlar. Ayrıca bu bir robots.txt nezaket kuralı değil,
    aktif bot tespiti — aşılmıyor.
    """
    assert _looks_blocked("<html><body>İstek Engellenmiştir. Referans no: 123</body></html>")
    assert _looks_blocked('window["bobcmn"] = "101111"')
    assert not _looks_blocked("<html><body>aylık %2,99</body></html>")


# ------------------------------------------------------------- sanity ----

def test_wildly_conflicting_rates_from_the_agent_are_rejected(db):
    collector = LlmLoanRateCollector(banks={})
    with pytest.raises(SanityCheckError, match="sapan"):
        collector.sanity_check(
            [
                LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0299),
                LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.1899),
            ]
        )


def test_term_bounds_must_be_ordered(db):
    collector = LlmLoanRateCollector(banks={})
    with pytest.raises(SanityCheckError, match="term_min"):
        collector.sanity_check(
            [
                LoanRateRecord(
                    institution="ING", loan_type="personal", monthly_rate=0.0299,
                    term_min=60, term_max=12,
                )
            ]
        )


# ------------------------------------------------------------ persist ----

def test_persist_keeps_the_lowest_rate_when_a_page_lists_several(db):
    """Kampanya oranı ile tabela oranı yan yana durabiliyor.

    Düşük göstermek yüksek göstermekten güvenlidir: kullanıcı bankaya
    gidince beklediğinden iyi bir oranla karşılaşır, tersi değil.
    """
    with SessionLocal() as s:
        s.add(Institution(code="ING", name="ING", kind="bank"))
        s.commit()

    LlmLoanRateCollector(banks={}).persist(
        [
            LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0299),
            LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0169),
            LoanRateRecord(institution="ING", loan_type="personal", monthly_rate=0.0348),
        ],
        run_id=None,
    )
    with SessionLocal() as s:
        rows = s.execute(select(LoanRate)).scalars().all()
    assert len(rows) == 1
    assert float(rows[0].monthly_rate) == pytest.approx(0.0169)


# ------------------------------------------------------- yapılandırma ----

def test_only_active_banks_are_collected():
    """Yalnızca `active` kaynaklar çekilmeli.

    İş Bankası WAF arkasında (2026-09-02'de yeniden doğrulandı: engel
    sayfası dönüyor) ve envanterde kapalı; toplayıcı ona istek ATMAMALI.

    Bankayı ADIYLA sabitlemiyoruz: TEB ve Garanti bir ara kapalıydı, sonra
    çalışan BAŞKA bir URL bulununca açıldılar. Testin sabitlemesi gereken
    şey banka listesi değil, `status` filtresinin uygulandığı.
    """
    from collectors.loan_rates_llm import _load_banks
    from config.loader import load_sources

    banks = _load_banks()
    kurumlar = {c["institution"] for c in banks.values()}
    assert "ISBANK" not in kurumlar

    envanter = load_sources()["loan_llm_endpoints"]
    kapali = {k for k, v in envanter.items() if (v or {}).get("status") != "active"}
    assert kapali & set(banks) == set(), "kapalı kaynak listeye sızdı"
    assert len(banks) >= 10, "envanter beklenenden dar"


def test_one_bank_can_have_several_pages():
    """Aynı bankaya ihtiyaç/konut/taşıt sayfaları AYRI AYRI eklenebilmeli.

    Envanter eskiden kurum koduyla anahtarlanıyordu: ikinci sayfa birinciyi
    sessizce eziyor, yani konut ve taşıt sayfaları hiç çekilmiyordu ve bunu
    gösteren bir hata da olmuyordu.
    """
    from collectors.loan_rates_llm import _load_banks

    banks = _load_banks()
    kurum_sayisi: dict[str, int] = {}
    for cfg in banks.values():
        kurum_sayisi[cfg["institution"]] = kurum_sayisi.get(cfg["institution"], 0) + 1
    # Anahtarlar YAML anahtarı olduğu için sayfa sayısı kurum sayısından
    # büyük olabilir; eski kurulumda bu yapısal olarak imkânsızdı.
    assert len(banks) == sum(kurum_sayisi.values())
    assert all(cfg["url"].startswith("http") for cfg in banks.values())


def test_visible_text_strips_scripts_and_tags():
    html = "<html><script>var x='%9,99';</script><p>aylık <b>%2,99</b></p></html>"
    text = _visible_text(html)
    assert "%2,99" in text
    # Script içindeki sayı metne SIZMAMALI: zeminleme kontrolü metin
    # üzerinde çalışıyor ve script'teki bir sayı sahte bir onay üretirdi.
    assert "%9,99" not in text


# ------------------------------------------- herkese açık olmayan oranlar ----
#
# CANLI GÖZLEM (2026-08-30): panel ING'yi aylık %0,99 gösteriyordu —
# DenizBank'ın %2,99'unun üçte biri. Sayı sayfada gerçekten yazıyordu, yani
# zeminleme kusursuz çalışmıştı; %0,99 "Turuncu Ekstra Avantajlı Kredi"nin
# oranıydı ve yalnızca kampanyanın İLK kredi kullanımında geçerliydi.

ING_SAYFA = (
    "İlk kez ING'li olanlara %1,69 6 ay vadede 25.000 TL'ye kadar kredi "
    "fırsatı ya da %2,99'dan başlayan faiz oranlarıyla 36 ay vadeli ihtiyaç "
    "kredisi. Mevcut ING'liler de %3,48'den başlayan faiz oranı ile tüketici "
    "kredisi kullanabilir. Turuncu Ekstra Avantajlı Kredi'de sözleşme faizi "
    "değişmeyecek olup %0,99'dan başlar."
)


def _satir(oran, herkese, why=""):
    return LlmLoanRow(loan_type="personal", monthly_rate_percent=oran,
                      available_to_all=herkese, why=why)


def test_a_rate_that_is_not_open_to_everyone_is_dropped():
    """`persist` en düşüğü saklıyor; eleme olmadan panel en yanıltıcı sayıyı
    gösteriyordu. Kullanıcı ING'yi DenizBank'tan üç kat ucuz sanırdı."""
    from collectors.loan_rates_llm import _ground

    rows = [
        _satir(0.99, False, "yalnızca kampanyanın ilk kredi kullanımında"),
        _satir(1.69, False, "ilk kez ING'li olanlara"),
        _satir(2.99, False, "yeni ING'lilerin kullanımlarında"),
        _satir(3.48, True, "Mevcut ING'liler de kullanabilir"),
    ]
    kept, dropped = _ground(rows, ING_SAYFA, "ING")

    assert [r.monthly_rate_percent for r in kept] == [3.48]
    assert dropped == 3
    # persist en düşüğü alıyor: eleme sonrası doğru cevap %3,48.
    assert min(r.monthly_rate_percent for r in kept) == 3.48


def test_grounding_still_runs_before_the_availability_check():
    """Erişilebilirlik kontrolü zeminlemenin YERİNE geçmiyor, ÜSTÜNE biniyor.

    Uydurulmuş bir oranı model "herkese açık" diye işaretleyerek geçiremez.
    """
    from collectors.loan_rates_llm import _ground

    kept, dropped = _ground([_satir(7.77, True, "uydurma")], ING_SAYFA, "ING")
    assert kept == [] and dropped == 1


def test_a_general_rate_survives_even_when_the_page_is_a_campaign_page():
    """Anahtar kelimeyle elemek İKİ bankayı birden kaybettirirdi.

    Halkbank'ın gerçek gerekçesi "yeni müşterilere özel olduğuna dair kısıt
    YOK" — "yeni müşteri" kelimesini arayan bir filtre onu da elerdi. Karar
    bu yüzden kelimede değil, modelin cevapladığı dar soruda.
    """
    from collectors.loan_rates_llm import _ground

    sayfa = "Avantajlı İhtiyaç Kredisini %4,19 faiz oranından kullanın."
    kept, _ = _ground(
        [_satir(4.19, True, "yalnızca yeni müşterilere özel olduğuna dair kısıt yok")],
        sayfa, "HALKBANK",
    )
    assert [r.monthly_rate_percent for r in kept] == [4.19]


# ---------------------------------------------- modele verilen girdi -------
#
# Bu bölümdeki hataların hepsi CANLI gözlendi (2026-09-02/03) ve hiçbiri
# modelin hatası değildi: modele yanlış ya da bozuk metin veriyorduk.


def test_section_headings_survive_flattening():
    """Bölüm başlıkları metinde KALMALI.

    DenizBank sayfasında başlıklar silinince "emeklilere özel borç transfer
    kredisi" cümlesi, iki bölüm sonraki GENEL %2,99 oranının hemen yanına
    düşüyordu; denenen altı modelin altısı da oranı bu paragrafa bağlayıp
    eledi. Başlık, modelin "burada yeni bir ürün başlıyor" diyebilmesi için
    metinde görünür olmak zorunda.
    """
    html = "<p>emeklilere özeldir.</p><h2>Özellikleri</h2><p>%2,99 ile başvurun</p>"
    text = _visible_text(html)
    assert "### Özellikleri" in text
    assert text.index("### Özellikleri") < text.index("%2,99")


def test_html_entities_are_decoded():
    """Varlıklar çözülmeli — model bozuk Türkçeden oran çıkaramıyor.

    CepteTEB sayfası modele "m&uuml;şterisi değilseniz" diye gidiyordu.
    Elle kurulmuş iki varlıklı bir liste (&nbsp; ve &amp;) geri kalan
    yüzlerce varlığı sessizce metinde bırakıyordu.
    """
    text = _visible_text("<p>Hen&uuml;z m&uuml;şteri de&#287;ilseniz %2,99</p>")
    assert "&uuml;" not in text and "Henüz" in text and "%2,99" in text


def test_window_starts_at_the_rates_own_section():
    """Pencere, oranın KENDİ bölümünden başlamalı — bir öncekinden değil."""
    from collectors.loan_rates_llm import _rate_regions

    text = _visible_text(
        "<p>" + "Bu ürün yalnızca emeklilere yöneliktir. " * 6 + "</p>"
        "<h2>Özellikleri</h2><p>%2,99 oranıyla herkes başvurabilir.</p>"
    )
    odak = _rate_regions(text)
    assert "%2,99" in odak
    assert "emeklilere" not in odak, "önceki bölüm pencereye sızdı"


def test_drop_reasons_are_reported_separately():
    """"Sayfada yok" ile "herkese açık değil" AYNI ŞEY DEĞİL.

    İkisi tek sayıya indirilip "sayfada bulunamadı" diye raporlanıyordu.
    Odeabank'ta yedi oranın yedisi de sayfada gerçekten yazıyordu; hepsi
    "ön onaylı müşterilere" özel olduğu için elenmişti. Yanlış etiket,
    olmayan bir uydurma hatasının peşine düşürdü.
    """
    from collectors.loan_rates_llm import _ground

    rows = [
        _satir(9.99, True, "sayfada olmayan oran"),
        _satir(3.29, False, "yalnızca ön onaylı müşterilere"),
    ]
    _kept, elenen = _ground(rows, "kredi %3,29 ile", "ODEABANK")
    assert elenen == 2
    assert elenen.yok_sayfada == 1 and elenen.kosullu == 1
    assert "sayfada bulunamadı" in elenen.aciklama()
    assert "herkese açık değil" in elenen.aciklama()


def test_loan_type_hint_is_injected_without_a_format_placeholder():
    """Tür ipucu prompt'a girmeli ama `.format()` tuzağı KURMAMALI.

    Prompt'a `{hint}` gibi bir yer tutucu koymak, prompt'u doğrudan
    `.format(html=...)` ile kullanan her çağrı yolunu KeyError ile
    patlatırdı.
    """
    from collectors.loan_rates_llm import LlmLoanRateCollector

    banks = {
        "x_tasit": {"key": "x", "url": "http://e", "institution": "X",
                    "loan_type_hint": "vehicle"},
        "y": {"key": "y", "url": "http://e", "institution": "Y",
              "loan_type_hint": None},
    }
    kol = LlmLoanRateCollector(banks=banks)
    ipuclu = kol._prompt_for("x_tasit")
    assert "TAŞIT" in ipuclu and "vehicle" in ipuclu
    assert kol._prompt_for("y") == kol.PROMPT      # ipucu yoksa dokunma
    ipuclu.format(html="<p>x</p>")                 # patlamamalı


# ------------------------------------------ ipucuyla uyuşmayan ürün türü ----

def test_housing_page_returning_only_personal_rates_is_not_folded_into_personal(db, monkeypatch):
    """SESSİZ KAYIP REGRESYONU (canlıda yakalandı, 2026-09-04).

    persist() aynı (kurum, tür) için EN DÜŞÜK oranı bırakıyor. Bir KONUT
    sayfasından `personal` satır dönerse, o satır aynı bankanın ihtiyaç
    satırıyla aynı anahtara düşüp sessizce kayboluyordu: kaynak her gün
    "ok, 1 satır" diyor, konut kredisi tablosunda hiçbir şey görünmüyor ve
    hiçbir yerde iz kalmıyordu.

    Anadolubank'ın konut sayfası tam olarak böyleydi — çekilen metni,
    sitenin her sayfasında duran "%3,09 İHTİYAÇ" banner'ıydı. Model DOĞRU
    davranıyordu; kaynak yanlıştı. Bunu görebilmek için kayıt şart.
    """
    import llm.extract as llm_extract
    import llm.settings as settings
    from collectors.loan_rates_llm import LlmLoanRow

    # conftest her testi KAPALI agent ile başlatıyor (kazara ağa çıkmasın).
    # Burada parse yolunu sınıyoruz, extract zaten sahte.
    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(
        llm_extract, "extract",
        lambda *a, **k: [LlmLoanRow(
            institution="ANADOLUBANK", loan_type="personal",
            monthly_rate_percent=3.09, rate_sentence="aylık %3,09 ihtiyaç kredisi",
            available_to_all=True,
        )],
    )
    collector = LlmLoanRateCollector(banks={
        "anadolubank_konut": {
            "institution": "ANADOLUBANK", "loan_type_hint": "housing",
            "url": "https://ornek/konut",
        }
    })
    payload = json.dumps({
        "anadolubank_konut": {"ok": True, "body": "aylık %3,09 ihtiyaç kredisi"}
    }).encode()

    with pytest.raises(ParseError):
        collector.parse(payload)   # tek kaynak, hiç kayıt kalmadı

    from sqlalchemy import select
    from store.db import SessionLocal
    from store.models import SourceRun
    with SessionLocal() as s:
        kayit = s.execute(
            select(SourceRun).where(SourceRun.source == "anadolubank_konut")
        ).scalars().all()
    assert kayit, "uyuşmazlık kaydı yazılmalı"
    assert kayit[-1].status == "empty"
    assert "housing" in (kayit[-1].error or "")


def test_a_hinted_page_that_does_return_the_right_type_is_kept(db, monkeypatch):
    """Doğru türü döndüren sayfa elbette yazılmalı — kontrol fazla geniş olmasın."""
    import llm.extract as llm_extract
    import llm.settings as settings
    from collectors.loan_rates_llm import LlmLoanRow

    # conftest her testi KAPALI agent ile başlatıyor (kazara ağa çıkmasın).
    # Burada parse yolunu sınıyoruz, extract zaten sahte.
    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(
        llm_extract, "extract",
        lambda *a, **k: [LlmLoanRow(
            institution="QNB", loan_type="housing",
            monthly_rate_percent=2.99, rate_sentence="konut kredisi aylık %2,99",
            available_to_all=True,
        )],
    )
    collector = LlmLoanRateCollector(banks={
        "qnb_konut": {"institution": "QNB", "loan_type_hint": "housing",
                      "url": "https://ornek/konut"}
    })
    kayitlar = collector.parse(json.dumps({
        "qnb_konut": {"ok": True, "body": "konut kredisi aylık %2,99"}
    }).encode())
    assert [r.loan_type for r in kayitlar] == ["housing"]


# ------------------------------------------------ zeminleme: sayı sınırı ----

@pytest.mark.parametrize("oran,metin,ad", [
    (5.00, "aylık taksit 5.014 TL", "taksit tutarı"),
    (3.00, "100.000 TL'ye kadar 36 ay", "kredi tutarı"),
    (2.00, "vade 24 ay", "vade"),
    (4.00, "geçerlilik 04.09.2026", "tarih"),
    (5.00, "yıllık maliyet %65.014", "daha uzun bir oranın parçası"),
])
def test_a_round_rate_is_not_grounded_by_a_digit_inside_another_number(oran, metin, ad):
    """ZEMİNLEME AÇIĞI REGRESYONU (canlıda yakalandı, 2026-09-05).

    Kontrol düz bir "alt dizge var mı" idi. "%5,00" ->
    `"5.00".rstrip("0").rstrip(".")` -> "5", yani TEK KARAKTERLİK arama.
    Sonuçta %2,00 gibi bir oran "vade 24 ay" yazan bir sayfada bile
    zeminlenmiş sayılıyordu — yani yuvarlak oranlar için bu güvence hiç
    yoktu.

    Somut zarar: qwen-flash Burgan'ın konut ve ihtiyaç oranını %5,00 diye
    döndürdü (sayfa 3,25% ve 3,75% yazıyor; 5,014 TL bir TAKSİT tutarıydı)
    ve zeminleme geçirdi. Yanlış faiz oranı veritabanına yazıldı.
    """
    assert _appears_in_text(oran, metin) is False, ad


@pytest.mark.parametrize("oran,metin,ad", [
    (5.00, "faiz oranı %5 uygulanır", "gerçek yuvarlak oran"),
    (5.00, "aylık %5,00 faiz", "iki ondalıklı yazım"),
    (3.25, "interest rate of 3.25% per month", "Burgan konut sayfasının GERÇEK oranı"),
    (3.75, "subject to an interest rate of 3.75%", "Burgan ihtiyaç sayfasının GERÇEK oranı"),
    (2.99, "aylık %2,99 faiz", "Türkçe virgül"),
    (3.09, "%3,09'dan başlayan", "kesme işareti bitişik"),
])
def test_a_real_rate_is_still_grounded_after_the_boundary_fix(oran, metin, ad):
    """Sınır kontrolü fazla dar olmamalı — gerçek oranlar geçmeye devam etmeli."""
    assert _appears_in_text(oran, metin) is True, ad


# --------------------------------------- satır bazında şema doğrulaması ----

def test_one_invalid_row_does_not_discard_the_valid_ones(db, monkeypatch):
    """TEK BOZUK SATIR REGRESYONU (canlıda yakalandı, 2026-09-05).

    Yanıtın tamamı `wrapper.model_validate` ile bir kerede doğrulanıyordu:
    bir satır bant kontrolüne takılınca istisna yükseliyor ve GEÇERLİ
    satırlar da dahil yanıtın tamamı çöpe gidiyordu — harcanan token'la
    birlikte.

    DenizBank taşıt sayfasında model 5 satır döndürdü; 4'ü kredi/değer
    oranını (%70, %50, %30) aylık faiz sanmıştı ve bant kontrolü onları
    HAKLI olarak reddetti. Ama 5. satır geçerliydi ve hepsi birden
    atıldığı için DenizBank'ın taşıt kredisi tablodan tamamen kayboldu.
    """
    import json as _json

    import llm.extract as llm_extract
    import llm.settings as settings
    from collectors.loan_rates_llm import LlmLoanRow

    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sahte")

    yanit = _json.dumps({"rows": [
        {"institution": "DENIZBANK", "loan_type": "vehicle",
         "monthly_rate_percent": 70.0, "rate_sentence": "kredi/değer oranı %70",
         "available_to_all": True},                       # bant dışı — elenmeli
        {"institution": "DENIZBANK", "loan_type": "vehicle",
         "monthly_rate_percent": 4.14, "rate_sentence": "aylık %4,14 faiz",
         "available_to_all": True},                       # GEÇERLİ — kalmalı
    ]})

    class _Msg:  content = yanit
    class _Choice:  message = _Msg()
    class _Resp:
        choices = [_Choice()]
        usage = type("u", (), {"prompt_tokens": 10, "completion_tokens": 5})()

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw): return _Resp()

    monkeypatch.setattr(llm_extract, "get_client", lambda: _Client())
    llm_extract.reset_budget()

    satirlar = llm_extract.extract(
        "aylık %4,14 faiz", LlmLoanRow, collector="loan_rates_llm",
        trigger_source="denizbank_tasit",
    )
    assert len(satirlar) == 1, "geçerli satır korunmalı"
    assert satirlar[0].monthly_rate_percent == 4.14


def test_a_response_where_every_row_is_invalid_still_fails(db, monkeypatch):
    """Eleme gevşememeli: hiçbir satır geçerli değilse bu bir hatadır."""
    import json as _json

    import llm.extract as llm_extract
    import llm.settings as settings
    from collectors.loan_rates_llm import LlmLoanRow

    monkeypatch.setattr(settings, "LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sahte")

    yanit = _json.dumps({"rows": [
        {"institution": "X", "loan_type": "vehicle", "monthly_rate_percent": 70.0,
         "rate_sentence": "s", "available_to_all": True},
    ]})

    class _Msg:  content = yanit
    class _Choice:  message = _Msg()
    class _Resp:
        choices = [_Choice()]
        usage = type("u", (), {"prompt_tokens": 10, "completion_tokens": 5})()
    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw): return _Resp()

    monkeypatch.setattr(llm_extract, "get_client", lambda: _Client())
    llm_extract.reset_budget()

    with pytest.raises(Exception):
        llm_extract.extract("metin", LlmLoanRow, collector="loan_rates_llm")
