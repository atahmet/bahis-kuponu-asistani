# Bahis Öneri Platformu — Teknik ve Algoritmik İnceleme

## Genel Değerlendirme

Proje; olasılık modeli, piyasa analizi, backtest, kalibrasyon ve otomatik raporlama altyapısını bir araya getiren kapsamlı bir sistem haline gelmiş durumda.

Ancak teknik olarak çalışan bir modelin matematiksel olarak doğru sonuç ürettiği veya uzun vadede kârlı olduğu varsayılmamalıdır.

Öncelikli inceleme alanları:

1. Model olasılıklarının güvenilirliği ve kalibrasyonu
2. Piyasa oranlarının doğru kullanılması
3. Backtest'te lookahead bias ve veri sızıntısı
4. Kombine kuponlarda korelasyon ve beklenen değer
5. 0–100 puanın tahmin kalitesiyle karıştırılmaması

> Bu değerlendirme, proje özetine dayalıdır. Kodların tamamı incelenmeden kesin kod hatası tespiti yapılamaz.

---

## 1. Mevcut Algoritmadaki Kritik Noktalar

### 1.1 Bağımsız Poisson Varsayımı

1X2, Alt/Üst ve KG olasılıklarının bağımsız Poisson dağılımından üretilmesi, futbol maçlarındaki gol bağımlılıklarını tam olarak yansıtmayabilir.

Dixon–Coles düzeltmesi bu sorunun bir kısmını ele alır; ancak bağımsız Poisson varsayımı tamamen ortadan kalkmaz.

**Öneri:**

- Bivariate Poisson modelini deneysel olarak test et.
- Bağımlı marketler için ortak gol dağılımı kullan.
- Bağımsız Poisson ve alternatif modelleri out-of-sample metriklerle karşılaştır.

### 1.2 Son 10 Maç ve 200 Günlük Yarı Ömür

Son 10 maç ile 200 günlük zaman ağırlığının birlikte kullanılması, takımların maç sıklığına göre farklı örneklem yapıları oluşturabilir.

Örneğin:

- Son 10 maç 45 günde oynanmışsa ağırlıklar birbirine yakın olabilir.
- Son 10 maç 120 günde oynanmışsa eski maçlar daha fazla zayıflar.
- Sezonlar arası veriler kullanılıyorsa kadro ve teknik direktör değişiklikleri modele yeterince yansımayabilir.

Denenecek alternatifler:

- Son 5 maç
- Son 10 maç
- Son 20 maç
- Tüm uygun geçmiş maçlar + zaman decay

Model seçimi yalnızca isabet oranına göre değil, aşağıdaki metriklere göre yapılmalı:

- Out-of-sample log loss
- Brier score
- Kalibrasyon
- ROI ve CLV gibi ikincil performans metrikleri

---

## 2. Poisson ve Dixon–Coles Modeli

### 2.1 Hücum ve Savunma Ortalamalarının Tutarlılığı

Kullanılan formüller:

```python
lambda_home = league_home_avg * home_attack * away_defense
lambda_away = league_away_avg * away_attack * home_defense
```

Ev/deplasman defans oranlarının referans ortalamalarıyla tutarlı olması gerekir.

Kontrol edilmesi gereken yapı:

| Parametre | Referans |
|---|---|
| Ev sahibi hücum | Lig ev sahibi gol ortalaması |
| Ev sahibi savunma | Lig deplasman gol ortalaması |
| Deplasman hücum | Lig deplasman gol ortalaması |
| Deplasman savunma | Lig ev sahibi gol ortalaması |

Özetindeki yapı ilk bakışta tutarlı görünse de kod seviyesinde şu noktalar doğrulanmalı:

- Ev ve deplasman ortalamalarının doğru tanımlanması
- Lig ortalamasının yalnızca tahmin tarihinden önceki verilerden hesaplanması
- Takımın ev/deplasman ayrımının doğru yapılması
- Sezonlar arası verilerin uygun şekilde ağırlıklandırılması

### 2.2 Tam Dixon–Coles Modeline Geçiş

Mevcut modelde hücum ve savunma gücü basit ortalamalarla hesaplanıyor, ardından `rho` parametresi öğreniliyor.

