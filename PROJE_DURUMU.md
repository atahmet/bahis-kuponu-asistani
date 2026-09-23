# Proje Durumu — Bahis Kuponu Asistanı

Bu dosya, projeye ileride kaldığımız yerden devam edebilmek için tutulan
kapsamlı bir durum özetidir. Kod detayları için `README.md` (kullanım) ve
`gelistirme-plani.md`ye (oyuncu eksikliği/öğrenen sistem tasarımı) bakın.

**Son güncelleme:** 2026-09-21

## 1. Proje ne yapıyor

Premier League, La Liga, Bundesliga, Serie A, Ligue 1, Süper Lig, Şampiyonlar
Ligi, Avrupa Ligi, Konferans Ligi ve Uluslar Ligi için:
- Takım istatistiklerinden (API-Football) bağımsız bir olasılık modeli kurar
- Bahis şirketi oranlarıyla karşılaştırıp "value" (edge) bulur
- Tekli öneriler ve tek-şirket kısıtlı kombine kuponlar üretir
- Her öneriyi 0-100 arası puanlar
- Sonuçları DB'de biriktirip modeli zamanla kalibre eder
- Günlük/haftalık bültenleri Telegram'a otomatik gönderir

**Kritik prensip (baştan beri):** Öneriler ORANLARDAN değil, takım
istatistiklerine dayanan modelimiz ile piyasa (oran) arasındaki FARKTAN
doğar. `model_prob` tamamen form/Elo/xG'den, `implied_prob` tamamen
oranlardan gelir; ikisi arasındaki fark (edge) value sinyalidir.

## 2. Kullanılan bahis teknikleri ve analiz algoritmaları

Bu bölüm, `model_prob`'u üreten ve onu piyasayla karşılaştırıp puanlayan
tüm matematiksel/istatistiksel yöntemleri özetler — sırasıyla veri
işlendiği akışa göre.

### 2.1 Value betting (temel prensip)
```
edge = model_prob − implied_prob (piyasa konsensüsü)
```
Yalnızca `edge >= min_edge` (arayüzde varsayılan %2, bültenlerde
`config.BULLETIN_MIN_EDGE`) olan seçimler öneri listesine girer.

### 2.2 Zaman ağırlıklı Poisson gol modeli (`poisson_model.py`)
- Takımın son `LAST_N_MATCHES` (10) maçı (tüm turnuvalar dahil, sezon
  sınırını aşabilir) kullanılır.
- Her maça `exp(-ln2 · gün_farkı / TIME_DECAY_HALF_LIFE_DAYS(200))` üstel
  zaman ağırlığı verilir (Dixon & Coles 1997 fikri) — eski maçlar otomatik
  önemsizleşir, ayrı bir "sezon başı" kuralına gerek kalmaz.
- Hücum/defans gücü:
  ```
  home_attack  = ev_takımı_ev_golü_ort / lig_ort_ev_golü
  away_defense = deplasman_takımı_deplasmanda_yediği_gol_ort / lig_ort_ev_golü
  away_attack  = deplasman_takımı_deplasman_golü_ort / lig_ort_deplasman_golü
  home_defense = ev_takımı_evde_yediği_gol_ort / lig_ort_deplasman_golü

  lambda_home = lig_ort_ev_golü × home_attack × away_defense
  lambda_away = lig_ort_deplasman_golü × away_attack × home_defense
  ```
- Bağımsız Poisson olasılık matrisi (0-6 gol, `MAX_GOALS=6`) kurulur; buradan
  1X2, Alt/Üst 2.5, KG Var/Yok olasılıkları toplanır.
- Lig ortalaması, o ana kadar cache'lenmiş takımların sezonluk ortalamasından
  hesaplanır (kaba yaklaşım, bkz. Bölüm 7).

### 2.3 Dixon-Coles düşük skor düzeltmesi
Bağımsız Poisson, 0-0/1-1 gibi düşük skorları olduğundan az tahmin eder. Bir
`rho` parametresiyle 0-0, 1-0, 0-1, 1-1 hücrelerine düzeltme (`_dc_tau`)
uygulanır, sonra matris yeniden normalize edilir. Varsayılan `rho=-0.13`;
yeterli veri birikince veriden öğrenilebilir (bkz. 2.14).

### 2.4 Elo reytingi (`elo.py`, gerçek sonuçtan)
- Başlangıç 1500 (`ELO_INITIAL_RATING`).
- K-faktörü gol farkına göre ölçeklenir: `K = K_BASE(20) × log2(|gol_farkı|+1)`
  (beraberlikte `K_BASE × 0.6`).
- **Ev sahibi avantajı artık sabit değil**: bir ligin en az
  `LEAGUE_HOME_ADV_MIN_MATCHES(30)` sonuçlanmış maçı birikince, o ligin
  ampirik ev sahibi puan payından (`galibiyet=1, beraberlik=0.5`) standart
  Elo formülü tersine çevrilerek hesaplanır: `HA = -400·log10(1/p - 1)`.
  Yetersiz veride sabit `ELO_HOME_ADVANTAGE(80)` kullanılır.
