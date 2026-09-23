"""Haftalık analiz çıktılarını DB'ye loglar, sonuçlanan maçları API'den çekip
geçmiş tahminlerle eşleştirir, Elo reytingini günceller, kapanış oranlarıyla
CLV (closing line value) hesaplar ve kalibrasyon raporu üretir
(gelistirme-plani.md Bölüm 2 + betting_logic geliştirmeleri)."""
import json

import api_football as api
import calibration
import config
import coupon_builder as cb
import db
import elo
import poisson_model as pm
import validation
from coupon_builder import MARKET_LABELS

REVERSE_MARKET_LABELS = {v: k for k, v in MARKET_LABELS.items()}


def _settle_and_update_ratings(
    fixture_id: int, home_goals: int, away_goals: int, status: str,
    home_team_id: int, away_team_id: int,
    league_id: int | None = None, league: str | None = None,
    fetch_xg: bool = True, force: bool = False,
) -> None:
    """Bir maçın sonucunu results tablosuna yazar; İLK KEZ kaydediliyorsa
    (daha önce bilinmiyorsa) gerçek-sonuç Elo'sunu günceller ve mümkünse
    xG istatistiğini çekip xG-Elo'yu da günceller (bkz. elo.py, config.py
    'xG tabanlı ikinci Elo' bölümü). Zaten bilinen bir sonuç tekrar
    işlenirse hiçbir reyting güncellenmez (idempotent)."""
    already_known = db.has_result(fixture_id)
    db.upsert_result(
        fixture_id, home_goals, away_goals, status, home_team_id, away_team_id,
        league_id=league_id, league=league,
    )
    if already_known:
        return

    elo.update_ratings(home_team_id, away_team_id, home_goals, away_goals)

    if fetch_xg:
        try:
            stats = api.get_fixture_statistics(fixture_id, force=force)
            home_xg, away_xg = api.extract_xg(stats, home_team_id, away_team_id)
        except api.ApiFootballError:
            home_xg = away_xg = None
        if home_xg is not None and away_xg is not None:
            db.set_result_xg(fixture_id, home_xg, away_xg)
            elo.update_xg_ratings(home_team_id, away_team_id, home_xg, away_xg)


def _prediction_key(fixture_id: int, market: str, selection: str, bookmaker: str) -> tuple:
    """bookmaker anahtarın parçasıdır: aynı (maç, pazar, seçim) farklı
    şirketlerde farklı oranlarla loglanabilir (kombine kupon tek-şirket
    kısıtı nedeniyle — bkz. coupon_builder.build_combo)."""
    return (fixture_id, market, selection, bookmaker)


def _insert_prediction_row(rec) -> int:
    fx = rec.fixture
    return db.insert_prediction(
        fixture_id=fx.fixture_id,
        league=fx.league,
        home_team=fx.home_team,
        away_team=fx.away_team,
        market=rec.pick.market,
        selection=rec.pick.selection,
        odd=rec.pick.odd,
        bookmaker=rec.pick.bookmaker,
        model_prob=rec.pick.model_prob,
        implied_prob=rec.pick.implied_prob,
        edge=rec.pick.edge,
        kelly_fraction=rec.pick.kelly_fraction,
        score_total=rec.score.total,
        match_date=fx.date,
        confident=fx.probs.confident,
        playable=rec.score.is_playable(),
    )


def log_predictions(recommendations: list) -> dict:
    """Üretilen tüm tekli tahminleri predictions tablosuna yazar (her biri
    yalnızca BİR KEZ). (fixture_id, market, selection, bookmaker) ->
    prediction_id eşlemesini döndürür — birden fazla kupon varyantı
    (1'li...7'li) aynı tahminleri paylaştığında bu id'ler tekrar kullanılır,
    yinelenen satır oluşmaz. Bu id_map, daha sonra kullanıcı analizi
    adlandırıp kaydetmek isterse db.set_predictions_analysis_id ile yeniden
    kullanılabilir."""
    id_map = {}
    for rec in recommendations:
        pred_id = _insert_prediction_row(rec)
        fx = rec.fixture
        id_map[_prediction_key(fx.fixture_id, rec.pick.market, rec.pick.selection, rec.pick.bookmaker)] = pred_id
    return id_map


