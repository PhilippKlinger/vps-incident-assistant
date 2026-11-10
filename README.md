# vps-incident-assistant

Incident assistant for the VPS platform: turns Uptime Kuma alerts into GitHub issues with rule-based triage and a checked local LLM analysis.

## Development

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv/Scripts/activate
pip install -r requirements-dev.txt

ruff check .
ruff format --check .
pytest
```
