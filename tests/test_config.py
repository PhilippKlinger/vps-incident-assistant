from pathlib import Path

import pytest

from assistant.config import SettingsError, load_config, load_settings

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"

MANDATORY = {
    "WEBHOOK_SECRET": "w",
    "GITHUB_READ_TOKEN": "r",
    "GITHUB_ISSUE_TOKEN": "i",
    "ISSUE_REPO": "owner/repo",
    "TELEGRAM_BOT_TOKEN": "t",
    "TELEGRAM_CHAT_ID": "1",
    "BEDROCK_REGION": "eu-north-1",
    "BEDROCK_MODEL": "eu.vendor.model-v1:0",
    "AWS_BEARER_TOKEN_BEDROCK": "b",
}


def test_project_for_known_monitor():
    project = load_config(CONFIG_PATH).project_for("Videoflix · API")
    assert project.name == "videoflix"
    assert project.llm is True


def test_project_for_unknown_monitor():
    project = load_config(CONFIG_PATH).project_for("Neu")
    assert project.name == "unknown"
    assert project.llm is False


def test_load_config_reads_llm_switch():
    config = load_config(CONFIG_PATH)
    assert config.llm_enabled is False
    assert config.llm_max_runs_per_hour == 6
    assert config.merge_window_minutes == 10


def test_load_config_maps_all_monitors():
    config = load_config(CONFIG_PATH)
    assert config.project_for("Statusseite").name == "status"
    assert config.project_for("Portfolio").llm is False
    assert config.project_for("Neuro Check-in · Web").name == "neuro-check-in"
    assert config.project_for("Blog").repos == ()


def test_load_settings_lists_all_missing():
    with pytest.raises(SettingsError) as error:
        load_settings({})
    for name in MANDATORY:
        assert name in str(error.value)


def test_load_settings_treats_empty_as_missing():
    with pytest.raises(SettingsError, match="WEBHOOK_SECRET"):
        load_settings({**MANDATORY, "WEBHOOK_SECRET": ""})


def test_settings_reads_bedrock_values():
    settings = load_settings(MANDATORY)
    assert settings.bedrock_region == "eu-north-1"
    assert settings.bedrock_model == "eu.vendor.model-v1:0"
    assert settings.bedrock_token == "b"
    assert settings.issue_repo == "owner/repo"