Tam Dixon–Coles yaklaşımında hücum ve savunma parametreleri de zaman ağırlıklı likelihood üzerinden optimize edilebilir.

Önerilen akış:

```text
Maç geçmişi
    ↓
Zaman ağırlıkları
    ↓
Hücum / savunma parametreleri
    ↓
Dixon–Coles likelihood
    ↓
Parametre optimizasyonu
    ↓
Gol olasılık matrisi
```

Öneri:

- Tam modeli mevcut modelin yerine hemen koyma.
- Ayrı bir deneysel model olarak geliştir.
- Mevcut modelle out-of-sample karşılaştır.
- Parametrelerin aşırı uyum gösterip göstermediğini kontrol et.

### 2.3 Rho Parametresinin Öğrenilmesi

Mevcut yaklaşım:

- En az 100 maç
- `-0.30` ile `0.10` aralığı
- `0.01` adımlı grid search
- Log-likelihood maksimizasyonu

Kontrol edilmesi gereken noktalar:

- 100 maçın ilgili ligi ve dönemi temsil edip etmediği
- Optimum değerin aralık dışında bulunup bulunmadığı
- Zaman ağırlığının kullanılıp kullanılmadığı
- Lambda parametrelerindeki hataların `rho` tarafından telafi edilip edilmediği
- Lig ve sezon bazlı farklılıklar

Öneriler:

- Zaman ağırlıklı likelihood kullan.
- Lig veya dönem bazlı parametreleri karşılaştır.
- Bootstrap veya benzeri yöntemlerle belirsizlik tahmini yap.
- Grid aralığının yeterli olup olmadığını kontrol et.

---

## 3. Value Betting ve Piyasa Analizi

### 3.1 Edge Hesabı

Mevcut formül:

\[
edge = p_{model} - p_{market}
\]

Bu, olasılık farkı açısından doğru bir tanımdır. Ancak tek başına kârlılığı garanti etmez.

Adil oran:

\[
fair\ odds = \frac{1}{p_{model}}
\]

Beklenen getiri:

\[
EV = p_{model} \times odds - 1
\]

Örnek:

| Değer | Sonuç |
|---|---:|
| Model olasılığı | %55 |
| Piyasa adil olasılığı | %50 |
| Edge | +%5 |
| Bahis oranı | 2.00 |
| EV | +%10 |

Model olasılığı hatalıysa edge ve EV de hatalı olacaktır. Bu nedenle model kalibrasyonu, value hesabının temel güvenlik katmanıdır.

### 3.2 Raw Implied Probability ve Fair Probability Ayrımı

Kodda bu iki kavram kesin şekilde ayrılmalı:

```python
raw_implied_prob = 1 / odds
fair_prob = devig(raw_implied_prob)
```

Ham implied probability ile devig edilmiş fair probability birbirinin yerine kullanılmamalıdır.

### 3.3 Çoklu Şirket Konsensüsü

Mevcut sistemde her şirketin oranı ayrı devig ediliyor ve ortalaması alınıyor.

Potansiyel sorun:

- Tüm şirketler eşit kalitede bilgi kaynağı değildir.
- Oranların güncellenme zamanları farklı olabilir.
- Market kapsamı ve likidite farklılık gösterebilir.
- Aynı piyasa verisinin kopyaları bağımsız gözlem değildir.

Önerilen metadata:

- Şirket adı
- Oranın alındığı tarih ve saat
- Market türü
- Oran güncelleme zamanı
- Maç başlama zamanına kalan süre
- Oranın geçerlilik durumu
- Oran değişim geçmişi

Karşılaştırılabilecek yöntemler:

1. Basit ortalama
2. Ağırlıklı ortalama
3. En iyi fiyat
4. Kalibrasyon performansına göre ağırlıklandırma

Hangi yöntemin kullanılacağı out-of-sample kalibrasyon sonuçlarına göre belirlenmelidir.

### 3.4 Türkiye Oranı Simülasyonu

Global oranı yaklaşık %11 oranında azaltan simülasyon, gerçek Türkiye piyasasının genel bir dönüşümü olarak kabul edilmemelidir.

Çünkü:

