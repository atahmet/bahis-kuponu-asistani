"""Merkezi ayarlar: ligler, sezon, cache süreleri."""
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _secret(key: str, default: str = "") -> str:
    """Önce Streamlit secrets'a (Streamlit Cloud'da 'Secrets' panelinden,
    yerelde .streamlit/secrets.toml'dan - ikisi de git'e girmez), sonra
    ortam değişkenine (.env) bakar. Streamlit dışı bir ortamda (örn. CLI
    script) st.secrets erişilemez olabileceğinden sessizce env'e düşer."""
    try:
        import streamlit as st

        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)


API_KEY = _secret("API_FOOTBALL_KEY", "")
API_BASE_URL = "https://v3.football.api-sports.io"

# Modelde puanlama/olasılık formülünü etkileyen bir değişiklik yapıldığında
# bu sürümü artır — predictions.model_version'a yazılır; geçmiş tahminleri
# hangi model mantığıyla üretildiğine göre ayırt etmek (backtest/kalibrasyon
# karşılaştırmalarında farklı sürümleri karıştırmamak) için kullanılır.
MODEL_VERSION = "1.2.0"  # 1.1.0: backtest lig-ortalaması lookahead düzeltmesi + sürekli xG-Elo + zaman ağırlıklı rho fitting
# 1.2.0: ensemble ağırlık optimizasyonu (validation.py) + olasılık kalibrasyonu (calibration.py) eklendi

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "bahis.db"

# API-Football lig/turnuva kimlikleri (canlı API'den /leagues?search=... ile
# doğrulandı, 2026-09-21). Şampiyonlar Ligi/Avrupa Ligi/Konferans Ligi/
# Uluslar Ligi 'Cup' tipindedir (grup+eleme formatı) — fikstür/oran/form
# çekme mantığı lig tipinden bağımsız olduğundan ek bir kod değişikliği
# gerektirmez; yalnızca örneklem (maç sayısı) domestik liglere göre daha
# küçük olabilir.
LEAGUES = {
    "Premier League": 39,
    "La Liga": 140,
    "Bundesliga": 78,
    "Serie A": 135,
    "Ligue 1": 61,
    "Süper Lig": 203,
    "Şampiyonlar Ligi": 2,
    "Avrupa Ligi": 3,
    "Konferans Ligi": 848,
    "Uluslar Ligi": 5,
}


def current_season() -> int:
    """Avrupa futbol sezonu Ağustos civarı başlar; API-Football sezonu başlangıç
    yılıyla ifade eder (ör. 2026-27 sezonu -> season=2026)."""
    today = datetime.now()
    return today.year if today.month >= 7 else today.year - 1


SEASON = current_season()


def set_season(year: int) -> None:
    """Sezonu çalışma anında değiştirir. Normalde config.SEASON (güncel sezon)
    kullanılır; ücretsiz API-Football planı güncel sezona erişemediğinden
    (yalnızca ~2022-2024 arası geçmiş sezonlara izin verir — bkz. README
    'Backtest modu'), arayüzden backtest için geçmiş bir sezon seçildiğinde
    bu fonksiyonla geçersiz kılınır."""
    global SEASON
    SEASON = year


# Ücretsiz API-Football planının erişebildiği bilinen son sezon (API hata
# mesajından: "Free plans do not have access to this season, try from 2022
# to 2024"). Yalnızca arayüzde varsayılan/uyarı amaçlı kullanılır; API'nin
# kendisi asıl kısıtı zaten uygular.
FREE_PLAN_MAX_SEASON = 2024
FREE_PLAN_MIN_SEASON = 2022

# --- Plan modu: "free" (100 istek/gün) veya "pro" (7500 istek/gün, tüm
# endpoint/lig). Streamlit arayüzünden set_plan() ile çalışma anında
# değiştirilir; api_football.py tüm cache TTL ve sayfalama limitlerini
# limits() üzerinden okur, böylece mod değişince davranış anında değişir. ---
PLAN = "free"

PLAN_LIMITS = {
    "free": {
        "daily_request_cap": 100,
        "ttl_fixtures": 6,      # saat
        "ttl_team_stats": 48,
        "ttl_odds": 3,
        "ttl_injuries": 6,
        "ttl_player_stats": 48,
        "ttl_results": 1,
        "player_stats_max_pages": 3,
        "max_next_n": 10,
        "availability_default_on": False,
    },
    "pro": {
        "daily_request_cap": 7500,
        # Pro planda kota sorun olmadığından veri her zaman tazelenir
        # (TTL'ler kısa/sıfıra yakın tutulur, cache yine de tekrar eden
        # işlemleri hızlandırmak için düşük süreyle kalır).
        "ttl_fixtures": 1,
        "ttl_team_stats": 6,
        "ttl_odds": 0.25,
        "ttl_injuries": 1,
        "ttl_player_stats": 6,
        "ttl_results": 0.1,
        "player_stats_max_pages": 6,
        "max_next_n": 20,
        "availability_default_on": True,
    },
}


