"""Günlük/haftalık kupon bültenleri: fikstürleri analiz eder, farklı bacak
sayılarında (1-7) kupon varyantları üretir, DB'ye idempotent şekilde loglar
ve Telegram kanalına gönderir. scheduler.py bu modülü çağırır; Streamlit
arayüzünden de manuel tetiklenebilir (ör. "şimdi test et" butonu).

İdempotenslik: her (bulletin_type, target_date) çifti için DB'de en fazla
bir bülten kaydı oluşur (bkz. db.insert_bulletin UNIQUE kısıtı). Böylece
scheduler yeniden başlasa veya aynı gün için fonksiyon tekrar çağrılsa bile
API isteği/Telegram mesajı tekrarlanmaz — 'sadece değişiklikleri kullan'
prensibi burada uygulanır."""
import json
from datetime import datetime, timedelta

import config
import coupon_builder as cb
import db
import history
import telegram_bot

LEG_LABELS = {
    1: "🔒 GÜNÜN BANKOSU",
    2: "2'Lİ KUPON",
    3: "3'LÜ KUPON",
    4: "4'LÜ KUPON",
    5: "5'Lİ KUPON",
    6: "6'LI KUPON",
    7: "7'Lİ KUPON",
}

WEEKDAYS_TR = {0: "Pazartesi", 1: "Salı", 2: "Çarşamba", 3: "Perşembe", 4: "Cuma", 5: "Cumartesi", 6: "Pazar"}

# Ayarlar (DB'deki "settings" tablosunda saklanan anahtarlar). scheduler.py
# her tikte get_schedule_settings() çağırdığından, arayüzden yapılan
# değişiklik zamanlayıcı yeniden başlatılmadan en geç bir sonraki tikte
# (config.SCHEDULER_TICK_SECONDS) devreye girer.
_SETTING_KEYS = {
    "daily_enabled": "bulletin_daily_enabled",
    "daily_hour": "bulletin_daily_hour",
    "daily_minute": "bulletin_daily_minute",
    "weekly_enabled": "bulletin_weekly_enabled",
    "weekly_weekday": "bulletin_weekly_weekday",
    "weekly_hour": "bulletin_weekly_hour",
    "weekly_minute": "bulletin_weekly_minute",
    "leagues": "bulletin_leagues",
}


def get_schedule_settings() -> dict:
    """Bülten zamanlama ayarlarını DB'den okur; hiç kaydedilmemişse
    config.py'deki varsayılanlara döner. scheduler.py ve app.py bunu
    kullanır — tek doğruluk kaynağı burasıdır."""
    leagues_raw = db.get_setting(_SETTING_KEYS["leagues"])
    try:
        leagues = json.loads(leagues_raw) if leagues_raw else None
    except (ValueError, TypeError):
        leagues = None

    return {
        "daily_enabled": db.get_setting(_SETTING_KEYS["daily_enabled"], "true") == "true",
        "daily_hour": int(db.get_setting(_SETTING_KEYS["daily_hour"], str(config.DAILY_BULLETIN_HOUR))),
        "daily_minute": int(db.get_setting(_SETTING_KEYS["daily_minute"], str(config.DAILY_BULLETIN_MINUTE))),
        "weekly_enabled": db.get_setting(_SETTING_KEYS["weekly_enabled"], "true") == "true",
        "weekly_weekday": int(db.get_setting(_SETTING_KEYS["weekly_weekday"], str(config.WEEKLY_BULLETIN_WEEKDAY))),
        "weekly_hour": int(db.get_setting(_SETTING_KEYS["weekly_hour"], str(config.WEEKLY_BULLETIN_HOUR))),
        "weekly_minute": int(db.get_setting(_SETTING_KEYS["weekly_minute"], str(config.WEEKLY_BULLETIN_MINUTE))),
        "leagues": leagues or config.DEFAULT_BULLETIN_LEAGUES,
    }


