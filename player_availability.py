"""Oyuncu eksikliklerinin (sakatlık/ceza) takım gücüne etkisini hesaplar
(gelistirme-plani.md Bölüm 1).

Pratik basitleştirme: "son N haftadaki oynama süresi" yerine, API kotasını
korumak amacıyla o ana kadarki SEZON ORTALAMASI dakika/maç ve ortalama
reyting kullanılır. Haftalık pencereli bir versiyon, her geçmiş maç için
ayrı `/fixtures/players` çağrısı gerektirir ve ücretsiz 100 istek/gün
kotasını hızla tüketir — bu yüzden MVP'de sezon ortalaması tercih edildi.
Bu modül opt-in'dir (varsayılan kapalı); her açık maç için 1 `/injuries` +
takım başına cache'lenen `/players` çağrısı ekler.
"""
import api_football as api
import config
import db


def _team_player_pool(team_id: int, league_id: int, season: int, force: bool = False) -> list[dict]:
    raw = api.get_players_stats(team_id, league_id, season, force=force)
    pool = []
    for entry in raw:
        player = entry.get("player", {})
        stats_list = entry.get("statistics", [])
        if not stats_list:
            continue
        games = stats_list[0].get("games", {})
        rating_raw = games.get("rating")
        pool.append(
            {
                "player_id": player.get("id"),
                "name": player.get("name"),
                "position": games.get("position"),
                "minutes": games.get("minutes") or 0,
                "appearances": games.get("appearences") or 0,
                "rating": float(rating_raw) if rating_raw else None,
            }
        )
    return pool


def team_average_rating(pool: list[dict]) -> float:
    rated = [p["rating"] for p in pool if p["rating"]]
    return sum(rated) / len(rated) if rated else config.DEFAULT_PLAYER_RATING


def player_importance(player: dict, team_avg_rating: float, team_matches_played: int) -> float:
    if team_matches_played <= 0:
        return 0.0
    max_minutes = team_matches_played * 90
    playing_time_ratio = min(player["minutes"] / max_minutes, 1.0) if max_minutes else 0.0
    position_weight = config.POSITION_WEIGHTS.get(player["position"], 0.85)
    rating = player["rating"] or team_avg_rating
    rating_factor = (rating / team_avg_rating) if team_avg_rating else 1.0
    return playing_time_ratio * position_weight * rating_factor


def get_missing_players(fixture_id: int, force: bool = False) -> dict[int, dict]:
    """fixture'da sakat/cezalı olan oyuncuları {player_id: {team_id, reason}}
    biçiminde döndürür (her iki takım birlikte)."""
    injuries = api.get_injuries(fixture_id, force=force)
    missing = {}
    for entry in injuries:
        player = entry.get("player", {})
        team = entry.get("team", {})
        pid = player.get("id")
        if pid is None:
            continue
        missing[pid] = {"team_id": team.get("id"), "reason": player.get("reason")}
    return missing


def compute_availability_adjustment(
    team_id: int, league_id: int, season: int, fixture_id: int, team_matches_played: int,
    missing_players: dict[int, dict] | None = None, force: bool = False,
) -> tuple[float, float, list[dict]]:
    """Bir takım için (attack_multiplier<=1, concede_multiplier>=1, eksik
    oyuncu detayları) döndürür. `missing_players` verilmezse /injuries
    tekrar çağrılır (aynı fixture için iki takım analiz edilirken tek
    seferde çekip paylaşmak, çağırana kalmıştır)."""
    pool = _team_player_pool(team_id, league_id, season, force=force)
    if not pool:
        return 1.0, 1.0, []

    avg_rating = team_average_rating(pool)
    pool_by_id = {p["player_id"]: p for p in pool if p["player_id"] is not None}

    if missing_players is None:
        missing_players = get_missing_players(fixture_id, force=force)

    attack_impact = 0.0
    defense_impact = 0.0
    details = []

    for pid, info in missing_players.items():
        if info["team_id"] != team_id:
            continue
        player = pool_by_id.get(pid)
        if not player:
            continue
        importance = player_importance(player, avg_rating, team_matches_played)
        position = player["position"]
        if position == "Attacker":
            attack_impact += importance
        elif position == "Midfielder":
            attack_impact += importance * 0.5
            defense_impact += importance * 0.5
        else:  # Defender / Goalkeeper / bilinmeyen
            defense_impact += importance
        details.append(
            {
                "player_id": pid,
                "name": player["name"],
                "position": position,
                "importance": round(importance, 3),
                "reason": info["reason"],
            }
        )

    attack_multiplier = 1 - min(attack_impact * config.AVAILABILITY_EFFECT_COEF, config.AVAILABILITY_CAP)
    concede_multiplier = 1 + min(defense_impact * config.AVAILABILITY_EFFECT_COEF, config.AVAILABILITY_CAP)
    return attack_multiplier, concede_multiplier, details


def log_availability(
    team_id: int, team_name: str, league_id: int, season: int, fixture_id: int, side: str,
    attack_multiplier: float, concede_multiplier: float, details: list[dict],
) -> None:
    """Hesaplanan eksiklik etkisini kalıcı DB'ye yazar (team_weekly_ratings +
    player_availability), böylece Bölüm 2'deki geri besleme döngüsünde
    hangi eksikliğin sonucu ne kadar etkilediği geriye dönük incelenebilir."""
    db.upsert_team(team_id, team_name, league_id, season)
    db.insert_team_weekly_rating(team_id, fixture_id, side, attack_multiplier, concede_multiplier)
    for player in details:
        db.upsert_player(player["player_id"], team_id, player["name"], player["position"], season)
        db.insert_availability(
            player_id=player["player_id"],
            team_id=team_id,
            fixture_id=fixture_id,
            status="injured_or_suspended",
            reason=player["reason"],
            importance_score=player["importance"],
        )
