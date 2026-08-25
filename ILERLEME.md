# İlerleme Günlüğü

Bu dosya `tasarim.html`'deki tasarımın neresinin yapıldığını, neresinin
yapılmadığını ve neden takip eder. Her oturumda güncellenmeli — yeni bir
oturum başladığında önce burayı oku.

> **Açık işlerin planı ve gerekçeleri: [PLAN.md](PLAN.md)** — kök neden
> analizi, değerlendirilen seçenekler ve neden hangisinin seçildiği.

Son güncelleme: **2026-08-24** (5. oturum — robots kısıtı kaldırıldı, Akbank/Ziraat/Kuveyt Türk eklendi, LLM + Docker)

---

## Genel durum

| Faz | Durum | Not |
|---|---|---|
| Faz 0 — Hesaplama motoru + testler | ✅ Tamamlandı | **265 test geçiyor** |
| Faz 1 — Şema + TCMB + fon fiyatları | ✅ Tamamlandı | TEFAS emekliye ayrıldı, yerine Ak Portföy |
| Faz 2 — Streamlit v1 | ✅ Tamamlandı | 4 panel, kurum bazlı tablolar, tarayıcıda doğrulandı |
| Faz 3 — Banka collector'ları | 🟢 Çoğunlukla tamam | 10 kurum + 2 portföy şirketi: 8 kur, 7 mevduat/katılma, 5 kredi, 2 kâr payı, 11 fon |
| Faz 4 — Zamanlama + gözlem | ✅ Tamamlandı | 5 katmanlı kayıt: koşu / kaynak / **istek** / **oran değişimi** / LLM |
| Faz 5 — LLM fallback | 🟡 Hazır, kapalı | Gemini bağlı, token korumaları yerinde; anahtar girilince açılacak |

**Panel durumu — hepsi canlı tarayıcı testinden geçti:**
- 🟢 **Döviz** — kurum başına ayrı tablo (6 kurum: TCMB, TEB, VakıfBank, Enpara,
  Emlak Katılım, Yapı Kredi), makas sütunu, tazelik +
  `≈` tahmini-zaman işareti, tablo altında veri çekilme zamanı
- 🟢 **Mevduat & Kar Payı** — kurum başına ayrı tablo (VakıfBank, TEB, Enpara,
  Yapı Kredi), TRY/USD/EUR, stopaj + brüt/net sütunları, tablo altında veri
  çekilme zamanı ve kullanılan tutar kademesi
- 🟢 **Kredi** — banka adı hem kıyas tablosunda hem ödeme planı başlığında;
  vade/tutar sınırı aşıldığında açık uyarı; tam ödeme planı + vergi kırılımı
- 🟢 **Fon Simülasyonu** — **11 fon, 2 sağlayıcı** (Ak Portföy + Garanti BBVA
  Portföy), panelden "Fon ekle", mevduat kıyası hangi bankadan geldiğini de
  söylüyor
- 🟢 **Kaynaklar** *(yeni)* — her sayının uç noktası, envanterin iddiası ile
  çalışan sistemin gerçeği yan yana, kapalı kaynakların gerekçesi
- 🟢 **Kayıtlar** — 7 alt sekme: kaynak sağlığı, HTTP istekleri, kurtarma,
  oran değişimleri, agent (LLM), koşular, olay akışı

---

## Bu oturumda yapılanlar (6. oturum — PLAN.md'deki yedi maddenin tamamı)

Kullanıcı isteği: *"sırayla hepsini yapmaya başla. tamamını bitirmeden durma.
her biri olmuş mu diye test et. hem browserdan test et hem de diğer türlü
testleri yap. gerçek bir kullanıcıymış gibi test etmek en önemlisi."*

**265 test geçiyor** (önce 188). Her madde ayrıca tarayıcıda, gerçek veriyle,
gerçek kullanıcı akışıyla denendi.

### Madde 5 — Ödeme planı şeffaflığı ✅

- Ödeme planının üstüne açık not: **"Bu plan bankanın resmî ödeme planı
  değildir."** Bankadan gelen tek veri aylık orandır; taksit, anapara/faiz
  ayrışması ve KKDF/BSMV kırılımı `core/loan.py`'nin anüite hesabıdır.
- **Çapraz doğrulama** (asıl kazanç): yeni `loan_reference_quotes` tablosu
  Yapı Kredi'nin **kendi taksit tutarını** saklıyor; her koşuda bizim
  hesabımızla karşılaştırılıp `source_runs`'a `validate` aşaması yazılıyor.
  Canlı sonuç: ihtiyaç 100.000 TL / 3 ay -> banka 38.654,31 · bizim 38.654,31;
  konut 1.000.000 TL / 36 ay -> ikisi de 48.156,85. **Sapma %0,00001.**
- Neden değerli: KKDF/BSMV oranları elle girilir ve yanlış olsalar hiçbir
  testimiz düşmez (testler de aynı dosyayı okur). Onları yakalayabilen tek
  bağımsız tanık bankanın kendi rakamıdır. Regresyon testi, konut kredisine
  yanlışlıkla ihtiyaç kredisinin vergisi uygulandığında kıyasın bunu
  yakaladığını kanıtlıyor.
- **Yıllık maliyet** sütunu eklendi: `(1+efektif_aylık)¹²−1`. Aylık oranı 12
  ile çarpmak yüksek oranlarda 10 puana varan hata veriyor.
- **PLAN.md'de bir hatam düzeltildi:** "Akbank da kendi taksitini döndürüyor"
  yazmıştım, yanlıştı — `urunTaksitTut` üç üründe de `null`.

### Madde 4 — Kredi kapsam görünürlüğü ✅

- Kredi türü seçicisinin altında kapsam satırı: *"Konut: 3 · Taşıt: 1 ·
  İhtiyaç: 5"*.
- Açılır blokta **her eksik kurum için gerekçe**: "Emlak Katılım — Konut
  finansmanı sayfasında oran yok; yalnızca teminat oranı ve azami vade
  yayınlanıyor" gibi. Gerekçeler `config/sources.yaml`'da artık **yapısal**
  (`covers` / `missing`), düz yorum değil — ekrana basılabiliyor.
- Test, gerekçesiz eksiklik bırakılmasını engelliyor: yeni banka eklenip
  gerekçesi yazılmazsa test düşer.
- `sources.yaml`'daki **Enpara kredi kaydı düzeltildi**: "CSRF gerekiyor,
  kapalı" yazıyordu ama toplayıcı onu aylardır sorunsuz çekiyordu.

### Madde 1 — Kaynak şeffaflığı ✅

- Yeni **"Kaynaklar" sekmesi**. `config/sources.yaml`'dan okunuyor; ikinci
  bir liste tutulmuyor.
