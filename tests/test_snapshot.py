import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import snapshot as snapshot_module
from farming import FARMING_POOLS, PoolMetrics, RateMetric, SourceStatus, unavailable_metrics
from farming_cache import FarmingStaleCache
from farming_history import FarmingHistoryStore
from price import PriceMetrics
from snapshot import SnapshotManager


NOW = datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)


def _farming(value="12.5", *, fetched_at=NOW, pool=FARMING_POOLS[0]):
    rate = RateMetric(Decimal(value), "APR", "apr_rewards")
    return PoolMetrics(
        pool=pool,
        farm_rates=(rate,),
        pool_rates=(RateMetric(Decimal("1.5"), "APR", "apr_fees"),),
        total_rate=RateMetric(Decimal(value) + Decimal("1.5"), "APR", "apr"),
        tvl_usd=Decimal("100000"),
        fetched_at=fetched_at,
        status=SourceStatus.OK,
        identity_valid=True,
    )


def _price(value="0.01865", *, fetched_at=NOW, status=SourceStatus.OK, source_timestamp=None):
    return PriceMetrics(
        price_usdt=Decimal(value) if value is not None else None,
        bid_usdt=Decimal("0.01861") if value is not None else None,
        ask_usdt=Decimal("0.01866") if value is not None else None,
        quote_volume_24h=Decimal("58986.419") if value is not None else None,
        symbol="JETTONUSDT",
        quote_currency="USDT",
        source="MEXC spot",
        fetched_at=fetched_at,
        source_timestamp=source_timestamp or fetched_at,
        status=status,
    )


def test_snapshot_keeps_current_and_previous_accepted_values():
    values = iter(("12.5", "14.0"))
    prices = iter(("0.01865", "0.01900"))

    async def fetch_farming():
        return [_farming(next(values))]

    async def fetch_price():
        return _price(next(prices))

    async def run():
        manager = SnapshotManager(fetch_farming, fetch_price)
        first = await manager.refresh(now=NOW)
        second = await manager.refresh(now=NOW + timedelta(minutes=1))
        return manager, first, second

    manager, first, second = asyncio.run(run())

    pool_id = FARMING_POOLS[0].address
    assert first.farming[pool_id].farm_rates[0].value_percent == Decimal("12.5")
    assert second.farming[pool_id].farm_rates[0].value_percent == Decimal("14.0")
    assert second.price.price_usdt == Decimal("0.01900")
    assert manager.current_snapshot is second
    assert manager.previous_snapshot is first


def test_price_snapshot_roundtrip_preserves_signed_24h_quote_metrics():
    original = replace(
        _price(),
        price_change_24h_percent=Decimal("-0.35"),
        high_price_24h=Decimal("0.01920"),
        low_price_24h=Decimal("0.01810"),
    )

    restored = SnapshotManager._decode_price(SnapshotManager._encode_price(original))

    assert restored.price_change_24h_percent == Decimal("-0.35")
    assert restored.high_price_24h == Decimal("0.01920")
    assert restored.low_price_24h == Decimal("0.01810")


def test_snapshot_records_confirmed_metrics_before_stale_cache(tmp_path):
    events = []
    first_at = datetime(2026, 10, 17, 23, 50, tzinfo=timezone.utc)
    second_at = datetime(2026, 10, 18, 0, 10, tzinfo=timezone.utc)
    responses = iter(
        (
            [_farming("12", fetched_at=first_at, pool=FARMING_POOLS[0]), _farming("13", fetched_at=first_at, pool=FARMING_POOLS[1])],
            [
                _farming("14", fetched_at=second_at, pool=FARMING_POOLS[0]),
                unavailable_metrics(FARMING_POOLS[1], SourceStatus.TIMEOUT, "offline", second_at),
            ],
        )
    )

    class OrderedHistoryStore(FarmingHistoryStore):
        def record_confirmed(self, metrics):
            events.append("history")
            return super().record_confirmed(metrics)

    class OrderedCache(FarmingStaleCache):
        def apply(self, current, *, now=None):
            events.append("cache")
            return super().apply(current, now=now)

    async def fetch_farming():
        return next(responses)

    async def fetch_price():
        return _price(fetched_at=first_at)

    store = OrderedHistoryStore(tmp_path / "history.sqlite3")

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            farming_cache=OrderedCache(1800),
            farming_history_store=store,
        )
        await manager.refresh(now=first_at)
        snapshot = await manager.refresh(now=second_at)
        return snapshot

    snapshot = asyncio.run(run())

    assert events == ["history", "cache", "history", "cache"]
    assert snapshot.farming[FARMING_POOLS[1].address].status == SourceStatus.TIMEOUT
    assert snapshot.farming[FARMING_POOLS[1].address].is_stale is True
    with sqlite3.connect(store.path) as connection:
        new_day = connection.execute(
            "SELECT series_key FROM observations WHERE date_utc = '2026-10-18'"
        ).fetchall()
    assert new_day == [("dedust_jetton_ton",)]


