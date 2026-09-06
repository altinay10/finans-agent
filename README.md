# Finans Agent

Sekiz bankanın kurlarını (+ TCMB referansı), dokuz kurumun mevduat/katılma
hesabı oranlarını, on iki kurumun kredi oranlarını, iki katılım bankasının kâr
paylaşım oranlarını ve iki portföy şirketinden fon fiyatlarını **canlı olarak
toplayan** (hiçbiri koda gömülü sabit değil); kredi, mevduat ve fon getirisini
deterministik hesaplayan; sonuçları tek bir Streamlit panelinde kurum kurum
karşılaştıran sistem.

**Hesaplarını kendi kendine denetler:** kredi taksidi Yapı Kredi'nin kendi
taksit tutarıyla, mevduat stopajı Emlak Katılım'ın kendi net oranıyla her
koşuda karşılaştırılır. `config/taxes.yaml`'daki bir vergi oranı eskirse
hiçbir birim test düşmez — bunu yakalayan tek şey bankaların kendi
rakamlarıdır.

**Panel yedi sekme:** Döviz · Mevduat & Kâr Payı · Kredi · Fon Simülasyonu ·
**Agent** (kendi LLM anahtarın + anında tazeleme) · **Kaynaklar** (her sayının
uç noktası) · **Kayıtlar** (koşu/istek/kurtarma/oran değişimi/token muhasebesi).

### Agent anahtarı — panelden

API'si olmayan bankaların (Halkbank, QNB, DenizBank, ING) kredi oranlarını bir
dil modeli sayfa metninden çıkarıyor; bunun için bir anahtar gerekiyor.
Anahtar yalnızca `.env`'den okunsaydı, anahtarı biten kullanıcı için sistem
kalıcı olarak yarım kalırdı — sunucuya kurulduktan sonra dosyayı düzenleyip
süreci yeniden başlatmak panele bakan kişinin yapabileceği bir şey değil.

**Agent** sekmesinden anahtar girilir. İki anahtarın rolü kesin olarak ayrı:

| | Nereden gelir | Neyi besler |
|---|---|---|
| **Kayıtlı anahtar** | panelden kaydedilir → **veritabanı** (ya da `.env`) | yalnızca **planlı** koşular (zamanlayıcı) |
| **Oturum anahtarı** | panelde girilir, diske yazılmaz | yalnızca **"Agent'ı çalıştır"** düğmesi |

Kalıcı anahtar `.env`'e **değil veritabanına** yazılır ve bu teknik bir
zorunluluk: `.env` `.dockerignore`'da olduğu için imaja hiç girmiyor, panel
ile zamanlayıcı ayrı konteynerler, üstelik `docker compose up --build` panel
konteynerinin yazacağı dosyayı her yeniden kurulumda silerdi. Veritabanı ise
`finans-data` volume'ünde ve iki konteyner de aynı dosyayı açıyor.

**Anahtar kaydedilmeden önce canlı test edilir** (en küçük istek, `max_tokens=1`);
çalışmayan anahtar kaydedilmez. Birden fazla anahtar tutulabilir: **en son
kaydedilen** kullanılır, o kimlik hatası verirse (kota doldu, iptal edildi)
otomatik olarak bir öncekine düşülür. Anahtarın sağlayıcısı da (taban URL,
model, ek gövde alanları) anahtarla birlikte saklanır — aynı anahtar Gemini'de
geçerli, Qwen'de değil.

**"Şimdi tazele" sunucunun anahtarını harcayamaz.** Aksi halde paneli açan
herkes bir düğmeye — üstelik sınırsız tekrarla — basarak sahibinin faturasını
şişirebilirdi. Düğmeyi `disabled` çizmek bu işi görmez (istemciden üretilmiş
bir olay sunucu yolunu yine çağırabilir), o yüzden karar koşu yolunun içinde
veriliyor: anahtar yoksa `AnahtarGerekli` yükselir ve toplayıcı hiç kurulmaz.

Oturum anahtarı süreç geneline yazılmaz. Streamlit tüm tarayıcı oturumlarını
aynı süreçte, ayrı iş parçacıklarında koşturuyor; modül globaline yazmak hem
zamanlayıcının planlı koşularına bulaşır hem de iki ziyaretçinin birbirinin
anahtarını görmesine yol açardı. Anahtar `ContextVar` ile yalnızca kendi
koşusunun bağlamına giriyor (`llm/settings.use_api_key`), çağrı bütçesi de
aynı şekilde.

