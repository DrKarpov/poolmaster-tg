from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from legacy_users import build_import_plan
from user_state import UserAlertStore


def _read_current_users(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("current user state is not readable JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("users"), dict):
        raise ValueError("current user state does not contain a users object")
    schema_version = payload.get("schema_version", 1)
    if schema_version not in {1, 2}:
        raise ValueError("current user state schema is unsupported")
    migrations = payload.get("migrations", {})
    if not isinstance(migrations, dict) or any(not isinstance(item, dict) for item in migrations.values()):
        raise ValueError("current user state migration metadata is invalid")
    return {
        key: UserAlertStore._normalize_user_record(key, record, schema_version=schema_version)
        for key, record in payload["users"].items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely preview or merge legacy POOLMASTER private users.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--user-state", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    current_users = _read_current_users(args.user_state)
    plan = build_import_plan(args.source, current_users)
    report: dict[str, Any] = plan.report.as_dict()

    if args.dry_run:
        report["mode"] = "dry_run"
    else:
        store = UserAlertStore(args.user_state)
        report["mode"] = "apply"
        report["imported"] = store.merge_legacy_private_users(
            plan.private_users,
            migration_metadata=plan.migration_metadata(),
        )

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