- **İki ayrı gerçek yan yana:** envanterin iddiası (status/last_verified) ve
  çalışan sistemin gerçeği (`source_runs`'taki son başarılı koşu). Çeliştiğinde
  panel bunu **"Uyuşmazlık"** sütununda söylüyor — Enpara vakasının bir daha
  sessizce yaşanmaması için.
- Tablo altı künyeler: döviz, mevduat ve kâr payı tablolarının altına
  "Kaynak: `...` · tip · son doğrulama · robots kararı" satırı.
- `sources.yaml`'a iki eksik bölüm eklendi: **kâr payı** ve **fon** kaynakları
  envanterde hiç yoktu (toplayıcı vardı, kaynağı yazmıyordu).
- robots.txt kararı ile bot tespiti ayrımı sekmede açıkça yazılı.

### Madde 7 — Ayrıntılı API loglama ✅

Yeni: `store/migrate.py` (additive SQLite sütun ekleme — `create_all` var olan
tabloya sütun eklemez ve sunucuda `no such column` ile patlardı).

| İstenen | Yapılan |
|---|---|
| "hangi API istekleri ne zaman dönmedi" | `http_requests` tablosu + `collectors/http.py` sarmalayıcısı: URL, method, HTTP kodu, gecikme, yanıt boyutu, content-type, deneme no |
| "daha sonraki istekleri döndü mü" | `source_recovery()` — kesinti başlangıcı/bitişi, "düzeldi" / "hâlâ bozuk" + kesinti süresi |
| "agent ne zaman hangi durumda devreye girdi" | `llm_calls.trigger_source` + `trigger_error` — modeli ÇAĞIRAN hata kaydediliyor |
| "kaç token harcadı" | + **tahmini maliyet** (`cost_usd`); fiyat tanımlı değilse NULL kalır — 0 yazmak "bedava" demek olurdu |

**Kullanıcının saymadığı, eklenenler:**
- **Oran değişim izi** (`rate_changes`): sessiz donma tespiti. Uç nokta 200
  dönüyor ama sayı haftalardır aynıysa besleme durmuş olabilir — `empty`
  kadar sinsi. Yalnızca DEĞİŞİM yazılır, tablo yavaş büyür.
- **Şema sapması uyarısı**: bir kaynaktan gelen satır sayısı önceki koşuların
  ortalamasına göre %40'tan fazla düştüyse uyarı. Parser patlamadan satır
  kaybetmek en sinsi bozulma türü.
- **Aşama bazında hata** (`scrape_runs.failure_kind`): fetch / parse /
  **sanity** / persist. `sanity` ayrı tutuluyor çünkü o bir arıza değil,
  korumanın çalıştığının kanıtı — "kaç kez bant dışı veri geldi" artık
  sayılabiliyor.
- **Tetikleyici izi** (`scrape_runs.trigger`): plan / açılış / **tazelik
  telafisi** / elle. Sunucuda erişim olmayacağı için telafinin gerçekten
  çalıştığını gösterebilen tek kayıt.
- **Saklama süresi** (`store/retention.py`): `http_requests` 30 gün,
  `source_runs`/`scrape_runs` 180 gün, snapshot 14 gün. `llm_calls` ve
  `rate_changes` **asla budanmaz** (kümülatif muhasebe). Zamanlayıcı günde
  bir buduyor.
- **Geçici ağ hatasında tek yeniden deneme**: canlı gözlemde Garanti Portföy
  ara sıra yanıt vermeden bağlantıyı kapatıyor, bir sonraki istek çalışıyor.
  Yalnızca ağ hataları denenir; HTTP 5xx **denenmez** (500 dönen uç noktayı
  dövmek ne çözer ne naziktir — onun yeri tazelik telafisi).

Kayıtlar sekmesi 7 alt sekmeye ayrıldı ve en üste **bekleyen bozulma
uyarıları** kondu (sekmeye gömülü uyarıyı kimse açmıyor).

### Madde 2 + 3 — Katılım bankası yıllık oranı ✅ (araştırma sonuç verdi)

**Her iki bankada da yıllık oran bulundu** — plandaki (c) seçeneği.

- **Kuveyt Türk** `lastProfitShareRates`: gerçekleşen brüt oranlar
  (1 ay %32,99 · 3 ay %34,42 · 6 ay %35,75 · 12 ay %39,32; TL/USD/EUR).
  Uç nokta adresi JS paketinde `ck0d84?<HASH>` biçiminde ve hash dağıtımla
  değişiyor; **koda gömülmedi**, her koşuda zincirleme keşfediliyor.
- **Emlak Katılım** `/Plugins/CalculateProfitShareRate`: tutar × vade ×
  para birimi matrisi, `SegmentName` ile kademe (Klasik/Altın/Platin/Platin+).
- Sonuç: katılım bankaları artık **faiz bankalarıyla aynı tabloda, aynı
  sütunlarda ve aynı sıralamada**. Canlı: Emlak Katılım yıllık net %35,20 ile
  1. sırada, Kuveyt Türk %33,42 ile 2.; Yapı Kredi %33,00 üçüncü.
- Kâr **paylaşım** oranı silinmedi; sayfanın **en altına** taşındı ve
  "yukarıdaki oranlarla aynı şey değil" diye etiketlendi. Eskiden en üstte
  duruyor ve tam da kullanıcının şikâyet ettiği yanılsamayı üretiyordu.
- **Bonus doğrulama:** Emlak Katılım hem brüt hem net yıllık oranı
  yayınlıyor; bizim stopaj modelimiz dört vadede de bankanın net rakamıyla
  birebir tuttu (%41,41 × 0,85 = %35,20). `taxes.yaml`'ın mevduat tarafı da
  artık bağımsız bir tanıkla sınanıyor.

### Madde 6 — Fon sağlayıcı mimarisi ✅

- `collectors/fund_providers.py`: `FundPriceProvider` arayüzü +
  `AkPortfoyProvider` + **`GarantiPortfoyProvider`** (yeni).
- `config/funds.yaml`: fon kayıt defteri. **Fon eklemek artık bir satır.**
- Panelde **"➕ Fon ekle"**: sağlayıcı seç, kod ve adres parçası gir, kayıt
  defterine yazılır. Panel veri toplamaz (tasarım §01); fiyatlar bir sonraki
  koşuda gelir.
- **Garanti Portföy akışı**: `/webservice/gettoken` (anonim bearer) ->
  `/webservice/lastdate` -> `/webservice/fundsinddailyds`. Belirteç bir
  kullanıcı kimliği değil, sitenin herkese açık ön yüz belirteci —
  VakıfBank akışıyla aynı desen.
- **DİKKAT ve panelde yazılı:** Garanti birim pay fiyatı yayınlamıyor,
  "1.000 TL yatırılsaydı" **endeksi** veriyor. Getiri hesabı yalnızca oranı
  kullandığı için sonuç doğru; ama sayı "fiyat" diye gösterilirse yanıltır.
  Sağlayıcı `price_is_unit_value` bayrağı taşıyor, panel etiketi ona göre
  seçiyor.
- Fon sayısı 4 -> **11**. İstenen kategoriler karşılandı: BIST 30 endeks
  (GAE), hisse senedi (GHS, AK3, ADP), para piyasası (GPB, GNP), altın
  (GTA, AFO), gümüş (GTZ), kısa vadeli borçlanma (TGT).
- **TEFAS yine açılmadı** — F5 Shape bot-challenge; kullanıcının robots
  kararı bunu kapsamıyor. **İş Portföy** yarım kaldı: fon listesi
  okunabiliyor ama fiyat serisi uç noktası bulunamadı; `sources.yaml`'da
  `unverified` olarak, nerede kalındığı yazılı.

### Gerçek kullanıcı testinde yakalanan hatalar

Bunların hiçbirini birim testler yakalayamazdı:

1. **`init_db` açılışta çöküyordu.** Panelden ad girmeden fon eklenince
   `seed_reference_data` `row["name"]` diyerek KeyError atıyordu — yani
   panelden fon eklemek bir sonraki başlatmada TÜM paneli karartıyordu.
   Düzeltildi + regresyon testi.
2. **User-Agent'ta Türkçe karakter.** `"kişisel kullanım"` içeren bir
   User-Agent, istek daha ağa çıkmadan `UnicodeEncodeError` ile patlıyor
   (HTTP başlıkları latin-1). Katılım toplayıcısı ilk canlı koşuda böyle
   düştü.
3. **TCMB'de yanlış "başarılı koşu yok" uyarısı.** Tek kaynaklı toplayıcılar
   `source_runs`'a hiç satır yazmıyordu; Kaynaklar sekmesi çalışan bir
   kaynağı bozuk gösteriyordu. `Collector.default_source` ile çözüldü.
4. **Kaynak envanteri panelde eskimiş kalıyordu.** `load_sources`'ın
   `lru_cache`'i `clear_cache()`'te temizlenmiyordu; sources.yaml'a eklenen
   bölümler süreç yeniden başlatılana kadar görünmedi.
5. **YAML yorumları siliniyordu.** Panelden fon eklemek `yaml.safe_dump` ile
   dosyayı yeniden yazıyor ve `is_equity_heavy`in neden kayıt defterinde
   tutulmadığını anlatan başlık açıklamalarını atıyordu. Başlık artık
   korunuyor + testi var.

---

## Bu oturumda yapılanlar (5. oturum, ikinci bölüm — robots kararı sonrası)

Kullanıcı kararı: **robots.txt kısıtları bu projede uygulanmayacak** (kişisel
kullanım, günde birkaç istek, herkese açık ve kimlik doğrulaması olmayan veri).
Her kaynak `config/sources.yaml`'da `robots_override: true` ile işaretli, karar
geri alınırsa o alanı taşıyanları kapatmak yeterli.

### Açılan kaynaklar
| Kurum | Ne geldi |
|---|---|
| **Akbank** | Kur + mevduat matrisi + **üç kredi türü** (`GetDovizKurlari`, `GetMevduatFaiz`, `GetCreditInfo`) |
| **Ziraat** | Kur (`GetZiraatVerileri`, HTML-sarılı JSON) |

Akbank'ın kredi uç noktası projedeki **tek taşıt kredisi kaynağı** — uzun
süredir boş olan tür artık dolu.

### Robots kararıyla İLGİSİ OLMAYAN, hâlâ kapalı olanlar
- **İş Bankası**: WAF. Normal tarayıcı gezintisinde bile blok sayfası dönüyor.
  Bu bir nezaket kuralı değil, aktif bot tespiti; aşmak parmak izi taklidi
  gerektirir. Kullanıcının kararı bunu kapsamıyor, açılmadı.
- **Garanti**: tam tarayıcı başlıklarıyla bile HTTP 500; oturum çerezi gerekiyor.
- **Halkbank**: TLS handshake tamamlanmıyor.

### Enpara kredisi — önceki tespit YANLIŞTI, düzeltildi
"CSRF token gerektiriyor" diye kapalı bırakılmıştı. Sayfadaki `-` yer tutucusu
JS ile dolduğu için öyle görünüyordu. Gerçekte oran tablosu sayfaya sunucu
tarafında gömülü:

    var loanInterestRates = JSON.parse('[{"Title":"0-12 Ay",...}]')

Tek GET yeterli. **Ders:** "değer ekranda JS ile geliyor" ile "veri sayfada yok"
aynı şey değil. Aynı kontrolü TEB'e de uyguladım — orada gerçekten yok.

### Katılım bankası kâr paylaşım oranları — yeni tablo
Kullanıcı "mevduat sekmesine Emlak Katılım'ı da ekle" dedi. Sorun: bu banka
yıllık getiri yayınlamıyor, **kâr paylaşım oranı** yayınlıyor (kârın müşteriye
düşen yüzdesi). `%92`'yi `annual_rate` diye yazmak paneli 1M TL için ~920.000 TL
getiri hesaplamaya iterdi — tamamen uydurma bir sayı.

Çözüm: ayrı tablo (`profit_share_ratios`), ayrı toplayıcı
(`collectors/profit_shares.py`), panelde **en üstte ayrı blok**, net getiri
sütunu YOK ve ne olduğu açıkça yazılı. Emlak Katılım + Kuveyt Türk.

Yan fayda: iki banka da stopaj oranlarını yayınlıyor ve ikisi de
`config/taxes.yaml`'la örtüşüyor (TL 17,5/17,5/17,5/17,5/15/10 · döviz 25) —
vergi tablomuzun bağımsız teyidi.

### Bulunan ve düzeltilen hatalar

| # | Hata | Etki | Düzeltme |
|---|---|---|---|
| 1 | **`valid_date` makinenin yerel tarihini kullanıyordu** | Docker UTC çalışır; Türkiye'de 00:00-03:00 arası çekilen BÜTÜN oranlar bir gün geriye yazılırdı ve "bugünün eski satırlarını temizle" mantığı yanlış günü hedeflerdi | `store/clock.py` — `istanbul_today()`. Konteynerin TZ'si de ayarlandı |
| 2 | Kurum etiketleri üç panelde üç kopya | Yeni kurum eklenince biri güncellenmeyi unutuyor; panelde ham kod (`AKBANK`, `ZIRAAT`) göründü | `app/panels/common.py` tek kaynak |
| 3 | LLM fallback hatası orijinal parse hatasını yutuyordu | Asıl sorun (kırık selector) log'da kayboluyordu | Fallback başarısızsa orijinal istisna yeniden fırlatılıyor |
| 4 | TEB mevduatta çakışan oranlar (60 ay: 15,0/15,25/15,50) | Rastgele biri seçilirse getiri sessizce yanlış | En düşük seçiliyor (eksik göstermek güvenli taraf) |

### Docker
Tek imaj iki rolü koşuyor. `docker compose up -d` ile panel + zamanlayıcı
birlikte kalkıyor; cron gerekmiyor. Root olmayan kullanıcı, healthcheck,
`TZ=Europe/Istanbul`, adlandırılmış volume.

> Not: bu makinede `docker build` daemon kaynaklı bir hatayla düştü
> (`unable to lease content: lease does not exist` — bozuk buildkit önbelleği,
> Dockerfile ile ilgisi yok). Çözüm: `docker builder prune -af` ya da Docker
> Desktop'ı yeniden başlatmak.

---

## Bu oturumda yapılanlar (5. oturum)

Kullanıcı sordu: kredi sekmesinde neden tüm bankalar yok, Enpara oranları
neden yanlış görünüyor, agent kullandık mı, Akbank eklenebilir mi.

### 1. Enpara "yanlış oran" iddiası — veri DOĞRU çıktı, ama gerçek bir hata ortaya çıkardı

Üç ayrı doğrulama yaptım:

1. Canlı sayfayı yeniden çektim → DB'deki 16 değerin hepsi birebir aynı.
2. Stopajı **TEB'in kendi hesaplama motoruna** sordurup ampirik olarak
   çıkardım: ≤180 gün %17,5 · 182–365 gün %15 · ≥366 gün %10 →
   `taxes.yaml` ile uyuşuyor.
3. **Enpara'nın kendi hesaplayıcısını** tarayıcıda 1.000.000 TL ile
   çalıştırdım → `%38,25 / 37,25 / 35,25 / 32,25` ve "750.000 - 1.500.000 TL"
   kademesi; panelle birebir aynı.

Karışıklığın kaynağı: Enpara'nın hesaplayıcısı **varsayılan 10.000 TL** ile
açılıp `%30,75` gösteriyor, oran sayfasında da en üstte 0–150.000 kademesi
duruyor. Panelin 1M için gösterdiği %38,25 ile kıyaslanınca "yanlış" görünüyor.
→ Düzeltme: kullanılan tutar kademesi artık **tablonun üstünde**, kurum
başlığının hemen altında ve anaparayla birlikte yazılıyor.

### 2. GERÇEK HATA: kurumlar mutlak getiriye göre sıralanıyordu

Panel bankaları "en iyi net getiri"ye göre sıralıyordu ve bu farklı vadeleri
kıyaslıyordu. Sonuç, **en kötü teklifi birinci gösteriyordu**:

| | Banka | En iyi mutlak net | Vade | Yıllıklandırılmış net |
|---|---|---|---|---|
| 1 | VAKIFBANK | 332.876 TL | 750 gün (%18,00) | **%16,20** |
| 2 | YAPIKREDI | 303.152 TL | 367 gün (%33,50) | **%30,15** |
| 3 | ENPARA | 135.935 TL | 181 gün (%32,25) | %27,41 |

Uzun vade sunan banka her zaman kazanıyordu. Düzeltme: tabloya
**"Yıllık net %"** sütunu eklendi (getiriyi 365 güne normalize eder, basit
yıllıklandırma — bileşiklemek uzun vadeyi haksız kayırırdı) ve hem satır
vurgusu hem kurum sıralaması bu sütuna bağlandı. Yeni sıralama:
VakıfBank %33,83 (30 gün) · Yapı Kredi %33,83 (92 gün) · Enpara %31,56
(32 gün) · TEB %30,11 (32 gün) — artık aynı ölçekte.

Regresyon testi `test_absolute_return_would_rank_the_worse_offer_first`
hatanın kendisini kilitliyor.

### 3. Kredi sekmesi: 2 → 3 kurum (**Emlak Katılım**)

"Neden tüm bankalar yok" sorusunun cevabı: kredi oranını erişilebilir bir
yerde yayınlayan az banka var. Mevduat oranı tabela olarak yayınlanıyor,
kredi oranı çoğu bankada başvuru akışının arkasında.

Yeni bulundu: Emlak Katılım `/bireysel/finansmanlar/ihtiyac-finansmani`
sayfasında sunucu-render bir tablo yayınlıyor — **%1,69 aylık kâr oranı**.
Katılım bankası olduğu için panel bunu "Kâr oranı" diye etiketliyor;
ayrım `institutions.kind` alanından okunuyor (elle tutulan liste yok).

> **Sınır, açıkça yazıldı:** tablonun başlığı "**Örnek** İhtiyaç Finansmanı
> Tablosu" ve tek satırı var (30.000 ₺ / 12 ay). Matris değil. Bu yüzden
> `term_min = term_max = 12` yazılıyor ve panel başka bir vade seçilince
> "bu oran o vade için ilan edilmedi" uyarısını veriyor.
>
> Konut ve taşıt finansmanı sayfaları **kasıtlı** okunmuyor: oradaki tablolar
> oran değil, teminat oranı (taşıt değerine oranı) ve azami vade yayınlıyor.

Ayrıca parser'da bir tuzak: sayfanın `data-title` öznitelikleri bozuk (oran
hücresi `data-title="Yabancı Para"`). Sütun eşlemesi `thead` başlıklarından
yapılıyor; regresyon testi bunu kilitliyor.