Anahtar hiçbir yerde tam gösterilmez, yalnızca son dört hane.

**"Sürekli kullanmak için kaydet" düğmesi ziyaretçinin anahtarını sunucunun
anahtarı yapar.** Panel `compose` kurulumunda `127.0.0.1`'e bağlı olduğu için
dışarıdan erişilemez; paneli bir reverse proxy ile dışarı açacaksan önüne
kimlik doğrulama koy, aksi halde açan herkes kayıtlı anahtarı değiştirebilir.

Tasarım dokümanı (`tasarim.html`) depoya dahil değildir; bu README ile
[ILERLEME.md](ILERLEME.md) ve [PLAN.md](PLAN.md) onun yerini tutar.

> **Tamlık çetelesi** — neresi bitti, neresi eksik:
> [ILERLEME.md](ILERLEME.md) sonundaki ÇETELE bölümü.
>
> **Açık işlerin planı** — kök neden analizi ve çözüm gerekçeleri:
> [PLAN.md](PLAN.md).

Kişisel kullanım içindir. Hiçbir bölümü yatırım tavsiyesi değildir.

> İlerleme takibi (yapıldı/yapılacak, öncelik sırasıyla eksikler) için
> [ILERLEME.md](ILERLEME.md)'ye bak.

## Kurulum

Python 3.11 veya üstü gerekir (konteyner imajı 3.12 kullanır).