- Elo farkından 3 sonuca ayrıştırma: lojistik `e_home = 1/(1+10^(-diff/400))`
  hesaplanır; beraberlik olasılığı, takımlar ne kadar yakınsa (e_home≈0.5)
  o kadar yüksek olacak şekilde bir çan eğrisiyle (`ELO_MAX_DRAW_PROB=0.28`
  tepe değeri) modellenir — tam bir ordinal regresyon değil, basitleştirilmiş
  ama şeffaf bir yaklaşım.

### 2.5 xG-Elo (şans hariç performans sinyali)
Araştırma bulgusu: ham xG'yi doğrudan özellik olarak kullanmak modeli
zayıflatıyor, ama bir Elo'nun kalibrasyonuna beslendiğinde işe yarıyor. Bu
yüzden **aynı Elo mantığı**, gerçek sonuç yerine **xG farkına** göre ayrı bir
reytingde (`team_xg_elo`) tutulur: `xg_diff > 0.15` → galibiyet,
`< -0.15` → mağlubiyet, arası beraberlik sayılır. Soğuk başlangıcı önlemek
için normal Elo'dan daha yüksek bir eşik kullanılır (`XG_ELO_MIN_MATCHES=8`)
— xG verisi her ligde/maçta bulunmayabilir. xG, sonuç senkronizasyonu
sırasında `/fixtures/statistics` (`expected_goals` alanı) üzerinden çekilir.

### 2.6 Üçlü ensemble harmanlama (`poisson_model.blend_signals`)
- Hiçbir ek sinyal hazır değilse: saf Poisson+Dixon-Coles.
- Yalnızca Elo hazırsa (`ELO_MIN_MATCHES=5` geçildiyse): Poisson %65 / Elo %35
  (`ENSEMBLE_POISSON_WEIGHT`).
- Elo VE xG-Elo ikisi de hazırsa: Poisson %55 / Elo %25
  (`ENSEMBLE_ELO_WEIGHT_WITH_XG`) / xG-Elo %20 (`ENSEMBLE_XG_ELO_WEIGHT`).
- Yalnızca 1X2 pazarı harmanlanır; Alt/Üst ve KG saf Poisson kalır (Elo'nun
  gol bazlı bir karşılığı yok).

### 2.7 Yorgunluk / maç sıkışıklığı faktörü
Ekstra API isteği gerektirmez (zaten çekilmiş son-maç verisinden türetilir):
- Dinlenme günü `FATIGUE_MIN_REST_DAYS(5)`'in altındaysa, orantılı bir ceza
  (tavan `FATIGUE_MAX_PENALTY=%5`).
- Son 14 günde `CONGESTION_MATCH_THRESHOLD(4)`'ten fazla maç oynanmışsa ek
  ceza (`CONGESTION_PENALTY=%3`).
- Sonuç, `adjust_team_form` ile hücum gücünü düşüren/savunma zaafını artıran
  bir çarpan çiftine (`attack_multiplier<=1`, `concede_multiplier>=1`) çevrilir.

### 2.8 Oyuncu eksikliği etkisi (opt-in, `player_availability.py`)
```
önem_skoru = oynama_süresi_oranı × mevki_ağırlığı × (oyuncu_reytingi / takım_ort_reyting)
```
- Mevki ağırlıkları: Kaleci 1.0, Defans 0.9, Orta Saha 0.85, Forvet 0.8.
- Forvet eksikliği hücum gücünü, defans/kaleci eksikliği savunmayı, orta saha
  ikisini yarı yarıya etkiler.
- Toplam etki `AVAILABILITY_CAP(%25)` ile tavanlanır (tek oyuncunun modeli
  aşırı çarpıtmasını önler).
- Pratik basitleştirme: "son N hafta" yerine sezon ortalaması oynama süresi
  kullanılır (haftalık pencere çok daha fazla API isteği gerektirirdi).

### 2.9 Devigging — power method (`betting_logic.py`)
Shin's method yerine tercih edildi (güncel ampirik testlerde en az o kadar
iyi, çözümü daha basit):
```
implied_i = 1 / oran_i
k öyle bulunur ki: Σ(implied_i ^ k) = 1   (ikili arama / grid search)
fair_prob_i = implied_i ^ k
```
Proportional (basit) yönteme göre favoriye daha yüksek, uzun orana daha
düşük adil olasılık verir — favori-uzun-oran (favourite-longshot) önyargısını
düzeltir.

### 2.10 Çoklu bahis şirketi konsensüsü + en iyi fiyat
- Taranan HER şirketin oranı power method ile devig edilir; ortalaması =
  **konsensüs** (piyasa) olasılığı — edge bu değere göre hesaplanır.
- **Tekli öneriler**: her seçim için en iyi fiyat (line shopping) önerilir —
  bağımsız tekli bahisler farklı sitelerde ayrı ayrı oynanabildiğinden
  geçerlidir.
- **Kombine kuponlar**: TEK şirket zorunludur (bir kombine kupon fiziksel
  olarak tek bir sitede oynanabilir). `coupon_builder.build_combo`, her
  şirketi ayrı ayrı dener ve tam istenen bacak sayısına ulaşan + en yüksek
  puanlı şirketi seçer (bkz. Bölüm 5, madde 14 — bu gerçek bir bug'dı,
  sonradan düzeltildi).

