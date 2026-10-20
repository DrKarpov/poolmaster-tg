import json
from datetime import datetime, timezone

import pytest
import admin_state

from admin_state import AdminStateStore


NOW = datetime(2026, 10, 17, 17, 0, tzinfo=timezone.utc)


def test_global_pause_defaults_from_config_and_persists_across_restart(tmp_path):
    path = tmp_path / "admin.json"
    store = AdminStateStore(path, initial_paused=True)

    assert store.is_global_auto_push_paused() is True
    store.set_global_auto_push_paused(False, actor_id=111, admin_ids={111, 222})

    reopened = AdminStateStore(path, initial_paused=True)
    assert reopened.is_global_auto_push_paused() is False
    reopened.set_global_auto_push_paused(True, actor_id=222, admin_ids={111, 222})
    assert AdminStateStore(path).is_global_auto_push_paused() is True


def test_non_admin_cannot_change_persistent_global_pause(tmp_path):
    store = AdminStateStore(tmp_path / "admin.json")

    with pytest.raises(PermissionError):
        store.set_global_auto_push_paused(False, actor_id=333, admin_ids={111, 222})

    assert store.is_global_auto_push_paused() is True


def test_corrupt_admin_state_is_backed_up_and_default_is_used(tmp_path):
    path = tmp_path / "admin.json"
    original = '{"schema_version": 1, "global_auto_push_paused": "false"}'
    path.write_text(original, encoding="utf-8")

    store = AdminStateStore(path, initial_paused=False)

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("admin.json.corrupt-*"))
    assert store.is_global_auto_push_paused() is False


def test_corrupt_admin_state_fails_closed_if_backup_cannot_be_created(tmp_path, monkeypatch):
    path = tmp_path / "admin.json"
    original = '{"schema_version": 1, "global_auto_push_paused": "false"}'
    path.write_text(original, encoding="utf-8")

    def fail_copy(*_args, **_kwargs):
        raise OSError("backup disk unavailable")

    monkeypatch.setattr(admin_state, "secure_backup_file", fail_copy)

    with pytest.raises(OSError, match="backup disk unavailable"):
        AdminStateStore(path)

    assert path.read_text(encoding="utf-8") == original


def test_manual_push_timestamp_persists(tmp_path):
    path = tmp_path / "admin.json"
    store = AdminStateStore(path)

    store.record_manual_push(NOW)

    assert AdminStateStore(path).last_manual_push_at() == NOW


def test_state_schema_has_no_secret_or_token_fields(tmp_path):
    path = tmp_path / "admin.json"
    store = AdminStateStore(path)
    store.set_global_auto_push_paused(False, actor_id=111, admin_ids={111})

    state = json.loads(path.read_text(encoding="utf-8"))
    assert set(state) == {"schema_version", "global_auto_push_paused", "last_manual_push_at"}
