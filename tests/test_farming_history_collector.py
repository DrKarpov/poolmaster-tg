import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot as bot_module
from bot import PoolmasterBot


def test_daily_history_loop_refreshes_while_posting_is_paused(monkeypatch):
    calls = []
    delays = []

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 18, 23, 59, 0, tzinfo=timezone.utc)

    class SnapshotReader:
        async def refresh_farming(self):
            calls.append("refresh")

    bot = object.__new__(PoolmasterBot)
    bot.settings = SimpleNamespace(global_auto_push_paused=True)
    bot.admin_state_store = SimpleNamespace(is_global_auto_push_paused=lambda: True)
    bot.snapshot_manager = SnapshotReader()
    bot.farming_history_store = object()

    async def cancel_after_cycle(delay):
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(bot_module, "datetime", FixedDateTime)
    monkeypatch.setattr(bot_module.asyncio, "sleep", cancel_after_cycle)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.farming_history_loop(SimpleNamespace()))

    assert calls == ["refresh"]
    assert delays == [360]


def test_history_store_initialization_failure_does_not_stop_bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")

    class UnavailableHistoryStore:
        def __init__(self, _path):
            raise OSError("history directory is read-only")

    monkeypatch.setattr(bot_module, "FarmingHistoryStore", UnavailableHistoryStore)

    bot = PoolmasterBot()

    assert bot.farming_history_store is None
    assert bot.snapshot_manager.farming_history_store is None


def test_seed_import_failure_does_not_stop_bot_or_disable_live_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    real_store = bot_module.FarmingHistoryStore

    class UnseedableHistoryStore(real_store):
        def seed_csv(self, _path):
            raise OSError("seed file unavailable")

    monkeypatch.setattr(bot_module, "FarmingHistoryStore", UnseedableHistoryStore)

    bot = PoolmasterBot()

    assert isinstance(bot.farming_history_store, UnseedableHistoryStore)
    assert bot.snapshot_manager.farming_history_store is bot.farming_history_store


def test_daily_history_loop_retries_persistence_before_fetching_again(monkeypatch):
    delays = []

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 18, 23, 59, 0, tzinfo=timezone.utc)

    class SnapshotReader:
        def __init__(self):
            self.last_history_write_error = None
            self.refresh_count = 0
            self.retry_count = 0

        async def refresh_farming(self):
            self.refresh_count += 1
            self.last_history_write_error = OSError("history disk unavailable")

        def retry_pending_farming_history_write(self):
            self.retry_count += 1
            self.last_history_write_error = None
            return True

    manager = SnapshotReader()
    bot = object.__new__(PoolmasterBot)
    bot.settings = SimpleNamespace(global_auto_push_paused=True)
    bot.admin_state_store = SimpleNamespace(is_global_auto_push_paused=lambda: True)
    bot.snapshot_manager = manager
    bot.farming_history_store = object()

    async def cancel_after_retry(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot_module, "datetime", FixedDateTime)
    monkeypatch.setattr(bot_module.asyncio, "sleep", cancel_after_retry)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.farming_history_loop(SimpleNamespace()))

    assert manager.refresh_count == 1
    assert manager.retry_count == 1
    assert delays == [300, 360]


def test_bot_initializes_store_and_seeds_curated_history_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    history_path = tmp_path / "runtime" / "farming_history.sqlite3"
    monkeypatch.setenv("FARMING_HISTORY_PATH", str(history_path))

    bot = PoolmasterBot()

    assert bot.farming_history_store.path == history_path
    assert bot.snapshot_manager.farming_history_store is bot.farming_history_store
    with sqlite3.connect(history_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 140
    seed_path = Path(bot_module.__file__).parent / "resources" / "farming_history_seed.csv"
    assert bot.farming_history_store.seed_csv(seed_path) == 0
