import importlib
import os
from pathlib import Path

import pytest


def _configuration_module():
    try:
        return importlib.import_module("config")
    except ModuleNotFoundError as exc:
        if exc.name == "config":
            pytest.fail("central configuration loader is missing")
        raise


def test_settings_load_dotenv_before_reading_environment(monkeypatch):
    config = _configuration_module()
    monkeypatch.delenv("PRICE_ALERT_COOLDOWN_SECONDS", raising=False)

    def load_sample_dotenv():
        monkeypatch.setenv("PRICE_ALERT_COOLDOWN_SECONDS", "137")

    monkeypatch.setattr(config, "load_dotenv", load_sample_dotenv)

    settings = config.load_settings()

    assert settings.price_alert_cooldown_seconds == 137


def test_chart_settings_are_read_after_dotenv_load(monkeypatch, tmp_path):
    config = _configuration_module()
    expected_path = tmp_path / "chart-cache"
    monkeypatch.delenv("CHART_CACHE_DIR", raising=False)

    def load_sample_dotenv():
        monkeypatch.setenv("CHART_CACHE_DIR", str(expected_path))

    monkeypatch.setattr(config, "load_dotenv", load_sample_dotenv)
    chart = importlib.import_module("chart")

    importlib.reload(chart)

    assert Path(chart.CHART_CACHE_DIR) == expected_path


def test_settings_parse_admin_ids_and_runtime_paths_without_loading_dotenv(monkeypatch):
    config = _configuration_module()
    monkeypatch.setattr(config, "load_dotenv", lambda: pytest.fail("explicit environment must not load dotenv"))

    settings = config.load_settings(
        {
            "TELEGRAM_BOT_TOKEN": "synthetic-test-token",
            "ADMIN_USER_IDS": "101, invalid, 202",
            "SUBSCRIBERS_PATH": "runtime/subscribers.json",
            "ADMIN_STATE_PATH": "runtime/admin.json",
            "MANAGED_DESTINATIONS_PATH": "runtime/destinations.json",
            "USER_STATE_PATH": "runtime/users.json",
            "FARMING_CACHE_PATH": "runtime/farming.json",
            "FARMING_HISTORY_PATH": "runtime/farming-history.sqlite3",
            "PRICE_ALERT_COOLDOWN_SECONDS": "900",
            "PRICE_STALE_MAX_AGE_SECONDS": "600",
        }
    )

    assert settings.admin_user_ids == frozenset({101, 202})
    assert settings.subscribers_path == Path("runtime/subscribers.json")
    assert settings.admin_state_path == Path("runtime/admin.json")
    assert settings.managed_destinations_path == Path("runtime/destinations.json")
    assert settings.user_state_path == Path("runtime/users.json")
    assert settings.farming_cache_path == Path("runtime/farming.json")
    assert settings.farming_history_path == Path("runtime/farming-history.sqlite3")
    assert settings.price_stale_max_age_seconds == 600
    assert settings.price_alert_cooldown_seconds == 900
    assert settings.ui_refresh_cooldown_seconds == 10


def test_auto_push_state_path_uses_spec_name_and_accepts_legacy_alias():
    config = _configuration_module()

    spec_named = config.load_settings(
        {
            "TELEGRAM_BOT_TOKEN": "synthetic-test-token",
            "AUTO_PUSH_STATE_PATH": "runtime/auto-push.json",
        }
    )
    legacy_named = config.load_settings(
        {
            "TELEGRAM_BOT_TOKEN": "synthetic-test-token",
            "ADMIN_STATE_PATH": "runtime/admin.json",
        }
    )

    assert spec_named.admin_state_path == Path("runtime/auto-push.json")
    assert legacy_named.admin_state_path == Path("runtime/admin.json")


def test_global_auto_push_pause_defaults_to_on():
    config = _configuration_module()

    settings = config.load_settings({"TELEGRAM_BOT_TOKEN": "synthetic-test-token"})

    assert settings.global_auto_push_paused is True


def test_farming_history_path_defaults_under_runtime_data():
    config = _configuration_module()

    settings = config.load_settings({"TELEGRAM_BOT_TOKEN": "synthetic-test-token"})

    assert settings.farming_history_path == Path("data/farming_history.sqlite3")


def test_example_configuration_does_not_grant_admin_access_to_a_placeholder_id():
    config = _configuration_module()
    example = Path(__file__).resolve().parents[1] / ".env.example"
    values = {}
    for line in example.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    settings = config.load_settings(values)

    assert settings.admin_user_ids == frozenset()


def test_anomaly_confirmation_has_safe_defaults_and_allows_explicit_overrides():
    config = _configuration_module()

    defaults = config.load_settings(
        {
            "TELEGRAM_BOT_TOKEN": "synthetic-test-token",
            "APR_ANOMALY_CONFIRM_THRESHOLD": "",
            "PRICE_ANOMALY_CONFIRM_THRESHOLD": "",
        }
    )
    overridden = config.load_settings(
        {
            "TELEGRAM_BOT_TOKEN": "synthetic-test-token",
            "APR_ANOMALY_CONFIRM_THRESHOLD": "125",
            "PRICE_ANOMALY_CONFIRM_THRESHOLD": "25",
        }
    )

    assert defaults.apr_anomaly_confirm_threshold == 50
    assert defaults.price_anomaly_confirm_threshold == 20
    assert overridden.apr_anomaly_confirm_threshold == 125
    assert overridden.price_anomaly_confirm_threshold == 25


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("APR_ANOMALY_CONFIRM_THRESHOLD", "inf"),
        ("PRICE_ANOMALY_CONFIRM_THRESHOLD", "nan"),
    ),
)
def test_float_settings_reject_non_finite_values(key, value):
    config = _configuration_module()

    with pytest.raises(ValueError, match=key):
        config.load_settings({"TELEGRAM_BOT_TOKEN": "synthetic-test-token", key: value})
