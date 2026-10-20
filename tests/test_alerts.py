import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from alerts import dispatch_user_alerts, evaluate_user_alerts
from farming import FARMING_POOLS, PoolMetrics, RateMetric, SourceStatus
from price import PriceMetrics
from snapshot import DataSnapshot
from user_state import UserAlertStore


NOW = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)


def _snapshot(
    *, apr="15.5", price="0.0210", ston_kind="APY", stale=False, anomalies=None, fetched_at=NOW
):
    farming = {}
    for index, pool in enumerate(FARMING_POOLS[:2]):
        kind = "APR" if index == 0 else ston_kind
        value = Decimal(apr) if index == 0 else Decimal("20")
        farming[pool.address] = PoolMetrics(
            pool=pool,
            farm_rates=(RateMetric(value, kind, "apr_rewards" if index == 0 else "apy"),),
            pool_rates=(),
            total_rate=RateMetric(value, kind, "total"),
            tvl_usd=None,
            fetched_at=fetched_at,
            status=SourceStatus.OK,
            identity_valid=True,
            is_stale=stale,
        )
    price_metrics = PriceMetrics(
        price_usdt=Decimal(price),
        bid_usdt=Decimal(price),
        ask_usdt=Decimal(price),
        quote_volume_24h=Decimal("10000"),
        symbol="JETTONUSDT",
        quote_currency="USDT",
        source="MEXC spot",
        fetched_at=fetched_at,
        source_timestamp=fetched_at,
        status=SourceStatus.OK,
        is_stale=stale,
    )
    return DataSnapshot(fetched_at, farming, price_metrics, {"farming": "ok", "price": "ok"}, anomalies or {})


def _yield_record(value, kind="APR", source_field="apr_rewards"):
    return {"value": str(value), "kind": kind, "source_field": source_field}


def _enabled_store(tmp_path, *, apr="12.5", price="0.0200", apr_threshold="3", price_threshold="5"):
    store = UserAlertStore(tmp_path / "users.json")
    store.register_private_user(
        SimpleNamespace(id=123, username="tester", first_name="Test", last_name=None),
        observed_at=NOW,
    )
    baseline = _snapshot(apr=apr, price=price)
    store.set_farming_threshold(123, Decimal(apr_threshold), baseline)
    store.set_price_threshold(123, Decimal(price_threshold), baseline)
    return store


def test_farming_yield_alert_threshold_is_measured_in_percentage_points_per_pool():
    old = _snapshot(apr="12.5")
    current = _snapshot(apr="15.5")
    state = {
        "farming_enabled": True,
        "farming_change_threshold_pp": "3",
        "last_notified_yield": {FARMING_POOLS[0].address: _yield_record("12.5")},
        "delivery_fingerprints": [],
        "paused_until": None,
        "price_enabled": False,
    }

    candidates = evaluate_user_alerts(123, state, current, now=NOW, price_cooldown_seconds=900)

    assert len(candidates) == 1
    assert candidates[0].kind == "yield"
    assert candidates[0].pool_id == FARMING_POOLS[0].address
    assert candidates[0].delta == Decimal("3.0")
    assert "изменение +3.00 пункта" in candidates[0].text


def test_dedust_alert_uses_reward_apr_instead_of_total_apr():
    snapshot = _snapshot(apr="15.5")
    pool_id = FARMING_POOLS[0].address
    metrics = snapshot.farming[pool_id]
    snapshot = replace(
        snapshot,
        farming={pool_id: replace(metrics, total_rate=RateMetric(Decimal("99"), "APR", "apr"))},
    )
    state = {
        "farming_enabled": True,
        "farming_change_threshold_pp": "3",
        "last_notified_yield": {
            pool_id: {"value": "12.5", "kind": "APR", "source_field": "apr_rewards"}
        },
        "delivery_fingerprints": [],
        "paused_until": None,
        "price_enabled": False,
    }

    candidates = evaluate_user_alerts(123, state, snapshot, now=NOW, price_cooldown_seconds=900)

    assert len(candidates) == 1
    assert candidates[0].kind == "yield"
    assert candidates[0].value == Decimal("15.5")
    assert candidates[0].delta == Decimal("3.0")
    assert "Доходность: 12.50% → 15.50% (изменение +3.00 пункта)" in candidates[0].text
    assert "DeDust JETTON/GRAM" in candidates[0].text
    assert "APR" not in candidates[0].text