- Marketlere göre marj değişebilir.
- İddaa ve global şirketlerin fiyatlama yapıları farklıdır.
- Oran farkı sonuç ve market bazında değişebilir.
- Gerçek fiyat dağılımı maç ve zaman bazında farklılaşır.

Simülasyon:

- Senaryo modu olarak tutulmalı.
- Gerçek piyasa verisi yerine kullanılmamalı.
- Gerçek İddaa verisiyle karşılaştırmalı olarak test edilmeli.
- Market bazlı hata oranı ölçülmeli.

---

## 4. Elo ve xG-Elo Ensemble

Mevcut ensemble:

| Model | Ağırlık |
|---|---:|
| Poisson | %55 |
| Elo | %25 |
| xG-Elo | %20 |

Bu ağırlıklar başlangıç varsayımı olabilir. Ancak sabit ağırlıkların optimal olduğu varsayılmamalıdır.

### 4.1 Ağırlıkların Optimize Edilmesi

Önerilen akış:

```text
Poisson olasılıkları
Elo olasılıkları
xG-Elo olasılıkları
        ↓
Out-of-sample tahminler
        ↓
Kalibrasyon / log loss ölçümü
        ↓
Ağırlık optimizasyonu
        ↓
Final olasılık
```

Kısıtlar:

\[
w_P + w_E + w_X = 1
\]

\[
w_i \geq 0
\]

Amaç fonksiyonu:

- Out-of-sample log loss'u minimize etmek
- Alternatif olarak Brier score'u minimize etmek

Ağırlıklar test verisinden değil, eğitim ve doğrulama dönemlerinden seçilmelidir.

### 4.2 xG-Elo'da Bilgi Kaybı

Mevcut yapı:

- xG farkı `+0.15` üzerindeyse galibiyet
- xG farkı `-0.15` altındaysa mağlubiyet
- Aradaki değerler beraberlik

Bu yaklaşım, sürekli xG farkını üç sınıfa dönüştürdüğü için bilgi kaybına neden olabilir.

Örneğin `+0.16` ile `+1.20` arasındaki değerler aynı sınıfa girebilir.

Alternatifler:

- Sürekli xG farkını kullanmak
- xG farkını Elo güncellemesinde ölçeklendirmek
- Rakip gücünü hesaba katmak
- xG farkının büyüklüğünü korumak
- xG-Elo'yu ayrı olarak backtest etmek

---

## 5. Oyuncu Eksikliği Modeli

Mevcut formül:

```python
importance = playing_time_ratio * position_weight * (
    player_rating / team_avg_rating
)
```

Bu formül anlaşılır bir başlangıçtır; ancak oyuncu etkisini yalnızca mevki ve rating ile ölçmek sınırlı kalabilir.

Eklenebilecek faktörler:

| Faktör | Açıklama |
|---|---|
| İlk 11 rolü | Düzenli başlangıç oyuncusu ve yedek oyuncu ayrımı |
| Alternatif oyuncu | Eksik oyuncunun yerine kimin oynayacağı |
| Taktik rol | Oyuncunun sistem içindeki görevi |
| Birlikte oynama süresi | Özellikle savunma uyumu |
| Eksikliğin kesinliği | Şüpheli ve kesin yok ayrımı |
| Rakip oyun tarzı | Eksikliğin rakibe göre farklı etkisi |

Önerilen yapı:

```text
Beklenen ilk 11
      ↓
Eksik oyuncuların çıkarılması
      ↓
Alternatif oyuncuların eklenmesi
      ↓
Takım gücü farkı
      ↓
Poisson parametrelerine etki
```

`AVAILABILITY_CAP = %25` tek başına yeterli olmayabilir.

Ek kontroller:

- Takım toplam etki sınırı
- Pozisyon grubu sınırı
- Belirsizlik payı
- Çoklu eksiklik senaryoları
- Oyuncu eksikliği verisinin güvenilirlik seviyesi

---

## 6. Kombine Kupon Algoritması

Mevcut sistemde:

- Tek şirket kısıtı bulunuyor.
- Aynı maçtan iki bacak seçilmiyor.
- Ortalama puan kullanılıyor.
- Bacak sayısı cezası var.
- Lig çeşitlendirme bonusu var.

Bunlar operasyonel açıdan faydalı olsa da kombine kupon puanı ile kombinenin gerçek beklenen değeri farklı kavramlardır.

