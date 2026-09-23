"""Telegram Bot API istemcisi: kupon bültenlerini kanala gönderir.

Kurulum: @BotFather'dan bir bot oluşturup token alın, botu kanalınıza admin
olarak ekleyin, kanal ID'sini bulun (bkz. README.md 'Telegram bot kurulumu')
ve TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID değerlerini .env dosyanıza ekleyin.
"""
import requests

import config

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4000  # Telegram sınırı 4096; güvenlik payı bırakıldı


class TelegramError(Exception):
    pass


def _require_config() -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise TelegramError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID .env dosyasında tanımlı değil. "
            "Kurulum adımları için README.md'ye bakın."
        )


def send_message(text: str, parse_mode: str | None = None) -> dict:
    """Tek bir mesaj gönderir. Metin MAX_MESSAGE_LENGTH'i aşarsa otomatik
    olarak birden fazla mesaja bölünür (Telegram'ın 4096 karakter sınırı
    nedeniyle); ilk mesajın sonucu döndürülür.

    parse_mode varsayılan olarak None (düz metin): takım isimlerindeki
    `_`, `*`, `[` gibi karakterler Markdown/HTML modunda ayrıştırma
    hatasına yol açabileceğinden bültenler düz metin olarak gönderilir."""
    _require_config()

    chunks = [text[i : i + MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)] or [text]

    first_result = None
    for chunk in chunks:
        url = f"{TELEGRAM_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(url, json=payload, timeout=20)
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise TelegramError(f"Telegram'dan beklenmeyen cevap: {resp.text[:200]}")

        if not data.get("ok"):
            raise TelegramError(f"Telegram API hatası: {data}")

        if first_result is None:
            first_result = data["result"]

    return first_result


def send_test_message() -> dict:
    return send_message("✅ Bahis Kuponu Asistanı botu bağlandı ve çalışıyor.")