### 2.11 Kelly kriteri
```
b = oran − 1
full_kelly = (b·p − q) / b       (p = model olasılığı, q = 1−p)
stake = min(full_kelly × 0.25, cap)   (%25 kesirli Kelly, bankroll'un %5 tavanı)
```

### 2.12 0-100 puanlama sistemi (`scoring.py`)
| Bileşen | Aralık | Mantık |
|---|---|---|
| Edge puanı | 0-40 | `edge / 0.10` oranında, ≥%10 edge tam puan |
| Oran aralığı puanı | 0-25 | 1.50-3.50 "tatlı nokta"; dışına çıkınca azalır |
| Kelly/bankroll puanı | 0-20 | `kelly_fraction / cap` oranında |
| Örneklem güveni puanı | 0-15 | Zaman ağırlıklı "etkin" örneklem yeterliyse 15, değilse 5 |

**"Oynanabilir seviye" (mor vurgu):** 4 bileşenin de KENDİ maksimumunun en
az `PLAYABLE_MIN_RATIO(%60)`'ına ulaşması gerekir — tek bir bileşenin (ör.
sadece yüksek edge) toplamı şişirdiği ama diğer sinyallerin zayıf kaldığı
durumlar hariç tutulur.

### 2.13 Kombine kupon puanlaması (`scoring.score_combo`)
- Bacakların ortalama puanı, başlangıç değeri.
- Bacak sayısı cezası: 3'ten fazla bacakta artan oranda (`leg_count_penalty`).
- Korelasyon cezası: aynı maçtan tekrar seçim varsa −15/tekrar (normalde
  `build_combo` zaten aynı maçtan iki bacak seçmez, bu ek bir güvenlik).
- Çeşitlendirme bonusu: farklı lig/maça yayılma +3/lig (en fazla +12).

### 2.14 Dixon-Coles rho'nun veriden öğrenilmesi (MLE-lite, `fit_dixon_coles_rho`)
- Her analiz edilen maçın `(lambda_home, lambda_away)` çifti `match_lambdas`
  tablosuna kaydedilir; sonuçlandığında gerçek skorla eşleşir.
- En az `DIXON_COLES_FIT_MIN_MATCHES(100)` böyle maç birikince, rho için
  `(-0.30, 0.10)` aralığında `0.01` adımlarla grid search yapılır; gerçek
  skorların log-likelihood'unu maksimize eden rho seçilir ve DB'ye
  (`model_params`) kaydedilip sonraki analizlerde otomatik kullanılır.
- **Bilinçli sınırlama:** zaman ağırlığının yarı ömrü (decay) AYNI yöntemle
  öğrenilmiyor — çünkü lambda hesabının kendisini etkiler, dolaşık bir
  yeniden-tahmin gerektirir; kapsam dışı bırakıldı.

### 2.15 CLV — Closing Line Value takibi
```
clv_pct = (bahis_yapılan_oran / kapanış_oranı − 1) × 100
```
Profesyonel bahisçilerin kısa vadeli kazanma oranından daha güvenilir
bulduğu uzun vadeli edge kanıtı — pozitif ortalama CLV, piyasanın bizden
sonra bizim lehimize hareket ettiğini gösterir.