### 6.1 Ortalama Puanın Sınırları

Örnek:

| Kupon | Bacak 1 | Bacak 2 | Ortalama |
|---|---:|---:|---:|
| A | 90 | 60 | 75 |
| B | 78 | 78 | 78 |

Ortalama puan B kuponunu öne çıkarır. Ancak bu puanlar birleşik olasılığı veya beklenen getiriyi doğrudan göstermez.

Kupon seçiminde ana metrikler:

- Birleşik olasılık
- Kombine oran
- Beklenen değer
- Bacak korelasyonu
- Belirsizlik
- Veri kalitesi

### 6.2 Bağımsız Bacak Hesabı

Bacaklar bağımsız kabul edilirse:

\[
P(combo) = \prod_{i=1}^{n} p_i
\]

\[
Odds_{combo} = \prod_{i=1}^{n} odds_i
\]

\[
EV = P(combo) \times Odds_{combo} - 1
\]

Örnek:

| Bacak | Olasılık | Oran |
|---|---:|---:|
| 1 | %70 | 1.60 |
| 2 | %65 | 1.70 |
| 3 | %60 | 1.80 |

Bağımsızlık varsayımında:

- Birleşik olasılık: yaklaşık %27.3
- Kombine oran: 4.896
- EV: yaklaşık +%33.6

Bu hesap yalnızca model olasılıklarının doğru olduğu ve bacakların bağımsız kabul edilebildiği varsayımında geçerlidir.

### 6.3 Korelasyon Motoru

Aynı maçtan iki bacağı engellemek tüm korelasyon sorunlarını çözmez.

Örnekler:

- Ev sahibi galibiyeti + ev sahibi üst 1.5 gol
- KG Var + Üst 2.5
- Bir takım galibiyeti + rakibin belirli gol sınırının altında kalması

Önerilen Monte Carlo yaklaşımı:

```text
Maç simülasyonları
       ↓
Her simülasyonda tüm bacakların sonucu
       ↓
Tüm bacakların aynı anda gerçekleştiği simülasyonlar
       ↓
Kombine olasılığı
       ↓
EV / risk / kupon değerlendirmesi
```

Örneğin 10.000 maç simülasyonu üzerinden tüm bacakların birlikte gerçekleşme oranı hesaplanabilir.

Bu yöntem, bağımlı marketlerde basit olasılık çarpımından daha uygun olabilir.

---

## 7. 0–100 Puanlama Sistemi

Mevcut yapı:

| Bileşen | Maksimum |
|---|---:|
| Edge | 40 |
| Oran aralığı | 25 |
| Kelly / bankroll | 20 |
| Örneklem güveni | 15 |

Öncelikle puanın neyi ölçtüğü netleştirilmeli:

- Tahminin gerçekleşme olasılığı mı?
- Value seviyesi mi?
- Veri güvenilirliği mi?
- Operasyonel oynanabilirlik mi?

Mevcut yapıda bu kavramlar aynı puanda birleştiriliyor.

### 7.1 Çift Sayım Riski

Edge ve Kelly birbiriyle ilişkilidir. Model olasılığı yükseldiğinde hem edge hem Kelly artabilir. Bu da aynı model sinyalinin toplam puana birden fazla kez yansımasına neden olabilir.

Ayrıca 1.50–3.50 oran aralığını "tatlı nokta" kabul etmek, uzun vadeli kârlılığın bu aralıkta daha iyi olduğunu kanıtlamaz.

### 7.2 Önerilen Metrik Ayrımı

#### Model Confidence

- Kalibrasyon
- Veri miktarı
- Model belirsizliği
- Tahmin istikrarı

#### Value / EV

- Model olasılığı ile piyasa fiyatı arasındaki fark
- Beklenen değer
- Fiyat kalitesi

#### Risk & Reliability

- Örneklem büyüklüğü
- Piyasa veri kalitesi
- Veri eksikliği
- Model belirsizliği
- Oran zamanlaması

Tek bir 0–100 skorunu ana karar metriği yapmak yerine bu metrikler ayrı gösterilmelidir.

---

## 8. Backtest ve Model Doğrulama

