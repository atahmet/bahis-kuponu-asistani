"""API-Football istemcisi: istekleri diske cache'leyerek API kotasını korur.
Cache süreleri (TTL) ve sayfalama limitleri, aktif plana (free/pro) göre
config.limits() üzerinden dinamik okunur — bkz. config.set_plan()."""
import glob
import hashlib
import json
import time
from pathlib import Path

import requests

import config


class ApiFootballError(Exception):
    pass


def _cache_path(name: str) -> Path:
    safe = hashlib.md5(name.encode()).hexdigest()
    return config.CACHE_DIR / f"{safe}.json"


def _read_cache(name: str, ttl_hours: float):
    path = _cache_path(name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    age_hours = (time.time() - payload["fetched_at"]) / 3600
    if age_hours > ttl_hours:
        return None
    return payload["data"]


def _write_cache(name: str, data) -> None:
    path = _cache_path(name)
    path.write_text(
        json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=False),
        encoding="utf-8",
    )


def _named_cache_path(prefix: str) -> str:
    """Belirli bir önek ile eşleşen cache dosyalarını bulmak için, dosya adını
    md5 yerine okunabilir tutmak amacıyla ayrı bir isimlendirme kullanıyoruz."""
    return str(config.CACHE_DIR / f"{prefix}.json")


def _read_named_cache(prefix: str, ttl_hours: float):
    path = Path(_named_cache_path(prefix))
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    age_hours = (time.time() - payload["fetched_at"]) / 3600
    if age_hours > ttl_hours:
        return None
    return payload["data"]


def _write_named_cache(prefix: str, data) -> None:
    path = Path(_named_cache_path(prefix))
    path.write_text(
        json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=False),
        encoding="utf-8",
    )


class RequestBudget:
    """Bu oturumda yapılan gerçek (cache'lenmemiş) API çağrılarını sayar."""

    def __init__(self):
        self.calls_made = 0
        self.last_remaining = None


BUDGET = RequestBudget()


def _get(endpoint: str, params: dict, cache_prefix: str, ttl_hours: float, force: bool = False):
    if not force:
        cached = _read_named_cache(cache_prefix, ttl_hours)
        if cached is not None:
            return cached

    if not config.API_KEY:
        raise ApiFootballError(
            "API_FOOTBALL_KEY ayarlanmamış. .env dosyasına api-sports.io'dan "
            "aldığınız ücretsiz key'i ekleyin (bkz. README.md)."
        )

    url = f"{config.API_BASE_URL}{endpoint}"
    headers = {"x-apisports-key": config.API_KEY}
    resp = requests.get(url, headers=headers, params=params, timeout=20)
    BUDGET.calls_made += 1
    BUDGET.last_remaining = resp.headers.get("x-ratelimit-requests-remaining")

    if resp.status_code == 429:
        raise ApiFootballError("Günlük API istek kotası doldu (429). Yarın tekrar deneyin.")
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise ApiFootballError(f"API hatası: {payload['errors']}")

    data = payload.get("response", [])
    _write_named_cache(cache_prefix, data)
    return data


def get_fixtures(league_id: int, season: int, next_n: int = 8, force: bool = False):
    prefix = f"fixtures_{league_id}_{season}_next{next_n}"
    return _get(
        "/fixtures",
        {"league": league_id, "season": season, "next": next_n},
        prefix,
        config.limits()["ttl_fixtures"],
        force=force,
    )


def get_team_statistics(team_id: int, league_id: int, season: int, force: bool = False):
    prefix = f"teamstats_{league_id}_{season}_{team_id}"
    return _get(
        "/teams/statistics",
        {"team": team_id, "league": league_id, "season": season},
        prefix,
        config.limits()["ttl_team_stats"],
        force=force,
    )


def get_odds(fixture_id: int, force: bool = False):
    prefix = f"odds_{fixture_id}"
    return _get(
        "/odds",
        {"fixture": fixture_id},
        prefix,
        config.limits()["ttl_odds"],
        force=force,
    )


def get_last_fixtures(team_id: int, last_n: int = 10, force: bool = False, to_date: str | None = None):
    """Bir takımın (tüm turnuvalar dahil, sezon sınırıyla kısıtlı değil) son
    N maçını, gerçek skorlarıyla döndürür. Zaman ağırlıklı Poisson modeli
    (poisson_model.build_weighted_form) bunu kullanır; eski sezona sarkan
    maçlar üstel zaman ağırlığıyla zaten düşük etkili hale gelir, bu yüzden
    erken sezonda ayrıca 'yetersiz veri' filtrelemesi gerekmez.

    to_date (YYYY-MM-DD) verilirse, yalnızca o tarihten ÖNCEKİ maçlar
    döndürülür — backtest modunda, analiz edilen geçmiş maçın kendisinden
    sonraki sonuçların forma sızmasını (lookahead bias) önlemek için
    kullanılır."""
    prefix = f"lastfixtures_{team_id}_{last_n}" + (f"_to{to_date}" if to_date else "")
    params = {"team": team_id, "last": last_n}
    if to_date:
        params["to"] = to_date
    return _get(
        "/fixtures",
        params,
        prefix,
        config.limits()["ttl_team_stats"],
        force=force,
    )


