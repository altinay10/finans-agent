# Finans Agent

Yedi bankanın kurlarını (+ TCMB referansı), yedi kurumun mevduat/katılma
hesabı oranlarını, beş kurumun kredi oranlarını, iki katılım bankasının kâr
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
| **Sunucu anahtarı** | `.env` | yalnızca **planlı** koşular (zamanlayıcı) |
| **Oturum anahtarı** | panelde girilir | yalnızca **"Şimdi tazele"** düğmesi |

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

**Panelden `.env` yazımı varsayılan olarak KAPALI.** "Kalıcı kaydet" düğmesi
ziyaretçinin anahtarını *sunucunun* anahtarı yapar; panel dışarı açıksa
herhangi biri sahibinin anahtarını sessizce değiştirebilir. Kendi makinende
tek başına çalıştırıyorsan `.env` içine `PANEL_ALLOW_ENV_WRITE=1` yazarak
açabilirsin — o zaman kaydedilen anahtarı zamanlayıcı da bir sonraki turunda
okur, yeniden başlatma gerekmez.

Tasarım dokümanı: `tasarim.html` (kaynak: `/Users/hectorpiece/Downloads/tasarim.html`).

> **Tamlık çetelesi** — neresi bitti, neresi eksik:
> [ILERLEME.md](ILERLEME.md) sonundaki ÇETELE bölümü.
>
> **Açık işlerin planı** — kök neden analizi ve çözüm gerekçeleri:
> [PLAN.md](PLAN.md).

Kişisel kullanım içindir. Hiçbir bölümü yatırım tavsiyesi değildir.

> İlerleme takibi (yapıldı/yapılacak, öncelik sırasıyla eksikler) için
> [ILERLEME.md](ILERLEME.md)'ye bak.

## Kurulum

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # İSTEĞE BAĞLI — varsayılanlar çalışır.
                       # LLM fallback ya da saklama süreleri için doldur.
```

## Çalıştırma

```bash
./venv/bin/streamlit run app/main.py
```

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
./venv/bin/python worker.py fx_tcmb    # TCMB resmi kur
./venv/bin/python worker.py fx_banks         # TEB, VakıfBank, Enpara, Emlak Katılım, Yapı Kredi, Akbank, Ziraat
./venv/bin/python worker.py deposits         # VakıfBank + TEB + Enpara + Yapı Kredi + Akbank matrisi
./venv/bin/python worker.py loan_rates       # VakıfBank + Akbank + Yapı Kredi + Enpara + Emlak Katılım
./venv/bin/python worker.py profit_shares    # Emlak Katılım kâr paylaşım oranları
./venv/bin/python worker.py profit_shares_kt # Kuveyt Türk kâr paylaşım oranları
./venv/bin/python worker.py funds            # Ak Portföy fon fiyat serisi
```

Hepsi canlı doğrulandı (2026-08-24). Kur verisi gün içinde saatlik, oran verisi
günde bir tazelenir.

**Zamanlayıcının üç güvencesi** (`scheduler.py`) — sunucuya kurulup
unutulacağı varsayımıyla:

1. **Plan** — her toplayıcı kendi saatinde koşar.
2. **Açılışta** — süreç başlarken bir kez hepsi çekilir; yeni kurulumda panel
   boş gelmez, yeniden başlatmada kaçırılan saat telafi edilir
   (`RUN_ON_START=0` ile kapatılır).
3. **Tazelik telafisi** — 30 dakikada bir, son *başarılı* koşusu kendi tazelik
   sınırını aşan toplayıcılar yeniden denenir. Bu olmadan, planlanan saatte
   hata alan bir toplayıcı ertesi güne kadar bayat kalır ve kimse fark etmezdi.

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
./venv/bin/python -m pytest tests/ -v
```

**154 test** (21 birim + 133 entegrasyon), hepsi ağsız ve saniyeler içinde koşar:

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
| Kuveyt Türk | | | | ✅ | Kâr paylaşım oranları ("87-13" biçimi) |
| VakıfBank | ✅ | ✅ | ✅ | | Sitenin kendi herkese-açık (login'siz) token akışı |
| Yapı Kredi | ✅ | ✅ | ✅ | | `_ajaxproxy` hesaplama araçları; kur gişe kurudur, makas geniş |
| Ziraat | ✅ | | | | robots kısıtı kaldırıldı; mevduat/kredi uç noktası bulunamadı |
| Ak Portföy | | | | | **Fon fiyat serisi** — 4 fon × ~2.160 gün |

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
| TEFAS | Uç nokta 404 + WAF "Request Rejected" — yerine Ak Portföy kullanılıyor |

Ayrıntılı bulgular ve tamlık çetelesi: [ILERLEME.md](ILERLEME.md).

## Uygulanan fazlar (bkz. tasarım §09)

- **Faz 0** — Hesaplama motoru + altın değer testleri: ✅
- **Faz 1** — Şema + TCMB + fon fiyatları: ✅ (TEFAS emekliye ayrıldı, yerine Ak Portföy)
- **Faz 2** — Streamlit v1: ✅ dört panel de gerçek veriyle, kurum kurum tablolar, tarayıcıda doğrulandı
- **Faz 3** — Banka collector'ları: 🟢 10 kurum aktif (8 kur, 5 mevduat, 5 kredi, 2 kâr payı, 1 fon); kalanlar yukarıdaki tabloya göre kapalı
- **Faz 4** — Zamanlama + gözlem: ✅ `worker.py`, `scrape_runs`, tazelik şeridi, cron
- **Faz 5** — LLM fallback: 🟡 Gemini bağlı, token korumaları yerinde, **varsayılan kapalı**; `.env`'e anahtar girilip `LLM_FALLBACK_ENABLED=1` yapılınca açılır

## Repo yapısı

Tasarım dokümanındaki (`§01`) yapının birebir karşılığı — `collectors/`,
`core/` (I/O yok), `store/`, `llm/`, `config/`, `app/`, `tests/`, `worker.py`.

## Docker ile dağıtım (önerilen)

### Tek konteyner (en basit)

```bash
docker run -d -p 8501:8501 -v finans-data:/app/data finans-agent
```

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

Panel **sadece localhost'a** bağlanır ve **oturum yönetimi yoktur**. Dışarı
açacaksan önüne kimlik doğrulamalı bir reverse proxy koy.

## Raspberry Pi / systemd alternatifi

Docker istemiyorsan `deploy/finans-panel.service` (systemd) ve
`deploy/crontab.example` hazır — venv + systemd + cron daha az katman.