def set_plan(mode: str) -> None:
    global PLAN
    if mode not in PLAN_LIMITS:
        raise ValueError(f"Bilinmeyen plan: {mode}")
    PLAN = mode


def limits() -> dict:
    return PLAN_LIMITS[PLAN]


# Tercih edilen bahis şirketleri sırası (odds cevabında bu sırayla aranır)
PREFERRED_BOOKMAKERS = ["Bet365", "Marathonbet", "1xBet", "Betfair", "Pinnacle"]

# Poisson modeli için lig ortalaması bulunamazsa kullanılacak varsayılan değerler
DEFAULT_LEAGUE_AVG_HOME_GOALS = 1.50
DEFAULT_LEAGUE_AVG_AWAY_GOALS = 1.15

# Güven eşiği artık ham maç sayısı değil, zaman ağırlıklı "etkin örneklem"
# (bkz. poisson_model.build_weighted_form / TeamForm.effective_weight).
MIN_MATCHES_FOR_CONFIDENCE = 5

# --- Zaman ağırlıklı Poisson modeli (Dixon & Coles 1997 tarzı) ---
# Takımın son kaç maçı (tüm turnuvalar, sezon sınırını aşabilir) çekilecek.
LAST_N_MATCHES = 10
# Üstel zaman ağırlığının yarı ömrü (gün). Dixon-Coles'un orijinal makalesi
# ~1 yıl (365 gün) kullanır; kişisel bir araç için sezon-içi forma daha
# duyarlı olması amacıyla 200 gün (~6-7 ay) tercih edildi.
TIME_DECAY_HALF_LIFE_DAYS = 200
# Düşük skorlu sonuçların (0-0, 1-0, 0-1, 1-1) bağımsız Poisson'un
# olduğundan düşük tahmin ettiği korelasyonu düzelten rho parametresi.
# Literatürde tipik değer aralığı -0.05 ile -0.15; ligler arası MLE ile
# ayrı ayrı kalibre edilmediği için sabit bir değer kullanılıyor (basitleştirme).
DIXON_COLES_RHO = -0.13

# --- Elo güç reytingi (ikinci bağımsız sinyal, Poisson modeliyle harmanlanır) ---
ELO_INITIAL_RATING = 1500.0
ELO_HOME_ADVANTAGE = 80.0
ELO_K_BASE = 20.0
# Berabere biten maçların "eşit güçte takım" durumunda ulaşabileceği tepe olasılık
ELO_MAX_DRAW_PROB = 0.28
# Elo'nun anlamlı bir sinyal sayılması için bir takımın DB'de en az kaç
# sonuçlanmış maçı olmalı (soğuk başlangıçta her takım 1500'de eşit başlar,
# bu eşiğin altında Elo katkısı sıfırlanır, saf Poisson kullanılır).
ELO_MIN_MATCHES = 5
# Nihai 1X2 olasılığında Poisson+Dixon-Coles modelinin ağırlığı (kalan pay
# Elo'ya gider). xG-Elo de devredeyse (bkz. aşağı) üçe bölünür.
ENSEMBLE_POISSON_WEIGHT = 0.65

# --- xG (expected goals) tabanlı ikinci bir Elo reytingi ---
# Araştırma bulgusu: ham xG'yi doğrudan özellik olarak kullanmak modeli
# zayıflatıyor, ama Elo'nun kalibrasyonuna (yani sonuç yerine xG farkına göre
# güncellenen ayrı bir reytinge) beslendiğinde işe yarıyor. Bu yüzden gerçek
# sonuçtan güncellenen team_elo'nun yanına, xG farkından güncellenen ayrı bir
# team_xg_elo eklendi — "şans dahil sonuç" ve "şans hariç performans" iki
# bağımsız sinyal olarak modele katkı sağlıyor.
XG_ELO_MIN_MATCHES = 8  # normal Elo'dan daha yüksek: xG verisi tüm liglerde/maçlarda gelmeyebilir
XG_ELO_ACTUAL_SCALE = 1.0  # xG farkını "actual score"a çeviren sigmoid'in ölçeği (xg_diff=1.0 -> ~0.73)

# validation.optimize_ensemble_weights: Elo/xG-Elo harman ağırlıklarını gerçek
# sonuçlanmış tahminlerin log-loss'una göre veriden kalibre etmek için.
ENSEMBLE_WEIGHT_FIT_MIN_MATCHES = 40  # DIXON_COLES_FIT_MIN_MATCHES'ten (100) düşük tutuldu: iki ayrı gruba (2-way/3-way) bölünüyor, veri daha yavaş birikir
ENSEMBLE_WEIGHT_SEARCH_STEP = 0.05