def get_recent_league_fixtures(league_id: int, season: int, last_n: int = 5, force: bool = False):
    """Bir ligin belirli bir sezonundaki son N (tamamlanmış) maçını döndürür.
    Yalnızca backtest modunda kullanılır: ücretsiz API-Football planı güncel
    sezona erişemediği için (bkz. config.FREE_PLAN_MAX_SEASON), geçmiş bir
    sezondan gerçek/tamamlanmış maçlar çekilip model bu maçlar üzerinde
    çalıştırılır — sonuç zaten bilindiğinden isabet anında görülebilir."""
    prefix = f"recentleaguefixtures_{league_id}_{season}_last{last_n}"
    return _get(
        "/fixtures",
        {"league": league_id, "season": season, "last": last_n},
        prefix,
        config.limits()["ttl_fixtures"],
        force=force,
    )


def get_fixtures_by_date(league_id: int, season: int, date_str: str, force: bool = False):
    """Bir ligin, belirli bir takvim gününde (YYYY-MM-DD) oynanacak/oynanan
    tüm maçlarını döndürür. Günlük bülten (scheduler.py) bunu kullanır."""
    prefix = f"fixturesbydate_{league_id}_{season}_{date_str}"
    return _get(
        "/fixtures",
        {"league": league_id, "season": season, "date": date_str},
        prefix,
        config.limits()["ttl_fixtures"],
        force=force,
    )


def get_fixtures_in_range(league_id: int, season: int, from_date: str, to_date: str, force: bool = False):
    """Bir ligin, verilen tarih aralığındaki (YYYY-MM-DD, dahil) tüm
    maçlarını döndürür. Haftalık bülten (scheduler.py) bunu kullanır."""
    prefix = f"fixturesrange_{league_id}_{season}_{from_date}_{to_date}"
    return _get(
        "/fixtures",
        {"league": league_id, "season": season, "from": from_date, "to": to_date},
        prefix,
        config.limits()["ttl_fixtures"],
        force=force,
    )


def get_fixture_statistics(fixture_id: int, force: bool = False):
    """Bitmiş bir maçın takım istatistiklerini (şutlar, top hakimiyeti,
    korner, xG='expected_goals' dahil) döndürür. Yalnızca sonuçlanan
    maçlar için anlamlıdır; xG-Elo reytingi (bkz. elo.py) ve gelecekteki
    kalibrasyonlar bunu kullanır. Sonuçlanmış maçlar için sonsuza kadar
    cache'lenir (TTL çok uzun) çünkü geçmiş bir maçın istatistiği değişmez."""
    prefix = f"fixturestats_{fixture_id}"
    return _get(
        "/fixtures/statistics",
        {"fixture": fixture_id},
        prefix,
        24 * 365,  # sonuçlanmış maç istatistiği degismez, 1 yil cache yeterli
        force=force,
    )


def extract_xg(stats_response: list, home_team_id: int, away_team_id: int) -> tuple[float | None, float | None]:
    """/fixtures/statistics cevabından (home_xg, away_xg) çıkarır. Bazı
    ligler/maçlar için xG verisi API'de bulunmayabilir (None döner)."""
    home_xg = away_xg = None
    for team_stats in stats_response or []:
        team_id = team_stats.get("team", {}).get("id")
        value = None
        for item in team_stats.get("statistics", []):
            if item.get("type") == "expected_goals":
                value = item.get("value")
                break
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if team_id == home_team_id:
            home_xg = parsed
        elif team_id == away_team_id:
            away_xg = parsed
    return home_xg, away_xg


def get_injuries(fixture_id: int, force: bool = False):
    """Verilen maç için sakat/cezalı oyuncu listesini döndürür (her iki takım
    birlikte, tek istek)."""
    prefix = f"injuries_{fixture_id}"
    return _get(
        "/injuries",
        {"fixture": fixture_id},
        prefix,
        config.limits()["ttl_injuries"],
        force=force,
    )


def get_players_stats(team_id: int, league_id: int, season: int, force: bool = False, max_pages: int | None = None):
    """Bir takımın sezonluk oyuncu istatistiklerini (dakika, maç sayısı,
    ortalama reyting, mevki) döndürür. API sayfalama kullandığı için birden
    fazla sayfa birleştirilir; sayfa sayısı üst sınırı plan moduna göre
    belirlenir (free: 3, pro: 6) — max_pages verilirse onu geçersiz kılar."""
    if max_pages is None:
        max_pages = config.limits()["player_stats_max_pages"]
    prefix = f"playerstats_{league_id}_{season}_{team_id}"
    cached = None if force else _read_named_cache(prefix, config.limits()["ttl_player_stats"])
    if cached is not None:
        return cached

    if not config.API_KEY:
        raise ApiFootballError(
            "API_FOOTBALL_KEY ayarlanmamış. .env dosyasına api-sports.io'dan "
            "aldığınız ücretsiz key'i ekleyin (bkz. README.md)."
        )

    all_players = []
    page = 1
    while page <= max_pages:
        url = f"{config.API_BASE_URL}/players"
        headers = {"x-apisports-key": config.API_KEY}
        params = {"team": team_id, "league": league_id, "season": season, "page": page}
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        BUDGET.calls_made += 1
        BUDGET.last_remaining = resp.headers.get("x-ratelimit-requests-remaining")
        if resp.status_code == 429:
            raise ApiFootballError("Günlük API istek kotası doldu (429). Yarın tekrar deneyin.")
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise ApiFootballError(f"API hatası: {payload['errors']}")

        all_players.extend(payload.get("response", []))
        paging = payload.get("paging", {})
        if page >= paging.get("total", 1):
            break
        page += 1

    _write_named_cache(prefix, all_players)
    return all_players


