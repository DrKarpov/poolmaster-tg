import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot as bot_module
import snapshot as snapshot_module
import user_state
from bot import PoolmasterBot
from chart_widget import ChartPreferences
from farming import FARMING_POOLS, PoolMetrics, RateMetric, SourceStatus, unavailable_metrics
from farming_history import FarmingHistoryStore
from price import PriceMetrics
from snapshot import DataSnapshot, SnapshotManager
from user_state import UserAlertStore


NOW = datetime(2026, 11, 9, 0, 5, tzinfo=timezone.utc)


def metrics(at, *, count=4):
    rate = RateMetric(Decimal('0'), 'APR', 'apr_rewards')
    return [PoolMetrics(pool, (rate,), (), rate, Decimal('1000'), at,
                        SourceStatus.OK, True) for pool in FARMING_POOLS[:count]]


def price(at):
    return PriceMetrics(Decimal('.01622'), None, None, None, 'JETTONUSDT',
                        'USDT', 'MEXC spot', at, at, SourceStatus.OK)


def test_daily_coverage_requires_each_pool_on_the_requested_utc_day(tmp_path):
    store = FarmingHistoryStore(tmp_path / 'history.sqlite3')
    store.record_confirmed(metrics(NOW - timedelta(days=1)))
    store.record_confirmed(metrics(NOW, count=3))
    assert store.has_confirmed_day(NOW.date()) is False
    store.record_confirmed(metrics(NOW)[3:])
    assert store.has_confirmed_day(NOW.date()) is True  # Real 0% is valid.
    assert store.has_confirmed_day((NOW + timedelta(days=1)).date()) is False


def history_bot(store, fetch, clock):
    async def fetch_price():
        return price(clock[0])
    manager = SnapshotManager(fetch, fetch_price, farming_history_store=store)
    bot = object.__new__(PoolmasterBot)
    bot.snapshot_manager = manager
    bot.farming_history_store = store
    return bot


def install_clock(monkeypatch, clock, delays, cycles):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    async def sleep(delay):
        delays.append(delay)
        if len(delays) >= cycles:
            raise asyncio.CancelledError
        clock[0] += timedelta(seconds=delay)

    monkeypatch.setattr(bot_module, 'datetime', Clock)
    monkeypatch.setattr(snapshot_module, 'datetime', Clock)
    monkeypatch.setattr(bot_module.asyncio, 'sleep', sleep)


def test_daily_loop_retries_timeout_and_partial_day_until_all_pools_are_persisted(tmp_path, monkeypatch):
    store = FarmingHistoryStore(tmp_path / 'history.sqlite3')
    clock, delays, calls = [NOW], [], []

    async def fetch():
        count = [0, 2, 4][len(calls)]
        calls.append(clock[0])
        return metrics(clock[0], count=count) + [
            unavailable_metrics(p, SourceStatus.TIMEOUT, 'source timeout')
            for p in FARMING_POOLS[count:]]

    bot = history_bot(store, fetch, clock)
    install_clock(monkeypatch, clock, delays, 3)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.farming_history_loop(SimpleNamespace()))
    assert len(calls) == 3
    assert delays == [300, 300, 85800]
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute('SELECT date_utc, value_pct FROM observations').fetchall()
    assert rows == [('2026-11-09', '0')] * 4


def test_daily_loop_retries_coverage_read_failure_instead_of_exiting(tmp_path, monkeypatch):
    store = FarmingHistoryStore(tmp_path / 'history.sqlite3')
    clock, delays = [NOW], []
    check = store.has_confirmed_day
    reads = []

    def flaky_read(day):
        reads.append(day)
        if len(reads) == 1:
            raise sqlite3.OperationalError('database temporarily unavailable')
        return check(day)

    monkeypatch.setattr(store, 'has_confirmed_day', flaky_read)

    async def fetch():
        return metrics(clock[0])

    bot = history_bot(store, fetch, clock)
    install_clock(monkeypatch, clock, delays, 2)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.farming_history_loop(SimpleNamespace()))
    assert delays == [300, 86100]
    assert check(NOW.date())


def test_pending_write_recovery_after_midnight_also_collects_the_new_day(tmp_path, monkeypatch):
    class FlakyStore(FarmingHistoryStore):
        writes = 0

        def record_confirmed(self, values):
            self.writes += 1
            if self.writes == 1:
                raise OSError('disk temporarily unavailable')
            return super().record_confirmed(values)

    store = FlakyStore(tmp_path / 'history.sqlite3')
    clock = [NOW.replace(day=8, hour=23, minute=59)]
    delays = []

    async def fetch():
        return metrics(clock[0])

    bot = history_bot(store, fetch, clock)
    install_clock(monkeypatch, clock, delays, 3)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.farming_history_loop(SimpleNamespace()))
    assert delays == [300, 300, 86160]
    with sqlite3.connect(store.path) as connection:
        counts = connection.execute('SELECT date_utc, COUNT(*) FROM observations GROUP BY date_utc').fetchall()
    assert counts == [('2026-11-08', 4), ('2026-11-09', 4)]
    assert bot.snapshot_manager.last_history_write_error is None


