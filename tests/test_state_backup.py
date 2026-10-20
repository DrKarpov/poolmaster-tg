import os

import pytest

import state_backup


def test_secure_backup_copies_exact_bytes_with_private_permissions(tmp_path):
    source = tmp_path / "state.json"
    backup = tmp_path / "state.json.corrupt"
    source.write_text('{"users": {"123": {}}}', encoding="utf-8")
    source.chmod(0o644)

    result = state_backup.secure_backup_file(source, backup)

    assert result == backup
    assert backup.read_bytes() == source.read_bytes()
    assert os.stat(backup).st_mode & 0o777 == 0o600


def test_secure_backup_removes_partial_copy_and_keeps_source_on_failure(tmp_path, monkeypatch):
    source = tmp_path / "state.json"
    backup = tmp_path / "state.json.corrupt"
    original = b"source state"
    source.write_bytes(original)

    def fail_copy(_source, _destination):
        raise OSError("copy failed")

    monkeypatch.setattr(state_backup.shutil, "copyfileobj", fail_copy)

    with pytest.raises(OSError, match="copy failed"):
        state_backup.secure_backup_file(source, backup)

    assert source.read_bytes() == original
    assert not backup.exists()


def test_secure_backup_does_not_overwrite_an_existing_backup(tmp_path):
    source = tmp_path / "state.json"
    backup = tmp_path / "state.json.corrupt"
    source.write_text("original", encoding="utf-8")
    backup.write_text("older backup", encoding="utf-8")

    with pytest.raises(FileExistsError):
        state_backup.secure_backup_file(source, backup)

    assert backup.read_text(encoding="utf-8") == "older backup"