def test_dedust_alert_pair_name_is_english_for_english_users():
    snapshot = _snapshot(apr="15.5")
    pool_id = FARMING_POOLS[0].address
    state = {
        "language": "en",
        "farming_enabled": True,
        "farming_change_threshold_pp": "3",
        "last_notified_yield": {pool_id: _yield_record("12.5")},
        "delivery_fingerprints": [],
        "paused_until": None,
        "price_enabled": False,
    }

    candidates = evaluate_user_alerts(123, state, snapshot, now=NOW, price_cooldown_seconds=900)

    assert len(candidates) == 1
    assert "DeDust JETTON/GRAM" in candidates[0].text
    assert "JETTON/TON" not in candidates[0].text


def test_primary_yield_alert_can_use_partial_source_without_auxiliary_fields():
    snapshot = _snapshot(apr="15.5")
    pool_id = FARMING_POOLS[0].address
    primary_only = replace(
        snapshot.farming[pool_id],
        status=SourceStatus.PARTIAL,
        pool_rates=(),
        total_rate=None,
        tvl_usd=None,
    )
    snapshot = replace(snapshot, farming={pool_id: primary_only})
    state = {
        "farming_enabled": True,
        "farming_change_threshold_pp": "3",
        "last_notified_yield": {pool_id: _yield_record("12.5")},
        "delivery_fingerprints": [],
        "paused_until": None,
        "price_enabled": False,
    }

    candidates = evaluate_user_alerts(123, state, snapshot, now=NOW, price_cooldown_seconds=900)

    assert len(candidates) == 1
    assert candidates[0].value == Decimal("15.5")


def test_ston_apy_alert_uses_same_primary_yield_without_converting_to_apr():
    pool = FARMING_POOLS[2]
    rate = RateMetric(Decimal("62.5"), "APY", "apy")
    ston_metrics = PoolMetrics(
        pool=pool,
        farm_rates=(rate,),
        pool_rates=(RateMetric(Decimal("1.3"), "APY", "apy_7d", "7d"),),
        total_rate=None,
        tvl_usd=Decimal("50000"),
        fetched_at=NOW,
        status=SourceStatus.OK,
        identity_valid=True,
    )
    base = _snapshot()
    snapshot = replace(base, farming={pool.address: ston_metrics})
    state = {
        "farming_enabled": True,
        "farming_change_threshold_pp": "1",
        "last_notified_yield": {
            pool.address: {"value": "60", "kind": "APY", "source_field": "apy"}
        },
        "delivery_fingerprints": [],
        "paused_until": None,
        "price_enabled": False,
    }

    candidates = evaluate_user_alerts(123, state, snapshot, now=NOW, price_cooldown_seconds=900)

    assert len(candidates) == 1
    assert candidates[0].kind == "yield"
    assert candidates[0].pool_id == pool.address
    assert candidates[0].delta == Decimal("2.5")
    assert "Доходность: 60.00% → 62.50% (изменение +2.50 пункта)" in candidates[0].text
    assert "APY" not in candidates[0].text


def test_mismatched_yield_metric_kind_is_not_compared_to_existing_baseline():
    state = {
        "farming_enabled": True,
        "farming_change_threshold_pp": "1",
        "last_notified_yield": {
            FARMING_POOLS[0].address: _yield_record("12.5"),
            FARMING_POOLS[1].address: _yield_record("10"),
        },
        "delivery_fingerprints": [],
        "paused_until": None,
        "price_enabled": False,
    }

    candidates = evaluate_user_alerts(
        123,
        state,
        _snapshot(apr="14", ston_kind="APY"),
        now=NOW,
        price_cooldown_seconds=900,
    )

    assert [item.pool_id for item in candidates] == [FARMING_POOLS[0].address]