def save_schedule_settings(
    daily_enabled: bool, daily_hour: int, daily_minute: int,
    weekly_enabled: bool, weekly_weekday: int, weekly_hour: int, weekly_minute: int,
    leagues: list[str],
) -> None:
    db.set_setting(_SETTING_KEYS["daily_enabled"], "true" if daily_enabled else "false")
    db.set_setting(_SETTING_KEYS["daily_hour"], str(daily_hour))
    db.set_setting(_SETTING_KEYS["daily_minute"], str(daily_minute))
    db.set_setting(_SETTING_KEYS["weekly_enabled"], "true" if weekly_enabled else "false")
    db.set_setting(_SETTING_KEYS["weekly_weekday"], str(weekly_weekday))
    db.set_setting(_SETTING_KEYS["weekly_hour"], str(weekly_hour))
    db.set_setting(_SETTING_KEYS["weekly_minute"], str(weekly_minute))
    db.set_setting(_SETTING_KEYS["leagues"], json.dumps(leagues, ensure_ascii=False))


def format_combo(n: int, combo) -> str:
    """Canlı bir ComboSuggestion nesnesini (coupon_builder.build_combo /
    build_multiple_combos çıktısı) Telegram mesaj metnine çevirir. Streamlit
    arayüzündeki 'Bu kuponu Telegram'a gönder' butonu da bunu kullanır."""
    label = LEG_LABELS.get(n, f"{n}'Lİ KUPON")
    lines = [f"\n{label} — Puan: {combo.combo_score.total:.0f}/100"]
    for leg in combo.legs:
        fx = leg.fixture
        p = leg.pick
        match_date = fx.date[:16].replace("T", " ")
        lines.append(
            f"  ⚽ {fx.home_team} - {fx.away_team} ({fx.league}, {match_date})\n"
            f"     {p.market}: {cb.translate_selection(p.selection)} @ {p.odd} ({p.bookmaker}) — Edge: %{p.edge * 100:.1f}"
        )
    lines.append(
        f"  ➜ Toplam Oran: {combo.combined_odd}  |  Birleşik İhtimal: %{combo.combined_probability * 100:.1f}"
        f"  |  Kombine EV: %{combo.combined_ev * 100:+.1f}"
        f"  |  Önerilen Stake: bankroll'un %{combo.recommended_stake_fraction * 100:.2f}'si"
    )
    return "\n".join(lines)


def format_coupon_from_rows(
    n_legs: int, legs: list[dict], combined_odd: float, combined_probability: float,
    combo_score_total: float, stake_fraction: float, title: str | None = None,
) -> str:
    """format_combo'nun DB'den (db.get_coupons_for_analysis) geri yüklenen,
    ham `predictions` satırlarından oluşan kupon verisi için karşılığı —
    kaydedilmiş bir analizi arayüzden yeniden gönderirken kullanılır (o an
    elde canlı bir ComboSuggestion/Recommendation nesnesi olmadığından)."""
    label = LEG_LABELS.get(n_legs, f"{n_legs}'Lİ KUPON")
    lines = [f"{title}\n" if title else "", f"{label} — Puan: {combo_score_total:.0f}/100"]
    for leg in legs:
        match_date = (leg.get("match_date") or "")[:16].replace("T", " ")
        lines.append(
            f"  ⚽ {leg['home_team']} - {leg['away_team']} ({leg['league']}, {match_date})\n"
            f"     {leg['market']}: {cb.translate_selection(leg['selection'])} @ {leg['odd']} "
            f"({leg['bookmaker']}) — Edge: %{leg['edge'] * 100:.1f}"
        )
    combined_ev = combined_odd * combined_probability - 1
    lines.append(
        f"  ➜ Toplam Oran: {combined_odd}  |  Birleşik İhtimal: %{combined_probability * 100:.1f}"
        f"  |  Kombine EV: %{combined_ev * 100:+.1f}"
        f"  |  Önerilen Stake: bankroll'un %{stake_fraction * 100:.2f}'si"
    )
    return "\n".join(lines)


def format_bulletin_message(title: str, combos: dict) -> str:
    if not combos:
        return (
            f"{title}\n\nBugün/bu hafta kriterlere uygun (yeterli edge taşıyan) "
            "bir value-bet bulunamadı. Piyasa verimli görünüyor, bekleyelim."
        )
    parts = [title]
    for n in sorted(combos.keys()):
        parts.append(format_combo(n, combos[n]))
    parts.append(
        "\n⚠️ Bu bülten istatistiksel bir tahmindir, kazanç garantisi vermez. "
        "Bankroll'unuzun kaybetmeyi göze alamayacağınız kısmını riske atmayın.\n"
        "ℹ️ Buradaki oranlar global bahis şirketlerinden (Bet365, Pinnacle vb.) "
        "alınmıştır — Türkiye'de yasal olarak erişilebilir değildir. Değer/edge "
        "tahmini için kullanılır; gerçek bahis yapmadan önce kendi yasal "
        "sitenizdeki (İddaa/Nesine/Misli) güncel oranla karşılaştırın."
    )
    return "\n".join(parts)


