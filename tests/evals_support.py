from pathlib import Path

from assistant.config import load_config
from evals.cases import HistoryStore, load_cases

FIXTURES = Path(__file__).parent / "evals_fixtures"
CONFIG = Path(__file__).parent.parent / "config.toml"


def load_fixture_set():
    store = HistoryStore.load(FIXTURES)
    cases = load_cases(FIXTURES / "cases", store, load_config(CONFIG))
    return store, cases