### 4. Akbank — eklenmedi, karar kayıtlı

`robots.txt: Disallow: /_layouts*`. Hem kur (`GetDovizKurlari`) hem mevduat
(`GetMevduatFaiz`) uç noktası tam orada. İzinli hesaplama sayfasını tarayıcıda
render ettim: oran DOM'da görünüyor (%41,00) **ama sayfanın kendisi o yasaklı
adresi çağırarak alıyor** — headless render kuralı dolaşmıyor. Ziraat ile
birebir aynı yapı. Kullanıcı kapalı bırakma kararı verdi (2026-08-23).

### 5. Agent / LLM durumu — hâlâ SIFIR çağrı

Şu ana kadar hiçbir LLM çağrısı yapılmadı; her şey deterministik HTTP +
ayrıştırma. Kullanıcı "API'de sorun çıkıyorsa agent yapamaz mı" diye sordu —
cevap büyük ölçüde hayır, çünkü engellerimiz ayrıştırma engeli değil:

- **robots.txt** bir izin meselesi; modelin okuması da yasak.
- **WAF** ağ seviyesinde blok; istek ulaşmıyor ki model okusun.
- **CSRF token** protokol meselesi.

Agent'ın gerçek değeri tasarımın §06'da tarif ettiği yerde: *zaten
çekebildiğimiz* bir sayfada parser kırıldığında. Enpara / Emlak Katılım /
Ak Portföy parser'ları sayfa yapısına bağlı ve kırılgan — orada değerli.
Yani **agent yeni banka açmaz, mevcut bankaları ayakta tutar.**

