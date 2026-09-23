# Bahis Kuponu Asistanı

Premier League, La Liga, Bundesliga, Serie A, Ligue 1, Süper Lig, Şampiyonlar
Ligi, Avrupa Ligi, Konferans Ligi ve Uluslar Ligi için Poisson gol modeli,
value betting ve Kelly kriterine dayalı kupon önerileri üreten, önerileri
100 üzerinden puanlayan kişisel bir araç.

Not: Şampiyonlar Ligi/Avrupa Ligi/Konferans Ligi/Uluslar Ligi grup+eleme
usulü ("Cup") turnuvalardır; bir takımın bu turnuvadaki maç sayısı domestik
lige göre daha azdır, bu yüzden "Güvenilir Örneklem" sütunu bu liglerde
daha sık "Hayır" görünebilir (bkz. `config.MIN_MATCHES_FOR_CONFIDENCE`).

Bu araç istatistiksel tahmin sunar, **kazanç garantisi vermez**. Sorumlu
bahis ilkelerine uyun.

## 1. Ücretsiz API key alma (API-Football / api-sports.io)

1. https://www.api-football.com/ (ya da api-sports.io) adresine gidin ve
   ücretsiz bir hesap oluşturun.
2. Hesap panelinizde (Dashboard) size özel API key'i kopyalayın. Ücretsiz plan
   günde 100 istek ile sınırlıdır — bu araç istekleri diske cache'leyerek bu
   kotayı korur.
3. Alternatif olarak RapidAPI üzerinden "API-Football" servisine abone
   olabilirsiniz; bu durumda `api_football.py` içindeki header'ı RapidAPI'nin
   istediği `x-rapidapi-key` / `x-rapidapi-host` ile değiştirmeniz gerekir
   (varsayılan kurulum doğrudan api-sports.io key'i içindir).

## 2. Kurulum

```powershell
cd C:\Users\gebze\Desktop\bahis
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env   # API_FOOTBALL_KEY=... satırına key'inizi yapıştırın
```

## 3. Çalıştırma

```powershell
streamlit run app.py
```

Tarayıcıda açılan arayüzden ligleri seçin, "Verileri Getir ve Analiz Et"
butonuna basın.

## Nasıl çalışıyor?

1. **Veri**: API-Football'dan seçilen liglerdeki yaklaşan maçlar, takımların
   **son N maçı** (last-fixtures, sezon sınırını aşabilir) ve TÜM bahis
   şirketlerinin oranları (1X2, alt/üst 2.5, karşılıklı gol) çekilir.
   İstekler `cache/` klasöründe plan moduna göre değişen TTL ile saklanır.
2. **Model — zaman ağırlıklı Poisson + Dixon-Coles**: Sezon ortalaması yerine
   takımın son `LAST_N_MATCHES` maçından, her maça `exp(-ln2·gün/yarı_ömür)`
   ağırlığı verilerek (Dixon & Coles 1997 tarzı) hücum/defans gücü çıkarılır;
   bu sayede eski sezona sarkan maçlar otomatik önemsizleşir ve sezon başı
   için ayrı bir kural gerekmez. Düşük skorlu sonuçlara (0-0, 1-0, 0-1, 1-1)
   aynı makaledeki rho düzeltmesi uygulanır (bağımsız Poisson'un beraberlik
   olasılığını olduğundan düşük tahmin etme eğilimini giderir).
3. **Elo + xG-Elo ensemble**: Her takım için, sonuçlanan her maçtan sonra
   güncellenen bir Elo reytingi (`elo.py`) tutulur ve buradan türetilen 1X2
   olasılığı Poisson modeliyle harmanlanır. Ayrıca API-Football'ın
   `/fixtures/statistics` uç noktasından gelen **xG (expected goals)**
   verisiyle güncellenen AYRI bir "xG-Elo" reytingi de tutulur — araştırma
   bulgusu: ham xG'yi doğrudan özellik olarak kullanmak modeli zayıflatıyor,
   ama bir Elo'nun kalibrasyonuna beslendiğinde ("şans hariç performans"
   sinyali) işe yarıyor. Bir takımın xG-Elo'su `XG_ELO_MIN_MATCHES` maça
   ulaşınca devreye girer (normal Elo'dan daha yüksek eşik — xG verisi her
   ligde/maçta bulunmayabilir); o zaman ağırlıklar Poisson %55 / Elo %25 /
   xG-Elo %20'ye kayar, hazır değilse Poisson %65 / Elo %35 ikili harman
   kullanılır (`ENSEMBLE_POISSON_WEIGHT`, `ENSEMBLE_ELO_WEIGHT_WITH_XG`,
   `ENSEMBLE_XG_ELO_WEIGHT`). Soğuk başlangıçta (yetersiz maç) ilgili sinyal
   sıfır ağırlıklıdır — sistem birkaç hafta kullanıldıkça kendiliğinden
   devreye girer.
   - **Ev sahibi avantajı artık lig bazında kalibre edilir**: sabit +80 Elo
     puanı yerine, bir ligin en az `LEAGUE_HOME_ADV_MIN_MATCHES` sonuçlanmış
     maçı birikince, o ligin kendi ev sahibi kazanma/beraberlik oranından
     ampirik bir Elo eşdeğeri hesaplanır (`elo.compute_league_home_advantage`).
   - **Dinlenme günü / maç sıkışıklığı (yorgunluk)**: ekstra API isteği
     gerektirmeden (zaten çekilmiş son-maç verisinden), bir sonraki maça
     kaç gün kaldığı ve son 14 günde kaç maç oynandığı hesaba katılıp
     takım gücüne küçük bir düzeltme uygulanır (`compute_fatigue_adjustment`).
   - **Dixon-Coles rho'nun veriden öğrenilmesi**: "📈 Geçmiş Performans"
     sekmesindeki "Modeli Şimdi Kalibre Et" butonu (veya scheduler'ın
     periyodik döngüsü), en az `DIXON_COLES_FIT_MIN_MATCHES` sonuçlanmış
     maç birikince, rho'yu gerçek skorlara karşı log-likelihood'u
     maksimize eden değere göre (grid search) yeniden hesaplar ve DB'ye
     kaydeder — sonraki analizler otomatik kullanır. Zaman ağırlığının
     yarı ömrü (decay) ise lambda hesabının kendisini etkilediğinden
     dolaşık bir yeniden-tahmin gerektirir; bu tur kapsam dışı bırakıldı
     (bilinen sınırlama).
