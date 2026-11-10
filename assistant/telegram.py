import httpx


class TelegramError(Exception):
    pass


def send_telegram(client: httpx.Client, token: str, chat_id: str, text: str) -> None:
    # The URL contains the bot token, so errors carry only the status or exception type.
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
    except httpx.HTTPError as exc:
        raise TelegramError(f"Telegram not reachable: {type(exc).__name__}") from None
    if not response.is_success:
        raise TelegramError(f"Telegram request failed: HTTP {response.status_code}")
