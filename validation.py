"""Log-loss tabanlı model doğrulama.

teknik_inceleme raporunun "ensemble ağırlıkları neden log-loss ile veriden
optimize edilmiyor" eleştirisine karşılık: coupon_builder her CANLI analizde
(backtest DEĞİL — Elo/xG-Elo yalnızca canlı modda hesaplanıyor, bkz.
coupon_builder.fetch_and_analyze docstring) her sinyalin (Poisson, Elo,
xG-Elo) harmanlanmadan ÖNCEKİ 1X2 olasılığını db.match_signals'a kaydeder.
O tahminler sonuçlandığında (db.get_settled_match_signals), bu modül farklı
ağırlık kombinasyonlarının log-loss'unu (düşük=iyi) karşılaştırıp en iyisini
bulur — grid search MLE-lite, poisson_model.fit_dixon_coles_rho ile aynı
felsefe.

Önemli: Bu "walk-forward backtest" DEĞİL, "gerçek zamanlı biriken veri
üzerinde geriye dönük log-loss karşılaştırması"dır — ama veri sızıntısı
riski YOKTUR, çünkü her satırdaki Elo/xG-Elo olasılığı o tahmin CANLI
üretildiği ANDAKİ reytingle hesaplanmıştır (sonradan hesaplanmış/yeniden
oynatılmış bir reyting değil). Bu, sentetik bir geçmiş-sezon replay'inden
daha dürüst bir ölçümdür çünkü gerçekte kullanılan sinyali ölçer."""
import math

import config
import db

WEIGHT_SEARCH_STEP = config.ENSEMBLE_WEIGHT_SEARCH_STEP
MIN_MATCHES = config.ENSEMBLE_WEIGHT_FIT_MIN_MATCHES


def log_loss(actual_idx: int, probs: tuple[float, float, float]) -> float:
    return -math.log(max(probs[actual_idx], 1e-10))


def _outcome_idx(home_goals: int, away_goals: int) -> int:
    if home_goals > away_goals:
        return 0
    if home_goals == away_goals:
        return 1
    return 2


def _blend(poisson, elo, xg_elo, w_elo: float, w_xg_elo: float) -> tuple[float, float, float]:
    w_poisson = 1.0 - w_elo - w_xg_elo
    home = poisson[0] * w_poisson
    draw = poisson[1] * w_poisson
    away = poisson[2] * w_poisson
    if elo is not None:
        home += elo[0] * w_elo
        draw += elo[1] * w_elo
        away += elo[2] * w_elo
    if xg_elo is not None:
        home += xg_elo[0] * w_xg_elo
        draw += xg_elo[1] * w_xg_elo
        away += xg_elo[2] * w_xg_elo
    total = home + draw + away
    if total <= 0:
        return (1 / 3, 1 / 3, 1 / 3)
    return (home / total, draw / total, away / total)


def _avg_log_loss(rows: list[dict], w_elo: float, w_xg_elo: float) -> float:
    total = 0.0
    for r in rows:
        poisson = (r["poisson_home"], r["poisson_draw"], r["poisson_away"])
        elo_sig = (r["elo_home"], r["elo_draw"], r["elo_away"])
        xg_sig = (r["xg_elo_home"], r["xg_elo_draw"], r["xg_elo_away"]) if r["xg_elo_home"] is not None else None
        idx = _outcome_idx(r["home_goals"], r["away_goals"])
        total += log_loss(idx, _blend(poisson, elo_sig, xg_sig, w_elo, w_xg_elo))
    return total / len(rows)


def _split_groups(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """elo sinyali olmayan satırlar (Elo hiç hazır değilken üretilmiş tahminler)
    ağırlık optimizasyonuna katkı sağlamaz, elenir."""
    two_way = [r for r in rows if r["elo_home"] is not None and r["xg_elo_home"] is None]
    three_way = [r for r in rows if r["elo_home"] is not None and r["xg_elo_home"] is not None]
    return two_way, three_way


def _grid_search_2way(rows: list[dict]) -> dict:
    best_w, best_ll = None, float("inf")
    w = 0.0
    while w <= 0.95 + 1e-9:
        ll = _avg_log_loss(rows, w, 0.0)
        if ll < best_ll:
            best_ll, best_w = ll, w
        w += WEIGHT_SEARCH_STEP
    current_w = 1 - config.ENSEMBLE_POISSON_WEIGHT
    current_ll = _avg_log_loss(rows, current_w, 0.0)
    return {
        "n": len(rows),
        "current_elo_weight": round(current_w, 2),
        "current_log_loss": round(current_ll, 4),
        "best_elo_weight": round(best_w, 2),
        "best_log_loss": round(best_ll, 4),
    }


def _grid_search_3way(rows: list[dict]) -> dict:
    best_we, best_wx, best_ll = None, None, float("inf")
    we = 0.0
    while we <= 0.9 + 1e-9:
        wx = 0.0
        while we + wx <= 0.9 + 1e-9:
            ll = _avg_log_loss(rows, we, wx)
            if ll < best_ll:
                best_ll, best_we, best_wx = ll, we, wx
            wx += WEIGHT_SEARCH_STEP
        we += WEIGHT_SEARCH_STEP
    current_we, current_wx = config.ENSEMBLE_ELO_WEIGHT_WITH_XG, config.ENSEMBLE_XG_ELO_WEIGHT
    current_ll = _avg_log_loss(rows, current_we, current_wx)
    return {
        "n": len(rows),
        "current_elo_weight": round(current_we, 2),
        "current_xg_elo_weight": round(current_wx, 2),
        "current_log_loss": round(current_ll, 4),
        "best_elo_weight": round(best_we, 2),
        "best_xg_elo_weight": round(best_wx, 2),
        "best_log_loss": round(best_ll, 4),
    }


def optimize_ensemble_weights(min_matches: int | None = None) -> dict:
    """DB'de biriken, sonuçlanmış CANLI tahminlerin (match_signals + results)
    log-loss'unu grid search ile karşılaştırır. Her grup (yalnızca Elo hazır /
    Elo+xG-Elo ikisi de hazır) ayrı optimize edilir; yeterli örnek yoksa o
    grup için None döner (mevcut config sabiti kullanılmaya devam eder)."""
    min_matches = min_matches or MIN_MATCHES
    rows = db.get_settled_match_signals()
    two_way, three_way = _split_groups(rows)

    result = {"two_way": None, "three_way": None}
    if len(two_way) >= min_matches:
        result["two_way"] = _grid_search_2way(two_way)
    if len(three_way) >= min_matches:
        result["three_way"] = _grid_search_3way(three_way)
    return result


def apply_ensemble_weights(result: dict) -> None:
    """optimize_ensemble_weights'in bulduğu en iyi ağırlıkları model_params'a
    yazar — coupon_builder bir sonraki analizden itibaren bunları otomatik
    okur (dixon_coles_rho ile aynı kalibrasyon deseni)."""
    if result.get("two_way"):
        db.set_model_param("ensemble_elo_weight_2way", result["two_way"]["best_elo_weight"])
    if result.get("three_way"):
        db.set_model_param("ensemble_elo_weight_3way", result["three_way"]["best_elo_weight"])
        db.set_model_param("ensemble_xg_elo_weight_3way", result["three_way"]["best_xg_elo_weight"])
