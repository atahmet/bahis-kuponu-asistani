"""Kalıcı SQLite katmanı (gelistirme-plani.md Bölüm 3).

`cache/*.json`, ham API cevaplarını geçici tutmaya devam eder. Bu modül ise
analiz edilen/üretilen verinin (tahminler, kuponlar, oyuncu eksiklikleri,
gerçek sonuçlar) kalıcı ve sorgulanabilir kaydını tutar; haftalık geri besleme
döngüsü (Bölüm 2) ve oyuncu eksikliği geçmişi (Bölüm 1) buradan beslenir.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    name TEXT,
    league_id INTEGER,
    season INTEGER
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    team_id INTEGER,
    name TEXT,
    position TEXT,
    season INTEGER
);

CREATE TABLE IF NOT EXISTS player_season_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    team_id INTEGER,
    league_id INTEGER,
    season INTEGER,
    appearances INTEGER,
    minutes INTEGER,
    avg_rating REAL,
    updated_at TEXT,
    UNIQUE(player_id, league_id, season)
);

CREATE TABLE IF NOT EXISTS player_availability (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    team_id INTEGER,
    fixture_id INTEGER,
    status TEXT,
    reason TEXT,
    importance_score REAL,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS team_weekly_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER,
    fixture_id INTEGER,
    side TEXT,
    attack_multiplier REAL,
    concede_multiplier REAL,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id INTEGER,
    league TEXT,
    home_team TEXT,
    away_team TEXT,
    match_date TEXT,
    market TEXT,
    selection TEXT,
    odd REAL,
    bookmaker TEXT,
    model_prob REAL,
    implied_prob REAL,
    edge REAL,
    kelly_fraction REAL,
    score_total REAL,
    confident INTEGER,
    playable INTEGER,
    created_at TEXT,
    result_settled INTEGER DEFAULT 0,
    correct INTEGER,
    closing_odd REAL,
    clv_pct REAL,
    analysis_id INTEGER
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    leagues TEXT,        -- JSON liste
    fixture_mode TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS team_elo (
    team_id INTEGER PRIMARY KEY,
    rating REAL,
    matches_count INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS bulletins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bulletin_type TEXT,     -- 'daily' | 'weekly'
    target_date TEXT,       -- daily: kapsanan maç günü; weekly: haftanın başlangıç günü
    leagues TEXT,           -- JSON liste
    created_at TEXT,
    telegram_posted INTEGER DEFAULT 0,
    telegram_message_id TEXT,
    UNIQUE(bulletin_type, target_date)
);

CREATE TABLE IF NOT EXISTS coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bulletin_id INTEGER,
    analysis_id INTEGER,
    created_at TEXT,
    n_legs INTEGER,
    combined_odd REAL,
    combined_probability REAL,
    combo_score REAL,
    stake_fraction REAL,
    technique_tags TEXT     -- JSON liste, bkz. coupon_builder.compute_technique_tags
);

CREATE TABLE IF NOT EXISTS coupon_legs (
    coupon_id INTEGER,
    prediction_id INTEGER,
    PRIMARY KEY (coupon_id, prediction_id)
);

CREATE TABLE IF NOT EXISTS results (
    fixture_id INTEGER PRIMARY KEY,
    home_goals INTEGER,
    away_goals INTEGER,
    status TEXT,
    home_team_id INTEGER,
    away_team_id INTEGER,
    league_id INTEGER,
    league TEXT,
    home_xg REAL,
    away_xg REAL,
    settled_at TEXT
);

CREATE TABLE IF NOT EXISTS team_xg_elo (
    team_id INTEGER PRIMARY KEY,
    rating REAL,
    matches_count INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS match_lambdas (
    fixture_id INTEGER PRIMARY KEY,
    lambda_home REAL,
    lambda_away REAL,
    computed_at TEXT
);

-- Her sinyalin (Poisson, Elo, xG-Elo) harmanlanmadan ÖNCEKİ 1X2 olasılığını
-- ayrı ayrı saklar (bkz. PROJE_DURUMU.md 6.1 madde 7 / validation.py) — bu,
-- ensemble ağırlıklarını GERÇEK (canlı, o anki Elo reytingiyle üretilmiş,
-- dolayısıyla veri sızıntısız) geçmiş tahminler üzerinden log-loss ile
-- optimize edebilmek için gereklidir. elo_*/xg_elo_* sinyal hazır değilse NULL.
CREATE TABLE IF NOT EXISTS match_signals (
    fixture_id INTEGER PRIMARY KEY,
    match_date TEXT,
    poisson_home REAL,
    poisson_draw REAL,
    poisson_away REAL,
    elo_home REAL,
    elo_draw REAL,
    elo_away REAL,
    xg_elo_home REAL,
    xg_elo_draw REAL,
    xg_elo_away REAL,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS model_params (
    key TEXT PRIMARY KEY,
    value REAL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_predictions_fixture ON predictions(fixture_id);
CREATE INDEX IF NOT EXISTS idx_predictions_settled ON predictions(result_settled);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection, table: str, column_defs: dict) -> None:
    """Var olan bir tabloya, eksikse yeni sütun ekler (idempotent migrasyon).
    CREATE TABLE IF NOT EXISTS zaten var olan tabloları değiştirmediği için
    şema büyüdükçe (ör. predictions.closing_odd) bu gereklidir."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, decl in column_defs.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn, "predictions", {
            "bookmaker": "TEXT",
            "closing_odd": "REAL",
            "clv_pct": "REAL",
        })
        _ensure_columns(conn, "results", {
            "home_team_id": "INTEGER",
            "away_team_id": "INTEGER",
            "league_id": "INTEGER",
            "league": "TEXT",
            "home_xg": "REAL",
            "away_xg": "REAL",
        })
        _ensure_columns(conn, "coupons", {
            "bulletin_id": "INTEGER",
            "technique_tags": "TEXT",
            "analysis_id": "INTEGER",
        })
        _ensure_columns(conn, "predictions", {
            "match_date": "TEXT",
            "confident": "INTEGER",
            "playable": "INTEGER",
            "analysis_id": "INTEGER",
            "model_version": "TEXT",
        })
        _ensure_columns(conn, "match_lambdas", {
            "match_date": "TEXT",
        })