def log_coupon(
    combo, id_map: dict, bulletin_id: int | None = None, analysis_id: int | None = None,
    technique_tags: list[str] | None = None,
) -> int | None:
    """Bir ComboSuggestion'ı, log_predictions'ın döndürdüğü id_map'teki
    mevcut prediction id'lerine referans vererek coupons/coupon_legs'e yazar.

    Kombine kupon tek-şirket kısıtı nedeniyle (bkz. coupon_builder.build_combo)
    bir bacağın (maç, pazar, seçim, şirket) kombinasyonu id_map'te henüz
    yoksa (ör. bu bacak en-iyi-fiyat şirketinden değil, kuponun oynandığı
    şirketten geliyorsa) burada YENİ bir prediction satırı eklenir ve
    id_map'e (mutasyonla) eklenir — hiçbir bacak sessizce atlanmaz."""
    leg_ids = []
    for leg in combo.legs:
        fx = leg.fixture
        key = _prediction_key(fx.fixture_id, leg.pick.market, leg.pick.selection, leg.pick.bookmaker)
        pred_id = id_map.get(key)
        if pred_id is None:
            pred_id = _insert_prediction_row(leg)
            id_map[key] = pred_id
        leg_ids.append(pred_id)

    return db.insert_coupon(
        n_legs=len(combo.legs),
        combined_odd=combo.combined_odd,
        combined_probability=combo.combined_probability,
        combo_score=combo.combo_score.total,
        stake_fraction=combo.recommended_stake_fraction,
        leg_prediction_ids=leg_ids,
        bulletin_id=bulletin_id,
        analysis_id=analysis_id,
        technique_tags=technique_tags,
    )


def log_analysis_run(recommendations: list, combo=None) -> dict:
    """Geriye dönük uyumluluk için ince sarmalayıcı: tek bir analiz + tek
    (opsiyonel) kombine kupon senaryosu (Streamlit arayüzü bunu kullanır).
    Çoklu bacak sayılı bülten kuponları için log_predictions + log_coupon'u
    doğrudan kullanın (bkz. bulletin.py)."""
    id_map = log_predictions(recommendations)
    if combo is not None:
        log_coupon(combo, id_map)
    return id_map


def save_named_analysis(
    name: str, leagues: list[str], fixture_mode: str, recommendations: list,
    recs_per_bookmaker: list | None = None,
    id_map: dict | None = None, leg_counts: list[int] | None = None,
) -> int:
    """Kullanıcının 'Analizi Kaydet' eylemi: analizi bir isimle DB'ye
    kalıcı olarak bağlar ve tüm bacak-sayısı varyantlarını (1-7, tek-şirket
    kısıtına uygun — bkz. coupon_builder.build_combo) kupon olarak üretip
    loglar — arayüzden daha sonra isimle geri çağrılabilir ve her kupon için
    Telegram'a gönderme butonu kullanılabilir.

    id_map verilmezse (bu çalıştırma daha önce log_predictions ile
    loglanmadıysa) tahminler burada ilk kez yazılır; verilmişse (log_to_history
    açıkken zaten loglanmışsa) YENİDEN KULLANILIR — aynı tahmin için
    yinelenen satır oluşmaz. recs_per_bookmaker verilmezse, kombine kupon
    varyantları (tek-şirket kısıtı) doğru kurulamayacağından recommendations
    (en iyi fiyat, karışık şirket) kullanılır — bu durumda tek bacaklı ('1'li
    banko') varyant hâlâ geçerlidir ama 2+ bacaklı varyantlar geçersiz
    (farklı şirket) olabilir; mümkünse her zaman recs_per_bookmaker verin."""
    if id_map is None:
        id_map = log_predictions(recommendations)

    analysis_id = db.insert_analysis(name, leagues, fixture_mode)

    combos = cb.build_multiple_combos(recs_per_bookmaker or recommendations, leg_counts=leg_counts)
    for combo in combos.values():
        tags = cb.compute_technique_tags(combo)
        log_coupon(combo, id_map, analysis_id=analysis_id, technique_tags=tags)

    # log_coupon, id_map'te bulunmayan bacakları (farklı şirket) anında ekler
    # ve id_map'i büyütür — bu yüzden analysis_id ataması en son, tüm
    # kuponlar loglandıktan SONRA yapılır (yeni eklenen id'ler de dahil olsun).
    db.set_predictions_analysis_id(list(id_map.values()), analysis_id)

    return analysis_id


