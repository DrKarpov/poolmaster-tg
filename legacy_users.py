from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


MIGRATION_KEY = "legacy_poolmaster_user_registry_v1"
_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")
_IDENTITY_FIELDS = ("username", "first_name", "last_name")


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value.strip()):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _aware_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("legacy timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("legacy timestamp is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("legacy timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_record(key: str, raw: Any) -> tuple[int | None, dict[str, Any] | None, bool]:
    if not isinstance(raw, dict):
        return None, None, True

    raw_user_id = raw.get("id", raw.get("user_id", key))
    user_id = _integer(raw_user_id)
    if user_id is None or user_id <= 0:
        return user_id, None, True

    invalid = False
    chat_id: int | None = None
    if "chat_id" in raw:
        chat_id = _integer(raw.get("chat_id"))
        if chat_id is None:
            invalid = True

    has_chat_type = "chat_type" in raw
    chat_type = raw.get("chat_type")
    if has_chat_type and not isinstance(chat_type, str):
        invalid = True
    elif isinstance(chat_type, str):
        chat_type = chat_type.strip().lower()
        if not chat_type:
            invalid = True

    identity: dict[str, str | None] = {}
    for field_name in _IDENTITY_FIELDS:
        value = raw.get(field_name)
        if value is not None and not isinstance(value, str):
            invalid = True
        identity[field_name] = value if isinstance(value, str) else None

    try:
        first_seen = _aware_timestamp(raw.get("first_seen_at", raw.get("first_seen")))
        last_seen = _aware_timestamp(raw.get("last_seen_at", raw.get("last_seen")))
    except ValueError:
        invalid = True
        first_seen = None
        last_seen = None

    if chat_type == "private":
        if chat_id is not None and chat_id != user_id:
            invalid = True
        is_private = chat_id is None or chat_id == user_id
    elif not has_chat_type:
        # Older direct-message records sometimes omitted chat_type. Only a
        # positive self-chat ID proves that the record was private.
        is_private = chat_id == user_id
    else:
        # Explicit groups, channels, and unknown chat kinds are never promoted.
        is_private = False

    if invalid:
        return user_id, None, True
    if not is_private:
        return user_id, None, False

    return user_id, {
        "user_id": user_id,
        "chat_id": user_id,
        **identity,
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
    }, False


@dataclass(frozen=True)
class LegacyImportReport:
    source_sha256: str
    source_mtime_utc: str
    source_records: int
    private_candidates: int
    group_only_ignored: int
    invalid_records: int
    duplicates: int
    overlaps: int
    new_private_users: int
    metadata_conflicts: int
    expected_final_private_registry_count: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "migration_key": MIGRATION_KEY,
            "source_sha256": self.source_sha256,
            "source_mtime_utc": self.source_mtime_utc,
            "source_records": self.source_records,
            "private_candidates": self.private_candidates,
            "group_only_ignored": self.group_only_ignored,
            "invalid_records": self.invalid_records,
            "duplicates": self.duplicates,
            "overlaps": self.overlaps,
            "new_private_users": self.new_private_users,
            "metadata_conflicts": self.metadata_conflicts,
            "expected_final_private_registry_count": self.expected_final_private_registry_count,
        }


@dataclass(frozen=True)
class LegacyUserImportPlan:
    report: LegacyImportReport
    private_users: tuple[dict[str, Any], ...] = field(repr=False)

    def migration_metadata(self) -> dict[str, Any]:
        return {
            "source_sha256": self.report.source_sha256,
            "source_mtime_utc": self.report.source_mtime_utc,
            "source_record_count": self.report.source_records,
            "private_candidate_count": self.report.private_candidates,
            "group_only_ignored": self.report.group_only_ignored,
            "invalid_records": self.report.invalid_records,
            "duplicates": self.report.duplicates,
            "overlaps": self.report.overlaps,
            "metadata_conflicts": self.report.metadata_conflicts,
        }


def build_import_plan(
    source_path: str | Path,
    current_users: Mapping[str, Any],
) -> LegacyUserImportPlan:
    """Build a pure import preview; only the report contains aggregate output."""
    path = Path(source_path)
    source_bytes = path.read_bytes()
    try:
        payload = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy user source is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("users"), dict):
        raise ValueError("legacy user source must contain a users object")

    parsed_rows: list[tuple[int | None, dict[str, Any] | None, bool]] = [
        _parse_record(str(key), raw) for key, raw in payload["users"].items()
    ]
    records_by_id: dict[int, list[tuple[dict[str, Any] | None, bool]]] = {}
    invalid_records = 0
    for user_id, private_record, invalid in parsed_rows:
        if invalid:
            invalid_records += 1
        if user_id is not None and user_id > 0:
            records_by_id.setdefault(user_id, []).append((private_record, invalid))

    duplicates = 0
    private_users: list[dict[str, Any]] = []
    group_only_ignored = 0
    for user_id, rows in records_by_id.items():
        if len(rows) > 1:
            duplicates += len(rows) - 1
            continue
        private_record, invalid = rows[0]
        if invalid:
            continue
        if private_record is None:
            group_only_ignored += 1
        else:
            private_users.append(private_record)
    private_users.sort(key=lambda record: record["user_id"])

    current_by_id: dict[int, Mapping[str, Any]] = {}
    for key, record in current_users.items():
        user_id = _integer(key)
        if user_id is not None and user_id > 0 and isinstance(record, Mapping):
            current_by_id[user_id] = record

    candidate_ids = {record["user_id"] for record in private_users}
    current_ids = set(current_by_id)
    registered_ids = {
        user_id
        for user_id, record in current_by_id.items()
        if record.get("registered") is True
    }
    metadata_conflicts = 0
    for legacy_record in private_users:
        current_record = current_by_id.get(legacy_record["user_id"])
        if current_record is None:
            continue
        if any(
            current_record.get(field_name) is not None
            and legacy_record.get(field_name) is not None
            and current_record[field_name] != legacy_record[field_name]
            for field_name in _IDENTITY_FIELDS
        ):
            metadata_conflicts += 1

    stat = path.stat()
    source_mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    report = LegacyImportReport(
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_mtime_utc=source_mtime,
        source_records=len(payload["users"]),
        private_candidates=len(private_users),
        group_only_ignored=group_only_ignored,
        invalid_records=invalid_records,
        duplicates=duplicates,
        overlaps=len(candidate_ids & current_ids),
        new_private_users=len(candidate_ids - current_ids),
        metadata_conflicts=metadata_conflicts,
        expected_final_private_registry_count=len(registered_ids | candidate_ids),
    )
    return LegacyUserImportPlan(report=report, private_users=tuple(private_users))