init_db()


# --- Takım / oyuncu ---

def upsert_team(team_id: int, name: str, league_id: int, season: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO teams (id, name, league_id, season) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, league_id=excluded.league_id, season=excluded.season
            """,
            (team_id, name, league_id, season),
        )


def upsert_player(player_id: int, team_id: int, name: str, position: str, season: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO players (id, team_id, name, position, season) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                team_id=excluded.team_id, name=excluded.name,
                position=excluded.position, season=excluded.season
            """,
            (player_id, team_id, name, position, season),
        )


def upsert_player_season_stats(
    player_id: int, team_id: int, league_id: int, season: int,
    appearances: int, minutes: int, avg_rating: float | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO player_season_stats
                (player_id, team_id, league_id, season, appearances, minutes, avg_rating, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id, league_id, season) DO UPDATE SET
                team_id=excluded.team_id, appearances=excluded.appearances,
                minutes=excluded.minutes, avg_rating=excluded.avg_rating,
                updated_at=excluded.updated_at
            """,
            (player_id, team_id, league_id, season, appearances, minutes, avg_rating, _now()),
        )


def insert_availability(
    player_id: int, team_id: int, fixture_id: int, status: str,
    reason: str | None, importance_score: float,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO player_availability
                (player_id, team_id, fixture_id, status, reason, importance_score, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (player_id, team_id, fixture_id, status, reason, importance_score, _now()),
        )


def insert_team_weekly_rating(
    team_id: int, fixture_id: int, side: str, attack_multiplier: float, concede_multiplier: float,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO team_weekly_ratings
                (team_id, fixture_id, side, attack_multiplier, concede_multiplier, computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (team_id, fixture_id, side, attack_multiplier, concede_multiplier, _now()),
        )


# --- Tahminler / kuponlar ---

def insert_prediction(
    fixture_id: int, league: str, home_team: str, away_team: str, market: str, selection: str,
    odd: float, bookmaker: str, model_prob: float, implied_prob: float, edge: float,
    kelly_fraction: float, score_total: float,
    match_date: str | None = None, confident: bool | None = None, playable: bool | None = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO predictions
                (fixture_id, league, home_team, away_team, match_date, market, selection, odd, bookmaker,
                 model_prob, implied_prob, edge, kelly_fraction, score_total, confident, playable, created_at,
                 model_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fixture_id, league, home_team, away_team, match_date, market, selection, odd, bookmaker,
             model_prob, implied_prob, edge, kelly_fraction, score_total,
             None if confident is None else int(confident),
             None if playable is None else int(playable),
             _now(), config.MODEL_VERSION),
        )
        return cur.lastrowid


def insert_coupon(
    n_legs: int, combined_odd: float, combined_probability: float, combo_score: float,
    stake_fraction: float, leg_prediction_ids: list[int],
    bulletin_id: int | None = None, analysis_id: int | None = None, technique_tags: list[str] | None = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO coupons
                (bulletin_id, analysis_id, created_at, n_legs, combined_odd, combined_probability,
                 combo_score, stake_fraction, technique_tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (bulletin_id, analysis_id, _now(), n_legs, combined_odd, combined_probability, combo_score,
             stake_fraction, json.dumps(technique_tags or [], ensure_ascii=False)),
        )
        coupon_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO coupon_legs (coupon_id, prediction_id) VALUES (?, ?)",
            [(coupon_id, pid) for pid in leg_prediction_ids],
        )
        return coupon_id


# --- Adlandırılmış analizler (kullanıcı tarafından kaydedilip geri yüklenebilir) ---

def insert_analysis(name: str, leagues: list[str], fixture_mode: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO analyses (name, leagues, fixture_mode, created_at) VALUES (?, ?, ?, ?)",
            (name, json.dumps(leagues, ensure_ascii=False), fixture_mode, _now()),
        )
        return cur.lastrowid


def list_analyses(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM analyses ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_predictions_analysis_id(prediction_ids: list[int], analysis_id: int) -> None:
    if not prediction_ids:
        return
    with get_conn() as conn:
        conn.executemany(
            "UPDATE predictions SET analysis_id = ? WHERE id = ?",
            [(analysis_id, pid) for pid in prediction_ids],
        )


def get_predictions_for_analysis(analysis_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM predictions WHERE analysis_id = ? ORDER BY score_total DESC", (analysis_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_coupons_for_analysis(analysis_id: int) -> list[dict]:
    with get_conn() as conn:
        coupons = conn.execute(
            "SELECT * FROM coupons WHERE analysis_id = ? ORDER BY n_legs", (analysis_id,)
        ).fetchall()
        result = []
        for c in coupons:
            legs = conn.execute(
                """
                SELECT p.* FROM coupon_legs cl JOIN predictions p ON p.id = cl.prediction_id
                WHERE cl.coupon_id = ? ORDER BY p.score_total DESC
                """,
                (c["id"],),
            ).fetchall()
            d = dict(c)
            d["technique_tags"] = json.loads(d["technique_tags"]) if d["technique_tags"] else []
            d["legs"] = [dict(leg) for leg in legs]
            result.append(d)
    return result


def delete_analysis(analysis_id: int) -> None:
    with get_conn() as conn:
        coupon_ids = [
            r["id"] for r in conn.execute("SELECT id FROM coupons WHERE analysis_id = ?", (analysis_id,)).fetchall()
        ]
        for cid in coupon_ids:
            conn.execute("DELETE FROM coupon_legs WHERE coupon_id = ?", (cid,))
        conn.execute("DELETE FROM coupons WHERE analysis_id = ?", (analysis_id,))
        conn.execute("DELETE FROM predictions WHERE analysis_id = ?", (analysis_id,))
        conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))


# --- Bültenler (günlük/haftalık Telegram yayınları) ---

def get_bulletin(bulletin_type: str, target_date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bulletins WHERE bulletin_type = ? AND target_date = ?",
            (bulletin_type, target_date),
        ).fetchone()
    return dict(row) if row else None


def bulletin_exists(bulletin_type: str, target_date: str) -> bool:
    return get_bulletin(bulletin_type, target_date) is not None


def insert_bulletin(bulletin_type: str, target_date: str, leagues: list[str]) -> int:
    """Aynı (tip, tarih) için zaten bir bülten varsa onun id'sini döndürür
    (idempotent) — scheduler'ın API kotasını boşa harcayarak aynı bülteni
    tekrar üretmesini engeller."""
    existing = get_bulletin(bulletin_type, target_date)
    if existing:
        return existing["id"]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bulletins (bulletin_type, target_date, leagues, created_at) VALUES (?, ?, ?, ?)",
            (bulletin_type, target_date, json.dumps(leagues, ensure_ascii=False), _now()),
        )
        return cur.lastrowid


def mark_bulletin_posted(bulletin_id: int, telegram_message_id) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE bulletins SET telegram_posted = 1, telegram_message_id = ? WHERE id = ?",
            (str(telegram_message_id), bulletin_id),
        )


# --- Sonuç eşleme / kalibrasyon ---

def get_pending_fixture_ids() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT fixture_id FROM predictions WHERE result_settled = 0"
        ).fetchall()
    return [r["fixture_id"] for r in rows]


def has_result(fixture_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM results WHERE fixture_id = ?", (fixture_id,)).fetchone()
    return row is not None


def upsert_result(
    fixture_id: int, home_goals: int, away_goals: int, status: str,
    home_team_id: int | None = None, away_team_id: int | None = None,
    league_id: int | None = None, league: str | None = None,
    home_xg: float | None = None, away_xg: float | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO results
                (fixture_id, home_goals, away_goals, status, home_team_id, away_team_id,
                 league_id, league, home_xg, away_xg, settled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fixture_id) DO UPDATE SET
                home_goals=excluded.home_goals, away_goals=excluded.away_goals,
                status=excluded.status, home_team_id=excluded.home_team_id,
                away_team_id=excluded.away_team_id,
                league_id=COALESCE(excluded.league_id, results.league_id),
                league=COALESCE(excluded.league, results.league),
                home_xg=COALESCE(excluded.home_xg, results.home_xg),
                away_xg=COALESCE(excluded.away_xg, results.away_xg),
                settled_at=excluded.settled_at
            """,
            (fixture_id, home_goals, away_goals, status, home_team_id, away_team_id,
             league_id, league, home_xg, away_xg, _now()),
        )


