from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from farming import PoolMetrics, SourceStatus, is_primary_farming_usable
from legacy_users import MIGRATION_KEY
from snapshot import DataSnapshot
from state_backup import secure_backup_file


ALERT_DIRECTIONS = frozenset({"up", "down", "both"})
MAX_PRICE_ALERT_PCT = Decimal("99.99")
MAX_YIELD_ALERT_PP = Decimal("10000")
MAX_DELIVERY_FINGERPRINTS = 100


def _default_user(user_id: int) -> dict[str, Any]:
    return {
        "chat_id": user_id,
        "registered": False,
        "blocked": False,
        "username": None,
        "first_name": None,
        "last_name": None,
        "first_seen_at": None,
        "last_seen_at": None,
        "blocked_at": None,
        "language": None,
        "farming_enabled": False,
        "farming_change_threshold_pp": None,
        "farming_direction": "both",
        "last_notified_yield": {},
        "price_enabled": False,
        "price_threshold_pct": None,
        "price_direction": "both",
        "price_alert_revision": 0,
        "yield_alert_revision": 0,
        "price_baseline": None,
        "price_last_alert_at": None,
        "last_alert_at": None,
        "paused_until": None,
        "delivery_fingerprints": [],
        "last_farming_push_fingerprint": None,
        "last_farming_push_at": None,
        "last_manual_farming_push_fingerprint": None,
        "last_manual_farming_push_at": None,
    }