4. **Value betting — konsensüs + en iyi fiyat**: Tek bir bahis şirketi yerine
   TÜM şirketlerin oranlarından **power method** ile (Shin's method'a göre
   daha basit ve ampirik olarak en az o kadar iyi performans gösterdiği için
   tercih edildi) komisyon arındırılmış olasılıklar çıkarılır ve ortalaması
   (konsensüs/piyasa olasılığı) edge hesabında kullanılır.
   - **Tekli öneriler** ("Tekli Öneriler" sekmesi): her seçim için taranan
     şirketler arasındaki **en iyi fiyattan** (line shopping) önerilir —
     bağımsız tekli bahisler farklı sitelerde ayrı ayrı oynanabildiğinden
     bu geçerlidir.
   - **Kombine kuponlar** ("Kombine Kupon" sekmesi, bültenler, kayıtlı
     analizler): bir kombine kupon fiziksel olarak **tek bir sitede**
     oynanabilir — farklı şirketlerin oranları aynı kuponda birleştirilemez.
     Bu yüzden `coupon_builder.build_combo`, her şirketi ayrı ayrı deneyip
     (o şirketin kendi oranlarıyla) en iyi tam-bacaklı kombinasyonu bulan
     şirketi seçer; sonuç garantili olarak tek şirkete aittir (kombine
     kupon ekranında hangi şirket olduğu ayrıca gösterilir).
