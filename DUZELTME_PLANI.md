# Düzeltme planı — kabul testi ve güvenlik raporları

**Kaynak:** `finans_site_kullanici_kabul_testi_raporu.md` (F-01…F-11) ve
`finans_site_teknik_guvenlik_raporu.md` (F-01…F-13), 16 Eylül 2026.
**Yöntem:** Her bulgu kodda aranıp kök nedeni doğrulandı. Doğrulanamayanlar
ve kök nedeni raporda yazandan FARKLI çıkanlar ayrıca işaretlendi.
**Temel durum:** `venv/bin/pytest -q` → 536 geçti.

---

## 0. Özet — üç ayrı iş, üç ayrı yer

Raporlardaki 24 bulgu tek bir yığın değil; **üç ayrı yerde** yaşıyorlar ve
karıştırılırsa hiçbiri kapanmaz:

| Küme | Nerede düzeltilir | Bulgu sayısı |
|---|---|---|
| **A. Hesap doğruluğu ve girdi doğrulaması** | Bu depo, `app/panels/` + `store/queries.py` | 7 |
| **B. Yetki, sır yönetimi ve SSRF** | Bu depo, `app/panels/agent.py` + `llm/` | 3 |
| **C. Taşıma güvenliği, başlıklar, 404, `lang`** | **Bu depoda YOK** — depoda hiç reverse proxy yapılandırması yok | 6 |

C kümesinin tamamı bugün depo dışında yaşıyor: `docker-compose.yml` paneli
yalnızca `127.0.0.1:8501`'e bağlıyor, önündeki katman (Cloudflare / nginx /
tunnel) hiçbir dosyada tanımlı değil. Bu yüzden C kümesinin ilk işi bir
düzeltme değil, **önündeki katmanı depoya almak**; aksi halde her düzeltme
elle yapılmış, kayıtsız ve tekrarlanamaz bir sunucu ayarı olarak kalır.

---

## 1. Bulgu → kök neden eşlemesi

### A. Hesap doğruluğu ve girdi doğrulaması

| # | Rapor | Gerçek kök neden | Yer |
|---|---|---|---|
| A1 | KKT F-01, GÜV F-09 — tutar alanı | `re.sub(r"\D", "", raw)` — rakam DIŞINDAKİ her karakteri siliyor. Eksi işareti de, ondalık ayıracı da. `-2500`→`2500`, `1234.56`→`123456`. Rakam hiç yoksa `None` dönüyor ve çağıran sessizce eski değere düşüyor. | [common.py:194](app/panels/common.py:194), [common.py:264](app/panels/common.py:264) |
| A2 | KKT F-02 — VakıfBank çoklu kademe | **Raporun teşhisi yanlış.** Satırlar zaten tekilleştiriliyor (`_resolve_overlapping_tiers`, her `(kurum, döviz, vade)` için tek satır). Ekrandaki üç kademe tek bir ALTYAZIDAN geliyor: `_tier_text()` kurumun TÜM satırlarındaki kademeleri bir kümede toplayıp virgülle yazıyor. Farklı vadeler farklı kademeye düşünce altyazı üç kademe listeliyor. Hesap doğru, **etiket yanlış**. | [deposit.py:282](app/panels/deposit.py:282), [deposit.py:173](app/panels/deposit.py:173), [queries.py:148](store/queries.py:148) |
| A3 | KKT F-03, GÜV F-09 — geçersiz vadede eski sonuç | `st.number_input(..., min_value=1, max_value=360)`. Streamlit sınır dışı değeri **kabul etmiyor**: kendi İngilizce hatasını basıyor ve widget'ı ESKİ geçerli değerde bırakıyor. Betik o eski değerle koşmaya devam ettiği için altta 360 aylık plan duruyor. Bizim kodumuzda hiçbir doğrulama yok. | [loan.py:99](app/panels/loan.py:99) |
| A4 | KKT F-04 — tablo araç düğmeleri | Streamlit'in `stElementToolbar` katmanı veri tablosunun altında kalıyor. Depoda tablo araç çubuğuna dair hiçbir CSS yok; tek CSS kuralı anapara kutusunun punto ayarı. | [common.py:239](app/panels/common.py:239) (`principal_style`) |
| A5 | KKT F-05, GÜV F-08 — "10/10 kaynak taze" | İki ayrı sorgu, iki ayrı kusur: (1) Üst şerit `v_freshness`'ten besleniyor; o view `scrape_runs`'ı **toplayıcı düzeyinde** grupluyor. `loan_rates_llm` koşusu 12 bankanın 3'ü düşse de `ok` biter → taze görünür. Banka bazlı sonuçlar `source_runs`'ta ve üst şerit oraya HİÇ bakmıyor. (2) "Şu an bozuk kaynaklar" listesi `_recovery(days)` ile geliyor; sorgu `WHERE started_at > datetime('now', -N days)`. Son deneme 49,5 saat önceyse "son 2 gün" penceresinde satır hiç oluşmuyor → "bozulma sinyali yok". | [db.py:32](store/db.py:32), [status.py:291](app/panels/status.py:291), [logs.py:167](app/panels/logs.py:167), [queries.py:634](store/queries.py:634) |
| A6 | KKT F-06 — geçersiz fon metni | `st.selectbox` serbest metinde seçimi değiştirmiyor; alanda yazan metin ile hesaplanan fon ayrışıyor. Sonuç bölümünde hesaplanan fon kodu **hiçbir yerde tekrar yazılmıyor**, bu yüzden ayrışma görünmüyor. | [fund.py:161](app/panels/fund.py:161) |
| A7 | KKT F-03 ek gözlem, GÜV F-10 — uygun olmayan kredi ürünleri | `_limit_warning()` uyarı METNİ üretiyor ama satır `comparisons` listesinde kalıyor ve `sort_values("Taksit")` ile normal sıralamaya giriyor. En ucuz görünen satır alınamayan ürün olabiliyor. | [loan.py:196](app/panels/loan.py:196), [loan.py:323](app/panels/loan.py:323) |

