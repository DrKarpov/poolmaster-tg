# Testing

The automated suite uses synthetic configuration, temporary paths, and mocked HTTP and Telegram responses. It does not require production credentials or start a Telegram bot.

Python 3.12 is used by CI. From the repository root:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
PYTHONPATH=. PYTHON_DOTENV_DISABLED=true .venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
.venv/bin/python -m pip check
git diff --check
```

Tests cover market and farming parsing, source identity/freshness checks, alert behavior, charts, localized user flows, admin authorization, managed destinations, SQLite history, state persistence, and backup/import tooling. Tests use synthetic identifiers; do not add production user data or credentials to fixtures.