def test_ston_stale_farm_fallback_is_not_recorded_as_confirmed_history(tmp_path):
    first_at = datetime(2026, 10, 17, 23, 50, tzinfo=timezone.utc)
    second_at = datetime(2026, 10, 18, 0, 10, tzinfo=timezone.utc)
    pool = FARMING_POOLS[2]
    transport_failure = replace(
        _farming("12.5", fetched_at=second_at, pool=pool),
        farm_rates=(),
        total_rate=None,
        status=SourceStatus.PARTIAL,
        status_detail="farm endpoint unavailable",
        stale_farm_yield_fallback_allowed=True,
    )
    farming_batches = iter(([_farming("12.5", fetched_at=first_at, pool=pool)], [transport_failure]))

    async def fetch_farming():
        return next(farming_batches)

    async def fetch_price():
        return _price(fetched_at=first_at)

    store = FarmingHistoryStore(tmp_path / "history.sqlite3")

    async def run():
        manager = SnapshotManager(fetch_farming, fetch_price, farming_history_store=store)
        await manager.refresh(now=first_at)
        return await manager.refresh(now=second_at)

    snapshot = asyncio.run(run())

    metrics = snapshot.farming[pool.address]
    assert metrics.is_stale is True
    assert metrics.farm_rates[0].value_percent == Decimal("12.5")
    assert _read_history_rows(store.path) == [("2026-10-17", "12.5")]


def test_snapshot_surfaces_and_retries_history_write_failures(tmp_path):
    class FlakyHistoryStore(FarmingHistoryStore):
        fail_writes = True

        def record_confirmed(self, metrics):
            if self.fail_writes:
                raise OSError("history disk unavailable")
            return super().record_confirmed(metrics)

    store = FlakyHistoryStore(tmp_path / "history.sqlite3")
    responses = iter(
        (
            [_farming("12.5")],
            [unavailable_metrics(FARMING_POOLS[0], SourceStatus.TIMEOUT, "offline", NOW + timedelta(minutes=1))],
        )
    )

    async def fetch_farming():
        return next(responses)

    async def fetch_price():
        return _price()

    async def run():
        manager = SnapshotManager(fetch_farming, fetch_price, farming_history_store=store)
        snapshot = await manager.refresh(now=NOW)
        await manager.refresh_farming(now=NOW + timedelta(minutes=1))
        return manager, snapshot

    manager, snapshot = asyncio.run(run())

    assert snapshot.farming[FARMING_POOLS[0].address].farm_rates[0].value_percent == Decimal("12.5")
    assert isinstance(manager.last_history_write_error, OSError)
    assert manager.retry_pending_farming_history_write() is False

    store.fail_writes = False
    assert manager.retry_pending_farming_history_write() is True
    assert manager.last_history_write_error is None
    assert manager._pending_farming_history_metrics is None
    assert len(_read_history_rows(store.path)) == 1


def _read_history_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT date_utc, value_pct FROM observations").fetchall()