### B. Yetki, sır yönetimi ve SSRF

| # | Rapor | Gerçek kök neden | Yer |
|---|---|---|---|
| B1 | GÜV F-03 — kimlik doğrulamasız yönetim yüzeyi | **Depoda hiçbir kimlik doğrulama kodu yok.** `grep -rn "ADMIN\|AUTH\|login"` → yalnızca banka sayfalarına dair yorumlar. Agent, Kaynaklar ve Kayıtlar sekmeleri `app/main.py`'de koşulsuz çiziliyor. `credentials.save()` çağıran taraf ne olursa olsun yazıyor. Panel `docker-compose.yml`'de `127.0.0.1`'e bağlı ve yorumda "önüne kimlik doğrulama koy" yazıyor — koyulmamış. | [main.py:44](app/main.py:44), [credentials.py:229](llm/credentials.py:229), [agent.py:465](app/panels/agent.py:465) |
| B2 | GÜV F-03 — özel Base URL / SSRF | Panelden girilen `base_url` hiçbir doğrulamadan geçmeden bağlam değişkenine yazılıyor ve OpenAI uyumlu istemci **sunucudan** o adrese istek atıyor. Şema, host, port, DNS çözümlemesi, yönlendirme zinciri — hiçbiri kontrol edilmiyor. | [agent.py:385](app/panels/agent.py:385), [settings.py:409](llm/settings.py:409), [extract.py:122](llm/extract.py:122) |
| B3 | GÜV F-04 — kayıtlarda ham URL ve iç ayrıntı | `_short()` yalnızca `https://` önekini atıp 60 karaktere kırpıyor; **sorgu dizesi olduğu gibi kalıyor**. Üstelik URL veritabanına da ham yazılıyor (`url[:1000]`) — yorumu bile "sorgu dizesi anahtar/token taşıyabilir" diyor ama redaksiyon yapılmıyor. Sağlayıcı hata gövdeleri `error[:2000]` olarak saklanıp panelde gösteriliyor. | [logs.py:589](app/panels/logs.py:589), [observability.py:141](store/observability.py:141) |

### C. Taşıma güvenliği ve sunum (depo dışı)

