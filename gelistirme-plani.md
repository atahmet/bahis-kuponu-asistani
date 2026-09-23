# Geliştirme Planı — Oyuncu Bazlı Değerlendirme ve Öğrenen Sistem

Bu doküman `yapilacak.md` içindeki üç fikri, mevcut kod tabanına (Poisson
modeli, API-Football cache katmanı, puanlama sistemi) nasıl oturacağı
belirtilerek netleştirir.

## Açık madde: Türkiye'de yasal bahis oranı kaynağı

Kullanıcı Türkiye'de yalnızca yasal siteler (İddaa/Nesine/Misli) üzerinden
bahis yapabiliyor; global bahis şirketlerine (Bet365, Pinnacle vb.) yasal
erişimi yok. API-Football'ın oran listesinde (`/odds/bookmakers`, 33 şirket)
İddaa/Nesine/Misli **yok** — doğrulandı (2026-09-21).

Araştırılan seçenekler:
- **nosyapi.com** — İddaa'ya özel, ücretli/kredi bazlı üçüncü taraf API.
  Ücretsiz deneme kredisi var. Dezavantaj: yalnızca "açılış oranı" verdiğini
  belirtiyor (canlı/güncel oran olmayabilir) — value hesabı için bu
  doğrulanmalı.
- **Nesine/İddaa'nın kendi frontend'inin kullandığı genel uç noktalar**
  (ör. `bulten.nesine.com/api/bulten/getprebultenfull`) — teknik olarak
  erişilebilir ama resmi/izinli üçüncü taraf kullanımı belirsiz, kullanım
  şartları riski var.
- Açık kaynak topluluk parser'ları (GitHub) — bakım/güvenilirlik belirsiz.

**Şimdilik karar:** kullanıcı global oranlarla devam etmeyi tercih etti;
`app.py` ve `bulletin.py`'ye gösterilen oranların global şirketlerden
geldiğini ve gerçek bahis öncesi yasal siteyle karşılaştırılması gerektiğini
belirten bir uyarı eklendi.

**Güncelleme (2026-09-21):** Konu tekrar ele alındı. Yeni bulgu: Türkiye'deki
6 lisanslı site (iddaa.com, Nesine, Bilyoner, Misli, Oley, Birebin) aynı
merkezi İddaa oranını paylaşıyor (yalnızca bazı özel maçlarda promosyon
farkları var) — yani global piyasadaki "çoklu şirket" modeli yerine TEK bir
referans kaynak yeterli. `iddaa_api.py` (nosyapi.com sarmalayıcısı) yazıldı
ve saf mantık kısımları (takım adı normalizasyonu, fikstür eşleştirme, oran
çıkarma) sahte veriyle test edildi. Kullanıcı nosyapi.com'da ücretsiz hesap
açıp `NOSYAPI_API_KEY`'i `.env`'e ekleyecek; gerçek key geldiğinde canlı bir
istekle nosyapi'nin gerçek JSON yapısı doğrulanıp arayüze (İddaa oranı
sütunu/karşılaştırması) bağlanacak. Bilinen risk: takım adı eşleştirmesi
(kısaltmalı Türkçe isimler, ör. "G.Saray") gerçek veri görülmeden tam
güvenilir değil — `iddaa_api.TEAM_ALIASES` tablosu gerçek veriyle
genişletilmeli.

## 1. Oyuncu eksikliklerinin takım gücüne etkisi

**Amaç:** Maç öncesi eksik oyuncuları (sakat/cezalı) tespit edip, eksikliğin
takımın hücum/defans gücünü ne kadar zayıflattığını sayısallaştırmak ve bunu
`poisson_model.py`'daki λ hesabına bir düzeltme çarpanı olarak sokmak.

**Oyuncu önem skoru** üç bileşenden oluşur:

- **Oynama süresi oranı** — son N haftada (örn. son 5 maç) fiilen sahada
  geçirdiği dakika / o dönemde oynanabilecek toplam dakika. Sık rotasyona
  giren ya da yeni sakatlıktan dönen oyuncunun etkisi otomatik düşer.
