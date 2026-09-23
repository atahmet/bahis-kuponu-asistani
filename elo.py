"""Elo tabanlı takım güç reytingi: her sonuçlanan maçtan sonra güncellenir ve
Poisson+Dixon-Coles modeliyle harmanlanarak (ensemble) ikinci, bağımsız bir
sinyal sağlar. Tek bir modele güvenmek yerine farklı yaklaşımların (gol
tabanlı, sonuç tabanlı, xG tabanlı) ortalamasını almak, bookmaker'ların ve
profesyonel bahis gruplarının da kullandığı bir sağlamlaştırma tekniğidir.

Bu modülde İKİ ayrı Elo reytingi tutulur:
  - team_elo: gerçek maç sonucundan (galibiyet/beraberlik/mağlubiyet) güncellenir.
  - team_xg_elo: xG (expected goals) farkından güncellenir — "şans hariç
    performans" sinyali. Araştırma bulgusu: ham xG'yi doğrudan özellik olarak
    kullanmak modeli zayıflatıyor, ama bir Elo reytingine besleyerek zaman
    içinde biriktirmek işe yarıyor (bkz. gelistirme-plani.md).

Soğuk başlangıç sorunu: yeni bir takım 1500 ile başlar ve bilgi taşımaz. Bu
yüzden config.ELO_MIN_MATCHES / config.XG_ELO_MIN_MATCHES altındaki takımlar
için ilgili sinyal coupon_builder tarafında sıfırlanır — sistem birkaç hafta
gerçek sonuç/xG topladıkça kendiliğinden devreye girer. xG için eşik daha
yüksektir çünkü xG verisi her ligde/maçta bulunmayabilir.

Basitleştirme: reytingler lig bağımsız global tutulur (ligler arası tam
kalibrasyon yapılmaz); ama her tahmin zaten aynı lig içindeki iki takım
arasında olduğundan bu pratikte sorun yaratmaz. Ev sahibi avantajı ise artık
sabit değil — yeterli veri birikince compute_league_home_advantage ile lig
başına ampirik olarak hesaplanır."""
import math

import config
import db


def get_rating(team_id: int) -> float:
    rating, _ = db.get_elo(team_id)
    return rating


def get_matches_count(team_id: int) -> int:
    _, n = db.get_elo(team_id)
    return n


def get_xg_rating(team_id: int) -> float:
    rating, _ = db.get_xg_elo(team_id)
    return rating


def get_xg_matches_count(team_id: int) -> int:
    _, n = db.get_xg_elo(team_id)
    return n


def _k_factor(goal_diff: int) -> float:
    """Fark büyüdükçe (net galibiyet) reyting değişimi büyür — yaygın
    'goal difference adjusted Elo' yaklaşımı: K * ln(|fark|+1)."""
    if goal_diff == 0:
        return config.ELO_K_BASE * 0.6
    return config.ELO_K_BASE * math.log(abs(goal_diff) + 1, 2)


def _xg_k_factor(xg_diff: float) -> float:
    """_k_factor'ün xG (sürekli değer) karşılığı; tam sıfıra eşitlik yerine
    küçük bir eşik kullanır."""
    if abs(xg_diff) < 0.05:
        return config.ELO_K_BASE * 0.6
    return config.ELO_K_BASE * math.log(abs(xg_diff) + 1, 2)


def update_ratings(
    home_team_id: int, away_team_id: int, home_goals: int, away_goals: int,
    home_advantage: float | None = None,
) -> None:
    home_advantage = config.ELO_HOME_ADVANTAGE if home_advantage is None else home_advantage
    home_rating, home_n = db.get_elo(home_team_id)
    away_rating, away_n = db.get_elo(away_team_id)

    expected_home = 1 / (1 + 10 ** (-((home_rating + home_advantage) - away_rating) / 400))
    if home_goals > away_goals:
        actual_home = 1.0
    elif home_goals == away_goals:
        actual_home = 0.5
    else:
        actual_home = 0.0

    k = _k_factor(home_goals - away_goals)
    new_home = home_rating + k * (actual_home - expected_home)
    new_away = away_rating + k * ((1 - actual_home) - (1 - expected_home))

    db.set_elo(home_team_id, new_home, home_n + 1)
    db.set_elo(away_team_id, new_away, away_n + 1)