| # | Rapor | Durum |
|---|---|---|
| C1 | GÜV F-01 — HTTP 200, HTTPS'e yönlenmiyor | Depoda proxy yapılandırması yok; doğrulanamadı, düzeltilecek yer yaratılmalı. |
| C2 | GÜV F-02 — TLS 1.0/1.1 açık | Cloudflare / origin ayarı. Depoda iz yok. |
| C3 | GÜV F-05 — HSTS/CSP/frame/MIME/referrer/permissions yok | Aynı. |
| C4 | GÜV F-06 — XSRF çerezinde `Secure` yok | `.streamlit/config.toml` **dosyası yok**; Streamlit tamamen varsayılanlarla koşuyor. Proxy `proxy_cookie_flags` ile de çözülebilir. |
| C5 | GÜV F-07, KKT F-07 — soft 404, sahte robots/sitemap | Proxy katmanı işi. |
| C6 | KKT F-11, GÜV F-12 — `lang="en"`, h1→h3, kontrast, 22×22 dokunma hedefi | `lang` Streamlit'in kendi `index.html`'inde sabit; kalan üçü bizim kodumuzda (`st.subheader` h3 üretiyor, CSS yok). |

---

## 2. Düzeltme planı

### P0 — bunlar kapanmadan gerçek API anahtarı internete açık kalmamalı

#### P0-1 · Yönetici kapısı (B1)

Streamlit tek sayfa olduğu için Cloudflare Access sekme bazında kapı
kuramaz; kapı **uygulama içinde** olmak zorunda.

- `app/auth.py`: `ADMIN_TOKEN` ortam değişkeni (boşsa yönetici yüzeyi
  tamamen kapalı — "ayar yok" = "herkese açık" olmamalı).
  `is_admin()` oturum durumundan okur, `require_admin()` yoksa `PermissionError`.
- `app/main.py`: Agent / Kaynaklar / Kayıtlar sekmeleri yalnızca
  `is_admin()` ise çizilir. Giriş kutusu ayrı bir "Yönetim" sekmesinde.
- **Sekmeyi gizlemek yetmez** (rapor haklı): yazma yolunun içine kontrol:
  - `credentials.save()` çağrısından önce `require_admin()` — [agent.py:465](app/panels/agent.py:465)
  - `run_agent()` içinde `require_admin()` — bu zaten kendi yorumunda
    "düğmeyi `disabled` yapmak yalnızca görseldir" diyor, aynı gerekçe.
  - `app_settings` fiyat yazımı ve `worker` tetikleyen her düğme.
- Her yönetici işlemi için denetim satırı (`source_runs` yerine ayrı
  `admin_audit` tablosu ya da mevcut `LlmCall.trigger_error` alanı değil —
  yeni tablo temiz olur).
- Test: `tests/test_admin_gate.py` — jetonsuz `credentials.save` çağrısı
  `PermissionError`; jetonsuz oturumda üç sekme render edilmiyor.

#### P0-2 · Base URL SSRF koruması (B2)

Yeni modül `llm/urlguard.py`, `validate_base_url(url) -> str`:

1. Yalnızca `https`.
2. Port allowlist: `443` (ve açıkça izin verilen ek portlar).
3. Host allowlist: `llm/providers.py`'deki sağlayıcı hostları + `.env`'den
   `LLM_EXTRA_ALLOWED_HOSTS`. Allowlist **varsayılan olarak kapalı liste**.
4. DNS çözümlemesi sonrası her IP için: loopback, private, link-local,
   `169.254.169.254` metadata, ULA, multicast → red.
5. `httpx` istemcisinde `follow_redirects=False`; yönlendirme gelirse hedef
   tekrar aynı kontrolden geçmeden izlenmez.
6. Yanıt boyutu ve zaman aşımı sınırı (`LLM_TIMEOUT_SECONDS` zaten var,
   boyut sınırı yok — eklenecek).

Çağrı noktaları: [agent.py:385](app/panels/agent.py:385) (girişte, hata
Türkçe), `credentials.save()` (kayıtta, ikinci kez), `settings.use_api_key`
(kullanımda, üçüncü kez — kayıttan sonra allowlist daralmış olabilir).

Test: `tests/test_urlguard.py` — `http://`, `https://127.0.0.1`,
`https://169.254.169.254`, `https://localhost`, DNS'i özel IP'ye çözülen
host, `:8080` portu, allowlist dışı host, yönlendirme zinciri. Hepsi red.

#### P0-3 · Proxy katmanını depoya al (C1–C5)

`deploy/` altına gerçek bir yapılandırma; şu an orada yalnızca iki systemd
birimi ve bir crontab örneği var.

- `deploy/Caddyfile` (önerilen — otomatik HTTPS, kısa) veya
  `deploy/nginx.conf`. `docker-compose.yml`'e isteğe bağlı `proxy` servisi.
