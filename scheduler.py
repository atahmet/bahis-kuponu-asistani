"""7/24 çalışan zamanlayıcı: her dakika saati kontrol eder, günlük bülteni
(config.DAILY_BULLETIN_HOUR:MINUTE) ve haftalık bülteni
(config.WEEKLY_BULLETIN_WEEKDAY, HOUR:MINUTE) tam zamanında üretip Telegram'a
gönderir; periyodik olarak (config.SCHEDULER_RESULT_SYNC_MINUTES) sonuç
senkronizasyonu (Elo güncelleme dahil) ve kapanış oranı/CLV kontrolü yapar.

Çalıştırma: `python scheduler.py` — pencereyi açık bırakın (PC 7/24 açık
kalacaksa bu yeterli). Daha sağlam bir kurulum isterseniz Windows Görev
Zamanlayıcı'ya "sistem başlangıcında çalıştır" olarak ekleyebilirsiniz
(bkz. README.md).

Aynı dakika içinde birden fazla tetiklenmeyi önlemek için, bültenler ayrıca
DB'de idempotent olarak işaretlenir (bkz. bulletin.py) — bu script kazayla
iki kez başlatılsa veya çöküp yeniden başlasa bile aynı gün için tekrar
API isteği yapıp Telegram'a ikinci kez göndermez.
"""
import logging
import time
import traceback
from datetime import datetime, timedelta

import bulletin
import config
import history

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.BASE_DIR / "scheduler.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("scheduler")

config.set_plan("pro")

_last_result_sync: datetime | None = None
_last_daily_trigger_date: str | None = None
_last_weekly_trigger_date: str | None = None


def _should_run_daily(now: datetime, settings: dict) -> bool:
    global _last_daily_trigger_date
    if not settings["daily_enabled"]:
        return False
    today_str = now.strftime("%Y-%m-%d")
    if now.hour == settings["daily_hour"] and now.minute == settings["daily_minute"]:
        if _last_daily_trigger_date != today_str:
            _last_daily_trigger_date = today_str
            return True
    return False


def _should_run_weekly(now: datetime, settings: dict) -> bool:
    global _last_weekly_trigger_date
    if not settings["weekly_enabled"]:
        return False
    today_str = now.strftime("%Y-%m-%d")
    if (
        now.weekday() == settings["weekly_weekday"]
        and now.hour == settings["weekly_hour"]
        and now.minute == settings["weekly_minute"]
    ):
        if _last_weekly_trigger_date != today_str:
            _last_weekly_trigger_date = today_str
            return True
    return False


def _maybe_sync_results(now: datetime) -> None:
    global _last_result_sync
    due = _last_result_sync is None or (now - _last_result_sync).total_seconds() >= config.SCHEDULER_RESULT_SYNC_MINUTES * 60
    if not due:
        return
    try:
        settled = history.sync_results()
        clv_updated = history.update_closing_odds()
        log.info(f"Sonuç senkronizasyonu: {settled} tahmin sonuçlandı, {clv_updated} kapanış oranı güncellendi.")
        fitted_rho = history.calibrate_dixon_coles_rho()
        if fitted_rho is not None:
            log.info(f"Dixon-Coles rho yeniden kalibre edildi: {fitted_rho:.4f}")
    except Exception:
        log.error("Sonuç senkronizasyonu sırasında hata:\n" + traceback.format_exc())
    finally:
        _last_result_sync = now


def tick() -> None:
    now = datetime.now()
    settings = bulletin.get_schedule_settings()  # her tikte DB'den okunur (arayüzden değişiklik anında yansır)

    if _should_run_daily(now, settings):
        target_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        log.info(f"Günlük bülten tetiklendi (hedef tarih: {target_date}).")
        try:
            bulletin_id = bulletin.run_daily_bulletin(settings["leagues"], target_date)
            log.info(f"Günlük bülten sonucu: bulletin_id={bulletin_id}")
        except Exception:
            log.error("Günlük bülten oluşturulurken hata:\n" + traceback.format_exc())

    if _should_run_weekly(now, settings):
        log.info("Haftalık bülten tetiklendi.")
        try:
            bulletin_id = bulletin.run_weekly_bulletin(settings["leagues"])
            log.info(f"Haftalık bülten sonucu: bulletin_id={bulletin_id}")
        except Exception:
            log.error("Haftalık bülten oluşturulurken hata:\n" + traceback.format_exc())

    _maybe_sync_results(now)


def main() -> None:
    s = bulletin.get_schedule_settings()
    log.info(
        f"Zamanlayıcı başladı. Günlük bülten: {'AÇIK' if s['daily_enabled'] else 'KAPALI'} "
        f"(her gün {s['daily_hour']:02d}:{s['daily_minute']:02d}) | "
        f"Haftalık bülten: {'AÇIK' if s['weekly_enabled'] else 'KAPALI'} "
        f"({bulletin.WEEKDAYS_TR[s['weekly_weekday']]} {s['weekly_hour']:02d}:{s['weekly_minute']:02d}) | "
        f"Sonuç senkronizasyonu her {config.SCHEDULER_RESULT_SYNC_MINUTES} dakikada bir. "
        "Bu ayarlar arayüzden ('⚙️ Telegram Ayarları') değiştirilebilir, her tikte DB'den yeniden okunur."
    )
    while True:
        try:
            tick()
        except Exception:
            log.error("Ana döngüde beklenmeyen hata:\n" + traceback.format_exc())
        time.sleep(config.SCHEDULER_TICK_SECONDS)


if __name__ == "__main__":
    main()