def test_stale_anomalous_and_identity_invalid_values_do_not_alert():
    state = {
        "farming_enabled": True,
        "farming_change_threshold_pp": "1",
        "last_notified_yield": {
            pool.address: _yield_record("12.5") for pool in FARMING_POOLS[:2]
        },
        "price_enabled": True,
        "price_threshold_pct": "1",
        "price_baseline": "0.0200",
        "delivery_fingerprints": [],
        "paused_until": None,
        "price_last_alert_at": None,
    }
    stale = _snapshot(apr="50", price="0.05", stale=True)
    invalid_metrics = dict(stale.farming)
    pool = FARMING_POOLS[0]
    invalid_metrics[pool.address] = replace(invalid_metrics[pool.address], identity_valid=False, is_stale=False)
    invalid_price = replace(stale.price, status=SourceStatus.INVALID_RESPONSE, is_stale=False)
    invalid = replace(stale, farming=invalid_metrics, price=invalid_price)

    assert evaluate_user_alerts(123, state, stale, now=NOW, price_cooldown_seconds=900) == []
    assert evaluate_user_alerts(123, state, invalid, now=NOW, price_cooldown_seconds=900) == []
    anomalous = replace(_snapshot(apr="50", price="0.05"), anomalies={
        f"farming:{pool.address}": "unconfirmed",
        "price": "unconfirmed",
    })
    assert evaluate_user_alerts(123, state, anomalous, now=NOW, price_cooldown_seconds=900) == []


def test_price_alert_uses_relative_percent_and_sets_candidate_baseline():
    state = {
        "farming_enabled": False,
        "price_enabled": True,
        "price_threshold_pct": "5",
        "price_baseline": "0.0200",
        "price_last_alert_at": None,
        "delivery_fingerprints": [],
        "paused_until": None,
    }

    candidates = evaluate_user_alerts(
        123,
        state,
        _snapshot(price="0.0210"),
        now=NOW,
        price_cooldown_seconds=900,
    )

    assert len(candidates) == 1
    assert candidates[0].kind == "price"
    assert candidates[0].delta == Decimal("5.000")
    assert "+5.00%" in candidates[0].text


def test_price_cooldown_suppresses_repeat_alerts():
    state = {
        "farming_enabled": False,
        "price_enabled": True,
        "price_threshold_pct": "1",
        "price_baseline": "0.0200",
        "price_last_alert_at": (NOW - timedelta(minutes=1)).isoformat(),
        "delivery_fingerprints": [],
        "paused_until": None,
    }

    assert evaluate_user_alerts(
        123,
        state,
        _snapshot(price="0.0210"),
        now=NOW,
        price_cooldown_seconds=900,
    ) == []


def test_global_pause_and_user_pause_suppress_all_alerts():
    store = {
        "farming_enabled": True,
        "farming_change_threshold_pp": "1",
        "last_notified_yield": {FARMING_POOLS[0].address: _yield_record("12.5")},
        "price_enabled": True,
        "price_threshold_pct": "1",
        "price_baseline": "0.0200",
        "price_last_alert_at": None,
        "delivery_fingerprints": [],
        "paused_until": (NOW + timedelta(hours=1)).isoformat(),
    }
    current = _snapshot(apr="14", price="0.0210")

    assert evaluate_user_alerts(123, store, current, now=NOW, price_cooldown_seconds=900) == []
    store["paused_until"] = None
    assert evaluate_user_alerts(
        123, store, current, now=NOW, price_cooldown_seconds=900, global_paused=True
    ) == []


def test_successful_apr_delivery_updates_only_that_users_baseline_and_suppresses_duplicates(tmp_path):
    store = _enabled_store(tmp_path, apr_threshold="1", price_threshold="10")
    snapshot = _snapshot(apr="14")

    async def run():
        bot = _RecordingBot()
        first = await dispatch_user_alerts(
            store, bot, snapshot, now=NOW, price_cooldown_seconds=900
        )
        second = await dispatch_user_alerts(
            store, bot, snapshot, now=NOW, price_cooldown_seconds=900
        )
        return bot, first, second

    bot, first, second = asyncio.run(run())

    assert first.sent == 1
    assert second.sent == 0
    assert len(bot.messages) == 1
    assert store.get_user(123)["last_notified_yield"][FARMING_POOLS[0].address] == _yield_record("14")
    assert bot.messages[0]["reply_markup"] is not None


def test_failed_delivery_does_not_advance_apr_baseline_or_fingerprint(tmp_path):
    store = _enabled_store(tmp_path, apr_threshold="1", price_threshold="10")
    snapshot = _snapshot(apr="14")

    async def run():
        bot = _RecordingBot(fail=True)
        result = await dispatch_user_alerts(
            store, bot, snapshot, now=NOW, price_cooldown_seconds=900
        )
        return bot, result

    bot, result = asyncio.run(run())

    user = store.get_user(123)
    assert result.failed == 1
    assert user["last_notified_yield"][FARMING_POOLS[0].address] == _yield_record("12.5")
    assert user["delivery_fingerprints"] == []


