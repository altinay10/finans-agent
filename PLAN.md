# Plan — açık görevler, kök nedenler ve çözümleri

Bu dosya "ne yapılacak" listesi değil, **neden böyle olduğu ve en iyi çözümün
neden o olduğu** kaydıdır.

Hazırlandı: **2026-08-24**. Her iddia uygulamadan/canlı siteden doğrulandı;
doğrulama çıktıları maddelerin içinde.

## ✅ DURUM — 2026-08-25: yedi maddenin tamamı uygulandı

| # | Madde | Durum |
|---|---|---|
| 1 | Kaynak şeffaflığı | ✅ "Kaynaklar" sekmesi + tablo altı künyeler |
| 2 | Katılım bankası yıllık oranı | ✅ Her iki bankada da bulundu ve çekiliyor |
| 3 | Katılım tabloları detaylandı | ✅ Faiz bankalarıyla aynı sütunlar ve aynı sıralama |
| 4 | Kredi kapsam görünürlüğü | ✅ Kapsam satırı + gerekçeli eksik kurum listesi |
| 5 | Ödeme planı şeffaflığı | ✅ Uyarı notu + bankanın kendi taksitiyle çapraz doğrulama |
| 6 | Fon sağlayıcı mimarisi | ✅ Adaptör + `config/funds.yaml` + panelden "Fon ekle" |
| 7 | Ayrıntılı API loglama | ✅ `http_requests`, kurtarma izi, şema sapması, oran değişimi, maliyet |

Uygulama ayrıntıları ve canlı doğrulama çıktıları için `ILERLEME.md`.
Aşağıdaki bölümler **kararların gerekçesi** olarak duruyor: bir gün "bu neden
böyle yapılmış?" diye sorulduğunda cevap burada.

---

## 1. ✅ "Hangi veriyi nereden çekiyorsun?" — şeffaflık

### Durum
Bilgi üç yere dağılmış: `config/sources.yaml` (asıl kaynak), `README.md`
(özet tablo), toplayıcı docstring'leri (ayrıntı). **Panelde hiç yok.** Kullanıcı
bir sayıya bakıp "bu nereden geldi?" diye soramıyor — depoyu açması gerekiyor.

### Kök neden
Kaynak envanteri geliştirici belgesi olarak tasarlandı, kullanıcı arayüzünün
parçası olarak değil. Oysa bu bir *finansal* panel: bir oranın kaynağı, oranın
kendisi kadar önemli.

### Çözüm
1. **Panelde "Kaynaklar" sekmesi.** `config/sources.yaml`'dan okunur — ikinci
   bir liste tutmak kayma üretir. Her satır: kurum, hangi veri (kur/mevduat/
   kredi/kâr payı), uç nokta URL'si, tip (JSON/HTML), son doğrulama tarihi,
   `robots_override` bayrağı, kapalıysa gerekçesi.
2. **Her tablonun altına kaynak satırı.** "Bu tablo: Akbank ·
   `GetMevduatFaiz` · 24.08.2026 14:12'de çekildi" — zaten çekilme zamanını
   yazıyoruz, yanına kaynağı da koymak küçük bir ek.
3. **Satır düzeyinde köken (provenance).** `deposit_rates`/`loan_rates`
   tablolarına `run_id` zaten var; `source_runs` ile birleştirip "bu satırı
   hangi istek getirdi" izini kurabiliriz.

### Neden bu çözüm
Alternatif "README'yi genişletmek"ti; reddedildi çünkü sunucuya kurulduktan
sonra kullanıcı README'yi görmüyor. Bilgi, sayının yanında olmalı.

---

## 2. ✅ Katılım bankası kâr paylaşım oranları "yanlış duruyor"

### Doğrulama — veri DOĞRU
Emlak Katılım sayfası canlı çekildi, panelle birebir aynı:

    Kademe 500.000 - 1.249.999 TL
    1 Günlük 85 | 31 Günlük 92 | 3 Aylık 93 | 6 Aylık 93 | Yıllık 93 | 1 Yıldan Uzun 93

Kuveyt Türk'te de "87-13" biçimindeki müşteri/banka payı doğru ayrıştırılıyor.