```bash
git clone <depo-adresi> finans-agent
cd finans-agent
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

`.env` **zorunlu değildir** — her ayarın kodda bir varsayılanı var ve panel
`.env` olmadan da açılır. Şu üç şeyden birini istiyorsan doldur: LLM
fallback'i açmak, TCMB EVDS anahtarı vermek, saklama sürelerini/portu
değiştirmek.

```bash
cp .env.example .env
```

Tüm değişkenlerin listesi ve hangisinin ne işe yaradığı aşağıda: [Ortam
değişkenleri](#ortam-değişkenleri-env).

## Çalıştırma

İki yol var, ve **eşdeğer değiller**. `run.py` `ROLE` değişkenine bakar;
varsayılan `all` ile panelle birlikte **zamanlayıcıyı da** kaldırır — tek
komutla çalışan kurulum budur:

```bash
./venv/bin/python run.py
```

Yalnızca paneli istiyorsan (veri toplama ayrı bir süreçte koşacaksa):

```bash
./venv/bin/streamlit run app/main.py
```

Panel: <http://127.0.0.1:8501>

> `run.py` paneli varsayılan olarak `0.0.0.0` adresine bağlar, yani makinenin
> tüm ağ arayüzlerinden erişilebilir; panelde **oturum yönetimi yoktur**.
> Ağa açık bir makinede `PANEL_ADDRESS=127.0.0.1` ver ya da önüne kimlik
> doğrulamalı bir reverse proxy koy. `docker compose` kurulumunda portun
> kendisi `127.0.0.1:8501:8501` ile bağlandığı için bu sorun oluşmaz.

## Ortam değişkenleri (.env)

Kopyalanabilir tam örnek — her satırın **neden** öyle olduğu yorumlarıyla
birlikte — [`.env.example`](.env.example) dosyasındadır. Aşağıdaki tablo aynı
değişkenlerin özetidir; hepsi isteğe bağlıdır, boş bırakılanlar için parantez
içindeki varsayılan geçerlidir.

| Değişken | Varsayılan | Ne yapar |
|---|---|---|
| `DB_URL` | *(boş → `data/finans_agent.db`)* | SQLite yerine Postgres: `postgresql+psycopg://kullanıcı:parola@sunucu:5432/finans_agent` |
| `LLM_BASE_URL` | Gemini OpenAI-uyumlu ucu | Sağlayıcı değiştirmek için **kod değişmez**, bu üçü değişir |
| `LLM_MODEL` | `gemini-3.5-flash-lite` | |
| `LLM_API_KEY` | *(boş)* | Yalnızca **planlı** koşuları besler; panelin "Şimdi tazele" düğmesi bunu harcayamaz |
| `LLM_FALLBACK_ENABLED` | `0` | **Güvenlik anahtarı.** Anahtar dolu olsa bile bu `1` olmadan tek çağrı yapılmaz |
| `LLM_EXTRA_BODY` | *(boş)* | Sağlayıcıya özel ek gövde alanları (JSON). **Qwen kullanıyorsan `{"enable_thinking": false}` zorunlu**, yoksa token yakar |
| `LLM_MAX_INPUT_CHARS` | `8000` | Modele giden HTML üst sınırı |
| `LLM_MAX_OUTPUT_TOKENS` | `2000` | Yanıt üst sınırı |
| `LLM_MAX_CALLS_PER_RUN` | `1` | Genel toplayıcı koşusu başına çağrı sınırı |
| `LLM_AGENT_MAX_CALLS_PER_RUN` | `60` | Agent toplayıcısı banka **başına** bir çağrı yapar; kaynak sayısından küçük olursa gerisi sessizce eksik kalır |
| `LLM_TIMEOUT_SECONDS` | `45` | Asılı kalan çağrıyı keser |
| `LLM_PRICE_INPUT_PER_1M` / `..._OUTPUT_...` | *(boş → "bilinmiyor")* | USD / 1.000.000 token. `0` yazmak "bedava" demektir, boş bırakmak "bilinmiyor" |
| `EVDS_API_KEY` | *(boş)* | TCMB'nin ücretsiz API'si — sektör ortalaması çapası için |
| `ROLE` | `all` | `all` · `panel` · `scheduler` — `run.py` neyi kaldırsın |
| `PANEL_ADDRESS` | `0.0.0.0` | **Ağa açık makinede `127.0.0.1` yap** (yukarıdaki uyarı) |
| `PANEL_PORT` | `8501` | |
| `SUPERVISE_INTERVAL_SECONDS` | `60` | `ROLE=all` iken ölen zamanlayıcı kaç saniyede bir yeniden denensin |
| `RUN_ON_START` | `1` | Süreç başlarken bir kez hepsini çek — yeni kurulumda panel boş gelmesin |
| `CATCHUP_INTERVAL_MINUTES` | `30` | Tazelik telafisi aralığı |
| `RETAIN_HTTP_REQUEST_DAYS` | `30` | `0` = sınırsız sakla |
| `RETAIN_SOURCE_RUN_DAYS` | `180` | |
| `RETAIN_SCRAPE_RUN_DAYS` | `180` | |
| `RETAIN_SNAPSHOT_DAYS` | `14` | Ham snapshot'lar |
| `LOG_DIR` | `data/logs` | Boş bırakılırsa dönen dosya log'u kapanır, konsol kalır |
| `LOG_LEVEL` | `INFO` | |
| `DATA_DIR` | *(boş)* | Verilirse fon tanımları önce `<DATA_DIR>/funds.yaml`'dan okunur |
| `FUNDS_YAML_PATH` | *(boş)* | Fon tanım dosyasını doğrudan gösterir, yukarıdakini atlar |
| `PANEL_ALLOW_ENV_WRITE` | — | **Artık okunmuyor** (2026-09-06). Panelden `.env`'e yazma yolu kaldırıldı; kalıcı anahtar veritabanına yazılıyor |

`llm_calls` ve `rate_changes` tabloları **asla budanmaz**; token/maliyet
muhasebesi ile "oran en son ne zaman değişti" izi kümülatiftir.

### Veri toplama — panel bunu YAPMAZ

Streamlit paneli bankalara hiç gitmez; yalnızca veritabanını okur (tasarım §01).
Veriyi tazeleyen ayrı bir süreçtir. Üç seçenekten **biri** kurulmalı:

| Yöntem | Ne yapmalı |
|---|---|
| **Docker** (önerilen) | `docker compose up -d` — `scheduler` servisi hallediyor, cron gerekmez |
| **systemd** | `finans-panel.service` **ve** `finans-scheduler.service` birlikte |
| **cron** | `deploy/crontab.example` (scheduler servisiyle aynı anda kurma, iki kez çalışır) |

Hiçbiri kurulmazsa panel ilk günden sonra bayat veri gösterir — ve bunu
fark etmen için üstteki tazelik şeridi kırmızı bir uyarı basar. Uyarı iki
ayrı arızayı KARIŞTIRMAZ: "zamanlayıcı süreci yok" (nabza bakar,
`store/heartbeat.py`) ile "süreç var ama kaynaklar düşmüş" farklı mesajlar
üretir, çünkü ikisinde yapılacak şey farklıdır.

Elle tek seferlik toplama:

```bash
./venv/bin/python worker.py all        # hepsini sırayla tazele
```