def sync_results(force: bool = False) -> int:
    """Sonucu henüz bilinmeyen tahminlerin maçlarını API'den 20'şerli
    gruplar halinde kontrol eder, biteni results tablosuna yazar, ilgili
    tahminleri sonuçlandırır ve İLK KEZ kaydedilen sonuçlar için Elo
    reytingini günceller (aynı fixture tekrar senkronize edilirse Elo
    ikinci kez güncellenmez). Sonuçlanan tahmin sayısını döndürür."""
    pending_ids = db.get_pending_fixture_ids()
    if not pending_ids:
        return 0

    newly_finished = 0
    for i in range(0, len(pending_ids), 20):
        batch = pending_ids[i : i + 20]
        try:
            fixtures = api.get_fixtures_by_ids(batch, force=force)
        except api.ApiFootballError:
            continue
        for fx in fixtures:
            status = fx["fixture"]["status"]["short"]
            if status not in ("FT", "AET", "PEN"):
                continue
            goals = fx.get("goals", {})
            home_goals, away_goals = goals.get("home"), goals.get("away")
            if home_goals is None or away_goals is None:
                continue

            league_info = fx.get("league", {})
            _settle_and_update_ratings(
                fixture_id=fx["fixture"]["id"],
                home_goals=home_goals,
                away_goals=away_goals,
                status=status,
                home_team_id=fx["teams"]["home"]["id"],
                away_team_id=fx["teams"]["away"]["id"],
                league_id=league_info.get("id"),
                league=league_info.get("name"),
                force=force,
            )
            newly_finished += 1

    if newly_finished:
        return db.settle_predictions()
    return 0


def update_closing_odds(force: bool = True) -> int:
    """Henüz sonuçlanmamış ve kapanış oranı kaydedilmemiş tahminler için
    güncel oranları çeker; mümkünse aynı bahis şirketinin (tahmin
    loglanırken kullanılan) güncel fiyatını, yoksa tercih edilen ilk
    şirketin fiyatını 'kapanış oranı' olarak kaydeder ve CLV% hesaplar.
    CLV% = (bahis_yapılan_oran / kapanış_oranı - 1) * 100; pozitif CLV,
    piyasanın bizden sonra bizim lehimize hareket ettiğini gösterir —
    profesyonel bahisçilerin kullandığı en güvenilir uzun vadeli edge ölçütü."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, fixture_id, market, selection, odd, bookmaker
            FROM predictions
            WHERE result_settled = 0 AND closing_odd IS NULL
            """
        ).fetchall()

    if not rows:
        return 0

    by_fixture: dict[int, list] = {}
    for row in rows:
        by_fixture.setdefault(row["fixture_id"], []).append(row)

    updated = 0
    for fixture_id, preds in by_fixture.items():
        try:
            odds_raw = api.get_odds(fixture_id, force=force)
            odds_by_market = api.parse_odds_all_bookmakers(odds_raw)
        except api.ApiFootballError:
            continue

        for pred in preds:
            market_key = REVERSE_MARKET_LABELS.get(pred["market"])
            if market_key is None:
                continue
            bookmakers = odds_by_market.get(market_key, {})
            if not bookmakers:
                continue

            closing_odd = None
            preferred = bookmakers.get(pred["bookmaker"])
            if preferred and preferred.get(pred["selection"]):
                closing_odd = preferred[pred["selection"]]
            else:
                for pref_name in config.PREFERRED_BOOKMAKERS:
                    candidate = bookmakers.get(pref_name)
                    if candidate and candidate.get(pred["selection"]):
                        closing_odd = candidate[pred["selection"]]
                        break
                if closing_odd is None:
                    any_book = next(iter(bookmakers.values()))
                    closing_odd = any_book.get(pred["selection"])

            if not closing_odd:
                continue

            clv_pct = (pred["odd"] / closing_odd - 1) * 100
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE predictions SET closing_odd = ?, clv_pct = ? WHERE id = ?",
                    (closing_odd, clv_pct, pred["id"]),
                )
            updated += 1

    return updated