def test_snapshot_does_not_record_unconfirmed_anomaly_or_stale_fallback(tmp_path):
    first_at = datetime(2026, 10, 17, 23, 50, tzinfo=timezone.utc)
    second_at = datetime(2026, 10, 18, 0, 10, tzinfo=timezone.utc)
    responses = iter(
        (
            [_farming("10", fetched_at=first_at)],
            [_farming("100", fetched_at=second_at)],
            [_farming("10", fetched_at=second_at + timedelta(seconds=1))],
        )
    )

    async def fetch_farming():
        return next(responses)

    async def fetch_price():
        return _price(fetched_at=first_at)

    store = FarmingHistoryStore(tmp_path / "history.sqlite3")

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            farming_history_store=store,
            apr_anomaly_confirm_threshold=Decimal("50"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=first_at)
        return await manager.refresh(now=second_at)

    snapshot = asyncio.run(run())

    metrics = snapshot.farming[FARMING_POOLS[0].address]
    assert snapshot.anomalies[f"farming:{FARMING_POOLS[0].address}"] == "unconfirmed"
    assert metrics.is_stale is True
    assert metrics.status == SourceStatus.PARTIAL
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT date_utc, value_pct FROM observations").fetchall()
    assert rows == [("2026-10-17", "10")]


def test_corrupt_snapshot_fails_closed_if_backup_cannot_be_created(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    original = '{"current": [], "previous": null}'
    path.write_text(original, encoding="utf-8")

    def fail_copy(*_args, **_kwargs):
        raise OSError("backup disk unavailable")

    monkeypatch.setattr(snapshot_module, "secure_backup_file", fail_copy)

    async def fetch_farming():
        return []

    async def fetch_price():
        return _price()

    with pytest.raises(OSError, match="backup disk unavailable"):
        SnapshotManager(fetch_farming, fetch_price, snapshot_path=path)

    assert path.read_text(encoding="utf-8") == original


def test_price_failure_does_not_discard_farming_and_uses_bounded_stale_price():
    price_responses = iter((_price(), _price(None, status=SourceStatus.TIMEOUT)))

    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return next(price_responses)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            price_stale_max_age_seconds=900,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(minutes=5))

    snapshot = asyncio.run(run())

    assert snapshot.farming[FARMING_POOLS[0].address].status == SourceStatus.OK
    assert snapshot.price.price_usdt == Decimal("0.01865")
    assert snapshot.price.status == SourceStatus.TIMEOUT
    assert snapshot.price.is_stale is True
    assert snapshot.source_statuses["price"] == SourceStatus.TIMEOUT.value


def test_price_cache_expires_instead_of_being_shown_as_current():
    responses = iter((_price(), _price(None, status=SourceStatus.UNAVAILABLE)))

    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return next(responses)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            price_stale_max_age_seconds=60,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(seconds=61))

    snapshot = asyncio.run(run())

    assert snapshot.price.price_usdt is None
    assert snapshot.price.is_stale is False
    assert snapshot.price.status == SourceStatus.UNAVAILABLE


def test_concurrent_refresh_calls_share_one_fetch():
    calls = {"farming": 0, "price": 0}
    release = asyncio.Event()

    async def fetch_farming():
        calls["farming"] += 1
        await release.wait()
        return [_farming()]

    async def fetch_price():
        calls["price"] += 1
        await release.wait()
        return _price()

    async def run():
        manager = SnapshotManager(fetch_farming, fetch_price)
        first = asyncio.create_task(manager.refresh(now=NOW))
        second = asyncio.create_task(manager.refresh(now=NOW))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(run())

    assert calls == {"farming": 1, "price": 1}
    assert first is second


def test_large_price_jump_is_rejected_when_confirmation_does_not_match():
    prices = iter((_price("0.01865"), _price("0.04"), _price("0.01870")))

    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return next(prices)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            price_anomaly_confirm_threshold=Decimal("20"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(minutes=1))

    snapshot = asyncio.run(run())

    assert snapshot.price.price_usdt == Decimal("0.01865")
    assert snapshot.price.is_stale is True
    assert snapshot.anomalies["price"] == "unconfirmed"