Tek tek:

```bash
./venv/bin/python worker.py <hedef>
```

Geçerli hedefler — `worker.py`'deki `COLLECTORS` kaydının tamamı:

| Hedef | Ne toplar |
|---|---|
| `fx_tcmb` | TCMB resmi XML referans kuru (anahtarsız) |
| `fx_banks` | Banka gişe/serbest kurları — TEB, VakıfBank, Enpara, Emlak Katılım, Yapı Kredi, Akbank, Ziraat, Kuveyt Türk |
| `deposits` | Mevduat faiz matrisi — VakıfBank, TEB, Enpara, Yapı Kredi, Akbank, Halkbank, Ziraat |
| `loan_rates` | Uç noktası olan bankaların kredi oranları — VakıfBank, Akbank, Yapı Kredi, Enpara, Emlak Katılım |
| `loan_rates_llm` | **Agent** — uç noktası olmayan bankaların kredi oranı sayfa metninden çıkarılır. Planlı koşusu **Pazartesi ve Perşembe 15:00**. LLM kapalıysa tek token harcamadan biter |
| `participation_rates` | Emlak Katılım **yıllık kâr payı** oranı (mevduatla aynı hesaba girer) |
| `participation_rates_kt` | Kuveyt Türk yıllık kâr payı oranı |
| `profit_shares` | Emlak Katılım kâr **paylaşım** oranı (getiri değil — hesaba girmez) |
| `profit_shares_kt` | Kuveyt Türk kâr paylaşım oranı |
| `funds` | Fon fiyat serileri — Ak / Garanti BBVA / Yapı Kredi Portföy, `config/funds.yaml`'daki fonlar |

`participation_rates` ile `profit_shares` karıştırılmamalı: ilki yıllık
**getiri** oranı (%33,97 gibi), ikincisi kârın müşteriye düşen **payı**
(%93 gibi). İkincisi bir getiri değildir ve hiçbir hesaba girmez.

Hepsi canlı doğrulandı (2026-08-24). Kur verisi gün içinde saatlik, oran verisi
günde bir tazelenir.

**Zamanlayıcının dört güvencesi** (`scheduler.py`) — sunucuya kurulup
unutulacağı varsayımıyla:

1. **Plan** — her toplayıcı kendi saatinde koşar.
2. **Açılışta** — süreç başlarken bir kez hepsi çekilir; yeni kurulumda panel
   boş gelmez, yeniden başlatmada kaçırılan saat telafi edilir
   (`RUN_ON_START=0` ile kapatılır).
3. **Tazelik telafisi** — 30 dakikada bir, son *başarılı* koşusu kendi tazelik
   sınırını aşan toplayıcılar yeniden denenir. Bu olmadan, planlanan saatte
   hata alan bir toplayıcı ertesi güne kadar bayat kalır ve kimse fark etmezdi.
4. **Geri çekilme (backoff)** — ayrıştırıcısı kırılıp LLM yedeği de
   başaramayan toplayıcı, ilk 6 denemede **5 dakikada bir**, sonrasında
   **4 saatte bir** denenir. Bu olmadan telafi mekanizması kalıcı bir arızayı
   30 dakikada bir sonsuza kadar yeniden dener ve her denemede bir LLM çağrısı
   yakardı. Sayaç yalnızca **gerçekten çağrı yapılıp başarısız olduğunda**
   artar: "agent kapalı" ve "bütçe doldu" tek token harcamaz, dolayısıyla geri
   çekilecek bir şey de yoktur.

## Log ve gözlem

Üç katman, üçü de sunucuda SSH'sız okunabilir:

| Katman | Nerede | Ne cevaplar |
|---|---|---|
| **Koşu** | `scrape_runs` tablosu + üstteki tazelik şeridi | "Toplayıcı çalıştı mı, kaç satır yazdı?" Şerit sekme adlarıyla dört rozet gösterir (rengi grubun **en kötü** üyesi belirler); toplayıcı toplayıcı yaş/sınır tablosu altındaki açılır bölümde |
| **Kaynak** | `source_runs` tablosu + **Kayıtlar** sekmesi | "**A bankası başarılı, B bankası başarısız**" — banka banka, aşama aşama (fetch/parse), süre ve hata metniyle |
| **LLM** | `llm_calls` tablosu + Kayıtlar sekmesi | "Agent devreye girdi mi, **kaç token harcadı**?" Girdi/çıktı/toplam token, kurtarılan satır, süre |