### Kök neden — sunum, veri değil
`%93` sayısı, yanındaki bankaların `%38`ini gördüğü yerde **faiz oranı gibi
okunuyor**. Oysa bambaşka bir büyüklük: kârın müşteriye düşen payı. Yan yana
konduğunda "katılım bankası 2,4 kat fazla veriyor" gibi görünüyor — tamamen
yanlış bir izlenim. Kullanıcının "yanlış duruyor" demesi doğru bir sezgi.

### Çözüm — üç seçenek değerlendirildi

| Seçenek | Değerlendirme |
|---|---|
| (a) Paylaşım oranını yıllık getiriye çevirmek | Bankanın **gerçekleşen kâr** rakamı gerekiyor. Emlak Katılım web'de yayınlamıyor. Tahmin üretmek = uydurma sayı. **Reddedildi.** |
| (b) Sadece oranı göstermek (bugünkü hâli) | Doğru ama kıyaslanamaz; kullanıcının şikâyeti tam bu. **Yetersiz.** |
| (c) **Bankanın kendi kâr payı hesaplayıcısından yıllık oranı çekmek** | Kuveyt Türk sayfasında "Katılma Hesabı Kar Payı Hesaplama Aracı" var ve çıktısında **"Net Oran (Yıllık)"** alanı geçiyor. Bu, mevduat faiziyle **doğrudan kıyaslanabilir** bir sayı. **Önerilen.** |

