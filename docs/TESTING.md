# Testing

These instructions describe owner-authorized maintenance. They do not grant execution or modification rights beyond [LICENSE.md](../LICENSE.md).

Tests use synthetic settings/identifiers, temporary state and mocked Telegram/HTTP transports. They need no real token, production state or running bot. CI uses Python 3.12 and Node 22, with Actions pinned to commit SHAs.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
PYTHONPATH=. PYTHON_DOTENV_DISABLED=true TELEGRAM_BOT_TOKEN=synthetic-test-token .venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
.venv/bin/python -m pip check
git diff --check
```

## JavaScript and template checks

Node is required for Canvas/runtime boundary tests. Templates are already committed; Terser is needed only for development rebuild/check commands. Builders invoke pinned Terser 5.51.2 through npx.

```bash
.venv/bin/python scripts/build_price_widget.py --check
.venv/bin/python scripts/build_farming_widget.py --check
.venv/bin/python scripts/build_widget_compat.py --check
```

Tests cover validation/freshness/anomalies, source-backed history and zero/missing values, alerts, settings rollback, user/message queues, ownership/revisions/recovery, Back, reserve-loading gestures, weekly popups, coin/haptics/menu delivery, localization and raw/compressed budgets. Public compatibility checks cover absent private logos and the TST outline. Internal concept/audit scripts are not dependencies of this suite.

`tests/benchmark_user_concurrency.py` is an opt-in offline fake-transport burst comparison. It is not a live capacity guarantee or a load test against Telegram/MEXC.

## Optional native Desktop regression checks

`tests/test_widget_numeric_compat.py` can exercise the pinned QuickJS engine under an isolated decimal-comma locale. Supply `POOLMASTER_QUICKJS_PROBE`, `POOLMASTER_QUICKJS_LOCALE_PRELOAD` and `POOLMASTER_QUICKJS_LOCALE_DIR`. End-to-end sandbox/validator cases also require `POOLMASTER_WIDGET_SDK_DIR` containing `sandbox-bundle.js` and `validator-bundle.js`.

The reference SDK revision is widget-sandbox `3959d4089a8f21b05b99dbd2a73e5aacd743e39a` (0.6.1). Native tools/SDK/locale binaries are supplied separately and are not vendored. Ordinary CI intentionally skips cases whose native tools are absent; a green CI run is not proof of native or live Telegram verification.

For real-device checks, use an independently authorized test identity and open fresh widgets on supported clients. Never start a second poller for a running token or put production identifiers in fixtures. [Security](../SECURITY.md).