Bu ayrım kritik: bir banka çökse bile koşu **`ok`** görünür, çünkü diğerleri
veri getirir. Hangi bankanın sessizce kaybolduğunu gösteren tek yer
`source_runs`. Kayıtlar sekmesindeki **kaynak sağlığı** tablosu bunu başarı
oranı ve son hata metniyle birlikte listeler.

LLM tarafında çağrının **yapılmadığı** durumlar da kaydedilir
(`disabled`, `budget_exceeded`) — böylece "fallback neden devreye girmedi"
sorusu cevaplanır ve sıfır token harcandığı kanıtlanır.

Ayrıca dönen dosya log'u: `data/logs/{worker,scheduler}.log` (5 × 2 MB).
Istisna izleri oraya düşer; `LOG_DIR=` boş bırakılırsa kapanır.

```bash
docker compose logs -f scheduler          # canlı
docker compose exec panel tail -f data/logs/scheduler.log
```

## Testler

```bash
./venv/bin/pytest -q
```

**417 test**, hepsi ağsız ve saniyeler içinde koşar (2026-09-06 ölçümü: 15,6 sn):

- `test_loan.py` / `test_deposit.py` / `test_fund.py` — `core/` saf fonksiyonları,
  elle hesaplanmış altın değerlerle.
- `test_integration_store.py` — şema + sorgular (kademe seçimi, para birimi
  izolasyonu, tazelik view'i) gerçek SQLite üzerinde.
- `test_integration_collectors.py` — `tests/fixtures/` altındaki gerçek sayfa
  parçalarıyla parser'lar; bir banka sayfası değişirse burada patlar. Ayrıca
  `sanity_check`'in kötü veriyi reddettiğini doğrular.
- `test_integration_panels.py` — uçtan uca kullanıcı akışı.

Testler geçici bir veritabanına yazar; `data/finans_agent.db` dosyasına dokunmaz.

## Durum (2026-08-24 itibarıyla, canlı doğrulandı)

Tasarım dokümanının §02'si her kaynağın elle doğrulanmasını şart koşuyor. Bu
doğrulama yapıldı; sonuçlar `config/sources.yaml` içinde `status` /
`blocked_reason` alanlarında duruyor.

**Aktif kaynaklar — hepsi canlı çalışıyor, hiçbiri hardcoded değil:**

| Kaynak | Döviz | Mevduat | Kredi | Kâr payı | Not |
|---|:--:|:--:|:--:|:--:|---|
| TCMB | ✅ | | | | Resmi XML referans kuru, anahtarsız |
| Akbank | ✅ | ✅ | ✅ | | **Projedeki tek taşıt kredisi kaynağı.** robots kısıtı kaldırıldı |
| CepteTEB | ✅ | ✅ | | | Mevduat: `VadeliHesapFaizOranList` tam matrisi |
| Emlak Katılım | ✅ | | ✅ | ✅ | Katılım bankası — kredi "kâr oranı", mevduat yerine kâr paylaşım oranı |
| Enpara (QNB) | ✅ | ✅ | ✅ | | Hepsi sunucu-render HTML'e gömülü |
| Halkbank | | ✅ | ✅ | | Mevduat: `depositinterestrates` (İNTERNET şube oranı; şube tablosu %5'te sabit). Kredi agent'tan |
| Kuveyt Türk | ✅ | ✅ | | ✅ | Kur: JS paketinden keşfedilen `exchangeRates` ucu. Kâr paylaşım oranları ("87-13" biçimi) |
| VakıfBank | ✅ | ✅ | ✅ | | Sitenin kendi herkese-açık (login'siz) token akışı |
| Yapı Kredi | ✅ | ✅ | ✅ | | `_ajaxproxy` hesaplama araçları; kur gişe kurudur, makas geniş |
| Ziraat | ✅ | ✅ | | | Kur: robots kısıtı kaldırıldı. Mevduat: "Fiyatlar ve Oranlar" sayfasındaki İNTERNET şube tablosu. Kredi oranı yayınlanmıyor |
| Ak Portföy | | | | | **Fon fiyat serisi** — 4 fon × ~2.160 gün |
| Garanti BBVA Portföy | | | | | **Fon serisi** — 7 fon. Birim pay fiyatı DEĞİL, "1.000 TL'nin değeri" endeksi |
| Yapı Kredi Portföy | | | | | **Fon fiyat serisi** — sitemap'ten 113 fon (76'sı açık), ~760 gün. Panelden **tüm katalog tek tuşla** eklenir |

**Kâr paylaşım oranı ≠ faiz.** Katılım bankalarının yayınladığı sayı yıllık
getiri değil, bankanın elde ettiği kârın müşteriye düşen yüzdesidir. Bunu faiz
gibi hesaplamak uydurma bir getiri üretirdi; bu yüzden ayrı tabloda tutulur,
panelde ayrı blokta gösterilir ve **getiri hesabına hiç girmez**.

**robots.txt** kısıtları kullanıcı kararıyla (2026-08-24) uygulanmıyor; ilgili
kaynaklar `config/sources.yaml`'da `robots_override: true` ile işaretli.

**Hâlâ kapalı olanlar — hiçbiri robots kaynaklı değil:**

| Kaynak | Engel |
|---|---|
| **İş Bankası** | **WAF.** Normal tarayıcı gezintisinde bile blok sayfası dönüyor. Bu robots gibi bir nezaket kuralı değil, aktif bot tespiti; aşmak parmak izi taklidi gerektirir ve bot-tespiti atlatma kapsamına girer. Kullanıcının robots kararı bunu kapsamıyor, **açılmadı** |
| Garanti BBVA | Uç nokta doğru ama tam tarayıcı başlıklarıyla bile HTTP 500 (nginx); oturum/cihaz çerezi gerekiyor. Headless tarayıcı gerekir |
| Halkbank | TLS handshake tamamlanmıyor (coğrafi/IP kısıtı olabilir) |
| Ziraat (mevduat/kredi) | Kur açıldı; oran için ayrı uç nokta bulunamadı, hesaplama sayfaları 404 |
| CepteTEB (kredi) | Hesap makinesi uç noktası HTTP 500'e yönleniyor, sayfada gömülü oran da yok |
| TEFAS | Uç nokta 404 + WAF "Request Rejected" — yerine portföy şirketleri kullanılıyor |
| **Deniz Portföy** | **Engel yok — veri güvenilir değil.** `FonGetiriList` uç noktası açık ama döndürdüğü fiyatlar sorulan tarihe ait değil: 90 fonun yalnızca 23'ü sitenin kendi tablosuyla tutuyor, bir PARA PİYASASI fonu düşüş gösteriyor ve yanıtta tarih alanı yok. Seriye yazmak fiyatları yanlış tarihe kaydetmek olurdu. Kanıt `config/sources.yaml` |
| DenizBank (banka sitesi) | Fon fiyat verisi yayınlamıyor; `fon-fiyatlari` sayfası pazarlama içeriği, tarayıcıda hiçbir veri isteği atmıyor |
| Yapı Kredi (banka sitesi) | 42 fonun yalnızca GÜNCEL fiyatı var, geçmiş seri yok; listesi zaten YK Portföy'ün alt kümesi |

Ayrıntılı bulgular ve tamlık çetelesi: [ILERLEME.md](ILERLEME.md).

## Uygulanan fazlar (bkz. tasarım §09)

- **Faz 0** — Hesaplama motoru + altın değer testleri: ✅
- **Faz 1** — Şema + TCMB + fon fiyatları: ✅ (TEFAS emekliye ayrıldı, yerine Ak Portföy)
- **Faz 2** — Streamlit v1: ✅ dört panel de gerçek veriyle, kurum kurum tablolar, tarayıcıda doğrulandı
- **Faz 3** — Banka collector'ları: ✅ envanterde **doğrulanmamış kaynak kalmadı** (38 aktif, 12 kapalı — hepsi gerekçeli). 9 kur, 9 mevduat, 12 kredi, 2 kâr payı, 3 fon sağlayıcısı
- **Faz 4** — Zamanlama + gözlem: ✅ `worker.py`, `scrape_runs`, tazelik şeridi, cron
- **Faz 5** — LLM fallback: ✅ sağlayıcı `.env`'den takas edilebilir (kurulu: Qwen `qwen3-max`), token korumaları yerinde, **varsayılan kapalı**; `.env`'e anahtar girilip `LLM_FALLBACK_ENABLED=1` yapılınca açılır. Qwen'de `LLM_EXTRA_BODY={"enable_thinking": false}` ZORUNLU — bkz. `.env.example`

## Repo yapısı

Tasarım dokümanındaki (`§01`) yapının birebir karşılığı — `collectors/`,
`core/` (I/O yok), `store/`, `llm/`, `config/`, `app/`, `tests/`, `worker.py`.

## Docker ile dağıtım (önerilen)

### Tek konteyner (en basit)

```bash
docker build -t finans-agent .
docker run -d -p 127.0.0.1:8501:8501 -v finans-data:/app/data finans-agent
```

Portu `-p 8501:8501` diye açarsan panel tüm ağ arayüzlerinden erişilebilir
olur; panelde oturum yönetimi olmadığı için yukarıdaki komut kasıtlı olarak
`127.0.0.1`'e bağlar.

Varsayılan `ROLE=all`: **panel VE zamanlayıcı birlikte kalkar.** Eskiden imaj
doğrudan Streamlit'i çalıştırıyordu; `docker run` diyen kullanıcı yalnızca
paneli ayağa kaldırıp hiç veri toplamıyor ve site günlerce eskiyen veri
gösteriyordu — sunucuda en sık yaşanan arıza buydu (bkz. `run.py`).

`ROLE` değerleri: `all` (varsayılan) · `panel` · `scheduler`.

### Compose (panel ve zamanlayıcı ayrı servis)

```bash
docker compose up -d      # panel: http://127.0.0.1:8501
```

`.env` **gerekmez** — tüm ayarların varsayılanı var. LLM fallback'i açmak ya
da kayıt saklama sürelerini değiştirmek istersen `cp .env.example .env`.

İki servis kalkar (panel `ROLE=panel` ile çalışır, çünkü zamanlayıcı ayrı
bir servistir; yanlışlıkla iki zamanlayıcı başlatılsa bile
`store/heartbeat.py`'deki kilit ikincisini kapatır):

- **panel** — Streamlit arayüzü, root olmayan kullanıcı, healthcheck'li.
  Üstte **zamanlayıcının nabzını** gösterir: "🟢 çalışıyor — son nabız 9 sn
  önce" ya da durmuşsa ne yapılacağını söyleyen açık bir hata. Panel artık
  "çalışmıyor **gibi**" demiyor; ölçüyor.
- **scheduler** — `scheduler.py`, toplayıcıları planına göre tetikler; **cron
  kurmaya gerek yok**. Açılışta bir kez hepsini toplar (`RUN_ON_START=0` ile
  kapatılır), böylece panel ilk açılışta boş gelmez.

Elle tek seferlik toplama:

```bash
docker compose run --rm worker all
```

Veri adlandırılmış `finans-data` volume'ünde durur. Yedek:

```bash
docker run --rm -v finans-data:/d -v "$PWD":/b alpine tar czf /b/finans-data.tgz -C /d .
```

Konteynerin saat dilimi `Europe/Istanbul` olarak ayarlıdır. Oran tarihleri
(`valid_date`) zaten saat diliminden bağımsız hesaplanır (`store/clock.py`),
ama log'ların yerel saatte okunması için TZ de ayarlanmıştır.

**Sürüm yükseltme güvenli.** Yeni bir imaja geçtiğinde `store/migrate.py`
şemadaki yeni sütun ve tabloları var olan veritabanına ekler; mevcut satırlar
korunur. Canlı doğrulandı (2026-08-25): eski şemalı bir volume yeni imajla
açıldığında 5 tablo + 2 sütun eklendi, eski kayıtlar yerinde kaldı.

**Kayıtlar sınırsız büyümez.** `scheduler` günde bir kez budama yapar:
`http_requests` 30 gün, `source_runs`/`scrape_runs` 180 gün, ham snapshot'lar
14 gün. Token/maliyet muhasebesi (`llm_calls`) ve oran değişim izi
(`rate_changes`) **asla budanmaz** — ikisi de kümülatif.

Panelde **oturum yönetimi yoktur** — açan herkes her şeyi görür. `compose`
kurulumu portu `127.0.0.1:8501:8501` ile bağladığı için dışarı kapalıdır;
`run.py`'yi konteynersiz çalıştırırken aynı korumayı `PANEL_ADDRESS=127.0.0.1`
verir (varsayılanı `0.0.0.0`). Dışarı açacaksan önüne kimlik doğrulamalı bir
reverse proxy koy.

## Raspberry Pi / systemd alternatifi

Docker istemiyorsan `deploy/finans-panel.service` (systemd) ve
`deploy/crontab.example` hazır — venv + systemd + cron daha az katman.
