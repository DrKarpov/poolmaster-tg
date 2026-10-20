from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from state_backup import secure_backup_file


class AdminStateStore:
    """Persistent global automation switch and manual-push audit timestamp."""

    def __init__(self, path: str | Path, *, initial_paused: bool = True) -> None:
        if type(initial_paused) is not bool:
            raise ValueError("initial pause state must be a boolean")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initial_paused = initial_paused
        self._data = self._load()

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "global_auto_push_paused": self._initial_paused,
            "last_manual_push_at": None,
        }

    @staticmethod
    def _timestamp(now: datetime | None = None) -> str:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("admin state timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    @classmethod
    def _backup_corrupt_file(cls, path: Path) -> None:
        if not path.exists():
            return
        backup = path.with_name(f"{path.name}.corrupt-{cls._timestamp()}")
        try:
            secure_backup_file(path, backup)
        except OSError:
            logging.exception("Could not preserve corrupt admin state %s", path.name)
            raise

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version", 1) != 1
                or type(payload.get("global_auto_push_paused")) is not bool
            ):
                raise ValueError("admin state schema is invalid")
            timestamp = payload.get("last_manual_push_at")
            if timestamp is not None:
                if not isinstance(timestamp, str):
                    raise ValueError("manual-push timestamp is invalid")
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo is None:
                    raise ValueError("manual-push timestamp must be timezone-aware")
            return {
                "schema_version": 1,
                "global_auto_push_paused": payload["global_auto_push_paused"],
                "last_manual_push_at": timestamp,
            }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._backup_corrupt_file(self.path)
            logging.exception("Admin state is unreadable; preserving it and using configured defaults")
            return self._empty()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(self._data, ensure_ascii=False, indent=2)
        descriptor, temporary_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(encoded)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            Path(temporary_name).replace(self.path)
            try:
                directory_descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                logging.warning("Could not fsync admin state directory %s", self.path.parent)
        finally:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                logging.warning("Could not remove temporary admin state file")

    @staticmethod
    def _require_admin(actor_id: int, admin_ids: set[int] | frozenset[int]) -> None:
        if actor_id not in admin_ids:
            raise PermissionError("admin state can only be changed by configured admins")

    def is_global_auto_push_paused(self) -> bool:
        with self._lock:
            return bool(self._data["global_auto_push_paused"])

    def set_global_auto_push_paused(
        self,
        paused: bool,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
    ) -> None:
        self._require_admin(actor_id, admin_ids)
        if type(paused) is not bool:
            raise ValueError("pause state must be a boolean")
        with self._lock:
            previous = deepcopy(self._data)
            self._data["global_auto_push_paused"] = paused
            try:
                self._save()
            except Exception:
                self._data = previous
                raise

    def last_manual_push_at(self) -> datetime | None:
        with self._lock:
            raw = self._data.get("last_manual_push_at")
        return datetime.fromisoformat(raw) if raw else None

    def record_manual_push(self, sent_at: datetime) -> None:
        if sent_at.tzinfo is None:
            raise ValueError("manual push timestamp must be timezone-aware")
        with self._lock:
            previous = deepcopy(self._data)
            self._data["last_manual_push_at"] = sent_at.astimezone(timezone.utc).isoformat()
            try:
                self._save()
            except Exception:
                self._data = previous
                raise