- **Mevki ağırlığı** — kaleci/defans eksikliği takımın **savunma gücünü**,
  orta saha/forvet eksikliği **hücum gücünü** etkiler. Başlangıç ağırlıkları
  konfigüre edilebilir olmalı (örn. GK 1.0, DF 0.9, MF 0.85, FW 0.8) ama
  zamanla gerçek sonuçlara göre kalibre edilecek (bkz. Bölüm 2).
- **Oyuncu reytingi / takım ortalamasına oranı** — API'den gelen maç
  reytingleri ile oyuncunun takım ortalamasının üstünde mi altında mı
  olduğu (`yapilacak.md` madde 3'teki reyting karşılaştırması burada
  kullanılır). Ortalamanın belirgin üstünde bir oyuncunun yokluğu, önem
  skorunu artırır.

`takım_önem_skoru = oynama_süresi_oranı × mevki_ağırlığı × (oyuncu_reytingi / takım_ortalama_reytingi)`

**Takım gücüne uygulanışı:**

```
eksiklik_etkisi = min(Σ(eksik_oyuncuların_önem_skoru) × etki_katsayısı, üst_sınır)
düzeltilmiş_hücum_gücü = ham_hücum_gücü × (1 - eksiklik_etkisi_hücum)
düzeltilmiş_defans_gücü = ham_defans_gücü × (1 - eksiklik_etkisi_defans)  # defans gücü düşerse rakibe gol beklentisi artar
```

`üst_sınır` önemli: tek bir haftada takım gücünü aşırı düşürmemek için
(örn. en fazla %25 azaltma) bir tavan konulmalı, aksi halde bir yıldız
oyuncunun yokluğu modeli gerçekçi olmayan biçimde çarpıtabilir.

**Veri kaynağı:** API-Football `/injuries` (sakat/cezalı listesi) ve
`/players` (oyuncu bazlı maç istatistikleri: dakika, reyting, mevki)
uç noktaları. Bu, ücretsiz kotaya ek yük bindireceğinden yalnızca o hafta
gerçekten analiz edilecek maçların kadroları için çağrılmalı ve agresif
cache'lenmeli (TTL: maça kadar günlük, maç sonrası kalıcı).

**Kod etkisi:** `poisson_model.py` içine `apply_availability_adjustment()`
fonksiyonu eklenecek; `TeamForm`'a `attack_multiplier` / `defense_multiplier`
alanları eklenip `compute_match_probabilities()` bunları λ hesabına
katacak. Yeni bir `player_availability.py` modülü (API çağrıları + önem
skoru hesabı) eklenecek.

## 2. Haftalık değerlendirmelerin geçmişe kaydı ve geri besleme döngüsü

**Amaç:** Her hafta yapılan tahminleri ve gerçek sonuçları kalıcı olarak
saklamak; zamanla "modelin yüksek puan verdiği seçimler gerçekten daha mı
sık tutuyor?" sorusunu cevaplayabilmek ve buna göre ağırlıkları (edge eşiği,
mevki ağırlıkları, Kelly kesri, puanlama bileşen ağırlıkları) kalibre etmek.

**Akış:**

1. Her analiz çalıştırmasında üretilen tahminler (`Recommendation`) ve
   kombine kupon önerileri (`ComboSuggestion`) DB'ye yazılır — henüz maç
   oynanmadan, "bu hafta böyle tahmin ettik" olarak.
2. Maç sonuçlandığında (API'den `FT` durumu geldiğinde) gerçek skor çekilir
   ve aynı kayda işlenir: tahmin doğru muydu, model olasılığı ile gerçekleşen
   sonuç ne kadar örtüştü (Brier score / log-loss gibi bir kalibrasyon
   metriği hesaplanabilir).
3. Periyodik olarak (örn. her ay) geçmiş kayıtlar üzerinden basit bir rapor
   çıkarılır: "Puanı 80+ olan seçimlerin gerçek başarı oranı %X", "Süper
   Lig'de model sistematik olarak ev sahibi golünü mü fazla tahmin ediyor"
   gibi. Bu rapor, `config.py`'daki sabitlerin (SWEET_SPOT, mevki
   ağırlıkları, MIN_MATCHES_FOR_CONFIDENCE) elle güncellenmesi için temel
   oluşturur — otomatik yeniden eğitim değil, veriye dayalı manuel ayar.

