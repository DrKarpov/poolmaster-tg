import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import user_state
from farming import FARMING_POOLS, PoolMetrics, RateMetric, SourceStatus
from price import PriceMetrics
from snapshot import DataSnapshot
from user_state import UserAlertStore


NOW = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)


def _snapshot():
    farming = {}
    for pool in FARMING_POOLS:
        kind = "APY" if pool.dex == "STON.fi" else "APR"
        source_field = "apy" if pool.dex == "STON.fi" else "apr_rewards"
        metric = PoolMetrics(
            pool=pool,
            farm_rates=(RateMetric(Decimal("7.5"), kind, source_field),),
            pool_rates=(),
            total_rate=RateMetric(Decimal("12.5"), kind, "total"),
            tvl_usd=None,
            fetched_at=NOW,
            status=SourceStatus.OK,
            identity_valid=True,
        )
        farming[pool.address] = metric
    price = PriceMetrics(
        price_usdt=Decimal("0.0200"),
        bid_usdt=Decimal("0.0199"),
        ask_usdt=Decimal("0.0201"),
        quote_volume_24h=Decimal("10000"),
        symbol="JETTONUSDT",
        quote_currency="USDT",
        source="MEXC spot",
        fetched_at=NOW,
        source_timestamp=NOW,
        status=SourceStatus.OK,
    )
    return DataSnapshot(NOW, farming, price, {"farming": "ok", "price": "ok"}, {})