def get_fixtures_by_ids(fixture_ids: list[int], force: bool = False):
    """Birden fazla maçın güncel durumunu/skorunu tek istekte döndürür
    (API-Football 'ids' parametresi ile en fazla 20 maç kabul eder;
    çağıran taraf 20'lik gruplar halinde çağırmalı)."""
    if not fixture_ids:
        return []
    ids_str = "-".join(str(i) for i in sorted(fixture_ids))
    prefix = f"fixturesbyids_{ids_str}"
    return _get(
        "/fixtures",
        {"ids": ids_str},
        prefix,
        config.limits()["ttl_results"],
        force=force,
    )


def cached_league_team_stats(league_id: int, season: int):
    """Bu lig/sezon için diskte daha önce cache'lenmiş tüm takım istatistiklerini
    tarar. Poisson modelinin lig ortalamasını hesaplamak için kullanılır ve
    yeni API isteği yapmaz."""
    prefix_pattern = str(config.CACHE_DIR / f"teamstats_{league_id}_{season}_*.json")
    results = []
    for path_str in glob.glob(prefix_pattern):
        try:
            payload = json.loads(Path(path_str).read_text(encoding="utf-8"))
            if payload.get("data"):
                results.append(payload["data"])
        except (json.JSONDecodeError, OSError):
            continue
    return results


def parse_odds_response(odds_response: list):
    """API-Football /odds cevabından maç sonucu, alt/üst 2.5 ve KG var/yok
    oranlarını çıkarır. Tercih edilen bahis şirketi sırasına göre seçim yapar."""
    if not odds_response:
        return None

    bookmakers = odds_response[0].get("bookmakers", [])
    if not bookmakers:
        return None

    by_name = {b["name"]: b for b in bookmakers}
    chosen = None
    for pref in config.PREFERRED_BOOKMAKERS:
        if pref in by_name:
            chosen = by_name[pref]
            break
    if chosen is None:
        chosen = bookmakers[0]

    result = {"bookmaker": chosen["name"], "match_winner": {}, "over_under_2_5": {}, "btts": {}}
    for bet in chosen.get("bets", []):
        name = bet.get("name")
        values = {v["value"]: float(v["odd"]) for v in bet.get("values", [])}
        if name == "Match Winner":
            result["match_winner"] = {
                "Home": values.get("Home"),
                "Draw": values.get("Draw"),
                "Away": values.get("Away"),
            }
        elif name == "Goals Over/Under":
            result["over_under_2_5"] = {
                "Over": values.get("Over 2.5"),
                "Under": values.get("Under 2.5"),
            }
        elif name == "Both Teams Score":
            result["btts"] = {"Yes": values.get("Yes"), "No": values.get("No")}

    if not result["match_winner"].get("Home"):
        return None
    return result


def parse_odds_all_bookmakers(odds_response: list) -> dict:
    """API-Football /odds cevabındaki TÜM bahis şirketlerini market bazında
    çıkarır: {market_key: {bookmaker_name: {selection: odd}}}. betting_logic
    bunu hem konsensüs (birden fazla şirketin ortalama devig'lenmiş olasılığı)
    hem de en iyi fiyat (line shopping) hesaplamak için kullanır."""
    result = {"match_winner": {}, "over_under_2_5": {}, "btts": {}}
    if not odds_response:
        return result

    for bm in odds_response[0].get("bookmakers", []):
        name = bm.get("name")
        for bet in bm.get("bets", []):
            bet_name = bet.get("name")
            values = {v["value"]: float(v["odd"]) for v in bet.get("values", [])}

            if bet_name == "Match Winner":
                mw = {"Home": values.get("Home"), "Draw": values.get("Draw"), "Away": values.get("Away")}
                if all(mw.values()):
                    result["match_winner"][name] = mw
            elif bet_name == "Goals Over/Under":
                ou = {"Over": values.get("Over 2.5"), "Under": values.get("Under 2.5")}
                if all(ou.values()):
                    result["over_under_2_5"][name] = ou
            elif bet_name == "Both Teams Score":
                btts = {"Yes": values.get("Yes"), "No": values.get("No")}
                if all(btts.values()):
                    result["btts"][name] = btts

    return result