# calibration.py: model_prob'un gerçek isabet oranıyla ne kadar örtüştüğünü
# öğrenip düzeltir. Platt (2 parametre) az veriyle kararlı, isotonic (adım
# fonksiyonu) çok daha fazla veri ister (aksi halde aşırı uyum riski taşır).
CALIBRATION_PLATT_MIN_MATCHES = 60
CALIBRATION_ISOTONIC_MIN_MATCHES = 300
ENSEMBLE_XG_ELO_WEIGHT = 0.20  # xG-Elo hazır olduğunda üçlü harmanda bu ağırlığı alır
# xG-Elo devredeyken üçlü harman ağırlıkları (toplam 1.0): Poisson payı biraz
# düşer, kalanı Elo ve xG-Elo paylaşır. xG-Elo hazır değilse ENSEMBLE_POISSON_WEIGHT
# ile ikili (Poisson/Elo) harman kullanılmaya devam eder.
ENSEMBLE_ELO_WEIGHT_WITH_XG = 0.25

# --- Ev sahibi avantajının lig bazında kalibrasyonu ---
# Sabit +80 yerine, yeterli veri (LEAGUE_HOME_ADV_MIN_MATCHES) birikince o
# ligin kendi geçmiş sonuçlarından (ev sahibi puan payı -> Elo eşdeğeri)
# otomatik hesaplanır (bkz. elo.compute_league_home_advantage). Yetersiz
# veride ELO_HOME_ADVANTAGE'a geri döner.
LEAGUE_HOME_ADV_MIN_MATCHES = 30

# --- Dinlenme günü / maç sıkışıklığı (yorgunluk) etkisi ---
# Zaten elimizde olan (ekstra API isteği gerektirmeyen) son-maç verisinden
# hesaplanır: bir sonraki maça kaç gün kaldığı ve son 14 günde kaç maç
# oynandığı. Araştırmaya göre etkisi küçük (~%0.5-1) — bu yüzden tavan
# düşük tutuldu.
FATIGUE_MIN_REST_DAYS = 5  # bu gün sayısının altı ceza almaya başlar
FATIGUE_MAX_PENALTY = 0.05  # 0 gün dinlenmede (art arda maç) en fazla %5
CONGESTION_MATCH_THRESHOLD = 4  # son 14 günde bu sayıdan fazla maç = sıkışıklık
CONGESTION_PENALTY = 0.03

# --- Dixon-Coles rho'nun veriden öğrenilmesi (MLE-lite) ---
# Sabit DIXON_COLES_RHO yalnızca yetersiz veri varken kullanılır; yeterli
# sonuçlanmış maç (DIXON_COLES_FIT_MIN_MATCHES) birikince rho, o maçların
# gerçek skorlarına karşı log-likelihood'u maksimize eden değere göre grid
# search ile güncellenir ve DB'de (model_params) saklanır (bkz.
# poisson_model.fit_dixon_coles_rho). Zaman ağırlığının yarı ömrü (decay),
# lambda hesabının kendisini etkilediğinden dolaşık bir yeniden-tahmin
# gerektirir — bu tur için kapsam dışı bırakıldı (bilinen sınırlama).
DIXON_COLES_FIT_MIN_MATCHES = 100
DIXON_COLES_RHO_SEARCH_RANGE = (-0.30, 0.10)
DIXON_COLES_RHO_SEARCH_STEP = 0.01

# --- Oyuncu eksikliği (sakatlık/ceza) etkisi (gelistirme-plani.md Bölüm 1) ---
# Mevki ağırlığı: eksikliğin takım gücüne ne kadar yansıyacağını belirler.
POSITION_WEIGHTS = {
    "Goalkeeper": 1.0,
    "Defender": 0.9,
    "Midfielder": 0.85,
    "Attacker": 0.8,
}
# Toplam önem skorunun etkiye çevrilme katsayısı
AVAILABILITY_EFFECT_COEF = 1.0
# Tek haftada takım gücünün en fazla ne kadar zayıflatılabileceği (tavan)
AVAILABILITY_CAP = 0.25
# API-Football'da reyting girilmemiş oyuncular için varsayılan takım ortalaması
DEFAULT_PLAYER_RATING = 6.5