def test_unconfirmed_price_anomaly_is_hidden_when_confirmed_value_is_too_old(caplog):
    prices = iter(
        (
            _price("0.01865", fetched_at=NOW),
            _price("0.04", fetched_at=NOW + timedelta(seconds=61)),
            _price("0.01870", fetched_at=NOW + timedelta(seconds=63)),
        )
    )

    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return next(prices)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            price_stale_max_age_seconds=60,
            price_anomaly_confirm_threshold=Decimal("20"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(seconds=61))

    snapshot = asyncio.run(run())

    assert snapshot.price.price_usdt is None
    assert snapshot.price.status == SourceStatus.PARTIAL
    assert snapshot.price.is_stale is True
    assert snapshot.anomalies["price"] == "unconfirmed"
    assert "Unconfirmed price anomaly" in caplog.text


def test_large_price_jump_is_accepted_when_second_sample_confirms_it():
    prices = iter((_price("0.01865"), _price("0.04"), _price("0.0401")))

    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return next(prices)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            price_anomaly_confirm_threshold=Decimal("20"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(minutes=1))

    snapshot = asyncio.run(run())

    assert snapshot.price.price_usdt == Decimal("0.0401")
    assert snapshot.price.status == SourceStatus.OK
    assert "price" not in snapshot.anomalies


def test_apr_anomaly_also_requires_a_confirming_sample():
    farming_batches = iter(([_farming("10")], [_farming("40")], [_farming("40.01")]))
    prices = iter((_price(), _price(), _price()))

    async def fetch_farming():
        return next(farming_batches)

    async def fetch_price():
        return next(prices)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            apr_anomaly_confirm_threshold=Decimal("100"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(minutes=1))

    snapshot = asyncio.run(run())

    assert snapshot.farming[FARMING_POOLS[0].address].farm_rates[0].value_percent == Decimal("40.01")
    assert "farming" not in snapshot.anomalies


def test_large_auxiliary_rate_change_does_not_confirm_stable_primary_yield():
    def metrics(primary, fees, total):
        result = replace(
            _farming(primary),
            pool_rates=(RateMetric(Decimal(fees), "APR", "apr_fees"),),
            total_rate=RateMetric(Decimal(total), "APR", "apr"),
        )
        if primary != "10":
            result = replace(result, status=SourceStatus.PARTIAL, tvl_usd=None)
        return result

    batches = iter(
        (
            [metrics("10", "1", "11")],
            [metrics("10.5", "50", "60.5")],
            [metrics("10.5", "50", "60.5")],
        )
    )
    calls = {"farming": 0}

    async def fetch_farming():
        calls["farming"] += 1
        return next(batches)

    async def fetch_price():
        return _price()

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            apr_anomaly_confirm_threshold=Decimal("50"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(minutes=1))

    snapshot = asyncio.run(run())

    assert calls["farming"] == 2
    assert (
        snapshot.farming[FARMING_POOLS[0].address].primary_farm_yield.value_percent
        == Decimal("10.5")
    )
    assert snapshot.farming[FARMING_POOLS[0].address].status == SourceStatus.PARTIAL
    assert f"farming:{FARMING_POOLS[0].address}" not in snapshot.anomalies


@pytest.mark.parametrize(
    ("confirmation_value", "expected_value", "expected_anomaly"),
    (("0.1", "0", True), ("100.5", "100.5", False)),
)
def test_nonzero_apr_from_zero_baseline_requires_a_confirming_sample(
    confirmation_value, expected_value, expected_anomaly
):
    def metrics(value):
        rate = RateMetric(Decimal(value), "APR", "apr")
        return PoolMetrics(
            pool=FARMING_POOLS[0],
            farm_rates=(rate,),
            pool_rates=(),
            total_rate=rate,
            tvl_usd=None,
            fetched_at=NOW,
            status=SourceStatus.OK,
            identity_valid=True,
        )

    farming_batches = iter(([metrics("0")], [metrics("100")], [metrics(confirmation_value)]))
    prices = iter((_price(), _price(), _price()))

    async def fetch_farming():
        return next(farming_batches)

    async def fetch_price():
        return next(prices)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            apr_anomaly_confirm_threshold=Decimal("50"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(minutes=1))

    snapshot = asyncio.run(run())
    metrics_result = snapshot.farming[FARMING_POOLS[0].address]

    assert metrics_result.total_rate.value_percent == Decimal(expected_value)
    assert (f"farming:{FARMING_POOLS[0].address}" in snapshot.anomalies) is expected_anomaly
    assert metrics_result.is_stale is expected_anomaly