- İçeriği:
  - `:80` → `:443` kalıcı yönlendirme; düz HTTP'de uygulama gövdesi yok.
  - Minimum TLS 1.2.
  - Başlıklar: `Strict-Transport-Security` (alt alanlar HTTPS olduktan
    sonra), `Content-Security-Policy` (en az `frame-ancestors 'none'`),
    `X-Content-Type-Options: nosniff`, `Referrer-Policy:
    strict-origin-when-cross-origin`, `Permissions-Policy: camera=(),
    microphone=(), geolocation=()`.
  - `proxy_cookie_flags ~ secure samesite=lax;` → C4.
  - Yalnızca bilinen Streamlit yolları (`/`, `/_stcore/*`, `/static/*`,
    `/media/*`, `/component/*`) yukarı geçer; gerisi **gerçek 404**.
  - Gerçek `robots.txt` ve `sitemap.xml` statik dosya olarak.
  - `sub_filter '<html lang="en">' '<html lang="tr">'` → C6'nın `lang`
    kısmı (Streamlit'in `index.html`'i bizim değil, tek makul yer burası).
- Cloudflare tarafı yapılandırma olarak yazılamaz; `deploy/CLOUDFLARE.md`'ye
  adım adım not: Always Use HTTPS, Minimum TLS 1.2.

Test: `tests/test_proxy_config.py` — yapılandırma dosyasının gerekli
direktifleri içerdiğini doğrulayan metin testi (canlı testin yerini tutmaz,
ama ayarın geri alınmasını yakalar). Canlı doğrulama `curl -I` ile elle.

#### P0-4 · Kayıt redaksiyonu (B3)

- `store/redact.py`: `redact_url(url)` → `scheme://host/path`, sorgu
  dizesi atılır; token benzeri path parçaları (`[A-Za-z0-9_-]{24,}`)
  `…` ile maskelenir.
- **Yazma anında** uygulanır ([observability.py:141](store/observability.py:141)) —
  gösterim anında değil; aksi halde sır veritabanında durmaya devam eder.
- Sağlayıcı hata gövdeleri: kullanıcıya yalnızca sınıflandırılmış mesaj
  (`zaman aşımı` / `yetki` / `ayrıştırılamadı`), ham gövde dosya log'unda.
- `_short()` bu yeni fonksiyonu kullanır, kendi kırpmasını bırakır.
- Mevcut satırlar için tek seferlik migrasyon (`store/migrate.py`).

Test: `tests/test_redact.py` — sorgu dizeli VakıfBank URL'i, token'lı path,
zaten temiz URL.

---

### P1 — hesap doğruluğu; bunlar kapanmadan site finansal karar aracı değil

#### P1-1 · Tutar ayrıştırıcısı (A1)

`parse_amount` tek bir `int | None` döndürmeyi bırakır; sonuç nesnesi:

```python
@dataclass(frozen=True)
class AmountParse:
    value: int | None
    error: str | None      # kullanıcıya gösterilecek Türkçe metin
    note: str | None       # "1.234,56 → 1.235 olarak yuvarlandı"
```

Kurallar:

| Girdi | Sonuç |
|---|---|
| boş | `error="Tutar gerekli."` — varsayılana sessiz dönüş YOK |
| `abc`, `12a3` | `error="Yalnızca rakam girin."` |
| `-2500` | `error="Anapara negatif olamaz."` |
| `0` | `error="Anapara sıfırdan büyük olmalı."` |
| `1.234,56` / `1234,56` | 1235, `note` ile yuvarlama bildirimi |
| `1234.56` | 1235 — `.` son ayıraç ve ardında 1-2 hane varsa ONDALIK |
| `1.234.567` | 1234567 — `.` ardında tam 3 hane ve birden çok grup → basamak |
| `2,500,000` / `2 500 000` | 2500000 |

Ayıraç kararı: iki ayıraç birden varsa **sondaki ondalıktır**; tek ayıraç
varsa ardındaki hane sayısı karar verir (3 hane ve tek grup değilse basamak,
1-2 hane ise ondalık).

`principal_input()` hatayı alanın hemen altında `st.error` ile gösterir ve
**geçerli bir tutar yoksa `None` döndürür**; çağıran panel sonucu çizmeden
döner (A3 ile aynı desen).

