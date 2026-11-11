import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://ollama:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:8b"

# Environment variables that must be set and non-empty.
MANDATORY_ENV = (
    "WEBHOOK_SECRET",
    "GITHUB_READ_TOKEN",
    "GITHUB_ISSUE_TOKEN",
    "ISSUE_REPO",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Project:
    name: str
    display_name: str
    monitors: tuple[str, ...]
    repos: tuple[str, ...]
    runbook_repos: tuple[str, ...]
    llm: bool


UNKNOWN_PROJECT = Project("unknown", "unknown", (), (), (), False)


@dataclass(frozen=True)
class Config:
    projects: tuple[Project, ...]
    llm_enabled: bool
    llm_max_runs_per_hour: int
    merge_window_minutes: int

    def project_for(self, monitor_name: str) -> Project:
        for project in self.projects:
            if monitor_name in project.monitors:
                return project
        return UNKNOWN_PROJECT


def load_config(path: Path) -> Config:
    with path.open("rb") as file:
        data = tomllib.load(file)
    projects = tuple(
        Project(
            name=entry["name"],
            display_name=entry["display_name"],
            monitors=tuple(entry["monitors"]),
            repos=tuple(entry.get("repos", ())),
            runbook_repos=tuple(entry.get("runbook_repos", ())),
            llm=entry["llm"],
        )
        for entry in data["projects"]
    )
    return Config(
        projects=projects,
        llm_enabled=data["llm"]["enabled"],
        llm_max_runs_per_hour=data["llm"]["max_runs_per_hour"],
        merge_window_minutes=data["lifecycle"]["merge_window_minutes"],
    )


@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    github_read_token: str
    github_issue_token: str
    issue_repo: str
    telegram_bot_token: str
    telegram_chat_id: str
    ollama_url: str
    ollama_model: str


def load_settings(env: Mapping[str, str]) -> Settings:
    missing = [name for name in MANDATORY_ENV if not env.get(name)]
    if missing:
        raise SettingsError("Missing environment variables: " + ", ".join(missing))
    return Settings(
        webhook_secret=env["WEBHOOK_SECRET"],
        github_read_token=env["GITHUB_READ_TOKEN"],
        github_issue_token=env["GITHUB_ISSUE_TOKEN"],
        issue_repo=env["ISSUE_REPO"],
        telegram_bot_token=env["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=env["TELEGRAM_CHAT_ID"],
        ollama_url=env.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL,
        ollama_model=env.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL,
    )