`llm/provider.py` zaten OpenAI-uyumlu; Qwen'e geçmek için sadece `.env`'de
`LLM_BASE_URL` + `LLM_MODEL` değişecek, kod değişmeyecek. ("Qwen 3.8 Flash"
diye bir model yok — Qwen3-8B (açık ağırlık, yerel) ya da API'deki
`qwen-flash` kastediliyor olmalı; kullanıcı netleştirecek.)

---

## Bu oturumda yapılanlar (4. oturum)

Kullanıcı isteği: kredi ve mevduat sekmelerini kurum bazlı tablolara ayır,
kredi tablosuna banka adı ekle, mevduat tablolarının altına verinin ne zaman
çekildiğini yaz, ve **bu verilerin hiçbiri hardcoded olmasın — döviz gibi
çekilsin, günlük yeterli**.

### 1. Mevduat kaynakları: 1 banka → 4 banka
Hepsi canlı doğrulandı, hepsi her gün yeniden çekiliyor.

| Banka | Uç nokta | Şekil |
|---|---|---|
| VakıfBank | (mevcut) token akışı | 6-750 gün, onlarca vade |
| **TEB** | `POST /Services/VadeliHesapFaizOranList` | para birimi başına 1 istek, tam matris |
| **Enpara** | `/hesaplar/mevduat-faiz-oranlari` | sunucu-render HTML, TL/USD/EUR tek sayfada |
| **Yapı Kredi** | `POST /_ajaxproxy/.../GetCalculationTool` | para birimi başına 1 istek, tam matris |

> **Yakalanan tuzak — TEB'in yanlış uç noktası.** İlk bulduğum
> `/services/GetMevduatFaizOranlari` tek GET ile çalışıyor ve tam matris
> döndürüyor; kolay seçim gibi görünüyordu. Ama TL için **her vadeye %3**
> veriyor. Sitenin kendi hesaplama aracı aynı gün aynı vade için **%37,5**
> gösteriyordu — yani o uç nokta şube tabelası, dijital CepteTEB müşterisinin
> oranı değil. Panelde TEB, piyasanın onda biri faiz veren bir banka gibi
> görünecekti. Doğrusu `VadeliHesapFaizOranList` + `ceptetebEH=E`.
> Regresyon testi: `test_teb_deposit_uses_cepteteb_rates_not_branch_board_rates`.

### 2. Kredi kaynakları: 1 banka → 2 banka
**Yapı Kredi** eklendi (`retail-credits.aspx`): ihtiyaç için
GetSubProduct → GetCampaign → GetPersonalCreditPaymentPlan, konut için
GetCategoryCodeListByCreditClass → CalculateByCreditAmount. Yanıt KKDF/BSMV
oranlarını da veriyor.

> **Tuzak:** `GetPersonalCreditPaymentPlan`'a `installmentAmount=0`
> gönderilirse yanıt HTTP 200 ama **boş** döner; `null` gönderilmesi şart.
> Taşıt (kategori A1) her tutar/vade denemesinde boş dönüyor — banka bu araç
> üzerinden taşıt fiyatı yayınlamıyor, uydurulmadı.

### 3. Paneller kurum bazlı tablolara ayrıldı
- **Döviz**: para birimi başına tablo → **kurum başına tablo**.
- **Mevduat**: tek karışık tablo → kurum başına tablo. Bankalar bambaşka vade
  setleri yayınlıyor (VakıfBank 6-750 gün, TEB 1-365, Enpara 32/46/92/181,
  Yapı Kredi vade aralıkları); tek tabloda birleştirmek okunamaz bir sonuç
  veriyordu. Kurumlar en iyi net getirilerine göre sıralanıyor.
- **Kredi**: banka adı artık hem kıyas tablosunda (`Banka` sütunu) hem ödeme
  planı başlığında.