**Kod etkisi:** `coupon_builder.py`'nin ürettiği nesneler DB'ye yazılacak
şekilde bir `history.py` / `db.py` modülü eklenecek (bkz. Bölüm 3). Streamlit
arayüzüne "Geçmiş Performans" adında yeni bir sekme eklenerek bu kalibrasyon
raporu görselleştirilecek.

## 3. Veritabanı şeması

Şu anki `cache/*.json` dosya tabanlı önbellek, ham API cevaplarını geçici
tutmak için kalmaya devam edebilir; ama kalıcı/analiz edilecek veri için
**SQLite** (`bahis.db`, tek dosya, sunucu gerektirmez, kişisel kullanım için
yeterli) ile ayrı bir kalıcı katman kurulacak.

```
teams
  id (api_team_id), name, league_id, season

players
  id (api_player_id), team_id, name, position, season

player_match_stats                 -- her maç sonrası oyuncu performansı
  id, player_id, fixture_id, minutes_played, rating, position_played, date

player_availability                -- maç öncesi eksiklik durumu
  id, player_id, fixture_id, status (injured/suspended/doubtful),
  importance_score, checked_at

team_weekly_ratings                -- o hafta takım için hesaplanan güç
  id, team_id, fixture_id, raw_attack, raw_defense,
  availability_adjustment, final_attack, final_defense, computed_at

predictions                        -- üretilen tekli öneriler
  id, fixture_id, market, selection, odd, model_prob, implied_prob,
  edge, kelly_fraction, score_total, created_at

coupons                            -- üretilen kombine kupon önerileri
  id, created_at, combined_odd, combined_probability, combo_score,
  stake_fraction

coupon_legs                        -- kupon <-> tahmin ilişkisi (N:N)
  coupon_id, prediction_id

results                            -- gerçekleşen sonuçlar
  fixture_id, home_goals, away_goals, settled_at
```

`predictions.fixture_id` ve `results.fixture_id` üzerinden join yapılarak
Bölüm 2'deki kalibrasyon raporu üretilir. `player_availability.importance_score`
ile `team_weekly_ratings.availability_adjustment` arasındaki ilişki, Bölüm
1'deki formülün DB'ye yansımasıdır.

**Kod etkisi:** Yeni `db.py` (SQLite bağlantısı + tablo oluşturma +
CRUD yardımcıları, `sqlite3` standart kütüphanesi yeterli, ekstra bağımlılık
gerekmez). `coupon_builder.py`'deki `fetch_and_analyze` ve `build_recommendations`
fonksiyonları, ürettikleri sonucu DB'ye yazan ince bir katmanla sarmalanacak.

## Uygulama sırası (öneri)

1. **DB iskeleti** (`db.py` + şema oluşturma) — diğer iki fikir buna yazacak,
   önce bu olmalı.
2. **Geçmiş kayıt + sonuç eşleme** (Bölüm 2) — mevcut sistemi bozmadan,
   sadece zaten üretilen tahminleri DB'ye loglamak. Kısa vadede en düşük
   riskli, en çabuk değer katan adım.
3. **Oyuncu eksikliği modülü** (Bölüm 1) — `/injuries` ve `/players`
   entegrasyonu, önem skoru, λ düzeltmesi. En çok API kotası tüketen ve en
   çok test gerektiren parça olduğu için son sırada.
4. **Kalibrasyon raporu arayüzü** — yeterli haftalık veri birikince (en az
   4-6 hafta / ~30-40 maç) anlamlı hale gelir, o yüzden zaman gerektirir.

## Açık sorular (ilerlemeden önce karar verilmeli)

- Oynama süresi oranı için kaç haftalık pencere (N) kullanılacak? (öneri: 5)
- Mevki ağırlıkları başlangıçta sabit mi olacak, yoksa lig bazlı mı
  farklılaşacak?
- Eksiklik etkisinin üst sınırı (tavan) yüzde kaç olmalı?
- Kalibrasyon raporunda hangi metrik esas alınacak (Brier score, basit
  isabet oranı, ROI/bankroll getirisi)?
