import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import farming_cache
from farming import FARMING_POOLS, PoolMetrics, RateMetric, SourceStatus, unavailable_metrics
from farming_cache import FarmingStaleCache
from bot import PoolmasterBot
from user_ui import render_farming_text


NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def _confirmed(*, fetched_at=NOW, pool=FARMING_POOLS[0]):
    return PoolMetrics(
        pool=pool,
        farm_rates=(RateMetric(Decimal("12.5"), "APR", "apr_rewards"),),
        pool_rates=(RateMetric(Decimal("1.5"), "APR", "apr_fees"),),
        total_rate=RateMetric(Decimal("14"), "APR", "apr"),
        tvl_usd=Decimal("100000"),
        fetched_at=fetched_at,
        status=SourceStatus.OK,
        identity_valid=True,
    )


def test_cache_uses_last_confirmed_value_for_temporary_source_failure():
    cache = FarmingStaleCache(max_age_seconds=1800)
    cache.apply([_confirmed()], now=NOW)

    result = cache.apply(
        [unavailable_metrics(FARMING_POOLS[0], SourceStatus.TIMEOUT, "request timed out", NOW + timedelta(minutes=5))],
        now=NOW + timedelta(minutes=5),
    )[0]

    assert result.is_stale is True
    assert result.fetched_at == NOW
    assert result.status == SourceStatus.TIMEOUT
    assert result.farm_rates[0].value_percent == Decimal("12.5")
    text = PoolmasterBot.__new__(PoolmasterBot).build_farming_text([result])
    aged_text = render_farming_text([result], now=NOW + timedelta(minutes=5))
    assert "данные временно не обновляются" in text.lower()
    assert "5 мин. назад" in aged_text
    assert "Доходность: <b>12.50%</b>" in text


def test_cache_retains_usable_primary_yield_from_partial_source():
    cache = FarmingStaleCache(max_age_seconds=1800)
    partial = replace(
        _confirmed(),
        status=SourceStatus.PARTIAL,
        pool_rates=(),
        total_rate=None,
        tvl_usd=None,
    )

    cache.apply([partial], now=NOW)
    result = cache.apply(
        [unavailable_metrics(FARMING_POOLS[0], SourceStatus.TIMEOUT, "offline", NOW + timedelta(minutes=5))],
        now=NOW + timedelta(minutes=5),
    )[0]

    assert result.is_stale is True
    assert result.farm_rates[0].value_percent == Decimal("12.5")


def test_ston_transport_failure_uses_only_bounded_confirmed_farm_yield():
    cache = FarmingStaleCache(max_age_seconds=1800)
    pool = FARMING_POOLS[2]
    cache.apply([_confirmed(pool=pool)], now=NOW)
    transport_failure = replace(
        _confirmed(fetched_at=NOW + timedelta(minutes=5), pool=pool),
        farm_rates=(),
        pool_rates=(RateMetric(Decimal("2.5"), "APY", "apy_1d", "1d"),),
        total_rate=None,
        tvl_usd=Decimal("110000"),
        status=SourceStatus.PARTIAL,
        status_detail="farm endpoint unavailable",
        stale_farm_yield_fallback_allowed=True,
    )

    result = cache.apply([transport_failure], now=NOW + timedelta(minutes=5))[0]

    assert result.status == SourceStatus.PARTIAL
    assert result.is_stale is True
    assert result.fetched_at == NOW
    assert result.farm_rates == _confirmed().farm_rates
    assert result.pool_rates == transport_failure.pool_rates
    assert result.tvl_usd == Decimal("110000")
    assert result.stale_farm_yield_fallback_allowed is False


def test_ston_transport_failure_does_not_expose_expired_farm_yield():
    cache = FarmingStaleCache(max_age_seconds=60)
    pool = FARMING_POOLS[2]
    cache.apply([_confirmed(pool=pool)], now=NOW)
    transport_failure = replace(
        _confirmed(fetched_at=NOW + timedelta(seconds=61), pool=pool),
        farm_rates=(),
        total_rate=None,
        status=SourceStatus.PARTIAL,
        stale_farm_yield_fallback_allowed=True,
    )

    result = cache.apply([transport_failure], now=NOW + timedelta(seconds=61))[0]

    assert result.is_stale is False
    assert result.farm_rates == ()


def test_cache_does_not_expose_values_past_configured_stale_age():
    cache = FarmingStaleCache(max_age_seconds=60)
    cache.apply([_confirmed()], now=NOW)

    result = cache.apply(
        [unavailable_metrics(FARMING_POOLS[0], SourceStatus.UNAVAILABLE, "offline", NOW + timedelta(seconds=61))],
        now=NOW + timedelta(seconds=61),
    )[0]

    assert result.is_stale is False
    assert result.farm_rates == ()
    assert result.total_rate is None