def test_large_apr_change_is_held_when_confirmation_disagrees():
    farming_batches = iter(([_farming("10")], [_farming("40")], [_farming("10.1")]))
    prices = iter((_price(), _price(), _price()))

    async def fetch_farming():
        return next(farming_batches)

    async def fetch_price():
        return next(prices)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            apr_anomaly_confirm_threshold=Decimal("100"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(minutes=1))

    snapshot = asyncio.run(run())
    metrics = snapshot.farming[FARMING_POOLS[0].address]

    assert metrics.farm_rates[0].value_percent == Decimal("10")
    assert metrics.is_stale is True
    assert snapshot.anomalies[f"farming:{FARMING_POOLS[0].address}"] == "unconfirmed"


@pytest.mark.parametrize(
    ("confirmation_value", "expected_value", "expected_anomaly"),
    (("10.1", "10", True), ("1000.5", "1000.5", False)),
)
def test_rate_bearing_partial_candidate_requires_confirmation(
    confirmation_value, expected_value, expected_anomaly
):
    partial_jump = replace(
        _farming("1000", fetched_at=NOW + timedelta(minutes=1)),
        status=SourceStatus.PARTIAL,
        status_detail="TVL is unavailable",
        tvl_usd=None,
    )
    farming_batches = iter(
        (
            [_farming("10")],
            [partial_jump],
            [_farming(confirmation_value, fetched_at=NOW + timedelta(minutes=1, seconds=2))],
        )
    )
    prices = iter((_price(), _price()))

    async def fetch_farming():
        return next(farming_batches)

    async def fetch_price():
        return next(prices)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            apr_anomaly_confirm_threshold=Decimal("50"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(minutes=1))

    snapshot = asyncio.run(run())
    metrics = snapshot.farming[FARMING_POOLS[0].address]

    assert metrics.total_rate.value_percent == Decimal(expected_value) + Decimal("1.5")
    assert (f"farming:{FARMING_POOLS[0].address}" in snapshot.anomalies) is expected_anomaly
    assert metrics.is_stale is expected_anomaly


def test_unconfirmed_apr_anomaly_is_hidden_when_confirmed_value_is_too_old(caplog):
    farming_batches = iter(
        (
            [_farming("10", fetched_at=NOW)],
            [_farming("40", fetched_at=NOW + timedelta(seconds=61))],
            [_farming("10.1", fetched_at=NOW + timedelta(seconds=63))],
        )
    )
    prices = iter((_price(), _price()))

    async def fetch_farming():
        return next(farming_batches)

    async def fetch_price():
        return next(prices)

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            farming_stale_max_age_seconds=60,
            apr_anomaly_confirm_threshold=Decimal("100"),
            anomaly_confirm_delay_seconds=0,
        )
        await manager.refresh(now=NOW)
        return await manager.refresh(now=NOW + timedelta(seconds=61))

    snapshot = asyncio.run(run())
    metrics = snapshot.farming[FARMING_POOLS[0].address]

    assert metrics.farm_rates == ()
    assert metrics.pool_rates == ()
    assert metrics.total_rate is None
    assert metrics.status == SourceStatus.PARTIAL
    assert metrics.is_stale is True
    assert snapshot.anomalies[f"farming:{FARMING_POOLS[0].address}"] == "unconfirmed"
    assert f"Unconfirmed farming anomaly pool={FARMING_POOLS[0].address}" in caplog.text


