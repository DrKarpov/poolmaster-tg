from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


def parse_admin_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError:
            logging.warning("Invalid admin id ignored: %s", item)
    return frozenset(ids)


def _read_int(
    values: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int = 0,
) -> int:
    raw = values.get(key, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{key} must be at least {minimum}")
    return value


def _read_float_or_default(
    values: Mapping[str, str],
    key: str,
    *,
    default: float,
) -> float:
    raw = values.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be greater than zero")
    return value


def _read_bool(values: Mapping[str, str], key: str, default: bool) -> bool:
    raw = values.get(key, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean")


def _read_path(values: Mapping[str, str], key: str, default: str) -> Path:
    raw = values.get(key, default).strip()
    return Path(raw or default)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_bot_username: str
    admin_user_ids: frozenset[int]
    admin_state_path: Path
    subscribers_path: Path
    stats_path: Path
    managed_destinations_path: Path
    user_state_path: Path
    farming_cache_path: Path
    farming_history_path: Path
    snapshot_cache_path: Path
    global_auto_push_paused: bool
    farming_poll_interval_seconds: int
    farming_stale_max_age_seconds: int
    apr_anomaly_confirm_threshold: float
    price_poll_interval_seconds: int
    price_source_max_age_seconds: int
    price_stale_max_age_seconds: int
    price_alert_cooldown_seconds: int
    price_anomaly_confirm_threshold: float
    ui_refresh_cooldown_seconds: int
    chart_cache_dir: Path
    chart_stale_max_age_seconds: int
    delete_command_messages: bool


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    if environ is None:
        load_dotenv()
        values: Mapping[str, str] = os.environ
    else:
        values = environ

    return Settings(
        telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_bot_username=values.get("TELEGRAM_BOT_USERNAME", "JETTONPOOL_BOT").strip().lstrip("@"),
        admin_user_ids=parse_admin_ids(values.get("ADMIN_USER_IDS", "")),
        admin_state_path=_read_path(
            values,
            "AUTO_PUSH_STATE_PATH",
            values.get("ADMIN_STATE_PATH", "data/admin_state.json"),
        ),
        subscribers_path=_read_path(values, "SUBSCRIBERS_PATH", "data/subscribers.json"),
        stats_path=_read_path(values, "STATS_PATH", "data/stats.json"),
        managed_destinations_path=_read_path(values, "MANAGED_DESTINATIONS_PATH", "data/managed_destinations.json"),
        user_state_path=_read_path(values, "USER_STATE_PATH", "data/user_state.json"),
        farming_cache_path=_read_path(values, "FARMING_CACHE_PATH", "data/farming_cache.json"),
        farming_history_path=_read_path(values, "FARMING_HISTORY_PATH", "data/farming_history.sqlite3"),
        snapshot_cache_path=_read_path(values, "SNAPSHOT_CACHE_PATH", "data/snapshot.json"),
        global_auto_push_paused=_read_bool(values, "GLOBAL_AUTO_PUSH_PAUSED", True),
        farming_poll_interval_seconds=_read_int(values, "FARMING_POLL_INTERVAL_SECONDS", 300, minimum=1),
        farming_stale_max_age_seconds=_read_int(values, "FARMING_STALE_MAX_AGE_SECONDS", 1800, minimum=1),
        apr_anomaly_confirm_threshold=_read_float_or_default(
            values, "APR_ANOMALY_CONFIRM_THRESHOLD", default=50.0
        ),
        price_poll_interval_seconds=_read_int(values, "PRICE_POLL_INTERVAL_SECONDS", 60, minimum=1),
        price_source_max_age_seconds=_read_int(values, "PRICE_SOURCE_MAX_AGE_SECONDS", 300, minimum=1),
        price_stale_max_age_seconds=_read_int(values, "PRICE_STALE_MAX_AGE_SECONDS", 900, minimum=1),
        price_alert_cooldown_seconds=_read_int(values, "PRICE_ALERT_COOLDOWN_SECONDS", 900, minimum=1),
        price_anomaly_confirm_threshold=_read_float_or_default(
            values, "PRICE_ANOMALY_CONFIRM_THRESHOLD", default=20.0
        ),
        ui_refresh_cooldown_seconds=_read_int(values, "UI_REFRESH_COOLDOWN_SECONDS", 10, minimum=1),
        chart_cache_dir=_read_path(values, "CHART_CACHE_DIR", "data/chart_cache"),
        chart_stale_max_age_seconds=_read_int(values, "CHART_STALE_MAX_AGE_SECONDS", 1800, minimum=1),
        delete_command_messages=_read_bool(values, "DELETE_COMMAND_MESSAGES", False),
    )
