import json
import os
from types import SimpleNamespace

import pytest

import bot
from bot import atomic_write_json, CommandStatsStore


def test_atomic_json_write_replaces_corrupted_contents_with_valid_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{partial", encoding="utf-8")

    atomic_write_json(path, {"enabled": False, "subscribers": [42]})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "enabled": False,
        "subscribers": [42],
    }


def test_corrupt_stats_state_is_preserved_before_tracking_new_command(tmp_path):
    path = tmp_path / "stats.json"
    damaged = '{"users": '
    path.write_text(damaged, encoding="utf-8")
    store = CommandStatsStore(str(path))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123, username="tester", first_name="Test", last_name=None),
        effective_chat=SimpleNamespace(id=123, type="private"),
    )

    store.track(update, "start")

    backups = list(tmp_path.glob("stats.corrupt.*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == damaged
    assert os.stat(backups[0]).st_mode & 0o777 == 0o600
    recovered = json.loads(path.read_text(encoding="utf-8"))
    assert recovered["users"]["123"]["total_commands"] == 1
    assert recovered["events"][0]["command"] == "start"


def test_stats_store_does_not_overwrite_corrupt_state_when_backup_fails(tmp_path, monkeypatch):
    path = tmp_path / "stats.json"
    damaged = '{"users": '
    path.write_text(damaged, encoding="utf-8")
    path.chmod(0o600)
    store = CommandStatsStore(str(path))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123, username="tester", first_name="Test", last_name=None),
        effective_chat=SimpleNamespace(id=123, type="private"),
    )

    def fail_backup(_source, _destination):
        raise OSError("backup unavailable")

    monkeypatch.setattr(bot, "secure_backup_file", fail_backup)

    with pytest.raises(OSError, match="backup unavailable"):
        store.track(update, "start")

    assert path.read_text(encoding="utf-8") == damaged
    assert not list(tmp_path.glob("stats.corrupt.*.json"))
