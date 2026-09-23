"""Fikstür + zaman ağırlıklı form + çoklu bahis şirketi oranlarını birleştirip
tekli value-bet önerileri ve kombine kupon önerileri üretir."""
import json
from dataclasses import dataclass
from datetime import timedelta

import api_football as api
import betting_logic as bl
import calibration
import config
import db
import elo
import player_availability
import poisson_model as pm
import scoring


def _load_calibration():
    """model_params/settings'te (history.calibrate_probabilities ile
    kaydedilmiş) yeterli veri varsa öğrenilmiş kalibrasyon parametrelerini
    okur; yoksa (None, None) döner ve calibration.calibrate_probs bu durumda
    olasılıkları olduğu gibi bırakır (kalibrasyon öncesi davranış)."""
    a = db.get_model_param("calibration_platt_a")
    b = db.get_model_param("calibration_platt_b")
    platt_params = (a, b) if a is not None and b is not None else None
    curve_json = db.get_setting("calibration_isotonic_curve")
    isotonic_curve = json.loads(curve_json) if curve_json else None
    return platt_params, isotonic_curve


@dataclass
class AnalyzedFixture:
    fixture_id: int
    league: str
    date: str
    home_team: str
    away_team: str
    home_team_id: int
    away_team_id: int
    probs: pm.MatchProbabilities
    odds: dict
    status: str = "NS"
    actual_home_goals: int | None = None
    actual_away_goals: int | None = None
    used_elo: bool = False
    used_availability: bool = False
    error: str = ""


@dataclass
class Recommendation:
    fixture: AnalyzedFixture
    pick: bl.ValuePick
    score: scoring.ScoreBreakdown