def test_legacy_subscriber_migration_backs_up_and_keeps_only_private_user_ids(tmp_path):
    legacy_path = tmp_path / "subscribers.json"
    legacy_path.write_text(json.dumps({"subscribers": [123, 456, -100987]}), encoding="utf-8")
    store = UserAlertStore(tmp_path / "users.json")

    migrated = store.migrate_legacy_subscribers(legacy_path, now=NOW)

    assert migrated == 2
    assert set(store.load_users()) == {"123", "456"}
    backups = list(tmp_path.glob("subscribers.json.backup-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"subscribers": [123, 456, -100987]}
    assert backups[0].stat().st_mode & 0o777 == 0o600

    assert store.migrate_legacy_subscribers(legacy_path, now=NOW + timedelta(minutes=1)) == 0
    assert len(list(tmp_path.glob("subscribers.json.backup-*.json"))) == 1


def test_first_alert_subscription_seeds_each_confirmed_primary_yield(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")
    snapshot = _snapshot()

    store.set_farming_threshold(123, Decimal("3"), snapshot)

    user = store.get_user(123, now=NOW)
    assert user["farming_enabled"] is True
    assert user["farming_change_threshold_pp"] == "3"
    assert user["last_notified_yield"] == {
        pool.address: {
            "value": "7.5",
            "kind": "APY" if pool.dex == "STON.fi" else "APR",
            "source_field": "apy" if pool.dex == "STON.fi" else "apr_rewards",
        }
        for pool in FARMING_POOLS
    }


def test_alert_baseline_uses_primary_yield_from_partial_source(tmp_path):
    snapshot = _snapshot()
    pool = FARMING_POOLS[0]
    partial = replace(
        snapshot.farming[pool.address],
        status=SourceStatus.PARTIAL,
        pool_rates=(),
        total_rate=None,
        tvl_usd=None,
    )
    snapshot = replace(snapshot, farming={pool.address: partial})
    store = UserAlertStore(tmp_path / "users.json")

    store.set_farming_threshold(123, Decimal("3"), snapshot)

    assert store.get_user(123)["last_notified_yield"][pool.address] == {
        "value": "7.5",
        "kind": "APR",
        "source_field": "apr_rewards",
    }


def test_alert_baselines_use_primary_yield_value_kind_and_source_field(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")

    store.set_farming_threshold(123, Decimal("3"), _snapshot())

    baselines = store.get_user(123)["last_notified_yield"]
    assert baselines[FARMING_POOLS[0].address] == {
        "value": "7.5",
        "kind": "APR",
        "source_field": "apr_rewards",
    }
    assert baselines[FARMING_POOLS[2].address] == {
        "value": "7.5",
        "kind": "APY",
        "source_field": "apy",
    }


def test_v1_apr_baselines_are_backed_up_and_invalidated_without_losing_preferences(tmp_path):
    state_path = tmp_path / "users.json"
    legacy = {
        "schema_version": 1,
        "users": {
            "123": {
                "chat_id": 123,
                "farming_enabled": True,
                "farming_change_threshold_pp": "3",
                "last_notified_apr": {FARMING_POOLS[0].address: "99"},
                "price_enabled": True,
                "price_threshold_pct": "5",
                "price_baseline": "0.0200",
                "paused_until": (NOW + timedelta(hours=4)).isoformat(),
            }
        },
        "migrations": {},
    }
    state_path.write_text(json.dumps(legacy), encoding="utf-8")

    store = UserAlertStore(state_path)
    user = store.get_user(123, now=NOW)

    assert user["farming_enabled"] is True
    assert user["farming_change_threshold_pp"] == "3"
    assert user["last_notified_yield"] == {}
    assert user["price_enabled"] is True
    assert user["price_threshold_pct"] == "5"
    assert user["price_baseline"] == "0.0200"
    assert user["paused_until"] == legacy["users"]["123"]["paused_until"]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    backups = list(tmp_path.glob("users.json.migration-*.json"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_private_user_registration_tracks_metadata_and_reactivation(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")
    first_seen = NOW
    second_seen = NOW + timedelta(hours=1)
    first_user = SimpleNamespace(id=123, username="old_name", first_name="Alex", last_name="One")
    renamed_user = SimpleNamespace(id=123, username="new_name", first_name="Alexandra", last_name="Two")

    store.register_private_user(first_user, observed_at=first_seen)
    store.mark_blocked(123, observed_at=second_seen)
    store.reactivate_user(renamed_user, observed_at=second_seen + timedelta(hours=1))

    record = store.get_user(123)
    assert record["registered"] is True
    assert record["blocked"] is False
    assert record["username"] == "new_name"
    assert record["first_name"] == "Alexandra"
    assert record["last_name"] == "Two"
    assert record["first_seen_at"] == first_seen.isoformat()
    assert record["last_seen_at"] == (second_seen + timedelta(hours=1)).isoformat()
    assert record["blocked_at"] == second_seen.isoformat()
    assert store.list_active_user_ids() == [123]


def test_new_user_language_stays_unset_until_choice_is_saved(tmp_path):
    state_path = tmp_path / "users.json"
    store = UserAlertStore(state_path)

    assert store.ensure_user(123)["language"] is None

    store.set_language(123, "en")
    reloaded = UserAlertStore(state_path)

    assert reloaded.get_user(123)["language"] == "en"


def test_legacy_user_record_without_language_defaults_to_russian(tmp_path):
    state_path = tmp_path / "users.json"
    store = UserAlertStore(state_path)
    store.ensure_user(123)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    del payload["users"]["123"]["language"]
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = UserAlertStore(state_path)

    assert reloaded.get_user(123)["language"] == "ru"


def test_private_user_registration_rejects_group_and_invalid_timestamps(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")

    with pytest.raises(ValueError, match="private user"):
        store.register_private_user(SimpleNamespace(id=-100123), observed_at=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.register_private_user(SimpleNamespace(id=123), observed_at=datetime(2026, 10, 17))


def test_legacy_user_import_merges_subscribers_and_private_stats_only(tmp_path):
    subscribers_path = tmp_path / "subscribers.json"
    stats_path = tmp_path / "stats.json"
    subscribers_path.write_text(json.dumps({"subscribers": [123, 456, -100987]}), encoding="utf-8")
    stats_path.write_text(json.dumps({
        "users": {
            "123": {
                "id": 123,
                "username": "private_user",
                "first_name": "Private",
                "last_name": "User",
                "chat_id": 123,
                "chat_type": "private",
                "first_seen_at": "2026-10-01T00:00:00+00:00",
                "last_seen_at": "2026-10-17T00:00:00+00:00",
            },
            "789": {
                "id": 789,
                "username": "private_stats_only",
                "first_name": "Stats",
                "last_name": "Only",
                "chat_id": 789,
                "chat_type": "private",
                "first_seen_at": "2026-10-02T00:00:00+00:00",
                "last_seen_at": "2026-10-16T00:00:00+00:00",
            },
            "321": {
                "id": 321,
                "username": "group_user",
                "first_name": "Group",
                "last_name": "Only",
                "chat_id": -100321,
                "chat_type": "supergroup",
                "first_seen_at": "2026-10-03T00:00:00+00:00",
                "last_seen_at": "2026-10-17T00:00:00+00:00",
            },
        },
        "events": [],
    }), encoding="utf-8")
    store = UserAlertStore(tmp_path / "users.json")

    migrated = store.migrate_legacy_private_users(subscribers_path, stats_path, now=NOW)

    assert migrated == 3
    assert store.list_registered_user_ids() == [123, 456, 789]
    assert store.get_user(123)["username"] == "private_user"
    assert store.get_user(789)["first_seen_at"] == "2026-10-02T00:00:00+00:00"
    assert store.get_user(456)["registered"] is True
    assert store.get_user(321)["registered"] is False
    assert len(list(tmp_path.glob("subscribers.json.backup-*.json"))) == 1
    assert len(list(tmp_path.glob("stats.json.backup-*.json"))) == 1


def test_blocked_users_are_removed_from_active_recipient_set(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")
    store.register_private_user(SimpleNamespace(id=123, username=None, first_name="A", last_name=None), observed_at=NOW)
    store.register_private_user(SimpleNamespace(id=456, username=None, first_name="B", last_name=None), observed_at=NOW)

    store.mark_blocked(123, observed_at=NOW + timedelta(minutes=1))

    assert store.list_registered_user_ids() == [123, 456]
    assert store.list_active_user_ids() == [456]


def test_migrated_user_is_seeded_from_confirmed_primary_yield_before_dispatch(tmp_path):
    state_path = tmp_path / "users.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "users": {
            "123": {
                "chat_id": 123,
                "farming_enabled": True,
                "farming_change_threshold_pp": "1",
                "last_notified_apr": {FARMING_POOLS[0].address: "1"},
            }
        },
        "migrations": {},
    }), encoding="utf-8")
    store = UserAlertStore(state_path)
    snapshot = _snapshot()

    seeded = store.seed_missing_baselines(123, snapshot, now=NOW)

    assert seeded["last_notified_yield"][FARMING_POOLS[0].address] == {
        "value": "7.5",
        "kind": "APR",
        "source_field": "apr_rewards",
    }


def test_first_confirmed_snapshot_after_enablement_seeds_primary_baselines(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")
    store.set_farming_threshold(123, Decimal("1"), snapshot=None)

    seeded = store.seed_missing_baselines(123, _snapshot())

    assert seeded["last_notified_yield"] == {
        pool.address: {
            "value": "7.5",
            "kind": "APY" if pool.dex == "STON.fi" else "APR",
            "source_field": "apy" if pool.dex == "STON.fi" else "apr_rewards",
        }
        for pool in FARMING_POOLS
    }


def test_reenabling_apr_alerts_resets_baseline_to_current_snapshot(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")
    store.set_farming_threshold(123, Decimal("1"), _snapshot())
    current = _snapshot()
    current_metrics = current.farming[FARMING_POOLS[0].address]
    changed_metrics = PoolMetrics(
        **{
            **current_metrics.__dict__,
                "farm_rates": (RateMetric(Decimal("20"), "APR", "apr_rewards"),),
            "total_rate": RateMetric(Decimal("20"), "APR", "total"),
        }
    )
    changed = DataSnapshot(
        NOW,
        {**current.farming, FARMING_POOLS[0].address: changed_metrics},
        current.price,
        current.source_statuses,
        current.anomalies,
    )
    store.set_farming_threshold(123, None, changed)

    store.set_farming_threshold(123, Decimal("3"), changed)

    assert store.get_user(123)["last_notified_yield"][FARMING_POOLS[0].address] == {
        "value": "20",
        "kind": "APR",
        "source_field": "apr_rewards",
    }


def test_first_price_alert_subscription_uses_current_confirmed_price_baseline(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")

    store.set_price_threshold(123, Decimal("5"), _snapshot())

    user = store.get_user(123)
    assert user["price_enabled"] is True
    assert user["price_threshold_pct"] == "5"
    assert user["price_baseline"] == "0.0200"

    reopened = UserAlertStore(tmp_path / "users.json")
    assert reopened.get_user(123)["price_baseline"] == "0.0200"


def test_pause_expires_after_24_hours_and_resume_preserves_alert_settings(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")
    store.set_farming_threshold(123, Decimal("1"), _snapshot())
    store.set_price_threshold(123, Decimal("2"), _snapshot())

    store.pause_user(123, now=NOW)
    paused = store.get_user(123, now=NOW)
    assert paused["paused_until"] == (NOW + timedelta(hours=24)).isoformat()

    store.resume_user(123)
    resumed = store.get_user(123)
    assert resumed["paused_until"] is None
    assert resumed["farming_enabled"] is True
    assert resumed["price_enabled"] is True
    assert resumed["last_notified_yield"][FARMING_POOLS[0].address]["value"] == "7.5"
    assert resumed["price_baseline"] == "0.0200"


def test_expired_pause_is_cleared_from_persistent_user_state(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")
    store.pause_user(123, now=NOW)

    user = store.get_user(123, now=NOW + timedelta(hours=25))

    assert user["paused_until"] is None
    assert UserAlertStore(tmp_path / "users.json").get_user(123)["paused_until"] is None


def test_corrupt_legacy_subscriber_file_is_preserved_without_creating_users(tmp_path):
    legacy_path = tmp_path / "subscribers.json"
    legacy_path.write_text('{"subscribers": [', encoding="utf-8")
    store = UserAlertStore(tmp_path / "users.json")

    assert store.migrate_legacy_subscribers(legacy_path, now=NOW) == 0

    assert legacy_path.read_text(encoding="utf-8") == '{"subscribers": ['
    assert list(tmp_path.glob("subscribers.json.backup-*.json"))
    assert store.load_users() == {}


def test_corrupt_user_state_is_backed_up_and_not_replaced_until_a_write(tmp_path):
    state_path = tmp_path / "users.json"
    state_path.write_text('{"users": [', encoding="utf-8")

    store = UserAlertStore(state_path)

    assert state_path.read_text(encoding="utf-8") == '{"users": ['
    assert list(tmp_path.glob("users.json.corrupt-*"))
    assert store.get_user(123)["chat_id"] == 123

    store.ensure_user(123)
    assert json.loads(state_path.read_text(encoding="utf-8"))["users"]["123"]["chat_id"] == 123


def test_corrupt_user_state_fails_closed_if_backup_cannot_be_created(tmp_path, monkeypatch):
    state_path = tmp_path / "users.json"
    original = '{"users": ['
    state_path.write_text(original, encoding="utf-8")

    def fail_copy(*_args, **_kwargs):
        raise OSError("backup disk unavailable")

    monkeypatch.setattr(user_state, "secure_backup_file", fail_copy)

    with pytest.raises(OSError, match="backup disk unavailable"):
        UserAlertStore(state_path)

    assert state_path.read_text(encoding="utf-8") == original


def test_malformed_nested_user_state_is_backed_up_instead_of_loaded(tmp_path):
    state_path = tmp_path / "users.json"
    malformed = json.dumps({
        "schema_version": 1,
        "users": {"123": {"chat_id": 123, "last_notified_apr": []}},
        "migrations": {},
    })
    state_path.write_text(malformed, encoding="utf-8")

    store = UserAlertStore(state_path)

    assert state_path.read_text(encoding="utf-8") == malformed
    assert list(tmp_path.glob("users.json.corrupt-*"))
    assert store.load_users() == {}


def test_alert_off_preserves_threshold_and_baselines(tmp_path):
    store = UserAlertStore(tmp_path / "users.json")
    snapshot = _snapshot()
    store.set_farming_threshold(123, Decimal("3"), snapshot)

    store.set_alert_enabled(123, "yield", False, snapshot)

    user = store.get_user(123)
    assert user["farming_enabled"] is False
    assert user["farming_change_threshold_pp"] == "3"
    assert user["last_notified_yield"][FARMING_POOLS[0].address]["value"] == "7.5"


def test_manual_farming_push_fingerprint_persists_per_user(tmp_path):
    path = tmp_path / "users.json"
    store = UserAlertStore(path)
    store.ensure_user(123)

    assert store.has_manual_farming_push(123, "card-hash") is False
    store.record_manual_farming_push(123, "card-hash", NOW)

    reopened = UserAlertStore(path)
    assert reopened.has_manual_farming_push(123, "card-hash") is True
    assert reopened.get_user(123)["last_manual_farming_push_at"] == NOW.isoformat()
