import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from legacy_users import MIGRATION_KEY, build_import_plan
from scripts.import_legacy_poolmaster_users import main
from user_state import UserAlertStore


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _private(user_id, **overrides):
    record = {
        "id": user_id,
        "chat_id": user_id,
        "chat_type": "private",
        "username": f"legacy_{user_id}",
        "first_name": f"Legacy {user_id}",
        "last_name": "User",
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-02-01T00:00:00+00:00",
    }
    record.update(overrides)
    return record


def _write_source(path, users):
    path.write_text(json.dumps({"users": users, "events": [{"type": "ignored"}]}), encoding="utf-8")
    return path


def test_private_candidate_filter_accepts_explicit_and_missing_type_private_but_excludes_groups(tmp_path):
    source = _write_source(
        tmp_path / "legacy.json",
        {
            "100": _private(100),
            "101": _private(101, chat_type=None),
            "102": {"id": 102, "chat_id": -100102, "chat_type": "supergroup"},
            "103": {"id": 103, "chat_id": 103, "chat_type": "group"},
            "104": {"id": 104, "chat_id": -100104},
            "105": "malformed record",
        },
    )
    with source.open("r", encoding="utf-8") as source_file:
        payload = json.load(source_file)
    payload["users"]["101"].pop("chat_type")
    source.write_text(json.dumps(payload), encoding="utf-8")

    plan = build_import_plan(source, {})

    assert plan.report.source_records == 6
    assert plan.report.private_candidates == 2
    assert plan.report.group_only_ignored == 3
    assert plan.report.invalid_records == 1
    assert {record["user_id"] for record in plan.private_users} == {100, 101}


def test_explicit_null_chat_type_and_boolean_chat_id_are_invalid(tmp_path):
    source = _write_source(
        tmp_path / "legacy.json",
        {
            "106": _private(106, chat_type=None),
            "107": _private(107, chat_id=True),
        },
    )

    plan = build_import_plan(source, {})

    assert plan.report.invalid_records == 2
    assert plan.report.private_candidates == 0


def test_malformed_and_duplicate_private_records_are_excluded(tmp_path):
    source = _write_source(
        tmp_path / "legacy.json",
        {
            "200": _private(200),
            "0200": _private(200, username="duplicate"),
            "0": _private(0),
            "201": _private(201, chat_id="not-a-chat-id"),
        },
    )

    plan = build_import_plan(source, {})

    assert plan.report.source_records == 4
    assert plan.report.duplicates == 1
    assert plan.report.invalid_records == 2
    assert plan.report.private_candidates == 0
    assert plan.private_users == ()


def test_dry_run_counts_overlaps_and_conflicting_metadata_without_mutating_current_state(tmp_path):
    source = _write_source(tmp_path / "legacy.json", {"300": _private(300)})
    current = {
        "300": {
            "registered": True,
            "username": "current_name",
            "first_name": "Current Name",
            "last_name": None,
        }
    }
    original = deepcopy(current)

    plan = build_import_plan(source, current)

    assert plan.report.overlaps == 1
    assert plan.report.new_private_users == 0
    assert plan.report.metadata_conflicts == 1
    assert plan.report.expected_final_private_registry_count == 1
    assert current == original
    assert "current_name" not in json.dumps(plan.report.as_dict())