Mevcut backtest'te Elo, xG-Elo ve oyuncu eksikliği modüllerinin devre dışı kaldığı belirtiliyor.

Bu, temel modelin test edilmesini sağlar; ancak üretimde kullanılan ensemble'ın tamamını ölçmez.

### 8.1 Walk-Forward Backtest

```text
Eğitim dönemi
      ↓
Doğrulama dönemi
      ↓
Model / ensemble parametre seçimi
      ↓
Gelecekteki test dönemi
      ↓
Sonuçların kaydedilmesi
      ↓
Zaman penceresini ileri taşı
```

Her tahminde yalnızca tahmin zamanından önceki bilgiler kullanılmalı.

### 8.2 Ölçülmesi Gereken Metrikler

| Metrik | Ölçtüğü şey |
|---|---|
| Log loss | Olasılık tahmin kalitesi |
| Brier score | Olasılık tahmin hatası |
| Calibration curve | Tahmin edilen ve gerçekleşen olasılık uyumu |
| CLV | Kapanış oranına göre fiyat kalitesi |
| ROI | Geçmiş bahis senaryosundaki finansal sonuç |
| Max drawdown | Sermaye düşüşü |
| Hit rate | Kazanan seçim oranı |

ROI tek başına model kalitesini kanıtlamaz. Şunlar da kaydedilmeli:

- Örneklem büyüklüğü
- Fiyat erişim zamanı
- Marj / komisyon
- İstatistiksel belirsizlik
- Gerçekleşen oran ile kullanılan oran arasındaki fark

### 8.3 Tahmin Metadata'sı

Her tahminde aşağıdaki alanlar bulunmalı:

- Tahmin oluşturulma tarihi ve saati
- Oranın kaydedilme tarihi ve saati
- Maç başlama zamanı
- Kapanış oranı
- Tahminin maçtan kaç dakika önce üretildiği
- Model versiyonu
- Parametre versiyonu
- Kullanılan veri sürümü
- Tahmin değişiklik geçmişi

Bu alanlar CLV ve backtest sonuçlarının doğru yorumlanması için önemlidir.

---

## 9. Önerilen Yeni Algoritmalar

### 9.1 Bivariate Poisson

Takımların attığı goller arasındaki ortak bağımlılığı modellemek için bağımsız Poisson'a alternatif olarak test edilebilir.

### 9.2 xG Tabanlı Hücum / Savunma Modeli

Ham xG'yi doğrudan tek özellik olarak kullanmak yerine:

- Hücum gücü
- Savunma gücü
- Rakip kalitesi
- Ev/deplasman
- Zaman ağırlığı

ile birlikte parametre tahminine entegre edilebilir.

### 9.3 Olasılık Kalibrasyonu

Platt scaling veya isotonic regression gibi yöntemler test edilebilir.

Kalibrasyon yalnızca ayrı doğrulama verisi üzerinde eğitilmeli ve yeni veride performansı ölçülmelidir.

### 9.4 Market Bazlı Modeller

1X2, Alt/Üst 2.5 ve KG marketleri için:

- Ayrı kalibrasyon
- Market özelinde hata analizi
- Market bazlı güven seviyesi

kullanılabilir.

### 9.5 Monte Carlo Simülasyonu

Gol dağılımlarından binlerce maç senaryosu üretilebilir.

Kullanım alanları:

- Birden fazla marketin birlikte gerçekleşme olasılığı
- Kombine kupon olasılığı
- Korelasyon
- Belirsizlik aralıkları
- Senaryo analizi

---

## 10. Ürün Tarafında Eklenebilecek Özellikler

| Özellik | Faydası |
|---|---|
| Tahmin geçmişi | Modelin geçmiş performansını inceleme |
| Model karşılaştırma | Poisson, Elo ve ensemble sonuçlarını karşılaştırma |
| Olasılık dağılımı | Tek tahmin yerine belirsizlik aralığı |
| Oran hareketi grafiği | Piyasa fiyatının zaman içindeki değişimi |
| Market filtreleri | Lig, market ve minimum edge seçimi |
| Açıklanabilirlik | Önerinin hangi sinyallerden oluştuğunu gösterme |
| Model sağlık paneli | Veri gecikmesi ve API hatalarını izleme |