- Her tablonun altında **veri çekilme zamanı** ve mevduatta ayrıca
  **kullanılan tutar kademesi**.

### 4. Bulunan ve düzeltilen hatalar

| # | Hata | Etki | Düzeltme |
|---|---|---|---|
| 1 | **TEB yanlış uç nokta** (yukarıda) | TL mevduat oranı %3 gösterilecekti (gerçek: %36,5) | Doğru uç nokta + regresyon testi |
| 2 | **Kredi paneli banka vade/tutar sınırını yok sayıyordu** | 61 ay vadeyle, bankanın en fazla 24 ay verdiği bir krediyi hesaplayıp 4M TL'lik gerçek dışı bir geri ödeme gösteriyordu | `term_min`/`term_max`/`amount_max` sorgudan geliyor; sınır aşılınca hem tabloda ⚠️ hem ödeme planında açık uyarı |
| 3 | **Zaman damgaları UTC gösteriliyordu** | "14:09'da çekildi" yerine "11:09" yazıyor, veri üç saat bayat sanılıyordu | `app/panels/common.py` — tüm paneller Europe/Istanbul'a çeviriyor |
| 4 | **`fetched_at` yeniden görülen kotasyonda güncellenmiyordu** | Kaynağı beş dakika önce kontrol etmiş olmamıza rağmen veri saatler öncesinden kalmış görünüyordu | FX persist artık aynı `quoted_at` görülünce `fetched_at`/`buy`/`sell` tazeliyor |
| 5 | **Aynı güne ait eski mevduat satırları siliniyordu** | Banka vade setini değiştirince (ya da biz uç nokta değiştirince) artık geçerli olmayan oranlar yenileriyle yan yana kalıyordu | `persist` her koşuda o kurumun o güne ait fazla satırlarını temizliyor |
| 6 | **Örtüşen tutar kademeleri** | VakıfBank hem "500.001-1.000.000" hem "1.000.000-2.999.999" yayınlıyor; tam 1M TL ikisine de düşüp aynı vade için iki satır üretebiliyordu | `_resolve_overlapping_tiers` — müşteri lehine (yüksek) oran bırakılıyor |
| 7 | **TEB aynı kademe için çakışan oranlar döndürüyor** (60 ay: 15,0 / 15,25 / 15,50) | Rastgele biri seçilirse getiri sessizce yanlış | Parser en düşüğünü seçiyor — eksik göstermek fazla göstermekten güvenli |
| 8 | **Tek banka düşünce tüm koşu düşüyordu** | Dört bankadan biri zaman aşımına uğrasa diğer üçünün verisi de yazılmıyordu | fetch/parse banka bazında hata yakalıyor; hepsi düşerse `ParseError` |
| 9 | Fon panelinde "TEFAS bloke" mesajı | 3. oturumda Ak Portföy'e geçilmişti, mesaj eskimişti | Güncellendi; ayrıca son fiyat tarihi ve kaynak yazılıyor |
| 10 | Fon mevduat kıyası rastgele banka seçiyordu | 4 banka gelince aynı vadede farklı oranlar var; kıyas çizgisi keyfî oluyordu | Aynı vadedekiler arasından en yükseği + hangi banka olduğu yazılıyor |
| 11 | **TEB ve VakıfBank kotasyon saatleri UTC sanılıyordu** | Bankalar saati İstanbul yerelinde yayınlıyor; doğrudan UTC etiketlemek kotasyonu 3 saat İLERİ kaydırıyordu (taze kur "gelecekten" gelmiş gibi, tazelik hesabı negatif yaş üretiyor) | `_istanbul_to_utc()` + regresyon testleri. Bozuk yazılmış eski TEB/VakıfBank satırları silinip yeniden çekildi |

### 5. Yapı Kredi döviz kuru da eklendi
`LoadMainCurrencies` — sayfanın kendi çağrısı, parametresiz, çerezsiz.
Kendi `lastUpdate` saatini verdiği için tek "tahmini değil" HTML-dışı
kaynaklardan biri. Böylece döviz sekmesi 6 kuruma çıktı.

### 6. Test: 63 → **97 test**
Yeni fixture'lar: `teb_deposit.json`, `enpara_deposit.html`,
`yapikredi_deposit.json`, `yapikredi_loan.json`. Hepsi gerçek yanıtlardan,
hepsi ağsız.

---

## Bu oturumda yapılanlar (3. oturum)

### 1. Fon fiyat kaynağı çözüldü — **Ak Portföy**
Önceki oturumun tek büyük boşluğuydu. Çözüm: `akportfoy.com.tr/tr/fon/{KOD}`
sayfaları sunucu tarafında render ediliyor ve **tüm geçmiş fiyat serisini**
sayfanın içine `var fundVals = {"AK3":[{"Close":..,"Date":..},...]}` olarak
gömüyor. Ayrı API çağrısı, token, CSRF **yok**; sitede robots.txt de yok
(HTTP 404 — dolayısıyla bir Disallow kuralı da yok).

- Yeni: [collectors/akportfoy.py](collectors/akportfoy.py) — `worker.py funds`
- **8.655 fiyat noktası**, 4 fon, 2018-01-02 → 2026-08-20 (panelin ihtiyacı 400 gün)
- Fon adı ve "hisse senedi yoğun" sınıflandırması sayfa başlığından okunup
  katalogla otomatik hizalanıyor — stopaj kararı elle girilen bayrağa değil,
  fonun resmî adındaki `(Hisse Senedi Yoğun Fon)` ibaresine dayanıyor.

> **Tuzak — zaman dilimi:** `Date` alanı epoch **milisaniye** ve İstanbul
> saatiyle gece yarısına denk geliyor. UTC olarak okunsaydı bütün tarihler bir
> gün geriye kayacak ve simülasyon yanlış günün fiyatını kullanacaktı.
> Regresyon testi: `test_akportfoy_epoch_is_interpreted_in_istanbul_time`.