Test: `tests/test_amount_parse.py` — yukarıdaki tablonun tamamı birim test,
artı `tests/test_integration_panels.py`'ye geçersiz girdide tablo
çizilmediğini doğrulayan senaryo.

#### P1-2 · Geçersiz girdide sonucu geçersizleştir (A3)

Ortak desen, üç panelde de aynı:

```python
tutar = principal_input("kredi")
if tutar is None:
    return                      # tablo, plan, grafik hiç çizilmez
```

Vade için `st.number_input`'un `min_value`/`max_value`'su **kaldırılır**
(Streamlit'in İngilizce hatasını üreten yer orası) ve doğrulama bize geçer:

```python
vade = st.number_input("Vade (ay)", value=12, step=1, format="%d")
if not 1 <= vade <= 360:
    st.error("Vade 1 ile 360 ay arasında olmalı.")
    return
```

Test: `tests/test_loan_validation.py` (zaten var) genişletilir — 0, -5, 361,
kesirli vade: kıyas tablosu ve ödeme planı **yok**.

#### P1-3 · Kademe etiketi (A2)

İki değişiklik:

1. `_tier_text()` kaldırılır; kademe **satır bazında** tabloya sütun olur:
   `"Tutar kademesi": "250.001 – 499.999 TRY"`. Böylece hangi vadenin hangi
   kademeden geldiği tek bakışta görünür ve üç kademenin virgülle yan yana
   yazılması diye bir durum kalmaz.
2. Kurum altyazısı tek kademe varsa onu yazar, birden çoksa
   "vadeye göre değişiyor — tabloda **Tutar kademesi** sütununa bak" der.
3. `_resolve_overlapping_tiers` davranışı (örtüşmede yüksek oranı seç)
   doğru ve belgeli; **değişmiyor** ama artık ekranda görünür hâle geliyor.

Test: `tests/test_deposit.py`'ye sınır testleri — `249999`, `250000`,
`250001`, `999999`, `1000000`, `1000001` için her `(kurum, döviz, vade)`
tam **bir** satır ve tam **bir** kademe etiketi.

#### P1-4 · Sağlık özetini iki sinyale böl (A5)

Bu bulgu tek satırlık bir metin değişikliği değil; eksik olan **veri yolu**:
üst şerit banka bazlı sonuçlara (`source_runs`) hiç bakmıyor.

- Yeni sorgu `queries.currently_broken()` — **pencere yok**. Her
  `(collector, source)` için son deneme; sonuncusu başarısızsa ve
  sonrasında başarı yoksa bozuk. Kesinti süresi hesabı için 30 günlük
  geçmiş okunur ama **listeye girme kararı pencereden bağımsızdır**.
- `logs._render_alerts()` artık `_recovery(days)` yerine bunu kullanır
  → 1/2/7/30 gün filtreleri listeyi değiştirmez ([logs.py:167](app/panels/logs.py:167)).
- `status._render_verdict()` iki ayrı sayı yazar:
  `"10/10 toplayıcı yaş bütçesi içinde · 3 alt kaynak son denemede başarısız"`.
- En az bir alt kaynak bozuksa grup rozeti yeşil kalamaz — `_worst()`
  hesabına alt kaynak durumu katılır.
- `v_freshness`'e dokunulmaz; alt kaynak sağlığı ayrı sorgu olarak gelir
  (view'i değiştirmek `status` ile `scheduler` eşiğini ayrıştırırdı).

Test: `tests/test_status_panel.py` + `tests/test_integration_observability.py` —
`loan_rates_llm` koşusu `ok`, alt kaynaklarından üçü başarısız: şerit yeşil
DEĞİL, sayı "3 alt kaynak" diyor; ve 1/2/7/30 gün filtrelerinde bozuk liste
aynı.

#### P1-5 · Tablo araç çubuğu katmanı ve dokunma hedefi (A4)

`app/panels/common.py`'de `principal_style()` yanına `table_toolbar_style()`:

```css
[data-testid="stElementToolbar"] { z-index: 10; }
[data-testid="stElementToolbar"] button { min-width: 44px; min-height: 44px; }
```

**Bu kural tarayıcıda doğrulanmadan kapatılmaz** (CLAUDE.md §5): dört
düğmenin merkezinde `document.elementFromPoint()` düğmeyi ya da çocuğunu
döndürmeli, `.dvn-scroller` değil. Testid adları Streamlit sürümüne bağlı;
önce `requirements.txt`'teki sürümde denetlenecek.

#### P1-6 · Uygun olmayan kredi ürünleri sıralamadan çıkar (A7)

`comparisons` ikiye ayrılır:

- `uygun` → mevcut tablo, `sort_values("Taksit")`.
- `uygun_degil` → kapalı bir `st.expander("Bu koşullarda uygun olmayan
  ürünler (n) — neden?")`, her satırın yanında `_limit_warning()` metni.
- "Ödeme planını göster" seçicisi varsayılan olarak yalnızca uygun
  ürünleri listeler.

Test: `tests/test_loan_coverage.py` — 1.000.000 TL / 360 ay senaryosunda
250.000 TL üst sınırlı ürün ana tabloda **yok**, uygun olmayanlar bloğunda
**var**.

#### P1-7 · Fon alanı ile sonucu bağla (A6)

- Sonuç bölümünün başlığı hesaplanan koddan yazılır:
  `st.markdown(f"#### {fund['code']} — {fund['name']}")`. Alanda ne yazarsa
  yazsın, ekranda hangi fonun hesaplandığı görünür. Bu, Streamlit'in
  combobox davranışından bağımsız olarak doğruyu garanti eder.
- `_render_add_fund()` negatif sonucu kısa süreli önbelleğe alır
  (`@st.cache_data(ttl=300)`), böylece aynı geçersiz kod ikinci kez 9
  saniye beklemez (KKT F-10).
- Arama sırasında hangi sağlayıcının denendiği `st.status` ile yazılır.

---

### P2 — erişilebilirlik, dil ve şeffaflık

| # | İş | Yer |
|---|---|---|
| P2-1 | `st.subheader` (h3) → sekme başlıkları `st.header` (h2); kurum blokları `###` kalır. h1→h2→h3 düzelir. | tüm paneller |
| P2-2 | Seçili sekme kontrastı 3,30:1 → ≥4,5:1. CSS `[data-baseweb="tab"][aria-selected="true"]`. | `common.py` |
| P2-3 | Erişilebilirlik ağacındaki ham float (`1.3500000000000014`): `st.dataframe`'e ham `df` yerine `column_config` ile biçimlenmiş sütunlar. `df.style.format` yalnızca GÖRSELİ biçimliyor, erişilebilirlik ağacına ham değer gidiyor — asıl neden bu. | `deposit.py`, `loan.py`, `fund.py`, `fx.py` |
| P2-4 | Fon stopaj varsayımı görünür (GÜV F-11): `resolve_fund_withholding()` sonucu tablonun yanında "Stopaj %X, fon sınıfı Y" olarak yazılır. Veri zaten var, gösterilmiyor. | [fund.py:171](app/panels/fund.py:171) |
| P2-5 | Ortak anapara açıkça söylenir (KKT F-08): kutunun altında "Bu tutar dört sekmede ortaktır" + "Varsayılana dön" düğmesi. | [common.py:279](app/panels/common.py:279) |
| P2-6 | `st.set_page_config`'e `page_icon` + favicon; ilk yüklemede skeleton. | [main.py:16](app/main.py:16) |
| P2-7 | Tablo araç düğmelerinin İngilizce erişilebilir adları — Streamlit içi; CSS/JS ile aria-label ezmek kırılgan. **Önerim: şimdilik kayıt altına al, üst sürümü bekle.** Diğer P2'ler gibi kapatılabilir bir iş değil. | — |

---

### P3

- CSV indirmenin Chrome/Firefox/Safari'de uçtan uca E2E testi (araç çubuğu
  düzeldikten sonra anlamlı olur).
- Mobilde ana başlık için `clamp()`.
- Paylaşılabilir durum: sekme ve filtreler `st.query_params`'a.

---

## 3. Raporlarda düzeltilmesi gereken noktalar

Bunları not ediyorum ki plan yanlış bir teşhisin üstüne kurulmasın:

1. **KKT F-02'nin teşhisi yanlış.** "Alt ve üst sınırların ikisinin de dahil
   değerlendirilmesi" denmiş; kod bunu zaten çözüyor
   ([queries.py:148](store/queries.py:148)) ve davranışı belgeli. Ekrandaki
   üç kademe bir **altyazı birleştirme** hatası. Raporun kabul kriteri
   ("eşleşen kademe sayısı 1 olmalı") bugün zaten sağlanıyor; eksik olan
   "kullanılan kademe satır bazında görünmeli".
2. **KKT F-01'de `0` ayrı bir vaka.** Rapor "hesap devam ediyor" diyor;
   kodda `_annualized_net_pct` `principal <= 0` için `0.0` döndürüyor, yani
   tablo çiziliyor ve her satır %0,00 gösteriyor. Bu bir hesap hatası değil,
   **doğrulama eksiği** — çözüm de ayrıştırıcıda, hesapta değil.
3. **GÜV F-06 (`Secure` çerez) için `.streamlit/config.toml` yok.**
   Streamlit tamamen varsayılanlarla koşuyor. Çözüm hem o dosyayı eklemek
   hem de proxy'de `proxy_cookie_flags` kullanmak — ikisi birbirinin yedeği.
4. **GÜV F-01/02/05/07 depoda doğrulanamaz.** Depoda proxy yapılandırması
   yok; bulgular canlı sitede ölçülmüş. Bu yüzden P0-3 bir "düzeltme" değil,
   **eksik katmanı yaratma** işi.
5. **Rapor "kalıcı kaydetme gerçekten çalışıyorsa kritik" diyor — çalışıyor.**
   `credentials.save()` ([credentials.py:229](llm/credentials.py:229))
   hiçbir yetki kontrolü yapmadan veritabanına yazıyor; panelde silme
   düğmesi de bilinçli olarak yok, yani yetkisiz yazılan bir anahtarı ancak
   sunucuya erişen biri kaldırabilir. Etki, raporun tahmininden bir adım
   daha kötü.

---

## 4. Regresyon paketi

Her dağıtımda koşacak minimum set (GÜV §8 ile hizalı, bu depoya uyarlandı):

**Birim (`venv/bin/pytest -q`)**

1. `test_amount_parse.py` — P1-1 tablosunun tamamı.
2. `test_urlguard.py` — P0-2'deki sekiz red vakası.
3. `test_redact.py` — sorgu dizesi ve token maskesi.
4. `test_admin_gate.py` — jetonsuz yazma `PermissionError`.
5. `test_deposit.py` — altı sınır tutarı, tek kademe.
6. `test_loan_validation.py` — geçersiz vadede sonuç yok.
7. `test_status_panel.py` — alt kaynak bozukken şerit yeşil değil.
8. `test_integration_observability.py` — 1/2/7/30 gün filtresinde bozuk
   liste değişmez.

**Tarayıcı (elle ya da Playwright)**

9. Dört tablo aracının merkezinde `elementFromPoint` düğmeyi döndürür.
10. 390 / 768 / 1280 px'te yatay taşma yok.
11. `axe-core`: `lang`, başlık sırası, kontrast, tablo başlık ilişkisi.
12. CSV indirme: dosya adı, MIME, UTF-8 Türkçe karakter, satır sayısı.

**Canlı (`curl`)**

13. `http://` → 301/308.
14. TLS 1.0/1.1 başarısız, 1.2/1.3 başarılı.
15. Altı güvenlik başlığı HTML yanıtlarında var.
16. `/bu-sayfa-yok` → gerçek 404; `/robots.txt` → gerçek robots.

---

## 5. Uygulama sırası

```
P0-1 yönetici kapısı ──┐
P0-2 SSRF ─────────────┼─→ aynı dal: yetki ve sır yüzeyi
P0-4 redaksiyon ───────┘
P0-3 proxy ──────────────→ ayrı dal: altyapı (depo dışı doğrulama gerekir)
P1-1 tutar ────────────┐
P1-2 geçersizleştirme ─┼─→ aynı dal: P1-2 P1-1'in döndürdüğü None'a bağlı
P1-3 kademe ───────────┤
P1-6 uygun olmayanlar ─┤
P1-7 fon ──────────────┘
P1-4 sağlık ─────────────→ ayrı dal: sorgu + iki panel
P1-5 araç çubuğu ────────→ ayrı dal: tarayıcı doğrulaması şart
P2 ──────────────────────→ tek dal, toplu
```

P1-1 ile P1-2 aynı değişikliktir (`None` dönüşü ve çağıranların erken
dönüşü); ayrı commit'lenirse ara durumda paneller patlar.