def update_xg_ratings(home_team_id: int, away_team_id: int, home_xg: float, away_xg: float) -> None:
    """team_elo ile aynı mantık, yalnızca 'kim kazandı' yerine 'kimin xG'si
    daha yüksekti' sorusuna göre günceller (küçük farklar beraberlik sayılır)."""
    home_rating, home_n = db.get_xg_elo(home_team_id)
    away_rating, away_n = db.get_xg_elo(away_team_id)

    expected_home = 1 / (1 + 10 ** (-((home_rating + config.ELO_HOME_ADVANTAGE) - away_rating) / 400))
    xg_diff = home_xg - away_xg
    # Sürekli (continuous) "actual score": xG farkını doğrudan lojistik
    # (sigmoid) fonksiyondan geçiriyoruz. Eski sürüm ±0.15 eşiğiyle xG
    # farkını W/D/L'e indirgiyordu (0.16 ile 1.20 farkı aynı "galibiyet"
    # sayılıyordu) — bu, farkın büyüklüğü hakkındaki bilgiyi kaybediyordu.
    # Sigmoid xG_diff=0 civarında yumuşakça 0.5'e yaklaşır, büyük farklarda
    # 0/1'e doğru gider; K-faktörü zaten |xg_diff|'e göre ölçekleniyor,
    # bu ikisi birlikte hem yönü hem büyüklüğü koruyor.
    actual_home = 1 / (1 + math.exp(-xg_diff / config.XG_ELO_ACTUAL_SCALE))

    k = _xg_k_factor(xg_diff)
    new_home = home_rating + k * (actual_home - expected_home)
    new_away = away_rating + k * ((1 - actual_home) - (1 - expected_home))

    db.set_xg_elo(home_team_id, new_home, home_n + 1)
    db.set_xg_elo(away_team_id, new_away, away_n + 1)


def match_probabilities(
    home_rating: float, away_rating: float, home_advantage: float | None = None
) -> tuple[float, float, float]:
    """(p_home_win, p_draw, p_away_win) döner. team_elo VEYA team_xg_elo
    reytingleriyle çağrılabilir (ikisi de aynı ölçekte).

    Yöntem (basitleştirilmiş, dokümante edilmiş): Elo farkından standart
    lojistik formülle 'beklenen puan payı' (E_home, ~ P(kaybetmemek))
    hesaplanır. Bunu üç sonuca ayırmak için, iki takım ne kadar yakınsa
    (E_home ~ 0.5) beraberlik olasılığının o kadar yüksek olacağını varsayan
    simetrik bir çan eğrisi kullanılır. Bu, tam bir ordinal regresyon kadar
    hassas değildir ama Poisson modeliyle harmanlanan ikinci bir sinyal için
    yeterli ve şeffaftır."""
    home_advantage = config.ELO_HOME_ADVANTAGE if home_advantage is None else home_advantage
    diff = (home_rating + home_advantage) - away_rating
    e_home = 1 / (1 + 10 ** (-diff / 400))

    closeness = 1 - abs(2 * e_home - 1)  # e_home=0.5 iken 1, uçlarda 0
    p_draw = config.ELO_MAX_DRAW_PROB * closeness

    p_home = max(e_home - p_draw / 2, 0.0)
    p_away = max((1 - e_home) - p_draw / 2, 0.0)

    total = p_home + p_draw + p_away
    if total <= 0:
        return 1 / 3, 1 / 3, 1 / 3
    return p_home / total, p_draw / total, p_away / total


def compute_league_home_advantage(league_name: str, min_matches: int | None = None) -> float | None:
    """Sabit config.ELO_HOME_ADVANTAGE yerine, bir ligin kendi sonuçlanmış
    maçlarından ampirik ev sahibi avantajını Elo puanına çevirir.

    Mantık: lig içindeki takımların ortalama gücü birbirine göre ~0 olduğundan
    (Elo tanımı gereği göreceli), ev sahiplerinin ortalama puan payı
    (galibiyet=1, beraberlik=0.5, mağlubiyet=0) yalnızca ev sahibi
    avantajından kaynaklanan 'beklenen skor'a karşılık gelir. Bu, standart
    Elo beklenen-skor formülü tersine çevrilerek bir Elo puanına dönüştürülür.

    Yeterli veri (min_matches) yoksa None döner — çağıran taraf
    config.ELO_HOME_ADVANTAGE'a geri dönmelidir."""
    min_matches = min_matches or config.LEAGUE_HOME_ADV_MIN_MATCHES
    results = db.get_league_results(league_name)
    if len(results) < min_matches:
        return None

    points = 0.0
    for r in results:
        if r["home_goals"] > r["away_goals"]:
            points += 1.0
        elif r["home_goals"] == r["away_goals"]:
            points += 0.5
    avg_home_points_share = points / len(results)
    avg_home_points_share = min(max(avg_home_points_share, 0.01), 0.99)  # log tanımsızlığına karşı sınırla

    return -400 * math.log10(1 / avg_home_points_share - 1)
