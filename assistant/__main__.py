import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn

from assistant.app import create_app
from assistant.config import SettingsError, load_config, load_settings
from assistant.github_read import GitHubReader, make_client
from assistant.issues import IssueClient, make_issue_client
from assistant.llm import BedrockClient, RateLimiter
from assistant.pipeline import Deps
from assistant.probe import default_resolve, default_tls_days, probe_url
from assistant.telegram import send_telegram


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # httpx logs request URLs at INFO, and the Telegram URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = load_settings(os.environ)
    except SettingsError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
    config = load_config(Path(__file__).resolve().parent.parent / "config.toml")

    probe_client = httpx.Client(
        timeout=10, follow_redirects=False, headers={"User-Agent": "vps-incident-assistant"}
    )
    telegram_client = httpx.Client(timeout=10)
    deps = Deps(
        config=config,
        settings=settings,
        probe=lambda url: probe_url(
            url, client=probe_client, tls_days=default_tls_days, resolve=default_resolve
        ),
        reader=GitHubReader(make_client(settings.github_read_token)),
        issues=IssueClient(make_issue_client(settings.github_issue_token), settings.issue_repo),
        llm=BedrockClient(
            httpx.Client(), settings.bedrock_region, settings.bedrock_model, settings.bedrock_token
        ),
        telegram=lambda text: send_telegram(
            telegram_client, settings.telegram_bot_token, settings.telegram_chat_id, text
        ),
        limiter=RateLimiter(config.llm_max_runs_per_hour),
        now=lambda: datetime.now(UTC),
    )
    uvicorn.run(create_app(deps), host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