def _build_combos_and_log(analyzed: list, bulletin_id: int) -> dict:
    recs = cb.build_recommendations(analyzed, min_edge=config.BULLETIN_MIN_EDGE)
    recs_per_bookmaker = cb.build_recommendations_per_bookmaker(analyzed, min_edge=config.BULLETIN_MIN_EDGE)
    if not recs_per_bookmaker:
        return {}
    # Tek-şirket kısıtı (bkz. coupon_builder.build_combo): bir kombine kupon
    # tek bir sitede oynanabilir, bu yüzden kombolar recs_per_bookmaker'dan kurulur.
    combos = cb.build_multiple_combos(recs_per_bookmaker, leg_counts=config.BULLETIN_LEG_COUNTS)
    if not combos:
        return {}

    id_map = history.log_predictions(recs) if recs else {}
    for n, combo in combos.items():
        tags = cb.compute_technique_tags(combo)
        history.log_coupon(combo, id_map, bulletin_id=bulletin_id, technique_tags=tags)

    return combos


def run_daily_bulletin(league_names: list[str] | None = None, target_date: str | None = None, force_rebuild: bool = False) -> int | None:
    """Yarının (target_date verilmezse bugün+1) seçili liglerdeki maçlarını
    analiz eder, 1-7 bacaklı kupon varyantları üretir ve Telegram'a gönderir.
    Zaten bu tarih için gönderilmiş bir bülten varsa (force_rebuild=False)
    hiçbir API isteği yapmadan None döner."""
    league_names = league_names or config.DEFAULT_BULLETIN_LEAGUES
    target_date = target_date or (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    if not force_rebuild and db.bulletin_exists("daily", target_date):
        return None

    analyzed, warnings = cb.fetch_and_analyze(
        league_names, fixture_mode="date", target_date=target_date,
        use_availability=False, use_elo=True,
    )
    if not analyzed:
        return None  # o gun secili liglerde mac yok, bulten kaydi olusturmaya gerek yok

    bulletin_id = db.insert_bulletin("daily", target_date, league_names)
    combos = _build_combos_and_log(analyzed, bulletin_id)

    title = f"📅 YARININ KUPONLARI — {target_date}"
    text = format_bulletin_message(title, combos)
    tg_result = telegram_bot.send_message(text)
    db.mark_bulletin_posted(bulletin_id, tg_result.get("message_id"))
    return bulletin_id


def run_weekly_bulletin(league_names: list[str] | None = None, week_start: str | None = None, force_rebuild: bool = False) -> int | None:
    """Önümüzdeki 7 günün (week_start verilmezse bugün) seçili liglerdeki
    tüm maçlarını tarar; farklı günlerdeki maçları aynı kuponda
    birleştirebilen 1-7 bacaklı varyantlar üretir."""
    league_names = league_names or config.DEFAULT_BULLETIN_LEAGUES
    week_start_dt = datetime.now() if week_start is None else datetime.strptime(week_start, "%Y-%m-%d")
    week_start_str = week_start_dt.strftime("%Y-%m-%d")
    week_end_str = (week_start_dt + timedelta(days=6)).strftime("%Y-%m-%d")

    if not force_rebuild and db.bulletin_exists("weekly", week_start_str):
        return None

    analyzed, warnings = cb.fetch_and_analyze(
        league_names, fixture_mode="range", from_date=week_start_str, to_date=week_end_str,
        use_availability=False, use_elo=True,
    )
    if not analyzed:
        return None

    bulletin_id = db.insert_bulletin("weekly", week_start_str, league_names)
    combos = _build_combos_and_log(analyzed, bulletin_id)

    title = f"🗓️ HAFTANIN BÜLTENİ — {week_start_str} / {week_end_str}"
    text = format_bulletin_message(title, combos)
    tg_result = telegram_bot.send_message(text)
    db.mark_bulletin_posted(bulletin_id, tg_result.get("message_id"))
    return bulletin_id