### Uygulama planı (c)
1. Kuveyt Türk hesaplama aracının uç noktasını bul (tarayıcıda ağ izleme;
   sayfa kaynağında görünmüyor, JS bundle'ında olmalı).
2. Emlak Katılım için aynısını ara; yoksa o banka için (b) hâlinde kalır ve
   tabloda "yıllık oran yayınlanmıyor" diye açıkça yazılır.
3. Yıllık oran bulunursa `deposit_rates`'e `is_profit_share=True` ile yazılır —
   şema zaten bu alanı taşıyor ve panel "Beklenen kar payı" etiketini basıyor.
4. Kâr paylaşım oranı **silinmez**; ek bilgi olarak kalır (bankanın ne kadarını
   paylaştığı ayrı ve gerçek bir bilgi).

### Kritik sınır
Yıllık oran bulunsa bile bu bir **taahhüt değil, beklenti**. Tabloda faizden
görsel olarak ayrı kalmalı ve "vade sonunda belli olur" notu düşmeli.

### ✅ SONUÇ (2026-08-25) — seçenek (c) uygulandı, İKİ bankada da bulundu

**Kuveyt Türk** — `lastProfitShareRates`. Uç nokta adresi JS paketindeki
`ApiEndpoints` nesnesinde `ck0d84?<HASH>` biçiminde; hash dağıtımla
değiştiği için **koda gömülmedi**, her koşuda zincirleme keşfediliyor
(sayfa -> `magiclick.core.min.js` -> `ApiEndpoints`). Dönen değer
**gerçekleşen** brüt oran: 1 ay %32,99 · 3 ay %34,42 · 6 ay %35,75 ·
12 ay %39,32 (TL; USD/EUR de var).

**Emlak Katılım** — `/Plugins/CalculateProfitShareRate`. Sayfanın kendi
hesaplama aracının uç noktası (`data-service-url` özniteliğinden bulundu).
Tutar + vade + para birimi veriliyor, `GrossProfitShareYearly` /
`NetProfitShareYearly` ve `SegmentName` (tutar kademesi) dönüyor. 1.000.000
TL için: 31 gün %31,38 · 91 gün %33,97 · 180 gün %35,56 · 364 gün %41,41.

**Beklenmedik kazanç:** Emlak Katılım hem brüt hem NET yıllık oranı
yayınlıyor. Bizim stopaj modelimiz (`config/taxes.yaml`) dört vadede de
bankanın net rakamıyla birebir tuttu (%41,41 × 0,85 = %35,20 = bankanın
dediği). Kredi tarafındaki Yapı Kredi taksit kontrolünün mevduat
karşılığı — regresyon testi: `tests/test_participation_rates.py`.

**Sınır aynen geçerli:** ikisi de taahhüt değil. Emlak Katılım'ınki gelecek
beklentisi, Kuveyt Türk'ünki geçmiş gerçekleşme; panel ikisini de
`is_profit_share=True` ile "Beklenen kar payı" diye etiketliyor.

---

## 3. ✅ Katılım tabloları diğer bankalar gibi detaylı olsun

### Durum
Faiz bankaları: Vade · Yıllık oran · Stopaj · Brüt getiri · Net getiri ·
**Yıllık net %** · Vade sonu değer · Tür.
Katılım bankaları: Vade · Kâr paylaşım oranı · Stopaj. Getiri sütunu yok.

### Kök neden
Getiri hesaplayacak bir yıllık oran yok (bkz. madde 2).

### Çözüm
**Madde 2'ye bağımlı.** Yıllık oran gelirse aynı hesaplama boru hattı
(`core/deposit.single_term_return`) aynen kullanılır ve tablo birebir aynı
sütunlara kavuşur; katılım bankaları da **Yıllık net %** sıralamasına girer.

Gelmezse yapılabilecek en iyi şey: tabloya **"eğer havuz şu kadar kâr ederse"**
senaryo sütunu eklemek — kullanıcı bir varsayım girer, panel `paylaşım oranı ×
varsayım − stopaj` hesabını gösterir. Bu uydurma değildir çünkü varsayım
kullanıcınındır ve açıkça öyle etiketlenir.

---

## 4. ✅ Kredi sekmesinde "ödeme planını göster"de sadece 3 banka

### Doğrulama — eksik değil, kredi TÜRÜNE bağlı

    personal  5 kurum: AKBANK, EMLAKKATILIM, ENPARA, VAKIFBANK, YAPIKREDI
    housing   3 kurum: AKBANK, VAKIFBANK, YAPIKREDI
    vehicle   1 kurum: AKBANK

Kullanıcının gördüğü "3 banka" **Konut** seçiliyken doğru sayıdır. Ödeme planı
seçicisi kıyas tablosundaki bankaları listeler; tablo da o türü yayınlayan
bankaları.

### Kök neden — UX
Panel, listenin neden kısa olduğunu söylemiyor. Kullanıcı "veri eksik" sanıyor;
gerçekte "bu bankalar bu ürünü yayınlamıyor".

### Çözüm
1. **Kapsam satırı**: kredi türü seçicisinin altında "Konut: 3 banka · İhtiyaç:
   5 · Taşıt: 1" — hangi türde ne kadar veri olduğu tek bakışta görünür.
2. **Eksik bankalar açıkça listelensin**: "Bu türde veri olmayan kurumlar:
   Enpara (yalnızca ihtiyaç), Emlak Katılım (yalnızca ihtiyaç)…" ve nedeni
   `sources.yaml`'daki gerekçeye bağlansın.
3. **Kapsamı genişlet** (ayrı iş): taşıt yalnızca Akbank'ta; VakıfBank
   kataloğunda ürün dönmüyor, Yapı Kredi boş yanıt veriyor. TEB/Ziraat kredi
   uç noktaları hâlâ kapalı.

---

## 5. ✅ Ödeme planı verisi nereden geliyor?

### Cevap — bankadan DEĞİL, biz hesaplıyoruz
`core/loan.py::amortize()`. Bankadan gelen tek şey **aylık faiz oranı**. Taksit,
anapara/faiz ayrışması, KKDF/BSMV kırılımı ve kalan bakiye bizim anüite
hesabımız:

    r_ef = aylık_oran × (1 + KKDF + BSMV)
    taksit = anapara × r_ef / (1 − (1 + r_ef)^−vade)

### Kök neden — şeffaflık boşluğu
Panel bunu hiç söylemiyor. Kullanıcı ödeme planını bankanın resmî planı
sanabilir; oysa bankanın kendi planı yuvarlama, sigorta primi, dosya masrafı ve
gün sayımı farklarıyla birkaç lira sapabilir.

### Çözüm
1. **Ödeme planı başlığına açık not**: "Bu plan bankanın resmî planı değildir;
   bankanın ilan ettiği aylık orandan hesaplanmıştır. Ücret/sigorta dahil
   değildir."
2. **Çapraz doğrulama (asıl kazanç)**: Yapı Kredi'nin uç noktası **kendi
   taksit tutarını** döndürüyor (`MonthlyInstallmentAmount`). Bizim
   hesabımızla karşılaştırıp fark eşiği aşarsa `source_runs`'a uyarı
   yazabiliriz.
   > **DÜZELTME (2026-08-25):** bu maddede "Akbank da kendi taksitini
   > döndürüyor" yazıyordu, YANLIŞTI. `GetCreditInfo` yanıtındaki
   > `urunTaksitTut` alanı üç üründe de `null` geliyor. Çapraz doğrulama
   > tek kaynakla, Yapı Kredi ile yapılıyor. Canlı sonuç: ihtiyaç 100.000
   > TL / 3 ay -> banka 38.654,31 TL, bizim hesap 38.654,31 TL; konut
   > 1.000.000 TL / 36 ay -> ikisi de 48.156,85 TL. Bu, vergi oranlarımızın ve gün sayımımızın
   sürekli denetlenmesi demektir — tasarım §05'in istediği kalibrasyonun
   otomatik hâli.
3. Toplam maliyet oranı (yıllık maliyet) sütunu eklenebilir; bankalar bunu
   yayınlıyor ve karşılaştırmada taksitten daha dürüst bir ölçü.

---

## 6. ✅ Fon simülasyonuna istenen fonu ekleme (BIST100, para piyasası, kıymetli maden)

### Doğrulama — TEFAS kapalı, ama robots yüzünden değil
`tefas.gov.tr` artık **F5 Shape/BIG-IP bot challenge** arkasında:

    window["bobcmn"] = "1011111111101120..."   /TSPD/...  "DOSL7.challenge.support_id"

Eski API uç noktaları 404. Ana sayfa 200 dönüyor ama içerik JS challenge'ı.
Bunu aşmak, obfuscated JS'i çalıştırıp çerez üretmek demek — **bot tespiti
atlatma**. Kullanıcının robots.txt kararı bunu kapsamıyor; İş Bankası WAF'ıyla
aynı kategoride. **Yapılmayacak.**

### Erişilebilir alternatifler (canlı doğrulandı)

| Kaynak | Durum | Not |
|---|---|---|
| Ak Portföy | ✅ çalışıyor | Fiyat serisi sayfaya gömülü (`fundVals`) |
| **Garanti BBVA Portföy** | ✅ erişilebilir | Temiz fon URL'leri: `/altin-fonu`, `/birinci-para-piyasasi-fonu`… Fiyat **gömülü değil**, GraphQL (`api/content/garantiportfoy/graphql`) — araştırılmalı |
| **İş Portföy** | ✅ erişilebilir | `/tr/yatirim-fonlari` 200, ~400 KB; fiyat API'si daha önce CSRF istemişti, yeniden bakılmalı |
| QNB Portföy | ⚠️ 500 | Farklı URL denenmeli |

### Kök neden — mimari
Asıl sorun "hangi site" değil: **fon listesi `config/sources.yaml`'a gömülü ve
tek bir sağlayıcıyı (Ak Portföy) okuyabilen tek bir toplayıcı var.** Yeni bir
fon eklemek kod değişikliği gerektiriyor.

### Çözüm — sağlayıcı adaptörü mimarisi
1. **`FundPriceProvider` arayüzü**: `fetch_series(code) -> [(date, price)]`.
   Ak Portföy mevcut kodun taşınmış hâli olur.
2. **Fon kayıt defteri**: `config/funds.yaml` — her fon için
   `code, name, provider, url_or_id, is_equity_heavy, benchmark`. Fon eklemek
   = bir satır.
3. **Panelde "Fon ekle"**: kullanıcı kodu girer, panel sağlayıcıyı dener,
   fiyat serisi gelirse `funds` tablosuna yazar. Kod değişikliği gerekmez —
   kullanıcının asıl istediği bu.
4. **Sağlayıcı ekleme sırası**: Garanti Portföy (istenen fonların çoğu orada:
   BIST100, para piyasası, altın) → İş Portföy → QNB.
5. `is_equity_heavy` bayrağı fon adındaki resmî "(Hisse Senedi Yoğun Fon)"
   ibaresinden okunmaya devam eder — stopaj kararı elle girilen bayrağa
   bırakılmaz.

### Risk
Her sağlayıcı ayrı bir kırılgan parser demek. Azaltma: her sağlayıcı için
fixture testi + `source_runs`'a kaynak bazlı kayıt (zaten var) + fon başına
"son fiyat tarihi" panelde görünür (zaten var).

---

## 7. ✅ Ayrıntılı API loglama

### Bugün ne var
- `scrape_runs` — koşu düzeyi
- `source_runs` — kaynak (banka) düzeyi: fetch/parse, ok/failed/empty, satır, süre, hata
- `llm_calls` — token muhasebesi, çağrılmama nedenleri dahil
- `data/logs/*.log` — dönen dosya log'u

### Eksik olan — kullanıcının saydıkları
| İstenen | Bugün | Plan |
|---|---|---|
| "hangi API istekleri ne zaman dönmedi" | Kaynak bazında var, **istek bazında yok** | `http_requests` tablosu: URL, method, HTTP durum, gecikme, yanıt boyutu, deneme no |
| "daha sonraki istekleri döndü mü" | Yok | Kaynak bazlı **durum geçişi**: `failed → ok` anını yakala, "kaç koşu sonra düzeldi" ve "kesinti süresi" hesapla |
| "agent ne zaman hangi durumda devreye girdi" | `llm_calls.collector` var, **tetikleyen hata yok** | `llm_calls`'a `trigger_error` + `trigger_source` ekle: hangi bankanın hangi hatası fallback'i çağırdı |
| "kaç token harcadı" | ✅ var | Üstüne **tahmini maliyet** (model başına birim fiyat `.env`'den) |
| Aklıma gelmeyenler | — | Aşağıda |

### Eklenmesi önerilen parametreler
- **İstek düzeyi**: URL, method, HTTP durumu, gecikme (ms), yanıt boyutu,
  content-type, deneme sayısı, zaman aşımı mı yoksa hata kodu mu.
- **Kurtarma izi**: bir kaynak bozulup düzeldiğinde kesinti başlangıcı/bitişi
  ve süresi. "Dün 3 koşu düştü, bugün düzeldi" cevaplanabilir olur.
- **Veri değişim izi**: bir bankanın oranı ne zaman değişti (rate change log).
  Sessiz donmayı yakalar: uç nokta 200 dönüyor ama sayı haftalardır aynıysa
  beslemesi durmuş olabilir — `empty` kadar sinsi bir bozulma türü.
- **Şema sapması**: parse edilen alan sayısı/satır sayısı önceki koşuya göre
  %X'ten fazla düştüyse uyarı. Sayfa kısmen değiştiğinde erken uyarı verir.
- **Sanity red kaydı**: `sanity_check` bir koşuyu reddettiğinde bu şu an
  yalnızca hata metninde; ayrı bir alan olsun ki "kaç kez bant dışı veri geldi"
  sayılabilsin.
- **Zamanlayıcı olayları**: hangi tetikleyici (plan / açılış / tazelik telafisi)
  hangi koşuyu başlattı. Telafinin gerçekten çalışıp çalışmadığı görünür olur.

### Tasarım kuralı
Log yazma **asla** toplayıcıyı düşürmemeli (bugünkü davranış, testle kilitli) ve
tablolar sınırsız büyümemeli — `http_requests` en gürültülü tablo olacağı için
saklama süresi (ör. 30 gün) ve temizlik işi baştan planlanmalı.

---

## Sıra önerisi (uygulandı — tarihsel kayıt)

Bağımlılık ve değere göre:

1. **Madde 5 (ödeme planı şeffaflığı)** — küçük, tamamen bizim elimizde, yanlış
   anlaşılma riskini hemen kaldırır.
2. **Madde 4 (kredi kapsam görünürlüğü)** — küçük, aynı gerekçe.
3. **Madde 1 (kaynak şeffaflığı)** — orta; sunucuya kurulmadan önce olmalı.
4. **Madde 7 (loglama)** — orta; sunucuda erişim olmayacağı için erken değerli.
5. **Madde 2+3 (katılım oranları)** — araştırma gerektiriyor (KT hesaplayıcı
   uç noktası), sonucu belirsiz.
6. **Madde 6 (fon sağlayıcıları)** — en büyük iş; mimari değişiklik + en az iki
   yeni parser.
