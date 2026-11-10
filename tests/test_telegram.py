import json

import httpx
import pytest

from assistant.telegram import TelegramError, send_telegram


def test_send_telegram_posts_message():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    send_telegram(client, "TOKEN", "42", "hallo")
    assert str(seen[0].url) == "https://api.telegram.org/botTOKEN/sendMessage"
    assert json.loads(seen[0].content) == {
        "chat_id": "42",
        "text": "hallo",
        "disable_web_page_preview": True,
    }


def test_send_telegram_error_has_status_only():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(TelegramError) as error:
        send_telegram(client, "TOKEN", "42", "x")
    assert "401" in str(error.value)
    assert "TOKEN" not in str(error.value)