class UserAlertStore:
    """Atomic JSON storage for private alert preferences and delivery baselines."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._needs_migration_save = False
        self._data = self._load()
        if self._needs_migration_save:
            self._save()

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 2, "users": {}, "migrations": {}}

    @staticmethod
    def _is_decimal_string(value: Any, *, positive: bool = False) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return False
        return parsed.is_finite() and (not positive or parsed > 0)

    @classmethod
    def _valid_threshold_string(cls, value: Any, maximum: Decimal) -> bool:
        if not cls._is_decimal_string(value, positive=True):
            return False
        return Decimal(value) <= maximum

    @staticmethod
    def _is_aware_timestamp(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        return parsed.tzinfo is not None

    @classmethod
    def _normalize_user_record(
        cls, key: str, record: Any, *, schema_version: int = 2
    ) -> dict[str, Any]:
        try:
            user_id = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("user state contains an invalid user ID") from exc
        if user_id <= 0 or str(user_id) != key or not isinstance(record, dict):
            raise ValueError("user state contains an invalid user record")

        merged = {**_default_user(user_id), **record}
        # A missing language field identifies a pre-language existing user.
        # Keep newly created records explicitly unset so they see onboarding.
        if "language" not in record:
            merged["language"] = "ru"
        if merged.get("language") is not None and merged.get("language") not in ("ru", "en"):
            merged["language"] = "ru"
        if (
            isinstance(merged.get("chat_id"), bool)
            or not isinstance(merged.get("chat_id"), int)
            or merged["chat_id"] != user_id
        ):
            raise ValueError("user state chat ID does not match its key")
        if type(merged.get("registered")) is not bool or type(merged.get("blocked")) is not bool:
            raise ValueError("private user registry flags are invalid")
        for field in ("username", "first_name", "last_name"):
            if merged.get(field) is not None and not isinstance(merged.get(field), str):
                raise ValueError(f"private user {field} is invalid")
        if type(merged.get("farming_enabled")) is not bool or type(merged.get("price_enabled")) is not bool:
            raise ValueError("user state alert switches must be booleans")
        if merged.get("farming_change_threshold_pp") is not None and not cls._valid_threshold_string(
            merged["farming_change_threshold_pp"], MAX_YIELD_ALERT_PP
        ):
            raise ValueError("user state farming threshold is invalid")
        if merged.get("price_threshold_pct") is not None and not cls._valid_threshold_string(
            merged["price_threshold_pct"], MAX_PRICE_ALERT_PCT
        ):
            raise ValueError("user state price threshold is invalid")
        for field in ("farming_direction", "price_direction"):
            if merged.get(field) not in ALERT_DIRECTIONS:
                raise ValueError(f"user state {field} is invalid")
        for field in ("price_alert_revision", "yield_alert_revision"):
            revision = merged.get(field)
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise ValueError(f"user state {field} is invalid")
        if schema_version == 1:
            legacy_baselines = merged.get("last_notified_apr", {})
            if not isinstance(legacy_baselines, dict) or any(
                not isinstance(pool_id, str) or not cls._is_decimal_string(value)
                for pool_id, value in legacy_baselines.items()
            ):
                raise ValueError("legacy user state APR baselines are invalid")
            merged["last_notified_yield"] = {}
            merged.pop("last_notified_apr", None)
        else:
            yield_baselines = merged.get("last_notified_yield")
            if not isinstance(yield_baselines, dict) or any(
                not isinstance(pool_id, str)
                or not isinstance(baseline, dict)
                or not cls._is_decimal_string(baseline.get("value"))
                or baseline.get("kind") not in {"APR", "APY"}
                or not isinstance(baseline.get("source_field"), str)
                or not baseline.get("source_field")
                for pool_id, baseline in yield_baselines.items()
            ):
                raise ValueError("user state yield baselines are invalid")
            merged.pop("last_notified_apr", None)
        if merged.get("price_baseline") is not None and not cls._is_decimal_string(
            merged["price_baseline"], positive=True
        ):
            raise ValueError("user state price baseline is invalid")
        for field in (
            "first_seen_at",
            "last_seen_at",
            "blocked_at",
            "paused_until",
            "price_last_alert_at",
            "last_alert_at",
            "last_farming_push_at",
            "last_manual_farming_push_at",
        ):
            timestamp = merged.get(field)
            if timestamp is not None and not cls._is_aware_timestamp(timestamp):
                raise ValueError(f"user state {field} timestamp is invalid")
        fingerprints = merged.get("delivery_fingerprints")
        if not isinstance(fingerprints, list) or any(not isinstance(item, str) for item in fingerprints):
            raise ValueError("user state delivery fingerprints are invalid")
        if merged.get("last_farming_push_fingerprint") is not None and not isinstance(
            merged["last_farming_push_fingerprint"], str
        ):
            raise ValueError("user state farming fingerprint is invalid")
        if merged.get("last_manual_farming_push_fingerprint") is not None and not isinstance(
            merged["last_manual_farming_push_fingerprint"], str
        ):
            raise ValueError("user state manual farming fingerprint is invalid")
        return merged

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._backup_corrupt_file(self.path)
            logging.exception("User alert state is unreadable; preserving the original and using empty state")
            return self._empty()

        try:
            if not isinstance(payload, dict) or not isinstance(payload.get("users", {}), dict):
                raise ValueError("user state root or users collection is invalid")
            if not isinstance(payload.get("migrations", {}), dict):
                raise ValueError("user state migrations collection is invalid")
            schema_version = payload.get("schema_version", 1)
            if schema_version not in {1, 2}:
                raise ValueError("user state schema version is unsupported")
            if schema_version == 1:
                backup_path = self.path.with_name(
                    f"{self.path.name}.migration-{self._timestamp()}.json"
                )
                secure_backup_file(self.path, backup_path)
            users = {
                key: self._normalize_user_record(key, record, schema_version=schema_version)
                for key, record in payload.get("users", {}).items()
            }
            migrations = payload.get("migrations", {})
            if any(not isinstance(record, dict) for record in migrations.values()):
                raise ValueError("user state migration record is invalid")
            if schema_version == 1:
                migrations = dict(migrations)
                migrations["primary_yield_baselines_v2"] = {
                    "status": "complete",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "backup_path": str(backup_path),
                }
                self._needs_migration_save = True
            return {
                "schema_version": 2,
                "users": users,
                "migrations": migrations,
            }
        except (TypeError, ValueError):
            self._backup_corrupt_file(self.path)
            logging.exception("User alert state is invalid; preserving the original and using empty state")
            return self._empty()

    @staticmethod
    def _timestamp(now: datetime | None = None) -> str:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("backup and state timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    @classmethod
    def _backup_corrupt_file(cls, path: Path) -> Path | None:
        if not path.exists():
            return None
        backup = path.with_name(f"{path.name}.corrupt-{cls._timestamp()}")
        try:
            secure_backup_file(path, backup)
            return backup
        except OSError:
            logging.exception("Could not preserve corrupt state file %s", path.name)
            raise

    @staticmethod
    def _copy_json(value: Any) -> Any:
        return deepcopy(value)

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
                logging.warning("Could not fsync user state directory %s", self.path.parent)
        finally:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                logging.warning("Could not remove temporary user state file")

    def load_users(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._copy_json(self._data["users"])

    def list_user_ids(self) -> list[int]:
        """Return registered private users, including users currently blocked."""
        return self.list_registered_user_ids()

    def list_registered_user_ids(self) -> list[int]:
        with self._lock:
            return sorted(
                int(user_id)
                for user_id, record in self._data["users"].items()
                if record.get("registered") is True
            )

    def list_active_user_ids(self) -> list[int]:
        with self._lock:
            return sorted(
                int(user_id)
                for user_id, record in self._data["users"].items()
                if record.get("registered") is True and record.get("blocked") is not True
            )

    def get_user(self, user_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            record = self._data["users"].get(str(user_id))
            if record is not None and self._clear_expired_pause_locked(record, now=now):
                self._save()
            return self._copy_json(record) if record is not None else _default_user(user_id)

    def ensure_user(self, user_id: int) -> dict[str, Any]:
        if user_id <= 0:
            raise ValueError("private user ID must be positive")
        with self._lock:
            key = str(user_id)
            if key not in self._data["users"]:
                self._data["users"][key] = _default_user(user_id)
                self._save()
            return self._copy_json(self._data["users"][key])

    def set_language(self, user_id: int, language: str) -> dict[str, Any]:
        if language not in ("ru", "en"):
            raise ValueError("unsupported user language")
        with self._lock:
            user = self._get_or_create_locked(user_id)
            user["language"] = language
            self._save()
            return self._copy_json(user)

    @staticmethod
    def _user_observation_timestamp(observed_at: datetime | None) -> str:
        value = observed_at or datetime.now(timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("private user timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()

    def register_private_user(
        self,
        user: Any,
        *,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        user_id = getattr(user, "id", None)
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("only a positive private user ID can be registered")
        timestamp = self._user_observation_timestamp(observed_at)
        with self._lock:
            record = self._get_or_create_locked(user_id)
            record.update({
                "registered": True,
                "blocked": False,
                "username": getattr(user, "username", None),
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "first_seen_at": record.get("first_seen_at") or timestamp,
                "last_seen_at": timestamp,
            })
            self._save()
            return self._copy_json(record)

    def reactivate_user(
        self,
        user: Any,
        *,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Re-register a user after a private interaction, retaining history."""
        return self.register_private_user(user, observed_at=observed_at)

    def mark_blocked(self, user_id: int, *, observed_at: datetime | None = None) -> None:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("only a positive private user ID can be blocked")
        timestamp = self._user_observation_timestamp(observed_at)
        with self._lock:
            record = self._get_or_create_locked(user_id)
            record["blocked"] = True
            record["blocked_at"] = timestamp
            self._save()

    def migrate_legacy_subscribers(self, source_path: str | Path, *, now: datetime | None = None) -> int:
        return self._migrate_legacy_private_users(source_path, None, now=now, migration_key="legacy_subscribers_v1")

    def migrate_legacy_private_users(
        self,
        subscribers_path: str | Path,
        stats_path: str | Path | None,
        *,
        now: datetime | None = None,
    ) -> int:
        return self._migrate_legacy_private_users(
            subscribers_path,
            stats_path,
            now=now,
            migration_key="private_user_registry_v2",
        )

    def _migrate_legacy_private_users(
        self,
        subscribers_path: str | Path | None,
        stats_path: str | Path | None,
        *,
        now: datetime | None,
        migration_key: str,
    ) -> int:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("migration timestamp must be timezone-aware")
        timestamp = current.astimezone(timezone.utc).isoformat()
        sources = {
            "subscribers": Path(subscribers_path) if subscribers_path is not None else None,
            "stats": Path(stats_path) if stats_path is not None else None,
        }
        with self._lock:
            if migration_key in self._data["migrations"]:
                return 0
            backups: dict[str, str] = {}
            for name, source in sources.items():
                if source is None or not source.exists():
                    continue
                backup_path = source.with_name(f"{source.name}.backup-{self._timestamp(current)}.json")
                try:
                    secure_backup_file(source, backup_path)
                except OSError:
                    logging.exception("Could not back up legacy %s state; migration skipped", name)
                    raise
                backups[name] = str(backup_path)

            imported: set[int] = set()
            status_by_source: dict[str, str] = {}
            subscribers_source = sources["subscribers"]
            if subscribers_source is not None and subscribers_source.exists():
                try:
                    payload = json.loads(subscribers_source.read_text(encoding="utf-8"))
                    legacy_ids = payload.get("subscribers") if isinstance(payload, dict) else None
                    if not isinstance(legacy_ids, list):
                        raise ValueError("legacy subscribers list is missing or invalid")
                    for value in legacy_ids:
                        if isinstance(value, bool):
                            continue
                        try:
                            user_id = int(value)
                        except (TypeError, ValueError):
                            continue
                        if user_id > 0:
                            record = self._get_or_create_locked(user_id)
                            record["registered"] = True
                            record["first_seen_at"] = record.get("first_seen_at") or timestamp
                            record["last_seen_at"] = record.get("last_seen_at") or timestamp
                            imported.add(user_id)
                    status_by_source["subscribers"] = "complete"
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    logging.exception("Legacy subscriber state is invalid; backup preserved")
                    status_by_source["subscribers"] = "invalid"
            elif subscribers_source is not None:
                status_by_source["subscribers"] = "missing"

            stats_source = sources["stats"]
            if stats_source is not None and stats_source.exists():
                try:
                    payload = json.loads(stats_source.read_text(encoding="utf-8"))
                    legacy_users = payload.get("users", {}) if isinstance(payload, dict) else None
                    if not isinstance(legacy_users, dict):
                        raise ValueError("legacy command stats users are invalid")
                    for key, old_record in legacy_users.items():
                        if not isinstance(old_record, dict) or old_record.get("chat_type") != "private":
                            continue
                        try:
                            user_id = int(old_record.get("id", key))
                            chat_id = int(old_record.get("chat_id", user_id))
                        except (TypeError, ValueError):
                            continue
                        if user_id <= 0 or chat_id != user_id:
                            continue
                        record = self._get_or_create_locked(user_id)
                        record["registered"] = True
                        for field in ("username", "first_name", "last_name"):
                            if field in old_record:
                                value = old_record.get(field)
                                record[field] = value if isinstance(value, str) or value is None else record.get(field)
                        old_first = old_record.get("first_seen_at")
                        old_last = old_record.get("last_seen_at")
                        if self._is_aware_timestamp(old_first):
                            existing_first = record.get("first_seen_at")
                            if not self._is_aware_timestamp(existing_first) or datetime.fromisoformat(old_first) < datetime.fromisoformat(existing_first):
                                record["first_seen_at"] = datetime.fromisoformat(old_first).astimezone(timezone.utc).isoformat()
                        if self._is_aware_timestamp(old_last):
                            existing_last = record.get("last_seen_at")
                            if not self._is_aware_timestamp(existing_last) or datetime.fromisoformat(old_last) > datetime.fromisoformat(existing_last):
                                record["last_seen_at"] = datetime.fromisoformat(old_last).astimezone(timezone.utc).isoformat()
                        record["first_seen_at"] = record.get("first_seen_at") or timestamp
                        record["last_seen_at"] = record.get("last_seen_at") or timestamp
                        imported.add(user_id)
                    status_by_source["stats"] = "complete"
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    logging.exception("Legacy command stats state is invalid; backup preserved")
                    status_by_source["stats"] = "invalid"
            elif stats_source is not None:
                status_by_source["stats"] = "missing"

            # Legacy subscriber/stat records belong to people who used the bot
            # before language onboarding. Preserve their Russian default.
            for user_id in imported:
                record = self._data["users"].get(str(user_id))
                if record is not None and record.get("language") is None:
                    record["language"] = "ru"

            self._data["migrations"][migration_key] = {
                "status": "complete",
                "completed_at": timestamp,
                "migrated_count": len(imported),
                "backup_paths": backups,
                "source_status": status_by_source,
            }
            self._save()
            return len(imported)

    @staticmethod
    def _merge_legacy_seen_at(current_value: str | None, legacy_value: str | None, *, earliest: bool) -> str | None:
        values = [
            datetime.fromisoformat(value).astimezone(timezone.utc)
            for value in (current_value, legacy_value)
            if isinstance(value, str) and UserAlertStore._is_aware_timestamp(value)
        ]
        if not values:
            return None
        return (min(values) if earliest else max(values)).isoformat()

    def merge_legacy_private_users(
        self,
        private_users: Sequence[Mapping[str, Any]],
        *,
        migration_metadata: Mapping[str, Any],
        now: datetime | None = None,
    ) -> int:
        """Merge verified private legacy records without replacing user preferences."""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("migration timestamp must be timezone-aware")
        completed_at = current_time.astimezone(timezone.utc).isoformat()

        source_sha256 = migration_metadata.get("source_sha256")
        if not isinstance(source_sha256, str) or len(source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in source_sha256
        ):
            raise ValueError("legacy migration source hash is invalid")
        source_mtime = migration_metadata.get("source_mtime_utc")
        if not self._is_aware_timestamp(source_mtime):
            raise ValueError("legacy migration source timestamp is invalid")
        count_fields = (
            "source_record_count",
            "private_candidate_count",
            "group_only_ignored",
            "invalid_records",
            "duplicates",
            "overlaps",
            "metadata_conflicts",
        )
        counts: dict[str, int] = {}
        for field_name in count_fields:
            value = migration_metadata.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"legacy migration {field_name} is invalid")
            counts[field_name] = value
        if counts["private_candidate_count"] != len(private_users):
            raise ValueError("legacy migration private candidate count does not match the import plan")

        normalized_users: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for raw_user in private_users:
            user_id = raw_user.get("user_id")
            if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
                raise ValueError("legacy private user ID is invalid")
            chat_id = raw_user.get("chat_id")
            if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id != user_id:
                raise ValueError("legacy private user chat ID does not match its user ID")
            if user_id in seen_ids:
                raise ValueError("legacy import plan contains duplicate private users")
            seen_ids.add(user_id)
            normalized: dict[str, Any] = {"user_id": user_id, "chat_id": user_id}
            for field_name in ("username", "first_name", "last_name"):
                value = raw_user.get(field_name)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"legacy private user {field_name} is invalid")
                normalized[field_name] = value
            for field_name in ("first_seen_at", "last_seen_at"):
                value = raw_user.get(field_name)
                if value is not None and not self._is_aware_timestamp(value):
                    raise ValueError(f"legacy private user {field_name} is invalid")
                normalized[field_name] = value
            normalized_users.append(normalized)

        with self._lock:
            previous = self._copy_json(self._data)
            prior_migration = self._data["migrations"].get(MIGRATION_KEY)
            if prior_migration is not None:
                if prior_migration.get("source_sha256") == source_sha256:
                    return 0
                raise ValueError("legacy user registry migration key was already used for another source")

            imported = 0
            try:
                for legacy_user in normalized_users:
                    user_id = legacy_user["user_id"]
                    key = str(user_id)
                    is_new_record = key not in self._data["users"]
                    user = self._get_or_create_locked(user_id)
                    if user.get("registered") is not True:
                        imported += 1
                    user["registered"] = True
                    if is_new_record:
                        user["blocked"] = False
                        user["language"] = "ru"

                    for field_name in ("username", "first_name", "last_name"):
                        if user.get(field_name) is None and legacy_user.get(field_name) is not None:
                            user[field_name] = legacy_user[field_name]

                    first_seen = self._merge_legacy_seen_at(
                        user.get("first_seen_at"), legacy_user.get("first_seen_at"), earliest=True
                    )
                    last_seen = self._merge_legacy_seen_at(
                        user.get("last_seen_at"), legacy_user.get("last_seen_at"), earliest=False
                    )
                    user["first_seen_at"] = first_seen or completed_at
                    user["last_seen_at"] = last_seen or completed_at

                self._data["migrations"][MIGRATION_KEY] = {
                    "status": "complete",
                    "completed_at": completed_at,
                    "source_sha256": source_sha256,
                    "source_mtime_utc": datetime.fromisoformat(source_mtime.replace("Z", "+00:00"))
                    .astimezone(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    **counts,
                    "imported_count": imported,
                }
                self._save()
            except Exception:
                self._data = previous
                raise
        return imported

    def set_farming_threshold(
        self,
        user_id: int,
        threshold_pp: Decimal | None,
        snapshot: DataSnapshot | None = None,
        *,
        enabled: bool = True,
    ) -> dict[str, Any]:
        threshold = self._validate_threshold(threshold_pp, MAX_YIELD_ALERT_PP, "yield threshold")
        with self._lock:
            user = self._get_or_create_locked(user_id)
            user["farming_change_threshold_pp"] = str(threshold) if threshold is not None else None
            user["farming_enabled"] = bool(enabled and threshold is not None)
            user["yield_alert_revision"] += 1
            user["last_notified_yield"] = {}
            self._seed_yield_baseline(user, snapshot)
            self._save()
            return self._copy_json(user)

    def set_price_threshold(
        self,
        user_id: int,
        threshold_pct: Decimal | None,
        snapshot: DataSnapshot | None = None,
        *,
        enabled: bool = True,
    ) -> dict[str, Any]:
        threshold = self._validate_threshold(threshold_pct, MAX_PRICE_ALERT_PCT, "price threshold")
        with self._lock:
            user = self._get_or_create_locked(user_id)
            user["price_threshold_pct"] = str(threshold) if threshold is not None else None
            user["price_enabled"] = bool(enabled and threshold is not None)
            user["price_alert_revision"] += 1
            user["price_baseline"] = None
            self._seed_price_baseline(user, snapshot)
            self._save()
            return self._copy_json(user)

    def set_alert_enabled(
        self,
        user_id: int,
        kind: str,
        enabled: bool,
        snapshot: DataSnapshot | None = None,
    ) -> dict[str, Any]:
        if kind not in {"price", "yield"}:
            raise ValueError("unknown alert kind")
        with self._lock:
            user = self._get_or_create_locked(user_id)
            threshold_field = "price_threshold_pct" if kind == "price" else "farming_change_threshold_pp"
            enabled_field = "price_enabled" if kind == "price" else "farming_enabled"
            if enabled and user.get(threshold_field) is None:
                raise ValueError("an alert threshold must be configured before enabling")
            revision_field = "price_alert_revision" if kind == "price" else "yield_alert_revision"
            user[revision_field] += 1
            user[enabled_field] = enabled
            if enabled:
                if kind == "price":
                    user["price_baseline"] = None
                    self._seed_price_baseline(user, snapshot)
                else:
                    user["last_notified_yield"] = {}
                    self._seed_yield_baseline(user, snapshot)
            self._save()
            return self._copy_json(user)

    def set_alert_direction(
        self,
        user_id: int,
        kind: str,
        direction: str,
        snapshot: DataSnapshot | None = None,
    ) -> dict[str, Any]:
        if kind not in {"price", "yield"} or direction not in ALERT_DIRECTIONS:
            raise ValueError("invalid alert direction")
        with self._lock:
            user = self._get_or_create_locked(user_id)
            field = "price_direction" if kind == "price" else "farming_direction"
            revision_field = "price_alert_revision" if kind == "price" else "yield_alert_revision"
            user[revision_field] += 1
            user[field] = direction
            if kind == "price":
                user["price_baseline"] = None
                self._seed_price_baseline(user, snapshot)
            else:
                user["last_notified_yield"] = {}
                self._seed_yield_baseline(user, snapshot)
            self._save()
            return self._copy_json(user)

    def seed_missing_baselines(
        self,
        user_id: int,
        snapshot: DataSnapshot,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            user = self._get_or_create_locked(user_id)
            before_yield = self._copy_json(user.get("last_notified_yield", {}))
            before_price = user.get("price_baseline")
            pause_cleared = self._clear_expired_pause_locked(user, now=now)
            self._seed_yield_baseline(user, snapshot)
            self._seed_price_baseline(user, snapshot)
            if (
                pause_cleared
                or before_yield != user.get("last_notified_yield", {})
                or before_price != user.get("price_baseline")
            ):
                self._save()
            return self._copy_json(user)

    def seed_all_baselines(self, snapshot: DataSnapshot) -> None:
        for user_id in self.list_user_ids():
            self.seed_missing_baselines(user_id, snapshot)

    @staticmethod
    def _validate_threshold(value: Decimal | None, maximum: Decimal, label: str) -> Decimal | None:
        if value is None:
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"invalid {label}") from exc
        if not parsed.is_finite() or parsed <= 0 or parsed > maximum:
            raise ValueError(f"unsupported {label}")
        return parsed

    def _get_or_create_locked(self, user_id: int) -> dict[str, Any]:
        if user_id <= 0:
            raise ValueError("private user ID must be positive")
        key = str(user_id)
        return self._data["users"].setdefault(key, _default_user(user_id))

    @staticmethod
    def _valid_primary_yield(metrics: PoolMetrics, snapshot: DataSnapshot, pool_id: str) -> bool:
        return is_primary_farming_usable(
            metrics,
            anomaly=f"farming:{pool_id}" in snapshot.anomalies,
        )

    @classmethod
    def _seed_yield_baseline(cls, user: dict[str, Any], snapshot: DataSnapshot | None) -> None:
        if snapshot is None:
            return
        baselines = user.setdefault("last_notified_yield", {})
        for pool_id, metrics in snapshot.farming.items():
            metric = metrics.primary_farm_yield
            if metric is None or not cls._valid_primary_yield(metrics, snapshot, pool_id):
                continue
            baseline = baselines.get(pool_id)
            if (
                not isinstance(baseline, dict)
                or baseline.get("kind") != metric.kind
                or baseline.get("source_field") != metric.source_field
                or not cls._is_decimal_string(baseline.get("value"))
            ):
                baselines[pool_id] = {
                    "value": str(metric.value_percent),
                    "kind": metric.kind,
                    "source_field": metric.source_field,
                }

    @staticmethod
    def _seed_price_baseline(user: dict[str, Any], snapshot: DataSnapshot | None) -> None:
        if (
            snapshot is not None
            and user.get("price_baseline") is None
            and snapshot.price.status == SourceStatus.OK
            and not snapshot.price.is_stale
            and snapshot.price.price_usdt is not None
        ):
            user["price_baseline"] = str(snapshot.price.price_usdt)

    def pause_user(self, user_id: int, *, now: datetime | None = None) -> datetime:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("pause timestamp must be timezone-aware")
        paused_until = current.astimezone(timezone.utc) + timedelta(hours=24)
        with self._lock:
            user = self._get_or_create_locked(user_id)
            user["paused_until"] = paused_until.isoformat()
            self._save()
        return paused_until

    def resume_user(self, user_id: int) -> dict[str, Any]:
        with self._lock:
            user = self._get_or_create_locked(user_id)
            user["paused_until"] = None
            self._save()
            return self._copy_json(user)

    def is_user_paused(self, user: dict[str, Any], *, now: datetime | None = None) -> bool:
        paused_until = user.get("paused_until")
        if not paused_until:
            return False
        try:
            value = datetime.fromisoformat(str(paused_until))
        except ValueError:
            return False
        current = now or datetime.now(timezone.utc)
        if value.tzinfo is None or current.tzinfo is None:
            return False
        return current.astimezone(timezone.utc) < value.astimezone(timezone.utc)

    @staticmethod
    def _clear_expired_pause_locked(user: dict[str, Any], *, now: datetime | None = None) -> bool:
        raw = user.get("paused_until")
        if not raw:
            return False
        current = now or datetime.now(timezone.utc)
        try:
            expiry = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            expiry = None
        if (
            current.tzinfo is None
            or expiry is None
            or expiry.tzinfo is None
            or current.astimezone(timezone.utc) >= expiry.astimezone(timezone.utc)
        ):
            user["paused_until"] = None
            return True
        return False

    def has_fingerprint(self, user_id: int, fingerprint: str) -> bool:
        return fingerprint in self.get_user(user_id).get("delivery_fingerprints", [])

    def alert_revision_matches(self, user_id: int, kind: str, revision: int) -> bool:
        if kind not in {"price", "yield"}:
            return False
        field = "price_alert_revision" if kind == "price" else "yield_alert_revision"
        return self.get_user(user_id).get(field) == revision

    def record_yield_delivery(
        self,
        user_id: int,
        pool_id: str,
        value: Decimal,
        metric_kind: str,
        source_field: str,
        fingerprint: str,
        *,
        sent_at: datetime | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        timestamp = sent_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or metric_kind not in {"APR", "APY"} or not source_field:
            raise ValueError("yield delivery requires a source metric and aware timestamp")
        with self._lock:
            user = self._get_or_create_locked(user_id)
            if expected_revision is not None and user.get("yield_alert_revision") != expected_revision:
                return False
            if fingerprint in user.get("delivery_fingerprints", []):
                return False
            user.setdefault("last_notified_yield", {})[pool_id] = {
                "value": str(value),
                "kind": metric_kind,
                "source_field": source_field,
            }
            user["last_farming_push_fingerprint"] = fingerprint
            user["last_farming_push_at"] = timestamp.astimezone(timezone.utc).isoformat()
            user["last_alert_at"] = timestamp.astimezone(timezone.utc).isoformat()
            self._remember_fingerprint(user, fingerprint)
            self._save()
            return True

    def record_price_delivery(
        self,
        user_id: int,
        value: Decimal,
        fingerprint: str,
        sent_at: datetime,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        if sent_at.tzinfo is None:
            raise ValueError("delivery timestamp must be timezone-aware")
        with self._lock:
            user = self._get_or_create_locked(user_id)
            if expected_revision is not None and user.get("price_alert_revision") != expected_revision:
                return False
            if fingerprint in user.get("delivery_fingerprints", []):
                return False
            user["price_baseline"] = str(value)
            user["price_last_alert_at"] = sent_at.astimezone(timezone.utc).isoformat()
            user["last_alert_at"] = sent_at.astimezone(timezone.utc).isoformat()
            self._remember_fingerprint(user, fingerprint)
            self._save()
            return True

    def has_manual_farming_push(self, user_id: int, fingerprint: str) -> bool:
        return self.get_user(user_id).get("last_manual_farming_push_fingerprint") == fingerprint

    def record_manual_farming_push(self, user_id: int, fingerprint: str, sent_at: datetime) -> None:
        if sent_at.tzinfo is None or not fingerprint:
            raise ValueError("manual farming push requires a fingerprint and aware timestamp")
        with self._lock:
            self._get_or_create_locked(user_id)
            previous = self._copy_json(self._data)
            user = self._data["users"][str(user_id)]
            user["last_manual_farming_push_fingerprint"] = fingerprint
            user["last_manual_farming_push_at"] = sent_at.astimezone(timezone.utc).isoformat()
            try:
                self._save()
            except Exception:
                self._data = previous
                raise

    @staticmethod
    def _remember_fingerprint(user: dict[str, Any], fingerprint: str) -> None:
        values = list(user.get("delivery_fingerprints", []))
        values.append(fingerprint)
        user["delivery_fingerprints"] = values[-MAX_DELIVERY_FINGERPRINTS:]