def test_merge_preserves_existing_preferences_and_registers_new_users_with_alerts_off(tmp_path):
    state_path = tmp_path / "user_state.json"
    store = UserAlertStore(state_path)
    store.register_private_user(
        SimpleNamespace(id=300, username="current_name", first_name="Current Name", last_name=None),
        observed_at=NOW - timedelta(days=10),
    )
    store.set_language(300, "en")
    store.set_farming_threshold(300, Decimal("3"))
    store.set_price_threshold(300, Decimal("5"))
    store.record_yield_delivery(
        300,
        "synthetic-pool",
        Decimal("9.5"),
        "APR",
        "apr_rewards",
        "yield-fingerprint",
        sent_at=NOW,
    )
    store.mark_blocked(300, observed_at=NOW - timedelta(days=1))
    store.pause_user(300, now=NOW)
    store.record_price_delivery(300, Decimal("0.02"), "existing-fingerprint", NOW)
    before = store.get_user(300, now=NOW)

    source = _write_source(
        tmp_path / "legacy.json",
        {
            "300": _private(
                300,
                username="older_name",
                first_name="Older Name",
                last_name="Filled Name",
                first_seen_at="2026-10-12T00:00:00+00:00",
                last_seen_at="2026-10-17T00:00:00+00:00",
            ),
            "301": _private(301),
        },
    )
    plan = build_import_plan(source, store.load_users())

    imported = store.merge_legacy_private_users(
        plan.private_users,
        migration_metadata=plan.migration_metadata(),
        now=NOW,
    )

    existing = store.get_user(300, now=NOW)
    new = store.get_user(301, now=NOW)
    assert imported == 1
    assert existing["registered"] is True
    assert existing["blocked"] is True
    assert existing["username"] == "current_name"
    assert existing["first_name"] == "Current Name"
    assert existing["last_name"] == "Filled Name"
    assert existing["language"] == "en"
    for field in (
        "farming_enabled",
        "farming_change_threshold_pp",
        "farming_direction",
        "last_notified_yield",
        "price_enabled",
        "price_threshold_pct",
        "price_direction",
        "price_baseline",
        "paused_until",
        "delivery_fingerprints",
        "blocked_at",
        "last_farming_push_fingerprint",
        "last_farming_push_at",
        "last_alert_at",
        "price_alert_revision",
        "yield_alert_revision",
    ):
        assert existing[field] == before[field]
    assert existing["first_seen_at"] == before["first_seen_at"]
    assert existing["last_seen_at"] == "2026-10-17T00:00:00+00:00"

    assert new["registered"] is True
    assert new["blocked"] is False
    assert new["language"] == "ru"
    assert new["farming_enabled"] is False
    assert new["farming_change_threshold_pp"] is None
    assert new["price_enabled"] is False
    assert new["price_threshold_pct"] is None
    assert new["first_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert new["last_seen_at"] == "2026-02-01T00:00:00+00:00"

    migration = json.loads(state_path.read_text(encoding="utf-8"))["migrations"][MIGRATION_KEY]
    assert migration["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert migration["source_record_count"] == 2
    assert migration["source_mtime_utc"].endswith("Z")
    assert "users" not in migration
    assert "older_name" not in json.dumps(migration)


def test_apply_cli_is_idempotent_and_repeat_does_not_change_state_bytes(tmp_path, capsys):
    source = _write_source(tmp_path / "legacy.json", {"400": _private(400), "401": _private(401)})
    state_path = tmp_path / "user_state.json"
    args = ["--source", str(source), "--user-state", str(state_path), "--apply"]

    assert main(args) == 0
    first_report = json.loads(capsys.readouterr().out)
    first_state = state_path.read_bytes()
    assert first_report["imported"] == 2
    assert first_report["private_candidates"] == 2

    assert main(args) == 0
    second_report = json.loads(capsys.readouterr().out)

    assert second_report["imported"] == 0
    assert second_report["overlaps"] == 2
    assert state_path.read_bytes() == first_state
    restored = UserAlertStore(state_path)
    assert restored.list_registered_user_ids() == [400, 401]


def test_dry_run_cli_outputs_only_aggregates_and_does_not_change_state(tmp_path, capsys):
    source = _write_source(
        tmp_path / "legacy.json",
        {"500": _private(500, username="private_name"), "501": {"id": 501, "chat_id": -100501, "chat_type": "group"}},
    )
    state_path = tmp_path / "user_state.json"
    current = UserAlertStore(state_path)
    current.ensure_user(500)
    state_before = state_path.read_bytes()
    args = ["--source", str(source), "--user-state", str(state_path), "--dry-run"]

    assert main(args) == 0

    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["source_records"] == 2
    assert report["private_candidates"] == 1
    assert report["group_only_ignored"] == 1
    assert "private_name" not in output
    assert state_path.read_bytes() == state_before


def test_import_cli_requires_exactly_one_mode(tmp_path):
    with pytest.raises(SystemExit):
        main(["--source", str(tmp_path / "legacy.json"), "--user-state", str(tmp_path / "users.json")])
