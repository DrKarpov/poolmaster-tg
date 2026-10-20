from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_JSON_FILES = (
    "admin_state.json",
    "auto_push_state.json",
    "managed_destinations.json",
    "stats.json",
    "subscribers.json",
    "user_state.json",
)
HISTORY_DATABASE = "farming_history.sqlite3"
ARCHIVE_PREFIX = "poolmaster-state-"
DEFAULT_RETENTION = 30


def _prepare_backup_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise NotADirectoryError(path)
    os.chmod(path, 0o700)
    return path.resolve()


def _backup_sqlite(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise sqlite3.DatabaseError("SQLite backup failed integrity_check")
            target.commit()
        finally:
            target.close()
    finally:
        source.close()


def _zip_file_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _retained_archives(backup_dir: Path, retention: int) -> None:
    archives = sorted(
        (
            path
            for path in backup_dir.glob(f"{ARCHIVE_PREFIX}*.zip")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    for old_archive in archives[retention:]:
        try:
            old_archive.unlink()
        except OSError:
            # A retention error must not undo a completed, verified archive.
            continue


def create_backup(
    data_dir: Path,
    backup_dir: Path,
    *,
    now: datetime | None = None,
    retention: int = DEFAULT_RETENTION,
) -> Path:
    """Create a private archive of allowlisted durable state files."""
    if retention < 1:
        raise ValueError("retention must be at least one archive")
    data_path = Path(data_dir).resolve()
    if not data_path.is_dir():
        raise NotADirectoryError(data_path)

    requested_backup_dir = Path(backup_dir).resolve()
    if requested_backup_dir == data_path or data_path in requested_backup_dir.parents:
        raise ValueError("backup directory must be outside the data directory")
    final_dir = _prepare_backup_directory(Path(backup_dir))

    created_at = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("backup timestamp must be timezone-aware")
    created_at = created_at.astimezone(timezone.utc)
    stamp = created_at.strftime("%Y%m%dT%H%M%S.%fZ")
    archive_path = final_dir / f"{ARCHIVE_PREFIX}{stamp}-{uuid.uuid4().hex[:8]}.zip"

    stage_dir = Path(tempfile.mkdtemp(prefix=".poolmaster-backup-", dir=final_dir))
    os.chmod(stage_dir, 0o700)
    staged_archive = stage_dir / "archive.zip"
    files: dict[str, bytes | Path] = {}

    try:
        for filename in STATE_JSON_FILES:
            source_path = data_path / filename
            if not source_path.exists():
                continue
            if source_path.is_symlink() or not source_path.is_file():
                raise ValueError(f"state file is not a regular file: {filename}")
            files[filename] = source_path.read_bytes()

        database_path = data_path / HISTORY_DATABASE
        if database_path.exists():
            if database_path.is_symlink() or not database_path.is_file():
                raise ValueError(f"history database is not a regular file: {HISTORY_DATABASE}")
            database_copy = stage_dir / HISTORY_DATABASE
            _backup_sqlite(database_path, database_copy)
            files[HISTORY_DATABASE] = database_copy

        manifest: dict[str, Any] = {
            "created_at_utc": created_at.isoformat().replace("+00:00", "Z"),
            "files": {},
        }
        for name, value in files.items():
            content = value.read_bytes() if isinstance(value, Path) else value
            manifest["files"][name] = {
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }

        with zipfile.ZipFile(staged_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in files.items():
                if isinstance(value, Path):
                    with value.open("rb") as source, archive.open(_zip_file_info(name), "w") as target:
                        shutil.copyfileobj(source, target)
                else:
                    archive.writestr(_zip_file_info(name), value)
            archive.writestr(
                _zip_file_info("manifest.json"),
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )

        os.chmod(staged_archive, 0o600)
        with staged_archive.open("rb") as completed:
            os.fsync(completed.fileno())
        os.replace(staged_archive, archive_path)
        directory_descriptor = os.open(final_dir, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    _retained_archives(final_dir, retention)
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a private POOLMASTER persistent-state backup.")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--retention", type=int, default=DEFAULT_RETENTION)
    args = parser.parse_args(argv)
    archive_path = create_backup(
        args.data_dir,
        args.backup_dir,
        retention=args.retention,
    )
    print(f"Created POOLMASTER backup: {archive_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