def fetch_and_analyze(
    league_names: list[str],
    next_n: int = 5,
    force: bool = False,
    use_availability: bool = False,
    use_elo: bool = True,
    fixture_mode: str = "upcoming",
    target_date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[list[AnalyzedFixture], list[str]]:
    """Seçilen liglerdeki maçları çeker, zaman ağırlıklı Poisson + Dixon-Coles
    modeliyle (opsiyonel Elo harmanlı) olasılık hesaplar ve çoklu bahis
    şirketi oranlarıyla (konsensüs + en iyi fiyat) eşleştirir.

    fixture_mode:
      "upcoming" (varsayılan): yaklaşan (next_n) maçlar — Pro plan gerektirir.
      "backtest": config.SEASON'daki GEÇMİŞ sezondan tamamlanmış maçlar;
        sonuç bilindiğinden isabet anında ölçülebilir. Lookahead bias'ı
        önlemek için takım formu, maçın TARİHİNDEN ÖNCEKİ maçlarla hesaplanır.
      "date": yalnızca target_date (YYYY-MM-DD) gününde oynanan maçlar —
        günlük bülten (scheduler.py) bunu kullanır.
      "range": from_date-to_date (YYYY-MM-DD, dahil) aralığındaki tüm maçlar
        — haftalık bülten (scheduler.py) bunu kullanır.

    use_availability=True ise (yalnızca upcoming/date/range modlarında) her
    maç için sakat/cezalı oyuncular tespit edilip takım gücüne düzeltme
    uygulanır. use_elo=True ise, her iki takımın da DB'de en az
    config.ELO_MIN_MATCHES sonuçlanmış maçı varsa Elo tabanlı ikinci bir
    sinyal Poisson modeliyle harmanlanır. İkisi de backtest modunda
    (geçmişe dönük anlık görüntü olmadığından veri sızıntısını önlemek için)
    otomatik atlanır."""
    analyzed: list[AnalyzedFixture] = []
    warnings: list[str] = []
    is_backtest = fixture_mode == "backtest"

    for league_name in league_names:
        league_id = config.LEAGUES[league_name]
        try:
            if fixture_mode == "backtest":
                fixtures = api.get_recent_league_fixtures(league_id, config.SEASON, next_n, force=force)
            elif fixture_mode == "date":
                fixtures = api.get_fixtures_by_date(league_id, config.SEASON, target_date, force=force)
            elif fixture_mode == "range":
                fixtures = api.get_fixtures_in_range(league_id, config.SEASON, from_date, to_date, force=force)
            else:
                fixtures = api.get_fixtures(league_id, config.SEASON, next_n, force=force)
        except api.ApiFootballError as exc:
            warnings.append(f"{league_name}: fikstür alınamadı ({exc})")
            continue

        for fx in fixtures:
            fixture_id = fx["fixture"]["id"]
            home = fx["teams"]["home"]
            away = fx["teams"]["away"]
            date = fx["fixture"]["date"]
            status = fx["fixture"]["status"]["short"]
            reference_date = pm.parse_date(date)

            if is_backtest and status not in ("FT", "AET", "PEN"):
                continue

            try:
                if is_backtest:
                    to_date = (reference_date - timedelta(days=1)).strftime("%Y-%m-%d")
                    home_last = api.get_last_fixtures(home["id"], config.LAST_N_MATCHES, force=force, to_date=to_date)
                    away_last = api.get_last_fixtures(away["id"], config.LAST_N_MATCHES, force=force, to_date=to_date)
                else:
                    home_last = api.get_last_fixtures(home["id"], config.LAST_N_MATCHES, force=force)
                    away_last = api.get_last_fixtures(away["id"], config.LAST_N_MATCHES, force=force)
            except api.ApiFootballError as exc:
                warnings.append(f"{home['name']} - {away['name']}: son maç verisi alınamadı ({exc})")
                continue

            home_form = pm.build_weighted_form(home_last, home["id"], reference_date)
            away_form = pm.build_weighted_form(away_last, away["id"], reference_date)

            # Dinlenme günü / maç sıkışıklığı (yorgunluk) — ekstra API isteği
            # gerektirmez, zaten çekilmiş son-maç verisinden türetilir; bu
            # yüzden her modda (backtest dahil) her zaman uygulanır.
            home_fatigue_atk, home_fatigue_conc = pm.compute_fatigue_adjustment(home_last, home["id"], reference_date)
            away_fatigue_atk, away_fatigue_conc = pm.compute_fatigue_adjustment(away_last, away["id"], reference_date)
            home_form = pm.adjust_team_form(home_form, home_fatigue_atk, home_fatigue_conc)
            away_form = pm.adjust_team_form(away_form, away_fatigue_atk, away_fatigue_conc)

            # Lig ortalaması (league_baseline) ve oyuncu eksikliği modülündeki
            # "takım bu sezon kaç maç oynadı" bilgisi için teams/statistics
            # hâlâ ayrıca çekilir (ucuz, uzun TTL ile cache'lenir).
            season_matches_home = season_matches_away = 0
            try:
                home_stats = api.get_team_statistics(home["id"], league_id, config.SEASON, force=force)
                away_stats = api.get_team_statistics(away["id"], league_id, config.SEASON, force=force)
                if home_stats:
                    season_matches_home = pm.extract_team_form(home_stats).matches_played
                if away_stats:
                    season_matches_away = pm.extract_team_form(away_stats).matches_played
            except api.ApiFootballError as exc:
                warnings.append(f"{home['name']} - {away['name']}: sezon istatistiği alınamadı ({exc})")

            if is_backtest:
                # KRİTİK: teams/statistics uç noktası her zaman sezonun GÜNCEL
                # (bugüne kadarki) toplam istatistiğini döner — backtest'te
                # "maçın tarihinden önce" filtrelemesi yapılamaz. Bu yüzden
                # lig ortalamasını buradan almak, maçtan SONRAKİ verinin
                # modele sızmasına (lookahead bias) yol açar. Takım formu
                # (home_last/away_last) to_date ile korunuyor olsa da, lig
                # ortalaması korunmuyordu — bu yüzden backtest'te her zaman
                # sabit varsayılan lig ortalamalarını kullanıyoruz.
                league_avg_home = config.DEFAULT_LEAGUE_AVG_HOME_GOALS
                league_avg_away = config.DEFAULT_LEAGUE_AVG_AWAY_GOALS
            else:
                all_league_stats = api.cached_league_team_stats(league_id, config.SEASON)
                league_avg_home, league_avg_away = pm.league_baseline(all_league_stats)

            applied_availability = False
            applied_elo = False

            if use_availability and not is_backtest:
                try:
                    missing = player_availability.get_missing_players(fixture_id, force=force)
                    home_atk, home_conc, home_details = player_availability.compute_availability_adjustment(
                        home["id"], league_id, config.SEASON, fixture_id, season_matches_home,
                        missing_players=missing, force=force,
                    )
                    away_atk, away_conc, away_details = player_availability.compute_availability_adjustment(
                        away["id"], league_id, config.SEASON, fixture_id, season_matches_away,
                        missing_players=missing, force=force,
                    )
                    home_form = pm.adjust_team_form(home_form, home_atk, home_conc)
                    away_form = pm.adjust_team_form(away_form, away_atk, away_conc)
                    player_availability.log_availability(
                        home["id"], home["name"], league_id, config.SEASON, fixture_id, "home",
                        home_atk, home_conc, home_details,
                    )
                    player_availability.log_availability(
                        away["id"], away["name"], league_id, config.SEASON, fixture_id, "away",
                        away_atk, away_conc, away_details,
                    )
                    applied_availability = True
                except api.ApiFootballError as exc:
                    warnings.append(f"{home['name']} - {away['name']}: eksik oyuncu verisi alınamadı ({exc})")

            calibrated_rho = db.get_model_param("dixon_coles_rho", default=config.DIXON_COLES_RHO)
            probs = pm.compute_match_probabilities(
                home_form, away_form, league_avg_home, league_avg_away, rho=calibrated_rho
            )
            db.insert_match_lambdas(fixture_id, probs.lambda_home, probs.lambda_away, match_date=date)
            poisson_signal = (probs.home_win, probs.draw, probs.away_win)
            elo_signal = xg_elo_signal = None

            if use_elo and not is_backtest:
                home_advantage = elo.compute_league_home_advantage(league_name) or config.ELO_HOME_ADVANTAGE

                home_n = elo.get_matches_count(home["id"])
                away_n = elo.get_matches_count(away["id"])
                elo_ready = home_n >= config.ELO_MIN_MATCHES and away_n >= config.ELO_MIN_MATCHES

                home_xg_n = elo.get_xg_matches_count(home["id"])
                away_xg_n = elo.get_xg_matches_count(away["id"])
                xg_elo_ready = home_xg_n >= config.XG_ELO_MIN_MATCHES and away_xg_n >= config.XG_ELO_MIN_MATCHES

                # Ağırlıklar önce model_params'ta (validation.optimize_ensemble_weights
                # ile veriden öğrenilmiş) varsa oradan, yoksa config sabitlerinden
                # okunur — dixon_coles_rho ile TAM AYNI kalibrasyon deseni.
                elo_weight_2way = db.get_model_param("ensemble_elo_weight_2way", default=1 - config.ENSEMBLE_POISSON_WEIGHT)
                elo_weight_3way = db.get_model_param("ensemble_elo_weight_3way", default=config.ENSEMBLE_ELO_WEIGHT_WITH_XG)
                xg_elo_weight_3way = db.get_model_param("ensemble_xg_elo_weight_3way", default=config.ENSEMBLE_XG_ELO_WEIGHT)

                signals = []
                if elo_ready:
                    elo_probs = elo.match_probabilities(
                        elo.get_rating(home["id"]), elo.get_rating(away["id"]), home_advantage=home_advantage
                    )
                    elo_signal = elo_probs
                    elo_weight = elo_weight_3way if xg_elo_ready else elo_weight_2way
                    signals.append((elo_probs, elo_weight))
                if xg_elo_ready:
                    xg_elo_probs = elo.match_probabilities(
                        elo.get_xg_rating(home["id"]), elo.get_xg_rating(away["id"]), home_advantage=home_advantage
                    )
                    xg_elo_signal = xg_elo_probs
                    signals.append((xg_elo_probs, xg_elo_weight_3way))

                if signals:
                    probs = pm.blend_signals(probs, signals)
                    applied_elo = True

            db.insert_match_signals(fixture_id, date, poisson_signal, elo_signal, xg_elo_signal)

            try:
                odds_raw = api.get_odds(fixture_id, force=force)
                odds_by_market = api.parse_odds_all_bookmakers(odds_raw)
            except api.ApiFootballError as exc:
                warnings.append(f"{home['name']} - {away['name']}: oran alınamadı ({exc})")
                continue

            if not odds_by_market.get("match_winner"):
                warnings.append(
                    f"{home['name']} - {away['name']}: bu maç için oran verisi yok "
                    "(backtest modunda geçmiş maçlarda oran genelde tutulmuyor)"
                    if is_backtest
                    else f"{home['name']} - {away['name']}: oran bulunamadı"
                )
                continue

            goals = fx.get("goals", {}) if is_backtest else {}

            analyzed.append(
                AnalyzedFixture(
                    fixture_id=fixture_id,
                    league=league_name,
                    date=date,
                    home_team=home["name"],
                    away_team=away["name"],
                    home_team_id=home["id"],
                    away_team_id=away["id"],
                    probs=probs,
                    odds=odds_by_market,
                    status=status,
                    actual_home_goals=goals.get("home"),
                    actual_away_goals=goals.get("away"),
                    used_elo=applied_elo,
                    used_availability=applied_availability,
                )
            )

    return analyzed, warnings


MARKET_MODEL_PROBS = {
    "match_winner": lambda p: {"Home": p.home_win, "Draw": p.draw, "Away": p.away_win},
    "over_under_2_5": lambda p: {"Over": p.over_2_5, "Under": p.under_2_5},
    "btts": lambda p: {"Yes": p.btts_yes, "No": p.btts_no},
}

MARKET_LABELS = {
    "match_winner": "Maç Sonucu",
    "over_under_2_5": "Alt/Üst 2.5",
    "btts": "Karşılıklı Gol",
}

# Seçim değerleri (API'den/iç mantıktan gelen İngilizce anahtarlar) sadece
# GÖRÜNTÜLEME amaçlı Türkçeleştirilir; betting_logic/scoring/db içindeki
# eşleştirmeler (ör. db._is_correct) hâlâ orijinal İngilizce değerleri kullanır.
SELECTION_LABELS_TR = {
    "Home": "Ev Sahibi Kazanır (MS1)",
    "Draw": "Beraberlik (MSX)",
    "Away": "Deplasman Kazanır (MS2)",
    "Over": "2.5 Üst",
    "Under": "2.5 Alt",
    "Yes": "KG Var",
    "No": "KG Yok",
}


def translate_selection(selection: str) -> str:
    return SELECTION_LABELS_TR.get(selection, selection)


def build_recommendations(
    analyzed: list[AnalyzedFixture], min_edge: float = 0.02, tr_simulation_factor: float | None = None
) -> list[Recommendation]:
    """Her maç için tüm pazarları tarar, pozitif ve anlamlı edge (varsayılan
    >= %2) taşıyan seçimleri Recommendation listesi olarak döndürür, puana
    göre büyükten küçüğe sıralar.

    tr_simulation_factor verilirse (bkz. betting_logic._apply_tr_simulation),
    gösterilen oran/EV/Kelly Türkiye (İddaa) fiyatına yaklaşık bir tahminle
    küçültülür — edge hesabı (value tespiti) hâlâ gerçek global konsensüse
    göre yapılır.

    Yeterli sonuçlanmış tahmin birikmişse (bkz. history.calibrate_probabilities),
    model_probs burada calibration.calibrate_probs ile düzeltilir — yani edge/EV/
    Kelly/puan HEP kalibre edilmiş olasılığa göre hesaplanır (TR simülasyonunun
    aksine, bu düzeltme value tespitini KASITLI olarak etkiler; amacı budur)."""
    recs: list[Recommendation] = []
    platt_params, isotonic_curve = _load_calibration()

    for fx in analyzed:
        for market_key, prob_fn in MARKET_MODEL_PROBS.items():
            odds_by_bookmaker = fx.odds.get(market_key) or {}
            if not odds_by_bookmaker:
                continue
            model_probs = calibration.calibrate_probs(prob_fn(fx.probs), platt_params, isotonic_curve)
            picks = bl.evaluate_market(
                MARKET_LABELS[market_key], odds_by_bookmaker, model_probs, tr_simulation_factor=tr_simulation_factor
            )
            for pick in picks:
                if pick.edge < min_edge:
                    continue
                score = scoring.score_pick(pick, fx.probs.confident)
                recs.append(Recommendation(fixture=fx, pick=pick, score=score))

    recs.sort(key=lambda r: r.score.total, reverse=True)
    return recs


def build_recommendations_per_bookmaker(
    analyzed: list[AnalyzedFixture], min_edge: float = 0.02, tr_simulation_factor: float | None = None
) -> list[Recommendation]:
    """build_recommendations'ın aksine, aynı (maç, pazar, seçim) için TEK bir
    en iyi fiyat değil, TARANAN TÜM ŞİRKETLERİN kendi oranlarıyla ayrı ayrı
    Recommendation üretir (aynı seçim birden fazla kez, farklı şirket/oranla
    görünebilir). Yalnızca kombine kupon inşası (build_combo) için kullanılır
    — bir kombine kupon tek bir sitede oynanabildiğinden, hangi şirketin
    hangi bacakları sunduğunu bilmek gerekir; tekli öneriler için hâlâ
    build_recommendations (en iyi fiyat / line shopping) kullanılır."""
    recs: list[Recommendation] = []
    platt_params, isotonic_curve = _load_calibration()

    for fx in analyzed:
        for market_key, prob_fn in MARKET_MODEL_PROBS.items():
            odds_by_bookmaker = fx.odds.get(market_key) or {}
            if not odds_by_bookmaker:
                continue
            model_probs = calibration.calibrate_probs(prob_fn(fx.probs), platt_params, isotonic_curve)
            picks = bl.evaluate_market_per_bookmaker(
                MARKET_LABELS[market_key], odds_by_bookmaker, model_probs, tr_simulation_factor=tr_simulation_factor
            )
            for pick in picks:
                if pick.edge < min_edge:
                    continue
                score = scoring.score_pick(pick, fx.probs.confident)
                recs.append(Recommendation(fixture=fx, pick=pick, score=score))

    recs.sort(key=lambda r: r.score.total, reverse=True)
    return recs


@dataclass
class ComboSuggestion:
    legs: list[Recommendation]
    combined_odd: float
    combined_probability: float
    combined_ev: float
    combo_score: scoring.ComboScore
    recommended_stake_fraction: float


def _build_combo_from_single_bookmaker_recs(
    recs: list[Recommendation], n_legs: int, bankroll_cap: float
) -> ComboSuggestion | None:
    """Tek bir şirketin ValuePick'lerinden (recs), aynı maçtan tekrar seçim
    içermeyen en yüksek puanlı n_legs bacağı greedy olarak seçer."""
    recs_sorted = sorted(recs, key=lambda r: r.score.total, reverse=True)
    legs: list[Recommendation] = []
    used_fixtures = set()

    for rec in recs_sorted:
        if rec.fixture.fixture_id in used_fixtures:
            continue
        legs.append(rec)
        used_fixtures.add(rec.fixture.fixture_id)
        if len(legs) == n_legs:
            break

    if not legs:
        return None

    combined_odd = 1.0
    combined_probability = 1.0
    for leg in legs:
        combined_odd *= leg.pick.odd
        combined_probability *= leg.pick.model_prob

    combo_score = scoring.score_combo(
        leg_scores=[leg.score.total for leg in legs],
        fixture_ids=[leg.fixture.fixture_id for leg in legs],
        leagues=[leg.fixture.league for leg in legs],
    )
    stake_fraction = bl.kelly_fraction(combined_probability, combined_odd, cap=bankroll_cap)
    # Kombine kuponun kendi (bacakların ortalama puanından BAĞIMSIZ) gerçek
    # beklenen değeri — teknik_inceleme raporunun 6.1 maddesindeki uyarı:
    # ortalama bacak puanı yüksek olsa bile kombine EV negatif olabilir
    # (özellikle çok bacaklı kuponlarda olasılıklar çarpımsal küçüldüğü için).
    combined_ev = combined_probability * combined_odd - 1

    return ComboSuggestion(
        legs=legs,
        combined_odd=round(combined_odd, 2),
        combined_probability=round(combined_probability, 4),
        combined_ev=round(combined_ev, 4),
        combo_score=combo_score,
        recommended_stake_fraction=round(stake_fraction, 4),
    )


def build_combo(recommendations: list[Recommendation], n_legs: int, bankroll_cap: float = 0.05) -> ComboSuggestion | None:
    """En yüksek puanlı, aynı maçtan tekrar seçim içermeyen VE TEK BİR BAHİS
    ŞİRKETİNDEN gelen bacakları birleştirerek bir kombine kupon önerisi
    oluşturur. Bir kombine kupon fiziksel olarak tek bir sitede oynanabilir
    — farklı şirketlerin oranları aynı kuponda birleştirilemez. Bu yüzden
    `recommendations` parametresi tek bir 'en iyi fiyat' değil, her seçim
    için TÜM şirketlerin ayrı ayrı ValuePick'lerini içermelidir (bkz.
    build_recommendations_per_bookmaker). Her şirket için ayrı ayrı en iyi
    n_legs'lik kombinasyon denenir; önce tam istenen bacak sayısına ulaşan,
    aralarında da en yüksek kupon puanını veren şirket seçilir."""
    by_bookmaker: dict[str, list[Recommendation]] = {}
    for rec in recommendations:
        by_bookmaker.setdefault(rec.pick.bookmaker, []).append(rec)

    best_combo: ComboSuggestion | None = None
    for bookmaker_recs in by_bookmaker.values():
        candidate = _build_combo_from_single_bookmaker_recs(bookmaker_recs, n_legs, bankroll_cap)
        if candidate is None:
            continue

        if best_combo is None:
            best_combo = candidate
            continue

        candidate_full = len(candidate.legs) == n_legs
        best_full = len(best_combo.legs) == n_legs
        if candidate_full and not best_full:
            best_combo = candidate
        elif candidate_full == best_full and candidate.combo_score.total > best_combo.combo_score.total:
            best_combo = candidate

    return best_combo


def build_multiple_combos(
    recommendations: list[Recommendation],
    leg_counts: list[int] | None = None,
    bankroll_cap: float = 0.05,
) -> dict[int, ComboSuggestion]:
    """Her bacak sayısı için ayrı bir kombine kupon üretir (1 = 'günün
    bankosu' tekli seçim, ...7 = yedili kombine). `recommendations`
    build_recommendations_per_bookmaker'dan gelmelidir (her seçim için tüm
    şirketlerin ayrı ValuePick'leri) — build_combo bunu şirket bazında
    gruplayıp tek-şirket kısıtını uygular (bkz. build_combo). Yalnızca
    istenen bacak sayısına tam ulaşabilen (yeterli farklı maç bulunan)
    varyantlar döndürülür — bülten içinde eksik/yinelenen boyutlar
    gösterilmez."""
    leg_counts = leg_counts or config.BULLETIN_LEG_COUNTS
    combos: dict[int, ComboSuggestion] = {}
    for n in leg_counts:
        combo = build_combo(recommendations, n_legs=n, bankroll_cap=bankroll_cap)
        if combo is not None and len(combo.legs) == n:
            combos[n] = combo
    return combos


def compute_technique_tags(combo: ComboSuggestion) -> list[str]:
    """Bu kuponu üretirken fiilen kullanılan tekniklerin etiket listesini
    çıkarır (DB'ye loglanır — gelistirme-plani.md Bölüm 2'deki 'hangi
    mantık kullanıldı' kaydının karşılığı)."""
    tags = {
        "value_edge", "power_method_devig", "multi_bookmaker_consensus",
        "dixon_coles", "time_weighted_form", "kelly_criterion",
    }
    for leg in combo.legs:
        if leg.fixture.used_elo:
            tags.add("elo_ensemble")
        if leg.fixture.used_availability:
            tags.add("player_availability")
    if combo.combo_score.diversification_bonus > 0:
        tags.add("league_diversification")
    if len(combo.legs) == 1:
        tags.add("banko_single_pick")
    return sorted(tags)