def test_persisted_snapshot_restores_current_and_previous_values(tmp_path):
    values = iter(("12.5", "14.0"))
    prices = iter(("0.01865", "0.01900"))

    async def fetch_farming():
        return [_farming(next(values))]

    async def fetch_price():
        return _price(next(prices))

    async def run():
        manager = SnapshotManager(fetch_farming, fetch_price, snapshot_path=tmp_path / "snapshot.json")
        await manager.refresh(now=NOW)
        await manager.refresh(now=NOW + timedelta(minutes=1))
        restored = SnapshotManager(fetch_farming, fetch_price, snapshot_path=tmp_path / "snapshot.json")
        return restored

    restored = asyncio.run(run())

    pool_id = FARMING_POOLS[0].address
    assert restored.current_snapshot.farming[pool_id].farm_rates[0].value_percent == Decimal("14.0")
    assert restored.previous_snapshot.farming[pool_id].farm_rates[0].value_percent == Decimal("12.5")
    assert restored.current_snapshot.price.price_usdt == Decimal("0.01900")


def test_persisted_non_stale_snapshot_is_refreshed_after_source_freshness_expires(tmp_path):
    path = tmp_path / "snapshot.json"

    async def save_initial_snapshot():
        async def fetch_farming():
            return [_farming("12.5", fetched_at=NOW)]

        async def fetch_price():
            return _price("0.01865", fetched_at=NOW)

        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            snapshot_path=path,
            farming_freshness_seconds=60,
            price_freshness_seconds=60,
        )
        await manager.refresh(now=NOW)

    asyncio.run(save_initial_snapshot())

    calls = {"farming": 0, "price": 0}

    async def fetch_farming():
        calls["farming"] += 1
        return [_farming("13.5", fetched_at=NOW + timedelta(minutes=10))]

    async def fetch_price():
        calls["price"] += 1
        return _price("0.01965", fetched_at=NOW + timedelta(minutes=10))

    restored = SnapshotManager(
        fetch_farming,
        fetch_price,
        snapshot_path=path,
        farming_freshness_seconds=60,
        price_freshness_seconds=60,
    )
    pool_id = FARMING_POOLS[0].address

    assert restored.current_snapshot.farming[pool_id].is_stale is False
    assert restored.current_snapshot.price.is_stale is False
    snapshot = asyncio.run(restored.get_snapshot(now=NOW + timedelta(minutes=10)))

    assert calls == {"farming": 1, "price": 1}
    assert snapshot.farming[pool_id].farm_rates[0].value_percent == Decimal("13.5")
    assert snapshot.price.price_usdt == Decimal("0.01965")


@pytest.mark.parametrize("bad_value", ("not-a-decimal", "NaN", "Infinity"))
def test_snapshot_with_invalid_price_decimal_is_backed_up_and_ignored(tmp_path, bad_value):
    path = tmp_path / "snapshot.json"

    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return _price()

    async def save_valid_snapshot():
        manager = SnapshotManager(fetch_farming, fetch_price, snapshot_path=path)
        await manager.refresh(now=NOW)

    asyncio.run(save_valid_snapshot())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["current"]["price"]["price_usdt"] = bad_value
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = SnapshotManager(fetch_farming, fetch_price, snapshot_path=path)

    assert restored.current_snapshot is None
    assert restored.previous_snapshot is None
    assert list(tmp_path.glob("snapshot.json.corrupt-*"))


@pytest.mark.parametrize(
    "timestamp_field",
    (
        "current.fetched_at",
        f"current.farming.{FARMING_POOLS[0].address}.fetched_at",
        "current.price.fetched_at",
        "current.price.source_timestamp",
    ),
)
def test_snapshot_with_naive_timestamp_is_backed_up_and_ignored(tmp_path, timestamp_field):
    path = tmp_path / "snapshot.json"

    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return _price()

    async def save_valid_snapshot():
        manager = SnapshotManager(fetch_farming, fetch_price, snapshot_path=path)
        await manager.refresh(now=NOW)

    asyncio.run(save_valid_snapshot())
    payload = json.loads(path.read_text(encoding="utf-8"))
    node = payload
    path_parts = timestamp_field.split(".")
    for part in path_parts[:-1]:
        node = node[part]
    node[path_parts[-1]] = "2026-10-17T13:00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = SnapshotManager(fetch_farming, fetch_price, snapshot_path=path)

    assert restored.current_snapshot is None
    assert restored.previous_snapshot is None
    assert list(tmp_path.glob("snapshot.json.corrupt-*"))