def test_forbidden_alert_delivery_blocks_recipient_until_new_private_interaction(tmp_path):
    from telegram.error import Forbidden

    store = _enabled_store(tmp_path, apr_threshold="1", price_threshold="10")
    snapshot = _snapshot(apr="14")

    class BlockedBot:
        def __init__(self):
            self.attempts = 0

        async def send_message(self, **_kwargs):
            self.attempts += 1
            raise Forbidden("bot was blocked")

    bot = BlockedBot()

    async def run():
        first = await dispatch_user_alerts(
            store, bot, snapshot, now=NOW, price_cooldown_seconds=900
        )
        second = await dispatch_user_alerts(
            store, bot, snapshot, now=NOW + timedelta(minutes=1), price_cooldown_seconds=900
        )
        return first, second

    first, second = asyncio.run(run())

    assert first.failed == 1
    assert second.failed == 0
    assert bot.attempts == 1
    assert store.list_active_user_ids() == []
    store.reactivate_user(
        SimpleNamespace(id=123, username="tester", first_name="Test", last_name=None),
        observed_at=NOW + timedelta(minutes=2),
    )
    assert store.list_active_user_ids() == [123]


def test_retry_after_retries_and_commits_baseline_only_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    store = _enabled_store(tmp_path, apr_threshold="1", price_threshold="10")
    snapshot = _snapshot(apr="14")

    async def run():
        bot = _RecordingBot(retry_once=True)
        result = await dispatch_user_alerts(
            store, bot, snapshot, now=NOW, price_cooldown_seconds=900
        )
        return bot, result

    bot, result = asyncio.run(run())

    assert result.sent == 1
    assert bot.attempts == 2
    assert store.get_user(123)["last_notified_yield"][FARMING_POOLS[0].address] == _yield_record("14")


def test_successful_price_delivery_resets_baseline_and_starts_cooldown(tmp_path):
    store = _enabled_store(tmp_path, apr_threshold="5", price_threshold="5")
    snapshot = _snapshot(price="0.0210")

    async def run():
        bot = _RecordingBot()
        result = await dispatch_user_alerts(
            store, bot, snapshot, now=NOW, price_cooldown_seconds=900
        )
        return bot, result

    bot, result = asyncio.run(run())
    user = store.get_user(123)

    assert result.sent == 1
    assert len(bot.messages) == 1
    assert "JETTON/USDT" in bot.messages[0]["text"]
    assert user["price_baseline"] == "0.0210"
    assert user["price_last_alert_at"] == NOW.isoformat()


@pytest.mark.parametrize("kind", ("yield", "price"))
def test_alert_is_sent_again_when_market_returns_to_a_previous_value(tmp_path, kind):
    baseline = _snapshot(apr="12.5", price="0.0200")
    store = UserAlertStore(tmp_path / f"{kind}.json")
    store.register_private_user(
        SimpleNamespace(id=123, username="tester", first_name="Test", last_name=None),
        observed_at=NOW,
    )
    if kind == "yield":
        store.set_farming_threshold(123, Decimal("1"), baseline)
        values = ("14", "13", "14")
    else:
        store.set_price_threshold(123, Decimal("1"), baseline)
        values = ("0.0220", "0.0200", "0.0220")

    async def run():
        bot = _RecordingBot()
        summaries = []
        for index, value in enumerate(values, start=1):
            timestamp = NOW + timedelta(minutes=20 * index)
            snapshot = _snapshot(
                apr=value if kind == "yield" else "12.5",
                price=value if kind == "price" else "0.0200",
                fetched_at=timestamp,
            )
            summaries.append(
                await dispatch_user_alerts(
                    store,
                    bot,
                    snapshot,
                    now=timestamp,
                    price_cooldown_seconds=900,
                )
            )
        return bot, summaries

    bot, summaries = asyncio.run(run())

    assert [summary.sent for summary in summaries] == [1, 1, 1]
    assert len(bot.messages) == 3


class _RecordingBot:
    def __init__(self, *, fail=False, retry_once=False):
        self.fail = fail
        self.retry_once = retry_once
        self.attempts = 0
        self.messages = []

    async def send_message(self, **kwargs):
        from telegram.error import RetryAfter

        self.attempts += 1
        if self.retry_once and self.attempts == 1:
            raise RetryAfter(0)
        if self.fail:
            raise RuntimeError("synthetic delivery failure")
        self.messages.append(kwargs)