# --- Telegram bülten botu ---
TELEGRAM_BOT_TOKEN = _secret("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _secret("TELEGRAM_CHAT_ID", "")

# --- Türkiye yasal bahis oranları (İddaa, nosyapi.com üzerinden) ---
NOSYAPI_API_KEY = _secret("NOSYAPI_API_KEY", "")

# --- Giriş ekranı (auth.py) ---
# Şifre asla düz metin saklanmaz; yalnızca PBKDF2-HMAC-SHA256 hash'i +
# salt tutulur (bkz. auth.py). Tümü st.secrets/.env üzerinden gelir.
AUTH_USERNAME = _secret("AUTH_USERNAME", "")
AUTH_PASSWORD_HASH = _secret("AUTH_PASSWORD_HASH", "")
AUTH_SALT = _secret("AUTH_SALT", "")
# Fikstür eşleştirmede takım adı benzerliğinin kabul edileceği alt sınır
# (bkz. iddaa_api.match_fixture) — çok düşükse yanlış eşleşme, çok yüksekse
# kısaltmalı isimlerde kaçırma riski artar.
IDDAA_MATCH_MIN_SIMILARITY = 0.5

# nosyapi'de kredi İSTEK BAŞINA değil, DÖNEN SONUÇ SAYISINA göre düşer ve
# ücretsiz plan ayda yalnızca 500 kredi veriyor — bu yüzden burada kotayı
# korumak için üç önlem var:
#   1) Varsayılan olarak yalnızca Süper Lig için sorgulanır (İddaa'nın en çok
#      fark yarattığı yer zaten burası; diğer liglerde global konsensüs
#      makul bir yaklaşıktır). Arayüzden genişletilebilir.
NOSYAPI_DEFAULT_LEAGUES = ["Süper Lig"]
#   2) Cache süresi uzun tutulur — aynı gün içindeki tekrar analizler/bülten
#      denemeleri krediyi tekrar harcamaz.
NOSYAPI_CACHE_TTL_HOURS = 20
#   3) Gerçek (500'ün altında) bir aylık güvenlik payı bırakılır; bütçe
#      dolunca yeni istek yapılmaz, kalan veriyle/global konsensüsle devam
#      edilir (bkz. iddaa_api.has_budget_remaining).
NOSYAPI_MONTHLY_CREDIT_BUDGET = 450

# --- Türkiye oranı SİMÜLASYONU (gerçek İddaa entegrasyonu bloke olduğu
# sürece geçici bir yaklaşım — bkz. betting_logic._apply_tr_simulation) ---
# Kullanıcının bet365/Bilyoner karşılaştırmasından türetildi (2026-09-21):
#   Norveç-Danimarka Üst 2.5:  1.73 -> 1.54  (oran: 0.890)
#   Türkiye-Fransa   Alt 2.5:  2.50 -> 2.29  (oran: 0.916)
#   Hollanda-Almanya Üst 2.5:  1.57 -> 1.37  (oran: 0.873)
#   Ortalama oran ≈ 0.893 (~%10.7 daha düşük). Sabit (flat) fark yerine
#   yüzdesel oran kullanılır çünkü düşük oranlarda (ör. 1.10) sabit bir
#   çıkarma geçersiz/anlamsız sonuç üretebilir. Arayüzden ayarlanabilir.
TR_ODDS_SIMULATION_DEFAULT_FACTOR = 0.89

# Bülten oluşturulurken taranacak ligler (scheduler.py bunu kullanır; arayüz
# aksine burada arayüzden seçim yapan biri olmadığından sabit tanımlıdır —
# değiştirmek için doğrudan bu listeyi düzenleyin).
DEFAULT_BULLETIN_LEAGUES = list(LEAGUES.keys())

# Günlük bülten: yarın (bugün + 1 gün) seçili liglerde maç varsa, bugün bu
# saatte otomatik oluşturulup Telegram'a gönderilir.
DAILY_BULLETIN_HOUR = 23
DAILY_BULLETIN_MINUTE = 55

# Haftalık bülten: her hafta bu gün/saatte, önündeki 7 günün fikstürlerini
# tarayıp (farklı günlerdeki maçları aynı kuponda birleştirebilen) bir bülten
# yayınlar. weekday: 0=Pazartesi ... 6=Pazar (Python datetime.weekday()).
WEEKLY_BULLETIN_WEEKDAY = 0  # Pazartesi
WEEKLY_BULLETIN_HOUR = 10
WEEKLY_BULLETIN_MINUTE = 0

# Bülten kuponlarında denenecek bacak sayıları (1 = "günün bankosu").
BULLETIN_LEG_COUNTS = [1, 2, 3, 4, 5, 6, 7]
# Bülten için minimum value edge (Streamlit arayüzündeki kullanıcı ayarından
# bağımsız, scheduler kendi başına çalıştığı için sabit bir eşik kullanır).
BULLETIN_MIN_EDGE = 0.02

# Zamanlayıcının her yoklamada (tick) ne sıklıkla uyanacağı (saniye) ve
# sonuç/CLV senkronizasyonunu ne sıklıkla tekrarlayacağı (dakika).
SCHEDULER_TICK_SECONDS = 60
SCHEDULER_RESULT_SYNC_MINUTES = 180
