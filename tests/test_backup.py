import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import scripts.backup_poolmaster_state as backup_module


NOW = datetime(2026, 10, 18, 3, 5, tzinfo=timezone.utc)
STATE_FILES = (
    "admin_state.json",
    "auto_push_state.json",
    "managed_destinations.json",
    "stats.json",
    "subscribers.json",
    "user_state.json",
)


def _data_dir(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for filename in STATE_FILES:
        (data_dir / filename).write_text('{"schema_version": 1}\n', encoding="utf-8")
    (data_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=never-back-up\n", encoding="utf-8")
    (data_dir / "snapshot.json").write_text("{}\n", encoding="utf-8")
    (data_dir / "farming_cache.json").write_text("{}\n", encoding="utf-8")
    chart_cache = data_dir / "chart_cache"
    chart_cache.mkdir()
    (chart_cache / "chart.png").write_bytes(b"rebuildable")
    return data_dir


def _create_history_database(data_dir):
    connection = sqlite3.connect(data_dir / "farming_history.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO observations (value) VALUES ('12.5')")
    connection.commit()
    return connection


def test_backup_contains_allowed_state_and_online_sqlite_copy_with_private_modes(tmp_path):
    data_dir = _data_dir(tmp_path)
    source_db = _create_history_database(data_dir)
    backup_dir = tmp_path / "backups"

    archive_path = backup_module.create_backup(data_dir, backup_dir, now=NOW)

    source_db.close()
    assert backup_dir.stat().st_mode & 0o777 == 0o700
    assert archive_path.stat().st_mode & 0o777 == 0o600
    restored_dir = tmp_path / "restored"
    restored_dir.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert names == {"manifest.json", *STATE_FILES, "farming_history.sqlite3"}
        assert all((item.external_attr >> 16) & 0o777 == 0o600 for item in archive.infolist())
        manifest = json.loads(archive.read("manifest.json"))
        assert set(manifest["files"]) == {*STATE_FILES, "farming_history.sqlite3"}
        archive.extractall(restored_dir)

        for filename in STATE_FILES:
            assert json.loads((restored_dir / filename).read_text(encoding="utf-8")) == {"schema_version": 1}
        with sqlite3.connect(restored_dir / "farming_history.sqlite3") as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT value FROM observations").fetchone()[0] == "12.5"

        for excluded in (".env", "snapshot.json", "farming_cache.json", "chart_cache/chart.png"):
            assert excluded not in names


def test_backup_retention_keeps_only_thirty_most_recent_archives(tmp_path):
    data_dir = _data_dir(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for day in range(31):
        timestamp = NOW - timedelta(days=31 - day)
        name = timestamp.strftime("poolmaster-state-%Y%m%dT%H%M%S.%fZ.zip")
        (backup_dir / name).write_bytes(b"older backup")

    new_archive = backup_module.create_backup(data_dir, backup_dir, now=NOW)

    archives = sorted(backup_dir.glob("poolmaster-state-*.zip"))
    assert len(archives) == 30
    assert archives[-1] == new_archive


def test_backup_creation_failure_keeps_previous_archives_and_cleans_staging(tmp_path, monkeypatch):
    data_dir = _data_dir(tmp_path)
    _create_history_database(data_dir).close()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    prior_archive = backup_dir / "poolmaster-state-20261017T030500.000000Z.zip"
    prior_archive.write_bytes(b"known good backup")

    def fail_backup(*_args, **_kwargs):
        raise OSError("sqlite backup failed")

    monkeypatch.setattr(backup_module, "_backup_sqlite", fail_backup)
    with pytest.raises(OSError, match="sqlite backup failed"):
        backup_module.create_backup(data_dir, backup_dir, now=NOW)

    assert prior_archive.read_bytes() == b"known good backup"
    assert sorted(backup_dir.glob("poolmaster-state-*.zip")) == [prior_archive]
    assert list(backup_dir.glob(".poolmaster-backup-*")) == []


def test_zip_write_failure_keeps_previous_archive_and_cleans_staging(tmp_path, monkeypatch):
    data_dir = _data_dir(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    prior_archive = backup_dir / "poolmaster-state-20261017T030500.000000Z.zip"
    prior_archive.write_bytes(b"known good backup")
    original_writestr = zipfile.ZipFile.writestr

    def fail_manifest_write(archive, name, *args, **kwargs):
        if getattr(name, "filename", name) == "manifest.json":
            raise OSError("zip manifest write failed")
        return original_writestr(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_manifest_write)
    with pytest.raises(OSError, match="zip manifest write failed"):
        backup_module.create_backup(data_dir, backup_dir, now=NOW)

    assert prior_archive.read_bytes() == b"known good backup"
    assert sorted(backup_dir.glob("poolmaster-state-*.zip")) == [prior_archive]
    assert list(backup_dir.glob(".poolmaster-backup-*")) == []


def test_archive_publish_failure_keeps_previous_archive_and_cleans_staging(tmp_path, monkeypatch):
    data_dir = _data_dir(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    prior_archive = backup_dir / "poolmaster-state-20261017T030500.000000Z.zip"
    prior_archive.write_bytes(b"known good backup")

    def fail_publish(*_args, **_kwargs):
        raise OSError("archive publish failed")

    monkeypatch.setattr(backup_module.os, "replace", fail_publish)
    with pytest.raises(OSError, match="archive publish failed"):
        backup_module.create_backup(data_dir, backup_dir, now=NOW)

    assert prior_archive.read_bytes() == b"known good backup"
    assert sorted(backup_dir.glob("poolmaster-state-*.zip")) == [prior_archive]
    assert list(backup_dir.glob(".poolmaster-backup-*")) == []


def test_backup_rejects_directory_inside_data_without_creating_it(tmp_path):
    data_dir = _data_dir(tmp_path)
    backup_dir = data_dir / "backups"

    with pytest.raises(ValueError, match="must be outside the data directory"):
        backup_module.create_backup(data_dir, backup_dir, now=NOW)

    assert not backup_dir.exists()
