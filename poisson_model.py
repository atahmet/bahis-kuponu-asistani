"""Zaman ağırlıklı Dixon-Coles tarzı Poisson gol modeli: takımların son N
maçından (üstel zaman ağırlığıyla) hücum/defans gücü çıkarılır, düşük skorlu
sonuçlar için korelasyon düzeltmesi uygulanır ve maç sonucu, alt/üst 2.5,
KG var/yok olasılıkları hesaplanır.

Yöntem:
  lambda_home = lig_ort_ev_sahibi_gol * ev_sahibi_hucum_gucu * deplasman_defans_gucu
  lambda_away = lig_ort_deplasman_gol * deplasman_hucum_gucu * ev_sahibi_defans_gucu

Hücum/defans güçleri, sezon ortalaması yerine takımın son LAST_N_MATCHES
maçından (tüm turnuvalar dahil), her maça exp(-ln(2)*gün/yarı_ömür) ağırlığı
verilerek hesaplanır (Dixon & Coles 1997'deki zaman ağırlıklandırma fikri).
Ayrıca 0-0/1-0/0-1/1-1 skorlarına aynı makaledeki rho düzeltmesi uygulanır.

Not: Lig ortalaması hâlâ o ana kadar cache'lenmiş takımların (teams/statistics
uç noktası) sezonluk ortalamasından hesaplanır — tüm lig taranmadığından kaba
bir yaklaşımdır; bkz. league_baseline().
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import config

MAX_GOALS = 6


def parse_date(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


def _poisson_pmf(lam: float, k: int) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam**k) / math.factorial(k)


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _decay_weight(days_since: float, half_life_days: float) -> float:
    days_since = max(days_since, 0.0)
    return math.exp(-math.log(2) * days_since / half_life_days)


def _dc_tau(home_goals: int, away_goals: int, lambda_home: float, lambda_away: float, rho: float) -> float:
    """Dixon-Coles (1997) düşük skor korelasyon düzeltmesi."""
    if home_goals == 0 and away_goals == 0:
        return 1 - lambda_home * lambda_away * rho
    if home_goals == 0 and away_goals == 1:
        return 1 + lambda_home * rho
    if home_goals == 1 and away_goals == 0:
        return 1 + lambda_away * rho
    if home_goals == 1 and away_goals == 1:
        return 1 - rho
    return 1.0


@dataclass
class TeamForm:
    scored_home: float
    conceded_home: float
    scored_away: float
    conceded_away: float
    matches_played: int
    effective_weight: float = 0.0  # zaman ağırlıklı "etkin" örneklem büyüklüğü


def extract_team_form(stats_json: dict) -> TeamForm:
    """teams/statistics (sezon ortalaması) cevabından TeamForm çıkarır.
    Artık maç sonucu olasılıklarının ana girdisi DEĞİL (bkz. build_weighted_form);
    sadece lig ortalaması (league_baseline) ve oyuncu eksikliği modülündeki
    'takımın bu sezon kaç maç oynadığı' bilgisi için kullanılır."""
    goals = stats_json.get("goals", {})
    fixtures = stats_json.get("fixtures", {})
    return TeamForm(
        scored_home=_safe_float(goals.get("for", {}).get("average", {}).get("home")),
        conceded_home=_safe_float(goals.get("against", {}).get("average", {}).get("home")),
        scored_away=_safe_float(goals.get("for", {}).get("average", {}).get("away")),
        conceded_away=_safe_float(goals.get("against", {}).get("average", {}).get("away")),
        matches_played=int(fixtures.get("played", {}).get("total") or 0),
    )


def build_weighted_form(
    fixtures_json: list, team_id: int, reference_date: datetime, half_life_days: float | None = None
) -> TeamForm:
    """Bir takımın son maçlarından (api_football.get_last_fixtures) zaman
    ağırlıklı hücum/defans formunu çıkarır. Eski sezona sarkan maçlar
    otomatik olarak düşük ağırlık alır (sabit bir 'sezon başı güvenilirliği
    düşük' kuralına gerek kalmaz)."""
    half_life_days = half_life_days or config.TIME_DECAY_HALF_LIFE_DAYS
    home_scored = home_conceded = home_weight = 0.0
    away_scored = away_conceded = away_weight = 0.0
    matches_played = 0

    for fx in fixtures_json:
        status = fx.get("fixture", {}).get("status", {}).get("short")
        if status not in ("FT", "AET", "PEN"):
            continue
        goals = fx.get("goals", {})
        gh, ga = goals.get("home"), goals.get("away")
        if gh is None or ga is None:
            continue

        fx_date = parse_date(fx["fixture"]["date"])
        days_since = (reference_date - fx_date).total_seconds() / 86400
        w = _decay_weight(days_since, half_life_days)

        home_id = fx["teams"]["home"]["id"]
        away_id = fx["teams"]["away"]["id"]
        matches_played += 1

        if home_id == team_id:
            home_scored += w * gh
            home_conceded += w * ga
            home_weight += w
        elif away_id == team_id:
            away_scored += w * ga
            away_conceded += w * gh
            away_weight += w

    total_weight = home_weight + away_weight

    if home_weight > 0:
        scored_home = home_scored / home_weight
        conceded_home = home_conceded / home_weight
    elif total_weight > 0:
        scored_home = (home_scored + away_scored) / total_weight
        conceded_home = (home_conceded + away_conceded) / total_weight
    else:
        scored_home = conceded_home = 0.0

    if away_weight > 0:
        scored_away = away_scored / away_weight
        conceded_away = away_conceded / away_weight
    elif total_weight > 0:
        scored_away = (home_scored + away_scored) / total_weight
        conceded_away = (home_conceded + away_conceded) / total_weight
    else:
        scored_away = conceded_away = 0.0

    return TeamForm(
        scored_home=scored_home,
        conceded_home=conceded_home,
        scored_away=scored_away,
        conceded_away=conceded_away,
        matches_played=matches_played,
        effective_weight=total_weight,
    )


def compute_fatigue_adjustment(
    fixtures_json: list, team_id: int, reference_date: datetime
) -> tuple[float, float]:
    """Dinlenme günü / maç sıkışıklığı etkisini hesaplar (ekstra API isteği
    gerektirmez — zaten çekilmiş son-maç verisinden türetilir). İki bileşen:
      - Az dinlenme (config.FATIGUE_MIN_REST_DAYS'in altı): art arda maça
        çıkan bir takım hafifçe zayıflar.
      - Sıkışıklık (son 14 günde config.CONGESTION_MATCH_THRESHOLD'dan fazla
        maç): ek bir küçük ceza.
    Döner: (attack_multiplier<=1, concede_multiplier>=1) — adjust_team_form
    ile aynı şekilde kullanılır. Araştırmaya göre gerçek etkisi küçük
    (~%0.5-1) olduğundan tavanlar düşük tutulmuştur."""
    finished = []
    for fx in fixtures_json:
        status = fx.get("fixture", {}).get("status", {}).get("short")
        if status not in ("FT", "AET", "PEN"):
            continue
        home_id = fx["teams"]["home"]["id"]
        away_id = fx["teams"]["away"]["id"]
        if team_id not in (home_id, away_id):
            continue
        finished.append(parse_date(fx["fixture"]["date"]))

    if not finished:
        return 1.0, 1.0

    finished.sort(reverse=True)
    last_match_date = finished[0]
    rest_days = (reference_date - last_match_date).total_seconds() / 86400
    matches_last_14_days = sum(1 for d in finished if 0 <= (reference_date - d).days <= 14)

    penalty = 0.0
    if rest_days < config.FATIGUE_MIN_REST_DAYS:
        shortfall = config.FATIGUE_MIN_REST_DAYS - max(rest_days, 0)
        penalty += min(shortfall / config.FATIGUE_MIN_REST_DAYS, 1.0) * config.FATIGUE_MAX_PENALTY
    if matches_last_14_days > config.CONGESTION_MATCH_THRESHOLD:
        penalty += config.CONGESTION_PENALTY

    attack_multiplier = max(1 - penalty, 1 - config.FATIGUE_MAX_PENALTY - config.CONGESTION_PENALTY)
    concede_multiplier = min(1 + penalty, 1 + config.FATIGUE_MAX_PENALTY + config.CONGESTION_PENALTY)
    return attack_multiplier, concede_multiplier


def adjust_team_form(form: TeamForm, attack_multiplier: float = 1.0, concede_multiplier: float = 1.0) -> TeamForm:
    """Eksik oyuncuların etkisini forma uygular: attack_multiplier<=1 gol
    ortalamasını düşürür (hücumda önemli oyuncu eksik), concede_multiplier>=1
    yenilen gol ortalamasını artırır (savunmada önemli oyuncu eksik)."""
    return TeamForm(
        scored_home=form.scored_home * attack_multiplier,
        conceded_home=form.conceded_home * concede_multiplier,
        scored_away=form.scored_away * attack_multiplier,
        conceded_away=form.conceded_away * concede_multiplier,
        matches_played=form.matches_played,
        effective_weight=form.effective_weight,
    )


def league_baseline(all_team_stats_json: list) -> tuple[float, float]:
    """Cache'deki tüm takım istatistiklerinden lig ortalama ev/deplasman golünü
    hesaplar. Yeterli veri yoksa varsayılan sabitlere döner."""
    home_scored, away_scored, n = 0.0, 0.0, 0
    for stats in all_team_stats_json:
        form = extract_team_form(stats)
        if form.matches_played == 0:
            continue
        home_scored += form.scored_home
        away_scored += form.scored_away
        n += 1
    if n < 4:
        return config.DEFAULT_LEAGUE_AVG_HOME_GOALS, config.DEFAULT_LEAGUE_AVG_AWAY_GOALS
    return home_scored / n, away_scored / n


@dataclass
class MatchProbabilities:
    lambda_home: float
    lambda_away: float
    home_win: float
    draw: float
    away_win: float
    over_2_5: float
    under_2_5: float
    btts_yes: float
    btts_no: float
    confident: bool


def compute_match_probabilities(
    home_form: TeamForm,
    away_form: TeamForm,
    league_avg_home: float,
    league_avg_away: float,
    rho: float | None = None,
) -> MatchProbabilities:
    if rho is None:
        rho = config.DIXON_COLES_RHO

    home_attack = home_form.scored_home / league_avg_home if league_avg_home else 1.0
    away_defense = away_form.conceded_away / league_avg_home if league_avg_home else 1.0
    away_attack = away_form.scored_away / league_avg_away if league_avg_away else 1.0
    home_defense = home_form.conceded_home / league_avg_away if league_avg_away else 1.0

    lambda_home = max(league_avg_home * home_attack * away_defense, 0.05)
    lambda_away = max(league_avg_away * away_attack * home_defense, 0.05)

    matrix = [
        [_poisson_pmf(lambda_home, i) * _poisson_pmf(lambda_away, j) for j in range(MAX_GOALS + 1)]
        for i in range(MAX_GOALS + 1)
    ]

    # Dixon-Coles düşük skor düzeltmesi (yalnızca 0/1 gollük hücreler)
    for i in (0, 1):
        for j in (0, 1):
            matrix[i][j] *= _dc_tau(i, j, lambda_home, lambda_away, rho)

    total_mass = sum(sum(row) for row in matrix)
    if total_mass > 0:
        matrix = [[v / total_mass for v in row] for row in matrix]

    home_win = draw = away_win = 0.0
    over_2_5 = btts_yes = 0.0

    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            p = matrix[i][j]
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p
            if i + j >= 3:
                over_2_5 += p
            if i >= 1 and j >= 1:
                btts_yes += p

    total = home_win + draw + away_win
    if total > 0:
        home_win, draw, away_win = home_win / total, draw / total, away_win / total

    confident = (
        home_form.effective_weight >= config.MIN_MATCHES_FOR_CONFIDENCE
        and away_form.effective_weight >= config.MIN_MATCHES_FOR_CONFIDENCE
    )

    return MatchProbabilities(
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        home_win=home_win,
        draw=draw,
        away_win=away_win,
        over_2_5=over_2_5,
        under_2_5=1 - over_2_5,
        btts_yes=btts_yes,
        btts_no=1 - btts_yes,
        confident=confident,
    )


def blend_signals(
    probs: MatchProbabilities, signals: list[tuple[tuple[float, float, float], float]]
) -> MatchProbabilities:
    """1X2 olasılığını, Poisson+Dixon-Coles modeliyle bir veya daha fazla ek
    sinyalin (ör. Elo, xG-Elo) ağırlıklı ortalamasıyla harmanlar (ensemble).
    `signals`: [(p_home_draw_away_üçlüsü, ağırlık), ...] — ağırlıkların
    toplamı 1'i geçmemeli, kalan pay Poisson'a ait olur. Alt/üst ve KG
    pazarları saf Poisson kalır (diğer sinyallerin gol bazlı bir karşılığı yok)."""
    base_weight = 1.0 - sum(w for _, w in signals)
    home_win = probs.home_win * base_weight
    draw = probs.draw * base_weight
    away_win = probs.away_win * base_weight

    for (p_home, p_draw, p_away), w in signals:
        home_win += p_home * w
        draw += p_draw * w
        away_win += p_away * w

    total = home_win + draw + away_win
    if total > 0:
        home_win, draw, away_win = home_win / total, draw / total, away_win / total

    return MatchProbabilities(
        lambda_home=probs.lambda_home,
        lambda_away=probs.lambda_away,
        home_win=home_win,
        draw=draw,
        away_win=away_win,
        over_2_5=probs.over_2_5,
        under_2_5=probs.under_2_5,
        btts_yes=probs.btts_yes,
        btts_no=probs.btts_no,
        confident=probs.confident,
    )


def blend_with_elo(
    probs: MatchProbabilities, elo_probs: tuple[float, float, float], poisson_weight: float
) -> MatchProbabilities:
    """blend_signals'ın tek-sinyalli (yalnızca Elo) kısayolu — geriye dönük
    uyumluluk için tutulur."""
    return blend_signals(probs, [(elo_probs, 1.0 - poisson_weight)])


def fit_dixon_coles_rho(
    settled_matches: list[dict],
    candidate_range: tuple[float, float] | None = None,
    step: float | None = None,
    min_matches: int | None = None,
) -> float | None:
    """Dixon-Coles rho'sunu, o ana kadar sonuçlanmış ve lambda'ları bilinen
    maçların gerçek skorlarına karşı log-likelihood'u maksimize eden değere
    grid search ile kalibre eder (MLE-lite: yalnızca rho, takım güçleri/
    lambda'lar zaten hesaplanmış kabul edilir — tam Dixon-Coles makalesindeki
    gibi ortak bir yeniden-tahmin YAPILMAZ, bu bilinen bir basitleştirmedir).

    settled_matches: [{"lambda_home", "lambda_away", "home_goals", "away_goals",
    "match_date"}, ...] (bkz. db.get_settled_matches_with_lambdas). "match_date"
    yoksa/None ise o maç ağırlık=1.0 ile (yaşı bilinmiyor kabul edilip) dahil
    edilir. Yeterli veri yoksa None döner.

    Diğer tüm sinyaller (takım formu, Elo) zaten TIME_DECAY_HALF_LIFE_DAYS ile
    zaman ağırlıklı iken rho fitting eskiden TÜM maçları eşit ağırlıklandırıyordu
    — tutarlılık için burada da aynı yarı ömür ile üstel zaman ağırlığı
    uygulanır (en yeni maç ~1.0, half_life_days öncesi ~0.5 ağırlık alır)."""
    min_matches = min_matches or config.DIXON_COLES_FIT_MIN_MATCHES
    if len(settled_matches) < min_matches:
        return None

    candidate_range = candidate_range or config.DIXON_COLES_RHO_SEARCH_RANGE
    step = step or config.DIXON_COLES_RHO_SEARCH_STEP

    now = datetime.now(timezone.utc)
    weights = []
    for m in settled_matches:
        match_date = m.get("match_date")
        if match_date:
            days_since = (now - parse_date(match_date)).total_seconds() / 86400
            weights.append(_decay_weight(days_since, config.TIME_DECAY_HALF_LIFE_DAYS))
        else:
            weights.append(1.0)

    best_rho = None
    best_log_likelihood = float("-inf")

    rho = candidate_range[0]
    while rho <= candidate_range[1] + 1e-9:
        log_likelihood = 0.0
        for m, w in zip(settled_matches, weights):
            lh, la = m["lambda_home"], m["lambda_away"]
            hg, ag = m["home_goals"], m["away_goals"]
            if hg > MAX_GOALS or ag > MAX_GOALS:
                continue
            p = _poisson_pmf(lh, hg) * _poisson_pmf(la, ag)
            if hg <= 1 and ag <= 1:
                p *= _dc_tau(hg, ag, lh, la, rho)
            p = max(p, 1e-10)
            log_likelihood += w * math.log(p)

        if log_likelihood > best_log_likelihood:
            best_log_likelihood = log_likelihood
            best_rho = rho
        rho += step

    return best_rho