def test_snapshot_with_non_boolean_identity_is_backed_up_and_ignored(tmp_path):
    path = tmp_path / "snapshot.json"

    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return _price()

    async def save_valid_snapshot():
        manager = SnapshotManager(fetch_farming, fetch_price, snapshot_path=path)
        await manager.refresh(now=NOW)

    asyncio.run(save_valid_snapshot())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["current"]["farming"][FARMING_POOLS[0].address]["identity_valid"] = "false"
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = SnapshotManager(fetch_farming, fetch_price, snapshot_path=path)

    assert restored.current_snapshot is None
    assert restored.previous_snapshot is None
    assert list(tmp_path.glob("snapshot.json.corrupt-*"))


def test_unverified_price_identity_is_rechecked_and_recovers_without_restart():
    checks = 0

    async def verify_identity():
        nonlocal checks
        checks += 1
        return True

    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return _price()

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            price_identity_valid=None,
            verify_price_identity=verify_identity,
        )
        return await manager.refresh(now=NOW)

    snapshot = asyncio.run(run())

    assert checks == 1
    assert snapshot.price.status == SourceStatus.OK
    assert snapshot.price.price_usdt == Decimal("0.01865")


def test_price_identity_mismatch_invalidates_cached_price_values():
    async def fetch_farming():
        return [_farming()]

    async def fetch_price():
        return _price()

    async def run():
        manager = SnapshotManager(fetch_farming, fetch_price)
        await manager.refresh(now=NOW)
        manager.set_price_identity(False)
        return manager

    manager = asyncio.run(run())

    assert manager.current_snapshot.price.price_usdt is None
    assert manager.current_snapshot.price.status == SourceStatus.INVALID_RESPONSE


def test_pool_identity_mismatch_invalidates_only_that_pool_in_cached_snapshot():
    async def fetch_farming():
        return [_farming(pool=FARMING_POOLS[0]), _farming("20", pool=FARMING_POOLS[1])]

    async def fetch_price():
        return _price()

    async def run():
        manager = SnapshotManager(fetch_farming, fetch_price)
        await manager.refresh(now=NOW)
        manager.set_farming_identity(FARMING_POOLS[0].address, False)
        return manager

    manager = asyncio.run(run())
    first = manager.current_snapshot.farming[FARMING_POOLS[0].address]
    second = manager.current_snapshot.farming[FARMING_POOLS[1].address]

    assert first.identity_valid is False
    assert first.status == SourceStatus.INVALID_RESPONSE
    assert first.farm_rates == ()
    assert second.identity_valid is True
    assert second.status == SourceStatus.OK


def test_startup_identity_checks_can_force_refresh_of_a_fresh_persisted_snapshot(tmp_path):
    path = tmp_path / "snapshot.json"
    old_farming = _farming("10")
    old_price = _price("0.018")

    async def initial_farming():
        return [old_farming]

    async def initial_price():
        return old_price

    async def run():
        initial = SnapshotManager(initial_farming, initial_price, snapshot_path=path)
        await initial.refresh(now=NOW)

        async def refreshed_farming():
            return [_farming("11")]

        async def refreshed_price():
            return _price("0.019")

        restored = SnapshotManager(refreshed_farming, refreshed_price, snapshot_path=path)
        restored.invalidate_freshness()
        return await restored.get_snapshot(now=NOW + timedelta(seconds=30))

    snapshot = asyncio.run(run())

    assert snapshot.farming[FARMING_POOLS[0].address].farm_rates[0].value_percent == Decimal("11")
    assert snapshot.price.price_usdt == Decimal("0.019")


def test_fresh_snapshot_serves_concurrent_shortcuts_without_extra_fetches():
    calls = {"farming": 0, "price": 0}

    async def fetch_farming():
        calls["farming"] += 1
        return [_farming()]

    async def fetch_price():
        calls["price"] += 1
        return _price()

    async def run():
        manager = SnapshotManager(fetch_farming, fetch_price, freshness_seconds=60)
        first = await manager.get_snapshot(now=NOW)
        second = await manager.get_snapshot(now=NOW + timedelta(seconds=30))
        return first, second

    first, second = asyncio.run(run())

    assert first is second
    assert calls == {"farming": 1, "price": 1}