### 2.16 Backtest ve kalibrasyon raporu
- **Backtest modu**: geçmiş bir sezondan tamamlanmış maçlar çekilir; takım
  formu, analiz edilen maçın TARİHİNDEN BİR GÜN ÖNCESİNE kadar olan verilerle
  hesaplanır (lookahead bias'a karşı korumalı) — Elo/xG-Elo/oyuncu eksikliği
  bu modda "şu anki" veriye dayandığından otomatik devre dışı kalır.
- **Kalibrasyon raporu**: sonuçlanan tahminler puan aralığına (0-19, 20-39,
  ..., 80-100) göre gruplanıp gerçek isabet oranı + Brier skoru hesaplanır —
  puanlama sistemi tutarlıysa yüksek puanlı grupların isabet oranı belirgin
  şekilde yüksek olmalı.

## 3. Dosya haritası

| Dosya | Görevi |
|---|---|
| `config.py` | Tüm sabitler, plan modu (free/pro), model parametreleri |
| `api_football.py` | API-Football istemcisi (fikstür, oran, istatistik, xG), disk cache |
| `betting_logic.py` | Power method devig, çoklu şirket konsensüsü, Kelly kriteri |
| `poisson_model.py` | Zaman ağırlıklı Poisson + Dixon-Coles, yorgunluk, rho öğrenme (MLE-lite) |
| `elo.py` | Elo + xG-Elo reytingi, lig bazlı ev sahibi avantajı kalibrasyonu |
| `scoring.py` | 0-100 puanlama, "oynanabilir seviye" tespiti |
| `player_availability.py` | Sakatlık/ceza etkisi (opt-in) |
| `coupon_builder.py` | Tüm sinyalleri birleştirip Recommendation/ComboSuggestion üretir |
| `validation.py` | Ensemble (Elo/xG-Elo) ağırlıklarını gerçek sonuçlardan log-loss ile optimize eder |
| `calibration.py` | model_prob'u gerçek isabet oranına göre düzeltir (Platt scaling / isotonic regresyon) |
| `monte_carlo.py` | Kombine kupon risk simülasyonu (kaç bacak tuttu dağılımı, bankroll varyansı) |
| `db.py` | SQLite katmanı — tüm tablolar burada (bkz. Bölüm 5) |
| `history.py` | DB'ye loglama, sonuç senkronizasyonu, CLV, kalibrasyon |
| `telegram_bot.py` | Telegram Bot API istemcisi (düz metin, 4096 karakter bölme) |
| `bulletin.py` | Günlük/haftalık bülten oluşturma + ayar okuma/yazma |
| `scheduler.py` | 7/24 arka plan döngüsü (`python scheduler.py`) |
| `iddaa_api.py` | **YARIM KALDI** — nosyapi.com üzerinden İddaa oranları (bkz. Bölüm 7) |
| `app.py` | Streamlit arayüzü — sol menüden 4 görünüm (aşağıda) |
| `run_app.bat` + masaüstü kısayolu | Konsoldan değil ikondan başlatma |

## 4. Streamlit arayüzü — görünüm yapısı

Sol menüde **"Görünüm"** radio butonu, hiçbiri analiz çalıştırmayı
gerektirmez (DB'ye direkt bakarlar):
- **🆕 Yeni Analiz** — asıl akış: lig/mod seç → "Verileri Getir ve Analiz Et"
  → 3 sekme (Tekli Öneriler, Kombine Kupon, Telegram Bülten)
- **💾 Kayıtlı Analizler** — isimle kaydedilmiş analizleri geri getirir, her
  kupon varyantı için ayrı Telegram gönder butonu
- **📈 Geçmiş Performans** — sonuç senkronizasyonu, CLV, kalibrasyon raporu,
  "Modeli Şimdi Kalibre Et" (Dixon-Coles rho), "Ensemble Ağırlıklarını Kalibre
  Et" (Elo/xG-Elo payları, log-loss ile), "Olasılık Kalibrasyonunu Öğren"
  (Platt/isotonic)
- **⚙️ Telegram Ayarları** — bülten saatleri/ligleri (DB'ye yazılır,
  `scheduler.py` her turda okur, yeniden başlatma gerekmez)

**Önemli:** Bu görünümler `render_saved_analyses_view()`,
`render_history_view()`, `render_telegram_settings_view()` fonksiyonları
olarak `app.py`'nin üst kısmında tanımlı, `view_mode` ile çağrılıyor.

## 5. DB şeması (`bahis.db`, SQLite)

`teams`, `players`, `player_season_stats`, `player_availability`,
`team_weekly_ratings`, `predictions` (match_date/confident/playable/
analysis_id/model_version dahil), `team_elo`, `team_xg_elo`, `bulletins`,
`coupons` (bulletin_id/analysis_id/technique_tags dahil), `coupon_legs`,
`results` (league/home_xg/away_xg dahil), `analyses`, `match_lambdas`
(match_date dahil — rho fitting zaman ağırlığı için), `match_signals`
(fixture başına Poisson/Elo/xG-Elo'nun harman ÖNCESİ 1X2 olasılığı —
ensemble ağırlık optimizasyonu için, bkz. validation.py), `model_params`
(dixon_coles_rho, ensemble_elo_weight_2way/3way, ensemble_xg_elo_weight_3way,
calibration_platt_a/b), `settings` (genel key-value, bülten saatleri ve
calibration_isotonic_curve — JSON — burada).

Şema `db.py`'de `_ensure_columns` ile idempotent migrate ediliyor — yeni
kolon eklemek veri kaybettirmiyor (defalarca test edildi).

## 6. Tamamlanan ve test edilmiş özellikler (kronolojik)

1. Temel model: zaman ağırlıklı Poisson (son 10 maç, üstel decay) +
   Dixon-Coles düşük skor düzeltmesi
2. Value betting: power method devig + çoklu şirket konsensüsü + en iyi fiyat
3. Kelly kriteri (%25 kesirli, bankroll'un %5 tavanı)
4. 0-100 puanlama sistemi + "oynanabilir seviye" (4 bileşenin de güçlü olması)
5. Free/Pro plan modu (varsayılan artık **Pro**)
6. Öğrenen sistem: DB'ye loglama, sonuç senkronizasyonu, kalibrasyon raporu
7. Oyuncu eksikliği modülü (opt-in, sezon ortalaması yaklaşımı)
8. Backtest modu (geçmiş sezon, lookahead-bias korumalı)
9. Elo ensemble → sonra **xG-Elo** ile 3'lü ensemble (Poisson/Elo/xG-Elo)
10. Lig bazlı ev sahibi avantajı kalibrasyonu (sabit +80 yerine ampirik)
11. Yorgunluk/maç sıkışıklığı faktörü (ek API maliyeti yok)
12. Dixon-Coles rho'nun veriden öğrenilmesi (grid search MLE-lite)
13. Telegram bot + günlük/haftalık bülten + 7/24 scheduler
14. Kombine kupon **tek-şirket kısıtı** düzeltmesi (kritik bug fix — farklı
    şirketlerin oranları karıştırılıyordu, gerçekte oynanamaz kupon üretiyordu)
15. Adlandırılmış analiz kaydetme/geri yükleme + kupon başına Telegram gönder
16. UEFA turnuvaları eklendi (Şampiyonlar/Avrupa/Konferans/Uluslar Ligi)
17. Arayüz Türkçeleştirme + sütun tooltip'leri + mor "oynanabilir" vurgusu
18. Masaüstü ikonu (`run_app.bat` + kısayol, headless flag + browser auto-open)
19. Bülten saat/gün/lig ayarlarının arayüzden yapılandırılması (DB → scheduler)
20. Türkiye oran simülasyonu (opt-in, ~%11 azaltma, yalnızca gösterilen
    oran/EV/Kelly'yi etkiler — edge/value tespiti etkilenmez)
21. `bahis_platformu_teknik_inceleme_claude.md` temel alınarak düzeltmeler
    (bkz. 6.1) + ensemble ağırlık optimizasyonu / olasılık kalibrasyonu /
    kombine kupon Monte Carlo simülasyonu (bkz. 6.2) (2026-09-21)

Hepsi mock/sentetik veriyle veya gerçek API ile test edildi; regresyon
testleri her büyük değişiklikte tekrar çalıştırıldı.

### 6.1 2026-09-21: Teknik inceleme raporu temelli düzeltmeler

`bahis_platformu_teknik_inceleme_claude.md` (ChatGPT'nin PROJE_DURUMU.md'yi
inceleyip ürettiği 12 bölümlük teknik rapor) tek tek doğrulanarak şu somut,
düşük riskli düzeltmeler uygulandı (hepsi test edildi, `MODEL_VERSION`
`1.1.0`'a çıkarıldı):

1. **[KRİTİK] Backtest'te lig ortalaması lookahead bias'ı düzeltildi.**
   `teams/statistics` her zaman sezonun GÜNCEL toplamını döndürdüğü için
   backtest'te lig ortalaması (league_avg_home/away), analiz edilen maçtan
   SONRAKİ verileri de içeriyordu (takım formu to_date ile korunuyordu ama
   lig ortalaması korunmuyordu). Artık backtest modunda her zaman
   `config.DEFAULT_LEAGUE_AVG_HOME/AWAY_GOALS` sabit değerleri kullanılıyor.
2. **xG-Elo artık sürekli (continuous) xG farkı kullanıyor.** Eskiden ±0.15
   eşiğiyle W/D/L'e indirgeniyordu (xG farkı 0.16 ile 1.20 aynı "galibiyet"
   sayılıyordu). Artık `1/(1+exp(-xg_diff/XG_ELO_ACTUAL_SCALE))` sigmoid'i
   ile farkın büyüklüğü de reyting güncellemesine yansıyor.
3. **Dixon-Coles rho fitting'e zaman ağırlığı eklendi.** `match_lambdas`
   tablosuna `match_date` sütunu eklendi; `fit_dixon_coles_rho` artık diğer
   sinyallerle (form/Elo) tutarlı şekilde `TIME_DECAY_HALF_LIFE_DAYS` ile
   üstel zaman ağırlığı uyguluyor (eski satırlarda match_date NULL ise
   ağırlık=1.0, veri sessizce atılmıyor).
4. **Kombine kupon EV'si ayrı bir metrik olarak eklendi.** `ComboSuggestion.
   combined_ev` = birleşik ihtimal × toplam oran − 1. Arayüzde ve Telegram
   mesajlarında "Kupon Puanı" (bacakların ortalaması) ile yan yana gösteriliyor
   — rapor, ortalama puanın kuponun gerçek beklenen değeriyle karıştırılmaması
   gerektiğini vurgulamıştı.
5. **Model versiyonlama eklendi.** `config.MODEL_VERSION`, her `predictions`
   satırına (`model_version` sütunu) yazılıyor — ileride farklı model
   sürümlerinin kalibrasyon/isabet oranlarını karıştırmadan karşılaştırmak için.
6. **Puan tooltip'i 3 kavramsal gruba göre netleştirildi** (Değer/EV,
   Risk/Güvenilirlik) — matematik değişmedi, yalnızca "edge_score ve
   kelly_score neden birlikte hareket ediyor" sorusuna arayüzden yanıt veriliyor.

Raporun DOĞRULANDI ama değişiklik GEREKTİRMEDİ dediği noktalar: lambda
formülündeki hücum/savunma-lig ortalaması eşleştirmesi (2.1) zaten doğru;
combo kuponda aynı maçtan iki pazar seçilmesi zaten `used_fixtures` ile
engelleniyor (6.3'teki örnek zaten önleniyor); `implied_prob` alanı isim
olarak "ham" çağrışım yapsa da DEĞERİ zaten devig edilmiş konsensüs olasılığı
(fonksiyon docstring'lerinde belirtili); TR oran simülasyonunun tasarımı
(yalnızca gösterilen oran/EV/Kelly'yi etkilemesi, edge/value tespitine
dokunmaması) raporun kendi 3.4 maddesindeki kriterle zaten uyumlu.

Raporun BÜYÜK ARAŞTIRMA/MİMARİ projesi olarak işaretlediği maddelerden
kullanıcının "2, 3 ve 4'ü de yap" demesiyle üçü aynı gün (2026-09-21) ayrıca
uygulandı — bkz. 6.2. Hâlâ UYGULANMAYAN (kasıtlı, ayrı bir kapsam kararı
gerektirir): Bivariate Poisson / tam Dixon-Coles ortak MLE, gerçek
walk-forward backtest çerçevesi (aşağıdaki 6.2'deki yöntem walk-forward
DEĞİL — bkz. validation.py docstring'indeki fark), pazar bazlı ayrı modeller,
puanlama sisteminin 3 BAĞIMSIZ metriğe (DB şeması değişikliği gerektirir) tam
ayrılması, oyuncu modelinin ilk-11/pozisyon detayına genişletilmesi, model
sağlık paneli/açıklanabilirlik ekranı.

### 6.2 2026-09-21: Ensemble ağırlık optimizasyonu + olasılık kalibrasyonu + Monte Carlo

Kullanıcının onayıyla, raporun büyük ölçekli önerilerinden 3 tanesi de aynı
oturumda uygulandı. Üçü de **yeterli veri birikene kadar mevcut davranışı
DEĞİŞTİRMEZ** (eşik altında sessizce eski sabit/varsayılan kullanılmaya devam
eder) — bu yüzden canlı ortamda hemen bir fark GÖRÜLMEYEBİLİR, veri
biriktikçe devreye girer.

1. **Ensemble ağırlık optimizasyonu (`validation.py`).** coupon_builder artık
   her CANLI analizde (backtest'te Elo/xG-Elo zaten hesaplanmıyor), harman
   ÖNCESİ Poisson/Elo/xG-Elo 1X2 olasılıklarını `match_signals` tablosuna
   kaydediyor. Bu tahminler sonuçlandığında, "Ensemble Ağırlıklarını Kalibre
   Et" butonu (Geçmiş Performans ekranı) log-loss grid search ile en iyi
   Elo/xG-Elo ağırlığını bulup `model_params`'a yazıyor; coupon_builder bir
   sonraki analizden itibaren bunu okuyor (dixon_coles_rho ile birebir aynı
   kalibrasyon deseni). Eşik: `config.ENSEMBLE_WEIGHT_FIT_MIN_MATCHES` (40),
   2-way (yalnızca Elo) ve 3-way (Elo+xG-Elo) grupları ayrı optimize edilir.
   **Not:** Bu gerçek bir "walk-forward backtest" değildir — sentetik bir
   geçmiş-sezon replay'i kurmak yerine, zaten CANLI üretilmiş (o anki Elo
   reytingiyle hesaplanmış, dolayısıyla veri sızıntısız) tahminler birikince
   geriye dönük log-loss karşılaştırması yapar. Daha az mühendislik, ama
   veri birikimi zaman alır (yalnızca Elo/xG-Elo hazırken çalıştırılan canlı
   analizlerden beslenir).
2. **Olasılık kalibrasyonu (`calibration.py`).** "Olasılık Kalibrasyonunu
   Öğren" butonu, DB'deki tüm sonuçlanmış tahminlerin (model_prob, tuttu mu)
   çiftlerinden Platt scaling (varsayılan, `config.CALIBRATION_PLATT_MIN_
   MATCHES`=60 eşiği, az veriyle kararlı) ve isotonic regresyon (PAVA,
   `CALIBRATION_ISOTONIC_MIN_MATCHES`=300 eşiği — çok daha esnek ama azken
   aşırı uyum riski taşır) öğrenir; ikisi de varsa isotonic tercih edilir.
   Uygulandıktan sonra coupon_builder her market_probs hesaplamasında bunu
   kullanır — yani **edge/EV/Kelly/puan artık kalibre edilmiş olasılığa göre
   hesaplanır** (TR simülasyonunun aksine, bu kasıtlı olarak value tespitini
   etkiler — amacı zaten budur). Basitleştirme: tüm pazarlar (1X2/Alt-Üst/KG)
   TEK bir eğriyle kalibre edilir, pazar bazlı ayrım yok (veri henüz yetmez).
3. **Kombine kupon Monte Carlo simülasyonu (`monte_carlo.py`).** Kombine
   kupon ekranında yeni bir "🎲 Monte Carlo Risk Simülasyonu" açılır paneli:
   (a) bacakları bağımsız Bernoulli örnekleyip analitik `combined_probability`
   formülünü doğrular, (b) "kaç bacağın tuttuğu" olasılık dağılımını gösterir
   (near-miss bilgisi — tek bir kazanma ihtimali bunu göstermez), (c) aynı
   kuponu tekrar tekrar oynama stratejisinin bankroll varyansını (medyan/%5/
   %95 senaryo, iflas ihtimali) simüle eder. **Bilinçli tasarım kararı:**
   bacaklar arası korelasyon MODELLENMEDİ — farklı maçlar istatistiksel
   bağımsız kabul edilir (bahis şirketlerinin parlay fiyatlaması da aynı
   varsayımı yapar), çünkü paylaşılan bir gizli korelasyon faktörü için
   hiçbir veri yok; uydurma bir korelasyon sayısı eklemek gerçek bir düzeltme
   değil, yanlış kesinlik olurdu.

Üçü de test edildi: sentetik senaryolarla (Elo daha iyi tahmin ediciyken
ağırlığın arttığı, model aşırı özgüvenliyken kalibrasyonun olasılığı
düşürdüğü, Monte Carlo'nun analitik formülle örtüştüğü) doğrulandı, ayrıca
`app.py` geçici bir portta (`streamlit run --server.port 8590`) başlatılıp
HTTP 200 ile yanıt verdiği, hiçbir import/syntax hatası olmadığı kontrol
edildi. **Gerçek bir canlı analiz ile uçtan uca test EDİLMEDİ** (API kredisi
harcamamak için) — kullanıcı bir sonraki canlı analizi çalıştırdığında bu
yeni yolların (özellikle Monte Carlo paneli ve kalibrasyon uygulama akışı)
ilk kez gerçek veriyle çalıştığını unutmayın.

## 7. YARIM KALAN İŞ: İddaa (Türkiye yasal oran) entegrasyonu

**Neden:** Türkiye'de sadece 6 lisanslı site (iddaa.com, Nesine, Bilyoner,
Misli, Oley, Birebin) yasal — hepsi AYNI merkezi İddaa oranını kullanıyor.
API-Football'da bu kaynak yok (33 global şirket var, hiçbiri TR'de erişilebilir
değil). Kullanıcı bunu düzeltmek istedi.

**Seçilen kaynak:** nosyapi.com (İddaa oranlarına özel üçüncü taraf API,
ücretsiz plan: ayda 500 kredi, kredi İSTEK başına değil DÖNEN SONUÇ SAYISINA
göre düşüyor).

**Yazılan kod (`iddaa_api.py`, tamamlandı ve test edildi):**
- `get_bettable_matches(date_str, league, sport_type)` — tarih/lig filtreli sorgu
- `normalize_team_name` + `TEAM_ALIASES` — Türkçe/kısaltmalı isim eşleştirme
  (ör. "G.Saray" → "galatasaray")
- `match_fixture` — API-Football fikstürünü İddaa maçıyla tarih+isim
  benzerliğiyle eşleştirir, belirsizse None döner (asla yanlış tahmin etmez)
- `extract_odds` — Maç Sonucu + Alt/Üst 2.5 oranlarını çıkarır
- **Kredi bütçesi koruması**: `config.NOSYAPI_MONTHLY_CREDIT_BUDGET = 450`
  (gerçek limitin altında güvenlik payı), `has_budget_remaining()` /
  `get_credits_remaining()`, bütçe dolunca `NosyapiBudgetExceeded` fırlatır,
  hiç istek yapmaz
- **Uzun cache** (`NOSYAPI_CACHE_TTL_HOURS = 20`) — günde tekrar tekrar
  kredi harcamayı önler
- **Varsayılan kapsam daraltıldı**: `NOSYAPI_DEFAULT_LEAGUES = ["Süper Lig"]`
  (İddaa'nın en çok fark yarattığı yer; diğer liglerde global konsensüs
  zaten makul yaklaşık)
- `creditUsed` alanı nosyapi cevabından resmi olarak okunuyor (tahmin değil)

Tüm bu mantık **sahte (mock) veriyle test edildi ve doğru çalışıyor** —
normalize/eşleştirme fonksiyonlarında gerçek bir hata bulup düzelttim (alias
tablosu anahtarları normalize edilmeden karşılaştırılıyordu).

**BLOKE OLAN NOKTA:** Gerçek API key ile yapılan CANLI istekler
`"Unable to find a valid record associated with your account"` /
`"Hesabınıza Tanımlı Uygun Kayıt Bulunamadı"` hatası veriyor. 5 farklı
kimlik doğrulama yöntemi denendi (query `apiKey=`/`apikey=`/`api_key=`,
header `X-NSYP`, `Authorization: Bearer`) — key'in TANINDIĞI doğrulandı
(`creditUsed: 0`, "key eksik" hatası DEĞİL bu) ama hesap bu API ürününe
(İddaa Oranları ve Programları API) henüz **abone/etkin değil** gibi
görünüyor. Kredi hiç harcanmadı (güvenlik kontrolü doğru çalıştı, tüm bu
denemeler ücretsizdi).

**Sıradaki adım:** Kullanıcının nosyapi.com panelinde
https://www.nosyapi.com/api/iddaa-oranlari-ve-programi-api sayfasındaki
**"Tanımla"** butonuna (Test Kredisi bölümü) tıklayıp bu API ürününü
hesabına özel olarak etkinleştirmesi gerekiyor — sade "Kayıt Ol" yetmiyor
gibi duruyor. Bu yapıldıktan sonra:
1. TEK bir canlı istek atıp gerçek JSON yapısını doğrula (nosyapi'nin kendi
   dokümantasyon örneğinden alan adları zaten teyit edildi: `MatchID`,
   `Date`, `Team1`, `Team2`, `HomeWin`, `Draw`, `AwayWin`, `Under25`,
   `Over25` — bunlar `iddaa_api.py`'deki kodla zaten uyumlu)
2. `coupon_builder.fetch_and_analyze`'a entegre et: her analiz edilen
   Süper Lig fikstürü için `iddaa_api.match_fixture` ile eşleştirmeyi dene,
   bulunursa İddaa oranını `AnalyzedFixture`'a ek bir alan olarak ekle
3. `app.py`'de "Tekli Öneriler" tablosuna "İddaa Oranı" sütunu ekle
   (global konsensüsün yanında karşılaştırma amaçlı — henüz value hesabının
   ANA kaynağı yapılmadı, önce gerçek veriyle eşleştirme doğruluğu
   gözlemlenmeli)
4. Eşleştirme doğruluğu yeterince güvenilir görülürse, `betting_logic`'teki
   edge hesabını Süper Lig için İddaa'yı birincil kaynak yapacak şekilde
   genişletmeyi değerlendir (şu an bu KARARLAŞTIRILMADI, sadece bir olasılık)

**Ara çözüm (2026-09-21, TAMAMLANDI):** Gerçek entegrasyon bloke olduğu için,
kullanıcının kendi bet365/Bilyoner karşılaştırmasından (Norveç-Danimarka
1.73→1.54, Türkiye-Fransa 2.50→2.29, Hollanda-Almanya 1.57→1.37, ortalama
oran ≈0.893) türetilen bir **simülasyon** eklendi:
- `betting_logic._apply_tr_simulation` — `evaluate_market`/
  `evaluate_market_per_bookmaker`'a `tr_simulation_factor` parametresi
  eklendi. Yalnızca en-iyi-fiyat `odd`'u (ve ona bağlı EV/Kelly) küçültür;
  `implied_prob`/`edge` gerçek global konsensüse göre HESAPLANMAYA DEVAM
  EDER (value tespiti bozulmaz — test edildi ve doğrulandı).
- Sol menüde "🇹🇷 Türkiye Oranlarını Simüle Et" checkbox'ı + ayarlanabilir
  yüzde slider'ı (varsayılan ~%11, `config.TR_ODDS_SIMULATION_DEFAULT_FACTOR
  = 0.89`). Aktifken sonuç ekranında büyük bir uyarı banner'ı gösterilir,
  şirket adının yanına `[TR sim.]` eklenir.
- Bu, gerçek nosyapi entegrasyonu tamamlanınca KALDIRILMAYACAK — güvenilir
  bir yedek/karşılaştırma aracı olarak kalabilir.

## 8. Bilinen sınırlamalar / teknik notlar (gelecekte tekrar keşfetmeyelim diye)

- **Ücretsiz API-Football planı** güncel sezona VE `last` parametresine
  erişemiyor — hem canlı hem backtest modu pratikte **Pro plan gerektiriyor**.
- **Streamlit'in bare-mode (`python -c "import app"`) testleri** her zaman
  `st.stop()` sonrası satırlarda hata verir — bu GERÇEK bir hata değildir,
  yalnızca gerçek `streamlit run` ile test geçerlidir.
- **Streamlit'te pandas Styler (satır renklendirme) ile `column_config`
  (tooltip) birlikte güvenilir çalışmıyor** — bu yüzden "oynanabilir" satırlar
  metin işareti (🟣) + ayrı özet kutusuyla vurgulanıyor, Styler kullanılmıyor.
- **Streamlit'in ilk çalıştırma "hoş geldin" e-posta istemi** konsolu kilitler
  — `--server.headless true` bayrağıyla atlanıyor (credentials.toml ile
  atlatmak GÜVENİLİR DEĞİL, denendi ve işe yaramadı).
- **Uzun süre çalışan bir Streamlit sürecinde yeni fonksiyon/modül eklemek**
  Python'ın modül cache'i yüzünden otomatik yansımaz — süreç TAMAMEN
  yeniden başlatılmalı (tarayıcıda "Rerun" yetmez).
- **nosyapi kredi modeli**: istek başına değil, dönen SONUÇ SAYISI başına
  düşüyor — sorguları her zaman date+league ile daralt.
- **Dixon-Coles zaman ağırlığının (decay) yarı ömrünü veriden öğrenme**
  kapsam dışı bırakıldı (lambda hesabının kendisini etkilediğinden dolaşık
  bir yeniden-tahmin gerektirir) — sadece rho öğreniliyor.
- **Elo/xG-Elo tek, lig bağımsız global reyting** — ligler arası tam
  kalibrasyon yok ama pratikte sorun yaratmıyor (karşılaştırmalar hep aynı
  lig içinde).
- **Gösterilen (global) oranlar Türkiye'de yasal olarak erişilebilir değil**
  — bu net şekilde arayüzde ve bültenlerde belirtiliyor (bkz. Bölüm 7,
  İddaa entegrasyonu bunu çözmeye çalışıyor).

## 9. Ortam / kurulum notları

- `.env` içinde: `API_FOOTBALL_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `NOSYAPI_API_KEY` (eklendi ama hesap aktivasyonu bekleniyor)
- Masaüstü kısayolu: `Bahis Kuponu Asistanı.lnk` → `run_app.bat`
- Zamanlayıcıyı 7/24 çalıştırmak için ayrıca `python scheduler.py`
  başlatılmalı (masaüstü ikonu sadece Streamlit arayüzünü açar, scheduler'ı
  DEĞİL — ikisi ayrı süreçler)