### 2. TEFAS emekliye ayrıldı
Çalıştırılamayacağı kesinleşti (404 + robots.txt `/api/` + WAF "Request
Rejected"). `worker.py`'den **kaydı kaldırıldı** — çalışamayan bir toplayıcının
tazelik şeridinde kalıcı kırmızı göstermesi yanıltıcıydı. Eski `scrape_runs`
kayıtları temizlendi. [collectors/tefas.py](collectors/tefas.py) dosyası
"neden kullanmıyoruz" sorusunun cevabı olarak duruyor ama kayıtlı değil.

### 3. Test altyapısı: 21 → **63 test**
- [tests/conftest.py](tests/conftest.py) — testler geçici bir SQLite'a yazar;
  gerçek `data/finans_agent.db` dosyasına **dokunulmadığı doğrulandı**.
- [tests/test_integration_store.py](tests/test_integration_store.py) (11) —
  şema + sorgular: kademe seçimi, sınır değerleri, para birimi izolasyonu,
  tazelik view'i.
- [tests/test_integration_collectors.py](tests/test_integration_collectors.py) (23) —
  **ağsız**, `tests/fixtures/` altındaki gerçek sayfa parçalarıyla. Bir banka
  sayfasının yapısı değişirse saniyeler içinde yakalar. `sanity_check`'in kötü
  veriyi (bant dışı, gelecek tarih, tekrar eden tarih, %20 sıçrama) gerçekten
  reddettiğini de doğrular.
- [tests/test_integration_panels.py](tests/test_integration_panels.py) (8) —
  uçtan uca kullanıcı akışı: anapara gir → doğru kademe → doğru stopaj → doğru net.

### 4. Bulunan ve düzeltilen hatalar

| # | Hata | Etki | Düzeltme |
|---|---|---|---|
| 1 | **`deposit_rates_for_amount` para birimi sızıntısı** — currency filtresi alt sorguda vardı ama dış join'de yoktu | **Ciddi:** USD sorgusu TRY oranını döndürüyordu; panel 1M USD için %45 TL faizi gösterip tamamen yanlış getiri hesaplıyordu | currency hem join anahtarına hem dış `WHERE`'e eklendi; regresyon testi yazıldı |
| 2 | Ölü fon kayıtları (TI2/GAF/TTE) katalogda kalıyordu | Panelde hiç fiyatı olmayan fonlar seçilebiliyordu | `seed_reference_data()` artık config'den çıkarılmış **ve hiç fiyatı olmayan** fonları temizliyor (veri kaybı riski yok) |
| 3 | `quoted_at_is_estimated` saklanıyor ama **panelde hiç gösterilmiyordu** | Tasarım §02'nin açık şartı ihlal ediliyordu: kullanıcı tahmini bir zamanı kesin sanıyordu | Döviz panelinde `≈` işareti + açıklama notu |
| 4 | Döviz tablosunda 6 haneli ondalık gösterim (`56.116900`, `53.900000`) | Okunaksız | Styler `.format()` ile 4 hane / 1 hane; ayrıca **Makas** sütunu eklendi |
| 5 | Fon adında kod tekrarı (`AK3 - Ak Portföy...`) | Kozmetik | Başlıktan kod ön eki temizleniyor |
| 6 | "Anapara (TL)" etiketi vs. mevduat panelinde USD/EUR seçimi | Kullanıcı 1M'i TL sanabilirdi | TRY dışı seçimde açıklayıcı not |

### 5. Tarayıcıda yapılan kullanıcı testleri
Dört sekme de elle gezildi; ayrıca kenar durumlar:
- **Anapara = 0** → tüm paneller sıfır gösteriyor, çökme/bölme hatası yok ✓
- **Fon değiştirme** (AK3 hisse yoğun → AFA normal) → stopaj doğru şekilde
  devreye giriyor, "stopajdan muaf" notu kayboluyor ✓
- **Para birimi değiştirme** (TRY → USD → EUR) → 1 numaralı hata düzeldikten
  sonra doğru oranlar geliyor ✓
- Kredi türü değiştirme, ödeme planı, tazelik şeridi ✓

---

## Kalan eksikler (öncelik sırasıyla)

### 1. Garanti BBVA — gerçek tarayıcı gerektiriyor
Widget'ın uç noktası doğrulandı ve canlı sayfada çalışıyor, ama doğrudan HTTP
isteği tam tarayıcı başlıkları + HTTP/2 ile bile hep **HTTP 500** dönüyor
(muhtemelen cihaz/oturum çerezi). Playwright ile widget'ı render edip DOM'dan
okumak teorik olarak mümkün (tasarım §08 "tarayıcı gerekirse") ama proje henüz
headless tarayıcı bağımlılığı içermiyor. Daha fazla başlık denemesi bypass
sayılacağı için durduruldu.

### 2. Meşru engellerle kapalı kaynaklar — açılmayacak
| Kaynak | Engel |
|---|---|
| İş Bankası | Normal gezintide bile WAF "İstek Engellenmiştir" blok sayfası |
| Ziraat | Ana sayfa widget'ı bile robots.txt'in yasakladığı `/_layouts/...` çağrısını tetikliyor |
| Akbank | robots.txt `Disallow: /_layouts*` |
| TEFAS | 404 + robots.txt `/api/` + WAF |
| İş Portföy | Fiyat API'si CSRF/antiforgery token gerektiriyor |

Bunlar teknik eksiklik değil, bilinçli sınır.

### 3. Doğrulanmamış / kısmen açık bankalar
- Halkbank: TLS handshake tamamlanmıyor.
- Kuveyt Türk: denenmedi (katılım bankası; Emlak Katılım'la aynı "kâr paylaşım
  oranı ≠ yıllık getiri" sorunu muhtemelen burada da var).
- Yapı Kredi: mevduat + kredi açık, **döviz** kuru henüz eklenmedi.
- Enpara ve TEB'in KREDİ oranları kapalı: Enpara CSRF token istiyor
  (İş Portföy'de aynı gerekçeyle durulmuştu, tutarlı olmak için burada da
  duruldu), TEB'in kredi hesap makinesi uç noktası HTTP 500'e yönlendiriyor.
- Akbank: robots.txt `/_layouts*` yasağı; kullanıcı kararıyla kapalı
  (bkz. 5. oturum, madde 4).

### 4. EVDS entegrasyonu yok
Sektör ortalaması çapası (§02) hâlâ kullanılmıyor; `.env.example`'da anahtar
alanı var ama kodu yok.

### 5. Onarım agent'ı yok
Tasarım §06'nın ikinci ayağı. `flag_for_repair()` şu an sadece log basıyor.

### 6. Küçükler
- Taşıt kredisi verisi hâlâ yok: VakıfBank kataloğunda taşıt ürünü dönmüyor,
  Yapı Kredi'nin taşıt kategorisi (A1) her tutar/vade denemesinde boş dönüyor,
  Emlak Katılım taşıt sayfasında oran değil teminat oranı yayınlıyor.
- Katılım bankası kar payı (`is_profit_share=True`) hiçbir toplayıcıda
  üretilmiyor: Emlak Katılım sayfasındaki sayılar yıllık oran değil, kârın
  **dağıtım yüzdesi**; yanlış gösterip yanıltmaktansa boş bırakıldı.
- Snapshot temizlik politikası yok (`data/snapshots/` büyüyor).
- Proje bir git deposu değil.

---

## Nasıl devam edilir

Sistem dört panelde de gerçek veriyle, kurum kurum çalışıyor ve 92 testle
korunuyor. Sıradaki en değerli işler: **(a)** LLM anahtarı girilince
fallback'i gerçek bir kırık sayfayla test etmek, **(b)** EVDS çapasını
`sanity_check`'e bağlamak (dört bankanın oranı toplandığı için artık bir
sektör ortalaması çapası gerçekten anlamlı olur), **(c)** Yapı Kredi döviz
kurunu eklemek (aynı `_ajaxproxy` deseni, `LoadMainCurrencies` uç noktası
sayfa yüklenirken zaten çağrılıyor). Garanti için headless tarayıcı yatırımı
yapmaya değip değmeyeceği ayrı bir karar.

---

# ÇETELE — güncel durum (2026-08-25, 6. oturum sonu)

**272 test geçiyor.** Docker dağıtımı ilk kez uçtan uca doğrulandı (aşağıda).

| Alan | Durum | Not |
|---|---|---|
| Döviz | ✅ | 8 kurum + TCMB; tablo altında kaynak künyesi |
| Mevduat & katılma | ✅ | 5 faiz bankası + **2 katılım bankası aynı sütunlarda** |
| Kredi | ✅ | 5 kurum; kapsam görünür, ödeme planı bankanın rakamıyla doğrulanıyor |
| Fon | ✅ | **11 fon / 2 sağlayıcı**; panelden fon eklenebiliyor |
| Kaynak şeffaflığı | ✅ | Yeni "Kaynaklar" sekmesi; envanter vs gerçek karşılaştırması |
| Loglama | ✅ | 5 katman: koşu / kaynak / **istek** / **oran değişimi** / LLM+maliyet |
| Zamanlayıcı | ✅ | 9 toplayıcı, telafi + günlük budama; konteynerde doğrulandı |
| Docker | ✅ | **Panel + scheduler + worker + şema migrasyonu canlı doğrulandı** |
| LLM fallback | 🟡 | Kapalı; anahtar girilince açılır. Tetikleyici ve maliyet kaydı hazır |
| Onarım agent'ı | ⬜ | `flag_for_repair()` hâlâ yalnızca log basıyor |
| EVDS çapası | ⬜ | Entegre edilmedi |

## Docker doğrulaması (2026-08-25) — daha önce hiç yapılmamıştı

| Kontrol | Sonuç |
|---|---|
| İmaj derleniyor | ✅ `docker build` (BuildKit) |
| Panel rolü | ✅ `healthy`, `/_stcore/health` 200 |
| Zamanlayıcı rolü | ✅ 9 toplayıcının tamamı boş volume'de başarılı (12.051 fon fiyatı, 801 mevduat satırı) |
| Worker rolü | ✅ `docker exec ... worker.py <ad>` canlı veri çekti |
| **Şema migrasyonu** | ✅ Eski şemalı volume yeni imajla açıldı: 5 tablo + 2 sütun eklendi, **eski satırlar korundu** |
| `docker compose up` | ✅ **`.env` olmadan** çalışıyor (aşağıdaki düzeltme) |
| Günlük budama | ✅ Zamanlayıcı açılışta ve günde bir çalıştırıyor |

**Düzeltilen dağıtım hatası:** `env_file: - .env` yazılıydı ve `.env` dosyası
yoksa `docker compose up` hiç başlamadan `open .env: no such file` ile
düşüyordu — kurulumun ilk adımında takılan bir engel. Compose'un
`required: false` biçimi ancak v2.24+ ile geldiği için (kullanıcıda v2.20)
değişkenler `${VAR:-varsayılan}` ile geçiriliyor. Artık `.env` varsa da
çalışıyor, yoksa da.

---

# ÇETELE — 5. oturum sonu (2026-08-24, tarihsel)

Bu bölüm "neresi bitti, neresi eksik" sorusunun tek cevabı. Her satır ya
tamamlandı (✅) ya bilinçli bir sınır (⛔) ya da açık iş (⬜).

## Veri kaynakları

### Döviz — 8 kurum ✅
- [x] TCMB (resmi referans XML)
- [x] Akbank *(robots kısıtı kullanıcı kararıyla kaldırıldı)*
- [x] CepteTEB
- [x] Emlak Katılım
- [x] Enpara (QNB)
- [x] VakıfBank
- [x] Yapı Kredi
- [x] Ziraat Bankası *(robots kısıtı kaldırıldı)*
- [ ] ⛔ **İş Bankası** — WAF. Normal tarayıcı gezintisinde bile blok sayfası
      dönüyor. Bu robots.txt gibi bir nezaket kuralı değil, aktif bot tespiti;
      aşmak parmak izi taklidi gerektirir. Kullanıcının robots kararı bunu
      kapsamıyor, açılmadı.
- [ ] ⬜ **Garanti BBVA** — uç nokta doğru, tam tarayıcı başlıklarıyla bile
      HTTP 500 (nginx). Cihaz/oturum çerezi gerektiriyor gibi. Headless
      tarayıcı (Playwright) ile denenebilir.
- [ ] ⬜ **Halkbank** — TLS handshake tamamlanmıyor (coğrafi/IP kısıtı olabilir).

### Mevduat faiz oranları — 5 banka ✅
- [x] VakıfBank (TUTAR × VADE matrisi, TRY/USD/EUR)
- [x] CepteTEB (TRY/USD/EUR)
- [x] Enpara (TRY/USD/EUR)
- [x] Yapı Kredi (TRY/USD/EUR)
- [x] Akbank (TL — banka yalnızca TL yayınlıyor)
- [ ] ⬜ **Ziraat** — kur uç noktası açıldı ama mevduat oranı için ayrı bir uç
      nokta bulunamadı; hesaplama araçları sayfaları 404.

### Katılım bankası kâr paylaşım oranları — 2 banka ✅
- [x] Emlak Katılım (TRY/USD/EUR/XAU/XAG + stopaj)
- [x] Kuveyt Türk (TRY/USD/EUR + stopaj, "87-13" biçimi)
- [x] Ayrı tabloda tutuluyor, getiri hesabına GİRMİYOR, panelde ne olduğu
      açıkça yazılıyor
- [ ] ⛔ **Gerçek yıllık getiriye çevirme** — mümkün değil: bankanın
      gerçekleşen kâr rakamı gerekiyor ve web'de yayınlanmıyor. Uydurmak
      yerine paylaşım oranı olarak gösteriliyor.

### Kredi oranları ✅
- [x] İhtiyaç — 5 kurum: VakıfBank, Akbank, Yapı Kredi, Enpara, Emlak Katılım
- [x] Konut — 3 banka: VakıfBank, Akbank, Yapı Kredi
- [x] **Taşıt — 1 banka: Akbank** (uzun süredir boş olan tür artık dolu)
- [ ] ⬜ **CepteTEB kredisi** — hesap makinesi uç noktası HTTP 500'e
      yönlendiriyor; sayfada gömülü oran da yok.

### Fon fiyatları ✅
- [x] Ak Portföy — 4 fon, 8.655 fiyat noktası (2018 → bugün)

## Uygulama

- [x] Döviz paneli — kurum başına tablo, makas, tazelik, `≈` tahmini-zaman işareti
- [x] Mevduat paneli — kurum başına tablo, **Yıllık net %** sütunu ve ona göre
      sıralama, kullanılan tutar kademesi tablonun üstünde, katılım bloğu en üstte
- [x] Kredi paneli — banka adı her yerde, faiz/kâr oranı ayrımı, banka vade ve
      tutar sınırı aşılınca uyarı, tam ödeme planı + vergi kırılımı
- [x] Fon paneli — fiyat serisi, senaryolar, mevduat kıyas sütunu (kaynağı yazılı)
- [x] Tazelik şeridi — 7 toplayıcının durumu
- [x] Tüm zaman damgaları Europe/Istanbul gösteriliyor
- [x] `valid_date` Türkiye takvim günü (`store/clock.py`) — Docker UTC'de bile doğru

## LLM / agent

- [x] Google Gemini (OpenAI-uyumlu uç nokta) varsayılan sağlayıcı
- [x] `gemini-2.5-flash-lite` varsayılan model
- [x] Token korumaları: fallback varsayılan KAPALI, girdi 8.000 karaktere
      daraltılır (oran bölgesine odaklı), çıktı 2.000 token, koşu başına 1
      çağrı, tekrar deneme yok, 45 sn zaman aşımı
- [x] Sağlayıcı değiştirmek kod değişikliği gerektirmiyor (`.env`)
- [ ] ⬜ **Gerçek anahtarla test** — kullanıcı `.env`'e `LLM_API_KEY` yazıp
      `LLM_FALLBACK_ENABLED=1` yapınca kırık bir sayfayla denenecek.

## Dağıtım

- [x] **Otomatik tazeleme** — `scheduler.py`: plan + açılışta toplama +
      **tazelik telafisi** (30 dk'da bir, sınırı aşan toplayıcı yeniden denenir)
- [x] `deploy/finans-scheduler.service` — Docker'sız sunucu için systemd birimi
- [x] Panel, tüm veri 36 saatten eskiyse "zamanlayıcı çalışmıyor gibi" uyarısı basıyor
- [x] Dockerfile — root olmayan kullanıcı, TZ=Europe/Istanbul, healthcheck,
      katmanlı pip önbelleği
- [x] `docker-compose.yml` — panel + scheduler + elle worker, adlandırılmış volume
- [x] `scheduler.py` — konteyner içi zamanlayıcı, cron gerekmiyor; açılışta
      bir kez hepsini toplar
- [x] `.dockerignore`
- [x] venv + systemd + cron alternatifi (`deploy/`)

## Log ve gözlem

- [x] `scrape_runs` — koşu düzeyi (zaten vardı)
- [x] **`source_runs`** — BANKA düzeyi: hangi kaynak, hangi aşama (fetch/parse),
      ok/failed/empty, satır sayısı, süre, hata metni
- [x] **`llm_calls`** — token muhasebesi: girdi/çıktı/toplam token, kurtarılan
      satır, süre; çağrının YAPILMADIĞI durumlar da (`disabled`,
      `budget_exceeded`) kaydediliyor
- [x] **Kayıtlar sekmesi** — kaynak sağlığı (başarı oranı + son hata), olay
      akışı, 30 günlük token toplamı. Sunucuda SSH'sız okunabilir
- [x] Dönen dosya log'u `data/logs/*.log` (5 × 2 MB), `LOG_DIR` ile kapatılabilir
- [x] Kayıt yazma hatası toplayıcıyı ASLA düşürmüyor (testle kilitli)

## Test

- [x] **154 test**, hepsi ağsız, saniyeler içinde
- [x] Her yeni kaynak için fixture + şema-değişikliği yakalayan test
- [x] Token korumalarının testleri (ağa çıkmadan)

## Zamanlayıcı hatası — yakalandı ve düzeltildi (2026-08-24)

Tazelik telafisini yazarken kendi teşhis çıktım bir hata gösterdi: `deposits`
ve `funds` az önce başarıyla koştukları hâlde "hiç çalışmamış" görünüyordu.

Sebep: **worker.py kayıt anahtarı ile `scrape_runs.collector` değeri aynı
değil.** `deposits` → `deposit_rates`, `funds` → `akportfoy`. Tazelik kontrolü
anahtarla sorgulasaydı bu iki toplayıcıyı sonsuza dek bayat sayar ve
**yarım saatte bir bankaları gereksiz yere yeniden çekerdi** — hem de
erişimimin olmayacağı bir sunucuda.

Düzeltme: eşleme elle yazılmıyor, kayıt defterinden okunuyor (`_db_name`), ki
yeni toplayıcı eklendiğinde kayamasın. Regresyon testi:
`test_worker_key_maps_to_the_real_collector_name`.

Ayrıca `tests/test_integration_scheduler.py` (21 test) planı, telafi
mantığını ve "her toplayıcı planda mı / plandaki her ad worker'da mı"
çift yönlü tutarlılığını kilitliyor.

---

## Neden banka bazlı log şart oldu (2026-08-24)

Bir bankanın uç noktasını bilerek bozup çalıştırdım:

    fx_banks/akbank: fetch başarısız: SİMÜLE EDİLDİ
    koşu: RunResult(ok=True, rows=12, status='ok')

Koşu **`ok`** dedi ve 12 satır yazdı — çünkü diğer altı banka çalıştı. Panel
Akbank'ı sessizce göstermemeye başlardı ve `scrape_runs`'a bakan hiç kimse
bunu fark etmezdi. `source_runs` tablosu tam bu boşluk için var; `empty`
durumu da ayrı tutuluyor (istek başarılı, ayrıştırma patlamadı, ama sıfır
kayıt — sessiz bozulmanın en sinsi hâli).

---

## Doğrulama kayıtları (2026-08-24) — iddia vs gerçek

Kullanıcının bildirdiği dört şüphe tek tek sınandı; ikisi doğru çıktı, ikisi
yanlış anlaşılmaydı. Ayrıntılı çözümler [PLAN.md](PLAN.md)'de.

| İddia | Sonuç | Bulgu |
|---|---|---|
| "Kâr paylaşım oranları yanlış" | **Veri doğru, sunum yanlış** | Emlak Katılım canlı sayfası birebir uyuşuyor (500K-1,25M kademesi: 85/92/93/93/93/93). Sorun `%93`'ün yanındaki `%38`lerin arasında **faiz gibi okunması** — bambaşka bir büyüklük olduğu hâlde |
| "Katılım tabloları neden farklı?" | **Doğru, eksik** | Getiri sütunu yok çünkü yıllık oran yok. Çözüm bankanın kendi kâr payı hesaplayıcısından yıllık oranı çekmek (Kuveyt Türk sayfasında "Net Oran (Yıllık)" ibaresi var) |
| "Ödeme planında sadece 3 banka" | **Eksik değil, tür bazlı** | Konut 3 · İhtiyaç 5 · Taşıt 1. Panel bunun *neden* kısa olduğunu söylemiyor — UX boşluğu |
| "Ödeme planı verisi nereden?" | **Şeffaflık boşluğu** | Bankadan gelen tek şey aylık oran; taksit/anapara/faiz/vergi kırılımını `core/loan.py` hesaplıyor. Panel bunu hiç söylemiyor |

**TEFAS** ayrıca yeniden denendi: artık F5 Shape bot-challenge arkasında
(`bobcmn`, `/TSPD/`, `DOSL7.challenge`). Bu robots.txt değil, aktif bot
tespiti — İş Bankası WAF'ıyla aynı kategori, açılmayacak. Fon genişletmesi
portföy şirketlerinin kendi siteleri üzerinden yapılacak (Garanti BBVA Portföy
ve İş Portföy erişilebilir olduğu doğrulandı).

---

## Bilinen açık işler (öncelik sırasıyla, 2026-08-25)

1. ⬜ **Proje bir git deposu değil** — `git init` yapılmadı. Artık en riskli
   eksik: kod tabanı büyüdü, geri alma imkânı yok.
2. ⬜ **LLM fallback hiç gerçek anahtarla denenmedi.** Tüm boru hattı
   (tetikleyici kaydı, token/maliyet muhasebesi, bütçe koruması) hazır ve
   testli, ama tek bir gerçek çağrı yapılmadı. Kullanıcının paylaştığı
   anahtar sohbete düz metin girdiği için **kullanılmadı ve hiçbir dosyaya
   yazılmadı**; iptal edilip yenisi `.env`'e elle konmalı.
3. ⬜ **İş Portföy fon fiyat serisi** — araştırma yarım kaldı. Fon listesi
   (kod + ad + sayfa adresi) okunabiliyor; fiyat serisi uç noktası
   bulunamadı, `/getiri-ve-fiyatlar` sayfası incelenmedi. Engel yok.
   `sources.yaml`'da `unverified`, nerede kalındığı yazılı.
4. ⬜ **EVDS çapası** — TCMB'nin sektör ortalaması `sanity_check`'e
   bağlanmadı. Artık 10 kurum toplandığı için gerçekten anlamlı olur.
5. ⬜ **Onarım agent'ı** (tasarım §06'nın ikinci ayağı) — `flag_for_repair()`
   şu an sadece log basıyor.
6. ⬜ **Garanti BBVA (banka)** — kur/mevduat uç noktası hâlâ HTTP 500;
   headless tarayıcı yatırımı gerektiriyor. *(Garanti BBVA **Portföy** ayrı
   bir konu ve çözüldü — fon fiyatları çekiliyor.)*
7. ⬜ Ziraat mevduat/kredi, TEB kredisi, Halkbank (yukarıda ayrıntılı).
8. ⬜ **TEFAS** — F5 Shape bot-challenge. Bilinçli olarak açılmıyor;
   kullanıcının robots.txt kararı bot tespitini kapsamıyor.

### ✅ Bu oturumda kapananlar
- ~~Snapshot temizlik politikası~~ → `store/retention.py`, zamanlayıcı
  günde bir buduyor (snapshot 14 gün, `http_requests` 30 gün).
- ~~Docker çalışma zamanı doğrulanmadı~~ → panel + scheduler + worker +
  şema migrasyonu canlı doğrulandı.
- ~~Kaynak envanteri panelde yok~~ → "Kaynaklar" sekmesi.
- ~~Katılım bankaları kıyaslanamıyor~~ → yıllık oranlar bulundu, aynı
  sıralamaya girdiler.