Platformda gerçek kazanç garantisi verilmemeli. Riskler açıkça gösterilmeli ve sorumlu kullanım kontrolleri eklenmelidir. Global üyelik modeli için ülke ve yargı alanı bazında hukuki inceleme ayrı bir iş akışı olarak ele alınmalıdır.

---

## 11. Geliştirme Önceliklendirmesi

### Aşama 1 — Doğrulama Altyapısı

1. Backtest altyapısını tamamla.
2. Tüm modeller aktifken walk-forward test yap.
3. Lookahead bias ve veri sızıntısı denetimi ekle.
4. Model versiyonlaması oluştur.

### Aşama 2 — Model Kalitesi

1. Brier score ve log loss raporlarını geliştir.
2. Calibration curve ekle.
3. Ensemble ağırlıklarını doğrula.
4. Rho optimizasyonunu genişlet.
5. xG-Elo yaklaşımını sürekli değişkenlerle test et.

### Aşama 3 — Piyasa Verisi

1. Oran zaman damgası ekle.
2. Devig yöntemlerini karşılaştır.
3. Gerçek Türkiye oranı entegrasyonunu tamamla.
4. Simülasyon ile gerçek oranlar arasındaki farkı ölç.

### Aşama 4 — Kombine Motoru

1. Birleşik olasılık hesabı ekle.
2. Marketler arası korelasyon motoru geliştir.
3. Monte Carlo simülasyonunu entegre et.
4. Kuponları ortalama skor yerine EV, risk ve olasılıkla karşılaştır.

### Aşama 5 — Oyuncu Modeli ve Ürün

1. Oyuncu değişimi yaklaşımını geliştir.
2. Alternatif oyuncu kalitesini ekle.
3. Açıklanabilirlik ekranı oluştur.
4. Model sağlık paneli ve veri kalitesi uyarıları ekle.

---

## 12. Claude İçin Kod İnceleme Talimatı

Aşağıdaki talimatı Claude'a ver:

> Bu bahis öneri platformunun kodunu teknik ve matematiksel açıdan denetle.
>
> Öncelik sırası:
>
> 1. `poisson_model.py`
> 2. `betting_logic.py`
> 3. `scoring.py`
> 4. `coupon_builder.py`
> 5. `elo.py`
> 6. `player_availability.py`
> 7. Backtest ve history modülleri
>
> Her dosya için:
>
> - Mantıksal hataları tespit et.
> - Matematiksel formülleri kontrol et.
> - Veri sızıntısı ve lookahead bias ihtimalini incele.
> - Edge, EV, devig ve Kelly hesaplarını doğrula.
> - Korelasyon ve kombine olasılığı problemlerini incele.
> - Sabit parametrelerin neden riskli olabileceğini belirt.
> - Hataları önem derecesine göre sınıflandır:
>   - Kritik
>   - Yüksek
>   - Orta
>   - Düşük
> - Her bulgu için:
>   - Sorunun bulunduğu dosya ve fonksiyon
>   - Sorunun teknik açıklaması
>   - Neden hatalı veya riskli olduğu
>   - Önerilen düzeltme
>   - Gerekirse örnek kod
>   - Düzeltme sonrası test senaryosu
>
> Özellikle aşağıdaki konulara odaklan:
>
> - Bağımsız Poisson varsayımı
> - Dixon–Coles düzeltmesi
> - Hücum / savunma ortalamalarının referansları
> - Zaman ağırlığı
> - Elo ve xG-Elo ensemble ağırlıkları
> - Oyuncu eksikliği etkisi
> - Power method devig
> - Çoklu şirket konsensüsü
> - Türkiye oran simülasyonu
> - Kelly kriteri ve bankroll cap
> - 0–100 puanlama sistemindeki çift sayım
> - Kombine kuponların gerçek birleşik olasılığı
> - Marketler arası korelasyon
> - Walk-forward backtest
> - CLV hesaplama
> - Kalibrasyon ve model versiyonlama
>
> Önce yalnızca inceleme raporu hazırla. Kod değişikliğine başlamadan önce kritik bulguları ve önerilen mimariyi listele. Mevcut çalışan özellikleri gereksiz yere bozma. Her değişiklikten sonra regresyon testleri ve matematiksel doğrulama testleri ekle.