def set_result_xg(fixture_id: int, home_xg: float | None, away_xg: float | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE results SET home_xg = ?, away_xg = ? WHERE fixture_id = ?",
            (home_xg, away_xg, fixture_id),
        )


# --- Elo reytingi (gerçek sonuçtan güncellenir) ---

def get_elo(team_id: int) -> tuple[float, int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rating, matches_count FROM team_elo WHERE team_id = ?", (team_id,)
        ).fetchone()
    if row:
        return row["rating"], row["matches_count"]
    return config.ELO_INITIAL_RATING, 0


def set_elo(team_id: int, rating: float, matches_count: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO team_elo (team_id, rating, matches_count, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                rating=excluded.rating, matches_count=excluded.matches_count, updated_at=excluded.updated_at
            """,
            (team_id, rating, matches_count, _now()),
        )


# --- xG-Elo reytingi (gerçek sonuç yerine xG farkından güncellenir) ---

def get_xg_elo(team_id: int) -> tuple[float, int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rating, matches_count FROM team_xg_elo WHERE team_id = ?", (team_id,)
        ).fetchone()
    if row:
        return row["rating"], row["matches_count"]
    return config.ELO_INITIAL_RATING, 0


def set_xg_elo(team_id: int, rating: float, matches_count: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO team_xg_elo (team_id, rating, matches_count, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                rating=excluded.rating, matches_count=excluded.matches_count, updated_at=excluded.updated_at
            """,
            (team_id, rating, matches_count, _now()),
        )


# --- Maç lambda'ları (Dixon-Coles rho'yu veriden öğrenmek için) ---

def insert_match_lambdas(fixture_id: int, lambda_home: float, lambda_away: float, match_date: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO match_lambdas (fixture_id, lambda_home, lambda_away, computed_at, match_date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fixture_id) DO UPDATE SET
                lambda_home=excluded.lambda_home, lambda_away=excluded.lambda_away,
                computed_at=excluded.computed_at, match_date=excluded.match_date
            """,
            (fixture_id, lambda_home, lambda_away, _now(), match_date),
        )


def get_settled_matches_with_lambdas() -> list[dict]:
    """Dixon-Coles rho'sunu MLE-lite ile kalibre etmek için: hem lambda
    tahminimiz hem de gerçek skoru bilinen maçları döndürür. match_date,
    rho fitting'de zaman ağırlıklandırması için kullanılır (bkz.
    poisson_model.fit_dixon_coles_rho) — eski satırlarda (bu sütun
    eklenmeden önce yazılmış) NULL olabilir; bu durumda o maç yaşı bilinmiyor
    kabul edilip tam ağırlıkla (1.0) değerlendirilir, veri sessizce atılmaz."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ml.lambda_home, ml.lambda_away, ml.match_date, r.home_goals, r.away_goals
            FROM match_lambdas ml
            JOIN results r ON r.fixture_id = ml.fixture_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


# --- Sinyal bazlı 1X2 olasılıkları (ensemble ağırlıklarını log-loss ile
# optimize etmek için, bkz. validation.py) ---

def insert_match_signals(
    fixture_id: int, match_date: str | None,
    poisson: tuple[float, float, float],
    elo: tuple[float, float, float] | None,
    xg_elo: tuple[float, float, float] | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO match_signals
                (fixture_id, match_date, poisson_home, poisson_draw, poisson_away,
                 elo_home, elo_draw, elo_away, xg_elo_home, xg_elo_draw, xg_elo_away, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fixture_id) DO UPDATE SET
                match_date=excluded.match_date,
                poisson_home=excluded.poisson_home, poisson_draw=excluded.poisson_draw, poisson_away=excluded.poisson_away,
                elo_home=excluded.elo_home, elo_draw=excluded.elo_draw, elo_away=excluded.elo_away,
                xg_elo_home=excluded.xg_elo_home, xg_elo_draw=excluded.xg_elo_draw, xg_elo_away=excluded.xg_elo_away,
                computed_at=excluded.computed_at
            """,
            (
                fixture_id, match_date, poisson[0], poisson[1], poisson[2],
                elo[0] if elo else None, elo[1] if elo else None, elo[2] if elo else None,
                xg_elo[0] if xg_elo else None, xg_elo[1] if xg_elo else None, xg_elo[2] if xg_elo else None,
                _now(),
            ),
        )


def get_settled_match_signals() -> list[dict]:
    """Ensemble ağırlıklarını (Elo/xG-Elo blend payları) gerçek, canlı üretilmiş
    (dolayısıyla o anki Elo reytingiyle hesaplanmış, veri sızıntısız) geçmiş
    tahminlerin log-loss'unu ölçerek optimize etmek için kullanılır
    (bkz. validation.optimize_ensemble_weights)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ms.*, r.home_goals, r.away_goals
            FROM match_signals ms
            JOIN results r ON r.fixture_id = ms.fixture_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_league_results(league_name: str) -> list[dict]:
    """Bir ligin sonuçlanmış maçlarını döndürür (elo.compute_league_home_advantage
    tarafından kullanılır)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT home_goals, away_goals FROM results WHERE league = ?", (league_name,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Öğrenilen model parametreleri (ör. kalibre edilmiş Dixon-Coles rho) ---

def get_model_param(key: str, default: float | None = None) -> float | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM model_params WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_model_param(key: str, value: float) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO model_params (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, _now()),
        )


# --- Genel ayarlar (ör. Telegram bülten saatleri) — scheduler.py bunları her
# turda DB'den okur, böylece arayüzden değiştirilen ayarlar zamanlayıcı
# yeniden başlatılmadan en geç bir sonraki tikte devreye girer. ---

def get_setting(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, _now()),
        )


# --- CLV (closing line value) ---

def clv_summary() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, AVG(clv_pct) AS avg_clv FROM predictions WHERE clv_pct IS NOT NULL"
        ).fetchone()
    return dict(row) if row and row["n"] else None


def _is_correct(market: str, selection: str, home_goals: int, away_goals: int) -> bool | None:
    if market == "Maç Sonucu":
        if selection == "Home":
            return home_goals > away_goals
        if selection == "Draw":
            return home_goals == away_goals
        if selection == "Away":
            return away_goals > home_goals
    elif market == "Alt/Üst 2.5":
        total = home_goals + away_goals
        if selection == "Over":
            return total >= 3
        if selection == "Under":
            return total <= 2
    elif market == "Karşılıklı Gol":
        both_scored = home_goals >= 1 and away_goals >= 1
        if selection == "Yes":
            return both_scored
        if selection == "No":
            return not both_scored
    return None


def is_correct(market: str, selection: str, home_goals: int, away_goals: int) -> bool | None:
    """_is_correct'in dışa açık hâli — backtest modunda app.py, DB'ye
    yazmadan önce bu çalıştırmanın anlık isabet oranını göstermek için kullanır."""
    return _is_correct(market, selection, home_goals, away_goals)


def settle_predictions() -> int:
    """Sonucu artık bilinen (results tablosunda karşılığı olan) bekleyen
    tahminleri sonuçlandırır. Kaç tahminin güncellendiğini döndürür."""
    updated = 0
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.market, p.selection, r.home_goals, r.away_goals
            FROM predictions p
            JOIN results r ON r.fixture_id = p.fixture_id
            WHERE p.result_settled = 0
            """
        ).fetchall()
        for row in rows:
            correct = _is_correct(row["market"], row["selection"], row["home_goals"], row["away_goals"])
            if correct is None:
                continue
            conn.execute(
                "UPDATE predictions SET result_settled = 1, correct = ? WHERE id = ?",
                (1 if correct else 0, row["id"]),
            )
            updated += 1
    return updated


def calibration_report() -> dict:
    """Puan aralığına (bucket) göre gerçek isabet oranı ve Brier skoru.
    Sistem tutarlıysa yüksek puanlı bucket'ların isabet oranı belirgin
    şekilde daha yüksek olmalı (bkz. gelistirme-plani.md Bölüm 2)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
              CASE
                WHEN score_total >= 80 THEN 4
                WHEN score_total >= 60 THEN 3
                WHEN score_total >= 40 THEN 2
                WHEN score_total >= 20 THEN 1
                ELSE 0
              END AS bucket_order,
              CASE
                WHEN score_total >= 80 THEN '80-100'
                WHEN score_total >= 60 THEN '60-79'
                WHEN score_total >= 40 THEN '40-59'
                WHEN score_total >= 20 THEN '20-39'
                ELSE '0-19'
              END AS bucket,
              COUNT(*) AS n,
              AVG(correct) AS hit_rate,
              AVG((model_prob - correct) * (model_prob - correct)) AS brier
            FROM predictions
            WHERE result_settled = 1
            GROUP BY bucket_order
            ORDER BY bucket_order DESC
            """
        ).fetchall()
        overall = conn.execute(
            """
            SELECT COUNT(*) AS n, AVG(correct) AS hit_rate,
                   AVG((model_prob - correct) * (model_prob - correct)) AS brier
            FROM predictions WHERE result_settled = 1
            """
        ).fetchone()

    return {
        "buckets": [dict(r) for r in rows],
        "overall": dict(overall) if overall and overall["n"] else None,
    }


def get_calibration_pairs() -> list[tuple[float, int]]:
    """Sonuçlanmış her tahmin için (model_prob, correct) çiftlerini döndürür —
    calibration.fit_platt / fit_isotonic'in ham girdisi. Tüm pazarlar
    (1X2/Alt-Üst/KG) birlikte kalibre edilir (bkz. calibration.py docstring —
    veri hacmi henüz pazar bazlı ayrı kalibrasyona yetmiyor, bu bilinen bir
    basitleştirmedir)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT model_prob, correct FROM predictions WHERE result_settled = 1 AND correct IS NOT NULL"
        ).fetchall()
    return [(r["model_prob"], r["correct"]) for r in rows]