def settle_from_analyzed(analyzed: list) -> int:
    """Backtest modu için: analiz sırasında zaten bilinen (geçmiş, tamamlanmış)
    maç sonuçlarını doğrudan DB'ye yazar, Elo'yu günceller ve ilgili
    tahminleri AYNI ÇALIŞTIRMADA sonuçlandırır — ayrı bir 'sonuç senkronize
    et' adımı beklemeye gerek kalmaz, isabet anında görülebilir."""
    settled_any = False
    for fx in analyzed:
        if fx.actual_home_goals is None or fx.actual_away_goals is None:
            continue
        _settle_and_update_ratings(
            fixture_id=fx.fixture_id,
            home_goals=fx.actual_home_goals,
            away_goals=fx.actual_away_goals,
            status=fx.status,
            home_team_id=fx.home_team_id,
            away_team_id=fx.away_team_id,
            league_id=config.LEAGUES.get(fx.league),
            league=fx.league,
            fetch_xg=False,  # backtest: eski sezon maçlarında xG API'de tutarsız/eksik olabilir, ayrıca gerekmez
        )
        settled_any = True

    if settled_any:
        return db.settle_predictions()
    return 0


def calibrate_dixon_coles_rho() -> float | None:
    """Yeterli sonuçlanmış maç birikince (config.DIXON_COLES_FIT_MIN_MATCHES),
    Dixon-Coles rho'sunu gerçek verilerden yeniden kalibre eder ve DB'ye
    (model_params) kaydeder — sonraki analizler otomatik olarak bu değeri
    kullanır (bkz. coupon_builder.fetch_and_analyze). Yetersiz veride hiçbir
    şey yapmaz ve None döner (mevcut sabit değer kullanılmaya devam eder)."""
    settled_matches = db.get_settled_matches_with_lambdas()
    fitted_rho = pm.fit_dixon_coles_rho(settled_matches)
    if fitted_rho is not None:
        db.set_model_param("dixon_coles_rho", fitted_rho)
    return fitted_rho


def calibrate_ensemble_weights() -> dict:
    """Elo/xG-Elo harman ağırlıklarını, DB'de biriken sonuçlanmış canlı
    tahminlerin log-loss'una göre yeniden kalibre eder ve (yeterli veri olan
    gruplar için) model_params'a kaydeder — bkz. validation.py."""
    result = validation.optimize_ensemble_weights()
    validation.apply_ensemble_weights(result)
    return result


def calibrate_probabilities() -> dict:
    """model_prob'un gerçek isabet oranıyla ne kadar örtüştüğünü DB'deki
    sonuçlanmış tahminlerden öğrenip Platt/isotonic parametrelerini kaydeder
    — bir sonraki analizden itibaren coupon_builder._load_calibration bunları
    otomatik okuyup uygular. Yetersiz veride ilgili yöntem None kalır (mevcut
    kalibrasyonsuz davranış korunur)."""
    pairs = db.get_calibration_pairs()
    platt = calibration.fit_platt(pairs)
    isotonic = calibration.fit_isotonic(pairs)
    if platt is not None:
        db.set_model_param("calibration_platt_a", platt[0])
        db.set_model_param("calibration_platt_b", platt[1])
    if isotonic is not None:
        db.set_setting("calibration_isotonic_curve", json.dumps(isotonic))
    return {
        "n": len(pairs),
        "platt": platt,
        "isotonic_fitted": isotonic is not None,
        "isotonic_n_breakpoints": len(isotonic) if isotonic else 0,
    }


def calibration_report() -> dict:
    return db.calibration_report()


def clv_summary() -> dict | None:
    return db.clv_summary()