def test_snapshot_refreshes_farming_and_price_on_their_own_intervals():
    calls = {"farming": 0, "price": 0}

    async def fetch_farming():
        calls["farming"] += 1
        return [_farming(str(10 + calls["farming"]))]

    async def fetch_price():
        calls["price"] += 1
        return _price(str(Decimal("0.018") + Decimal(calls["price"]) / Decimal("1000")))

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            freshness_seconds=60,
            farming_freshness_seconds=300,
            price_freshness_seconds=60,
        )
        first = await manager.get_snapshot(now=NOW)
        second = await manager.get_snapshot(now=NOW + timedelta(seconds=60))
        third = await manager.get_snapshot(now=NOW + timedelta(seconds=300))
        return first, second, third

    first, second, third = asyncio.run(run())
    pool_id = FARMING_POOLS[0].address

    assert calls == {"farming": 2, "price": 3}
    assert second.farming[pool_id].farm_rates[0].value_percent == first.farming[pool_id].farm_rates[0].value_percent
    assert second.price.price_usdt != first.price.price_usdt
    assert third.farming[pool_id].farm_rates[0].value_percent != second.farming[pool_id].farm_rates[0].value_percent


def test_targeted_force_refresh_calls_only_the_requested_source():
    calls = {"farming": 0, "price": 0}

    async def fetch_farming():
        calls["farming"] += 1
        return [_farming(str(10 + calls["farming"]))]

    async def fetch_price():
        calls["price"] += 1
        return _price(str(Decimal("0.018") + Decimal(calls["price"]) / Decimal("1000")))

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            farming_freshness_seconds=300,
            price_freshness_seconds=300,
        )
        await manager.refresh(now=NOW)
        farming_snapshot = await manager.refresh_farming(now=NOW + timedelta(seconds=1))
        price_snapshot = await manager.refresh_price(now=NOW + timedelta(seconds=2))
        return farming_snapshot, price_snapshot

    farming_snapshot, price_snapshot = asyncio.run(run())

    assert calls == {"farming": 2, "price": 2}
    assert farming_snapshot.price.price_usdt == _price("0.019").price_usdt
    assert price_snapshot.farming[FARMING_POOLS[0].address].primary_farm_yield.value_percent == Decimal("12")


def test_concurrent_due_source_refreshes_do_not_skip_a_source():
    calls = {"farming": 0, "price": 0}
    price_refresh_started = asyncio.Event()
    release_price_refresh = asyncio.Event()

    async def fetch_farming():
        calls["farming"] += 1
        return [_farming(str(10 + calls["farming"]))]

    async def fetch_price():
        calls["price"] += 1
        if calls["price"] == 2:
            price_refresh_started.set()
            await release_price_refresh.wait()
        return _price(str(Decimal("0.018") + Decimal(calls["price"]) / Decimal("1000")))

    async def run():
        manager = SnapshotManager(
            fetch_farming,
            fetch_price,
            freshness_seconds=60,
            farming_freshness_seconds=60,
            price_freshness_seconds=59,
        )
        await manager.get_snapshot(now=NOW)
        price_only = asyncio.create_task(manager.get_snapshot(now=NOW + timedelta(seconds=59)))
        await price_refresh_started.wait()
        both_due = asyncio.create_task(manager.get_snapshot(now=NOW + timedelta(seconds=60)))
        await asyncio.sleep(0)
        assert not both_due.done()
        release_price_refresh.set()
        await asyncio.gather(price_only, both_due)
        return manager.current_snapshot

    current = asyncio.run(run())

    assert calls == {"farming": 2, "price": 2}
    assert current.farming[FARMING_POOLS[0].address].farm_rates[0].value_percent == Decimal("12")
    assert current.price.price_usdt == Decimal("0.020")