5. **Kelly kriteri**: Her seçim için %25 kesirli Kelly (bankroll'un en fazla
   %5'i ile sınırlı) önerilen bahis miktarını hesaplar.
6. **Puanlama (0-100)**: Edge büyüklüğü, oran aralığı (1.50-3.50 "tatlı
   nokta"), Kelly/bankroll uygunluğu ve örneklem güveni (zaman ağırlıklı
   "etkin" örneklem >= eşik) bileşenlerinden oluşan sezgisel bir puan
   hesaplanır. Kombine kuponlarda ayrıca bacak sayısı arttıkça ceza, aynı
   maçtan tekrar seçim (korelasyon riski) cezası ve farklı lig/maça yayılma
   bonusu uygulanır.
7. **CLV (closing line value) takibi**: "Geçmiş Performans" sekmesinde,
   loglanan tahminlerin oranı ile maça yakın (kapanış) oranı karşılaştırılıp
   CLV% hesaplanır — profesyonel bahisçilerin kısa vadeli kazanma oranından
   daha güvenilir buldukları, piyasayı gerçekten yenip yenmediğinizin uzun
   vadeli göstergesi.

## Free / Pro plan modu

Sol menüdeki **"API-Football planı"** seçimi, uygulamanın kota-koruma
davranışını çalışma anında değiştirir (`config.set_plan()`):

| | Ücretsiz | Pro |
|---|---|---|
| Günlük istek | 100 | 7500 |
| Lig başına maç tarama üst sınırı | 10 | 20 |
| Cache süreleri (TTL) | uzun (6-48 saat) | kısa (0.1-6 saat) — veri hep taze |
| Eksik oyuncu analizi | varsayılan kapalı | varsayılan açık |
| "Cache'i yoksay" | varsayılan kapalı | varsayılan açık |

Plan seçimi yalnızca arayüz/istemci tarafındaki davranışı değiştirir; hangi
plana gerçekten abone olduğunuz api-sports.io hesabınızdaki key ile
belirlenir. Pro planda sunucu tarafında zaten 100/gün kısıtı olmadığından
bu mod tüm ligleri, tüm maçları ve eksik oyuncu analizini kota kaygısı
olmadan aynı anda çalıştırmanıza izin verir.

## Analizleri kaydetme, geri çağırma ve Telegram'a manuel paylaşım

"Verileri Getir ve Analiz Et" sonrası görünen **"💾 Bu analizi isimle kaydet"**
kutusuyla, o anki analizi (tüm value-bet önerileri + 1-7 bacaklı kupon
varyantlarının tamamı) bir isimle kalıcı olarak DB'ye kaydedebilirsiniz.
`log_to_history` açık olsa da olmasa da çalışır; zaten loglanmış tahminler
varsa yinelenen satır oluşturmaz (aynı tahmin id'leri yeniden kullanılır).

**"💾 Kayıtlı Analizler"** sekmesinden geçmişte kaydettiğiniz analizleri
isimleriyle listeleyip tekrar ekrana getirebilir, sonuçlanmış tahminlerin
tuttu/tutmadığını görebilir ve her kupon varyantı için ayrı ayrı
**"📤 Telegram'a Gönder"** butonuna basarak kanala paylaşabilirsiniz. Aynı
gönder butonu, "🎟️ Kombine Kupon" sekmesinde o an ekranda duran (henüz
kaydedilmemiş) kupon için de mevcuttur.

## Türkiye oranı simülasyonu (İddaa entegrasyonu tamamlanana kadar geçici)

Gerçek İddaa oranları (nosyapi.com üzerinden, bkz. `iddaa_api.py`) hesap
aktivasyonu beklerken, sol menüde "🇹🇷 Türkiye Oranlarını Simüle Et"
seçeneğiyle gösterilen oran/EV/Kelly, gerçek global fiyattan tahmini bir
yüzde kadar küçültülüp gösterilebilir. Bu yüzde, kullanıcının gerçek
bet365/Bilyoner karşılaştırmasından (3 maç, ortalama ~%10.7 fark) türetildi
ve arayüzden ayarlanabilir. **Value tespiti (edge) etkilenmez** — yalnızca
gerçekte oynanacak fiyatı ve buna bağlı EV/Kelly stake'i daha gerçekçi
gösterir. Bu bir TAHMİNDİR, gerçek veri değildir; şirket adının yanında
`[TR sim.]` ile işaretlenir.

## Backtest modu

Sol menüde "Veri Modu" → "Geçmiş sezon testi (backtest)" seçilirse, gerçek
zamanlı (`next_n`) maçlar yerine `config.SEASON` içindeki GEÇMİŞ bir
sezondan tamamlanmış maçlar çekilir; sonuç zaten bilindiğinden model
isabeti anında görülebilir. **Önemli:** api-sports.io'nun ücretsiz planı
hem güncel sezona (yalnızca ~2022-2024 arası sezonlara izin verir) hem de
`last` parametresine (backtest'in takım formu hesaplamak için kullandığı)
erişemiyor — bu yüzden backtest modu da pratikte **Pro plan gerektirir**.
Lookahead bias'ı (gelecek verisinin sızması) önlemek için takım formu,
analiz edilen maçın tarihinden bir gün öncesine kadar olan maçlarla
hesaplanır; Elo ve oyuncu eksikliği modülleri (yalnızca "şu anki" veriyi
tuttuklarından) bu modda otomatik devre dışı kalır.

## Bülten saatlerini/liglerini yapılandırma

Sol menüden **"⚙️ Telegram Ayarları"** görünümünü seçerek günlük ve haftalık
bültenlerin hangi saatte, hangi liglerle gönderileceğini değiştirebilirsiniz
(varsayılan: günlük 23:55, haftalık Pazartesi 10:00, tüm ligler). Ayarlar
DB'ye (`settings` tablosu) kaydedilir; ayrı bir süreç olarak çalışan
`scheduler.py`, bu ayarları her turda (en geç `SCHEDULER_TICK_SECONDS`
saniyede bir) yeniden okur — **zamanlayıcıyı yeniden başlatmanıza gerek
yoktur**, değişiklik en geç 1 dakika içinde devreye girer. Her iki bülten
türü de ayrı ayrı açılıp kapatılabilir.

## Telegram bülten sistemi ve 7/24 zamanlayıcı

Uygulama, PC'nizi sunucu olarak kullanıp kuponları otomatik bir Telegram
kanalına gönderecek şekilde tasarlandı.

### Kurulum

1. Telegram'da **@BotFather**'a `/newbot` yazarak bir bot oluşturun, verdiği
   **token**'ı not edin.
2. Kuponların gönderileceği bir Telegram **kanalı** oluşturun, botu kanala
   **admin** olarak ekleyin.
3. Kanala bir mesaj atıp `https://api.telegram.org/bot<TOKEN>/getUpdates`
   adresini tarayıcıda açarak `"chat":{"id":-100...}` değerini bulun.
4. `.env` dosyanıza ekleyin:
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=-100...
   ```
5. Streamlit arayüzünde **"🤖 Telegram Bülten"** sekmesinden "Bağlantıyı Test
   Et" ile kurulumu doğrulayın, isterseniz aynı sekmeden günlük/haftalık
   bülteni elle tetikleyip test edin.

### Nasıl çalışır (`scheduler.py`)

PC'nizi 7/24 açık tutup arka planda çalıştırın:

```powershell
python scheduler.py
```

Bu script her dakika saati kontrol eder:

- **Günlük bülten** (`config.DAILY_BULLETIN_HOUR:MINUTE`, varsayılan
  **23:55**): seçili liglerde YARIN maç varsa, o maçlardan `config.BULLETIN_LEG_COUNTS`
  (varsayılan 1-7) bacak sayısında kupon varyantları üretir ("🔒 Günün
  Bankosu" = 1 maçlık, ta 7'li kombineye kadar) ve Telegram'a gönderir.
- **Haftalık bülten** (`config.WEEKLY_BULLETIN_WEEKDAY/HOUR/MINUTE`,
  varsayılan **Pazartesi 10:00**): önündeki 7 günün TÜM maçlarını tarar;
  farklı günlerdeki maçları aynı kuponda birleştirebilir.
- **Sonuç/CLV senkronizasyonu** (`config.SCHEDULER_RESULT_SYNC_MINUTES`,
  varsayılan 3 saatte bir): `history.sync_results()` ve
  `history.update_closing_odds()` otomatik çalışır — kupon tutsun tutmasın
  sonuç DB'ye kalıcı olarak işlenir, Elo güncellenir.

Daha kalıcı bir kurulum isterseniz `scheduler.py`'yi Windows Görev
Zamanlayıcı'da "sistem başlangıcında/oturum açılışında çalıştır" olarak
ayarlayabilirsiniz (Görev Zamanlayıcı → Temel Görev Oluştur → Tetikleyici:
"Oturum açıldığında" → Eylem: `pythonw.exe` ile `scheduler.py`'yi başlat).

### "Sadece değişiklikleri kullan" — API isteği israfını önleme

- Her (bülten tipi, tarih) çifti için DB'de en fazla bir bülten kaydı
  oluşur (`bulletins` tablosu, `UNIQUE(bulletin_type, target_date)`).
  Zamanlayıcı yeniden başlasa veya aynı tetikleyici dakikada iki kez tikse
  bile, o gün için bülten zaten varsa **hiçbir API isteği yapılmadan**
  atlanır.
- Normal cache TTL sistemi (bkz. "Free / Pro plan modu") aynı fikstür/oran/
  form verisinin kısa aralıklarla tekrar tekrar çekilmesini zaten önler.
- Bir kuponun bacakları (ör. 1'li, 2'li, ..., 7'li varyantlar) aynı
  tahminleri paylaştığında, o tahminler yalnızca BİR KEZ `predictions`
  tablosuna yazılır (`history.log_predictions` + `history.log_coupon`
  ayrımı) — yinelenen satır oluşmaz.

### Kuponlarda kullanılan mantığın DB'ye kaydı

Her kupon, `coupons.technique_tags` sütununda (JSON liste) hangi
tekniklerin fiilen kullanıldığını taşır: `value_edge`, `power_method_devig`,
`multi_bookmaker_consensus`, `dixon_coles`, `time_weighted_form`,
`kelly_criterion`, ayrıca o kupon için geçerliyse `elo_ensemble`,
`player_availability`, `league_diversification`, `banko_single_pick`.
Kupon tutsun ya da tutmasın (`predictions.correct`, `predictions.clv_pct`
üzerinden), tüm bu veriler kalıcı olarak saklanır — zamanla "hangi teknik
kombinasyonu gerçekten daha isabetli" sorusu bu tablodan cevaplanabilir.

## Öğrenen sistem: geçmiş kayıt ve oyuncu eksikliği (gelistirme-plani.md)

`gelistirme-plani.md` içindeki üç fikir uygulandı:

- **Kalıcı veritabanı** (`db.py`, `bahis.db` — SQLite, tek dosya): her analiz
  çalıştırmasında üretilen tekli öneriler ve kombine kupon önerileri
  kaydedilir (sol menüde "Bu analizi geçmişe kaydet" açık olmalı).
- **Sonuç eşleme ve kalibrasyon** (`history.py`): "📈 Geçmiş Performans"
  sekmesindeki "Sonuçlanan Maçları Kontrol Et" butonu, bekleyen tahminlerin
  maçlarını API'den kontrol eder, biteni `results` tablosuna yazar ve
  isabet durumunu işaretler. Aynı sekmede puan aralığına göre gerçek isabet
  oranı ve Brier skoru görülür — puanlama sisteminin gerçekten işe yarayıp
  yaramadığının kanıtı zamanla burada birikir.
- **Oyuncu eksikliği etkisi** (`player_availability.py`): sol menüde "Eksik
  oyuncu etkisini modele dahil et" açılırsa, her maç için sakat/cezalı
  oyuncular (`/injuries`) ve takımın sezonluk oyuncu istatistikleri
  (`/players`) çekilir; oynama süresi oranı × mevki ağırlığı × (oyuncu
  reytingi / takım ortalaması) formülüyle bir önem skoru hesaplanıp takımın
  hücum/defans gücüne en fazla %25 düzeltme uygulanır. **Bu özellik ekstra
  API isteği tüketir** (maç başına 1 `/injuries` + takım başına cache'lenen
  `/players`); ücretsiz 100 istek/gün kotasıyla aynı anda çok sayıda ligi
  taramak yerine, önce standart analizle daralttığınız birkaç maçta
  kullanmanız önerilir.

Pratik basitleştirme: oynama süresi "son N hafta" yerine o ana kadarki
**sezon ortalaması** olarak hesaplanır (haftalık pencere, her geçmiş maç
için ayrı bir API çağrısı gerektirdiğinden kotayı hızla tüketir).

## Sınırlamalar

- Ücretsiz API planı günde 100 istekle sınırlı; ayrıca **güncel sezona** ve
  **`last` parametresine** erişemiyor (API'nin kendi hata mesajlarından
  doğrulandı) — bu da "yaklaşan maç" ve backtest senaryolarının ikisini de
  pratikte kullanılamaz kılıyor. Gerçek kullanım (canlı kupon + Telegram
  bülteni) için **Pro plan gerekiyor**.
- Lig ortalaması, o ana kadar cache'lenmiş takımların ortalamasından
  hesaplanır (tüm lig taranmadığı için kaba bir yaklaşımdır).
- Dixon-Coles rho parametresi ve zaman ağırlığının yarı ömrü, lig başına ayrı
  ayrı MLE ile kalibre edilmez; literatürden alınan sabit değerler kullanılır
  (`config.DIXON_COLES_RHO`, `config.TIME_DECAY_HALF_LIFE_DAYS`).
- Elo reytingi, sistem gerçek sonuçları senkronize ettikçe (her takım için
  en az `ELO_MIN_MATCHES` maç) birikir; ilk kullanımda katkısı sıfırdır.
- Elo tek, lig bağımsız global bir reyting olarak tutulur (ligler arası tam
  kalibrasyon yapılmaz) — pratikte sorun yaratmaz çünkü karşılaştırmalar
  hep aynı lig içindeki takımlar arasındadır.
- Model; sakatlık/ceza etkisini (opt-in modül hariç) ve motivasyon, hava
  durumu gibi faktörleri hesaba katmaz.