def change_setting(store, operation):
    snapshot = DataSnapshot(NOW, {m.pool.address: m for m in metrics(NOW)}, price(NOW), {}, {})
    actions = {
        'language': lambda: store.set_language(123, 'en'),
        'chart': lambda: store.set_chart_preferences(123, ChartPreferences('1M')),
        'farming': lambda: store.set_farming_threshold(123, Decimal('4'), snapshot),
        'price': lambda: store.set_price_threshold(123, Decimal('4'), snapshot),
        'enabled': lambda: store.set_alert_enabled(123, 'price', False),
        'direction': lambda: store.set_alert_direction(123, 'yield', 'up', snapshot),
        'pause': lambda: store.pause_user(123, now=NOW + timedelta(minutes=1)),
        'resume': lambda: store.resume_user(123),
        'baselines': lambda: store.seed_missing_baselines(123, snapshot, now=NOW),
        'blocked': lambda: store.mark_blocked(123, observed_at=NOW),
        'registration': lambda: store.register_private_user(SimpleNamespace(id=123, first_name='Changed'), observed_at=NOW),
        'price_delivery': lambda: store.record_price_delivery(123, Decimal('.02'), 'p1', NOW),
        'yield_delivery': lambda: store.record_yield_delivery(123, FARMING_POOLS[0].address, Decimal('10'), 'APR', 'apr_rewards', 'y1', sent_at=NOW),
        'manual_delivery': lambda: store.record_manual_farming_push(123, 'm1', NOW),
    }
    return actions[operation]()


@pytest.mark.parametrize('operation', [
    'language', 'chart', 'farming', 'price', 'enabled', 'direction', 'pause',
    'resume', 'baselines', 'blocked', 'registration', 'price_delivery',
    'yield_delivery', 'manual_delivery',
])
@pytest.mark.parametrize('fault', ['fsync', 'replace'])
def test_failed_user_write_preserves_memory_and_disk_then_can_retry(tmp_path, monkeypatch, operation, fault):
    path = tmp_path / 'users.json'
    store = UserAlertStore(path)
    store.register_private_user(SimpleNamespace(id=123), observed_at=NOW)
    store.set_language(123, 'ru')
    store.set_farming_threshold(123, Decimal('3'))
    store.set_price_threshold(123, Decimal('3'))
    store.pause_user(123, now=NOW)
    before, disk = store.load_users(), path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError('simulated disk failure')

    with monkeypatch.context() as patch:
        patch.setattr(user_state.os, 'fsync', fail) if fault == 'fsync' else patch.setattr(Path, 'replace', fail)
        with pytest.raises(OSError, match='simulated disk failure'):
            change_setting(store, operation)
    assert store.load_users() == before
    assert path.read_bytes() == disk
    assert not list(tmp_path.glob('.users.json.*'))
    change_setting(store, operation)
    assert store.load_users() != before
    assert UserAlertStore(path).load_users() == store.load_users()


def test_failed_batched_registration_does_not_leave_any_ghost_users(tmp_path, monkeypatch):
    store = UserAlertStore(tmp_path / 'users.json')

    def fail(*args, **kwargs):
        raise OSError('disk full')

    async def run():
        return await asyncio.gather(*(store.reactivate_user_async(SimpleNamespace(id=i)) for i in range(1, 301)), return_exceptions=True)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', fail)
        results = asyncio.run(run())
    assert all(isinstance(result, OSError) for result in results)
    assert store.load_users() == {}
    assert not store.path.exists()
    results = asyncio.run(run())
    assert len(results) == len(UserAlertStore(store.path).list_registered_user_ids()) == 300


def test_failed_expired_pause_cleanup_keeps_committed_state(tmp_path, monkeypatch):
    store = UserAlertStore(tmp_path / 'users.json')
    store.pause_user(123, now=NOW)
    before = store.load_users()

    def fail(*args, **kwargs):
        raise OSError('disk full')

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', fail)
        with pytest.raises(OSError):
            store.get_user(123, now=NOW + timedelta(days=2))
    assert store.load_users() == before
    assert store.get_user(123, now=NOW + timedelta(days=2))['paused_until'] is None


@pytest.mark.parametrize('operation', ['threshold', 'ensure', 'manual_delivery'])
def test_failed_first_setting_write_does_not_create_an_unsaved_user(tmp_path, monkeypatch, operation):
    store = UserAlertStore(tmp_path / 'users.json')

    def fail(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError):
        if operation == 'threshold':
            store.set_price_threshold(123, Decimal('3'))
        elif operation == 'ensure':
            store.ensure_user(123)
        else:
            store.record_manual_farming_push(123, 'm1', NOW)
    assert store.load_users() == {}


def test_expired_pause_result_cannot_mutate_committed_settings(tmp_path):
    store = UserAlertStore(tmp_path / 'users.json')
    store.set_language(123, 'ru')
    store.pause_user(123, now=NOW)
    result = store.get_user(123, now=NOW + timedelta(days=2))
    assert result['paused_until'] is None
    result['language'] = 'en'
    assert store.get_user(123)['language'] == 'ru'
    assert UserAlertStore(store.path).get_user(123)['language'] == 'ru'