def test_cache_persists_confirmed_values_between_processes(tmp_path):
    path = tmp_path / "farming.json"
    FarmingStaleCache(max_age_seconds=1800, path=path).apply([_confirmed()], now=NOW)

    restored = FarmingStaleCache(max_age_seconds=1800, path=path)
    result = restored.apply(
        [unavailable_metrics(FARMING_POOLS[0], SourceStatus.TIMEOUT, "offline", NOW + timedelta(minutes=1))],
        now=NOW + timedelta(minutes=1),
    )[0]

    assert result.is_stale is True
    assert result.farm_rates[0].value_percent == Decimal("12.5")


@pytest.mark.parametrize("bad_value", ("not-a-decimal", "NaN", "Infinity"))
def test_cache_with_invalid_rate_decimal_is_backed_up_and_ignored(tmp_path, bad_value):
    path = tmp_path / "farming.json"
    FarmingStaleCache(max_age_seconds=1800, path=path).apply([_confirmed()], now=NOW)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["confirmed"][FARMING_POOLS[0].address]["farm_rates"][0]["value_percent"] = bad_value
    path.write_text(json.dumps(payload), encoding="utf-8")

    cache = FarmingStaleCache(max_age_seconds=1800, path=path)
    result = cache.apply(
        [unavailable_metrics(FARMING_POOLS[0], SourceStatus.TIMEOUT, "offline", NOW + timedelta(minutes=1))],
        now=NOW + timedelta(minutes=1),
    )[0]

    assert result.is_stale is False
    assert result.farm_rates == ()
    assert list(tmp_path.glob("farming.json.corrupt-*"))


def test_cache_with_naive_timestamp_is_backed_up_and_ignored(tmp_path):
    path = tmp_path / "farming.json"
    FarmingStaleCache(max_age_seconds=1800, path=path).apply([_confirmed()], now=NOW)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["confirmed"][FARMING_POOLS[0].address]["fetched_at"] = "2026-10-17T12:00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")

    cache = FarmingStaleCache(max_age_seconds=1800, path=path)
    result = cache.apply(
        [unavailable_metrics(FARMING_POOLS[0], SourceStatus.TIMEOUT, "offline", NOW + timedelta(minutes=1))],
        now=NOW + timedelta(minutes=1),
    )[0]

    assert result.is_stale is False
    assert result.farm_rates == ()
    assert list(tmp_path.glob("farming.json.corrupt-*"))


def test_cache_with_non_boolean_identity_is_backed_up_and_ignored(tmp_path):
    path = tmp_path / "farming.json"
    FarmingStaleCache(max_age_seconds=1800, path=path).apply([_confirmed()], now=NOW)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["confirmed"][FARMING_POOLS[0].address]["identity_valid"] = "false"
    path.write_text(json.dumps(payload), encoding="utf-8")

    cache = FarmingStaleCache(max_age_seconds=1800, path=path)
    result = cache.apply(
        [unavailable_metrics(FARMING_POOLS[0], SourceStatus.TIMEOUT, "offline", NOW + timedelta(minutes=1))],
        now=NOW + timedelta(minutes=1),
    )[0]

    assert result.is_stale is False
    assert result.farm_rates == ()
    assert list(tmp_path.glob("farming.json.corrupt-*"))


def test_cache_never_falls_back_across_invalid_pool_identity():
    cache = FarmingStaleCache(max_age_seconds=1800)
    cache.apply([_confirmed()], now=NOW)
    invalid = unavailable_metrics(
        FARMING_POOLS[0],
        SourceStatus.INVALID_RESPONSE,
        "pool address mismatch",
        NOW + timedelta(minutes=1),
    )

    result = cache.apply([invalid], now=NOW + timedelta(minutes=1))[0]

    assert result.is_stale is False
    assert result.farm_rates == ()


def test_cache_ignores_structurally_corrupt_json_without_crashing(tmp_path):
    path = tmp_path / "farming.json"
    path.write_text('{"confirmed": []}', encoding="utf-8")

    cache = FarmingStaleCache(max_age_seconds=1800, path=path)

    assert cache.apply([], now=NOW) == []
    assert list(tmp_path.glob("farming.json.corrupt-*"))
    assert path.read_text(encoding="utf-8") == '{"confirmed": []}'


def test_corrupt_farming_cache_fails_closed_if_backup_cannot_be_created(tmp_path, monkeypatch):
    path = tmp_path / "farming.json"
    original = '{"confirmed": []}'
    path.write_text(original, encoding="utf-8")

    def fail_copy(*_args, **_kwargs):
        raise OSError("backup disk unavailable")

    monkeypatch.setattr(farming_cache, "secure_backup_file", fail_copy)

    with pytest.raises(OSError, match="backup disk unavailable"):
        FarmingStaleCache(max_age_seconds=1800, path=path)

    assert path.read_text(encoding="utf-8") == original


def test_unconfirmed_anomaly_has_a_distinct_stale_label():
    metrics = replace(
        _confirmed(),
        status=SourceStatus.PARTIAL,
        status_detail="large APR change was not confirmed",
        is_stale=True,
    )

    text = PoolmasterBot.__new__(PoolmasterBot).build_farming_text([metrics])

    assert "изменение доходности не подтверждено" in text.lower()
    assert "данные временно не обновляются" not in text
