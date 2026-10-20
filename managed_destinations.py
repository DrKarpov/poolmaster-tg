from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram.error import Forbidden, RetryAfter, TelegramError

from farming import FARMING_POOLS, PoolMetrics, is_primary_farming_usable
from keyboards import channel_post_keyboard, group_response_keyboard
from snapshot import DataSnapshot
from state_backup import secure_backup_file
from user_ui import render_farming_text


SCHEDULE_PRESETS_HOURS = frozenset({3, 6, 12, 24})
DESTINATION_TYPES = frozenset({"group", "supergroup", "channel"})
ACTIVE_BOT_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})
KNOWN_BOT_STATUSES = ACTIVE_BOT_STATUSES | {"left", "kicked"}


@dataclass(frozen=True)
class DestinationVerification:
    chat_id: int
    name: str
    destination_type: str
    bot_status: str
    can_post_messages: bool


def destination_connection_state(destination: Mapping[str, Any]) -> str:
    """Derive publishing connection state from the stored membership fields."""
    status = destination.get("bot_status")
    if status in {"left", "kicked"}:
        return "disconnected"
    if status in ACTIVE_BOT_STATUSES and destination.get("can_post_messages") is True:
        return "connected"
    return "insufficient_rights"


def membership_can_publish(
    destination_type: str,
    membership: Any,
    *,
    existing: bool,
    chat_permissions: Any = None,
    previously_verified_can_post: bool = False,
) -> bool:
    """Evaluate effective publishing rights from Telegram membership fields."""
    status = getattr(membership, "status", None)
    if destination_type == "channel":
        return status == "creator" or (
            status == "administrator"
            and getattr(membership, "can_post_messages", False) is True
        )
    if destination_type not in {"group", "supergroup"}:
        return False
    if status in {"creator", "administrator"}:
        return True
    if status == "member":
        member_can_send = getattr(chat_permissions, "can_send_messages", None)
        if member_can_send is True:
            return existing
        return existing and member_can_send is None and previously_verified_can_post is True
    if status == "restricted":
        return (
            existing
            and getattr(membership, "is_member", False) is True
            and getattr(membership, "can_send_messages", False) is True
        )
    return False


async def verify_managed_destination(
    bot: Any,
    chat_id: int,
    bot_id: int,
    *,
    existing: bool,
    previously_verified_can_post: bool = False,
) -> DestinationVerification:
    """Verify Telegram chat identity and the bot's effective publish access."""
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id >= 0:
        raise ValueError("managed destination chat ID is invalid")
    if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id <= 0:
        raise ValueError("runtime bot ID is invalid")

    target = await bot.get_chat(chat_id)
    verified_chat_id = getattr(target, "id", None)
    destination_type = getattr(target, "type", None)
    if verified_chat_id != chat_id:
        raise ValueError("Telegram returned a different chat ID")
    if destination_type not in DESTINATION_TYPES:
        raise ValueError("Telegram returned an unsupported chat type")

    membership = await bot.get_chat_member(chat_id, bot_id)
    member_user_id = getattr(getattr(membership, "user", None), "id", None)
    if member_user_id != bot_id:
        raise ValueError("Telegram returned a different bot identity")
    status = getattr(membership, "status", None)
    if status not in KNOWN_BOT_STATUSES:
        raise ValueError("Telegram returned an unknown bot membership status")

    can_post = membership_can_publish(
        destination_type,
        membership,
        existing=existing,
        chat_permissions=getattr(target, "permissions", None),
        previously_verified_can_post=previously_verified_can_post,
    )

    name = getattr(target, "title", None) or getattr(target, "username", None) or str(chat_id)
    return DestinationVerification(
        chat_id=chat_id,
        name=str(name).strip(),
        destination_type=destination_type,
        bot_status=status,
        can_post_messages=bool(can_post),
    )


def _default_destination(chat_id: int, name: str, destination_type: str) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "name": name,
        "type": destination_type,
        "enabled": False,
        "farming_interval_hours": None,
        "last_publication_at": None,
        "last_fingerprint": None,
        "last_manual_publication_at": None,
        "last_manual_fingerprint": None,
        "last_any_publication_at": None,
        "last_any_fingerprint": None,
        "bot_status": "unknown",
        "can_post_messages": False,
        "membership_seen_at": None,
    }


class ManagedDestinationStore:
    """Persistent, explicit allow-list for managed group and channel posts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    @staticmethod
    def _timestamp(now: datetime | None = None) -> str:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("destination state timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"schema_version": 1, "destinations": {}}

    @classmethod
    def _backup_corrupt_file(cls, path: Path) -> None:
        if not path.exists():
            return
        backup = path.with_name(f"{path.name}.corrupt-{cls._timestamp()}")
        try:
            secure_backup_file(path, backup)
        except OSError:
            logging.exception("Could not preserve corrupt managed-destination state %s", path.name)
            raise

    @staticmethod
    def _valid_timestamp(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        return parsed.tzinfo is not None

    @classmethod
    def _normalize_destination(cls, key: str, record: Any) -> dict[str, Any]:
        try:
            chat_id = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("destination ID is invalid") from exc
        if chat_id >= 0 or str(chat_id) != key or not isinstance(record, dict):
            raise ValueError("destination must be a canonical non-private chat ID")
        merged = {**_default_destination(chat_id, "", ""), **record}
        if "can_post_messages" not in record:
            # Older explicitly bound destinations were validated before persistence.
            merged["can_post_messages"] = True
            merged["bot_status"] = "administrator"
        if (
            isinstance(merged.get("chat_id"), bool)
            or not isinstance(merged.get("chat_id"), int)
            or merged["chat_id"] != chat_id
        ):
            raise ValueError("destination chat ID does not match its key")
        name = merged.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError("destination name is invalid")
        if merged.get("type") not in DESTINATION_TYPES:
            raise ValueError("destination type is invalid")
        if type(merged.get("enabled")) is not bool:
            raise ValueError("destination enabled flag is invalid")
        interval = merged.get("farming_interval_hours")
        if interval is not None and (isinstance(interval, bool) or interval not in SCHEDULE_PRESETS_HOURS):
            raise ValueError("destination schedule is invalid")
        if merged["enabled"] != (interval is not None):
            raise ValueError("destination enabled flag and schedule disagree")
        if merged.get("bot_status") not in {"unknown", "creator", "administrator", "member", "restricted", "left", "kicked"}:
            raise ValueError("destination bot membership status is invalid")
        if type(merged.get("can_post_messages")) is not bool:
            raise ValueError("destination posting-right flag is invalid")
        membership_seen = merged.get("membership_seen_at")
        if membership_seen is not None and not cls._valid_timestamp(membership_seen):
            raise ValueError("destination membership timestamp is invalid")
        for field in ("last_publication_at", "last_manual_publication_at", "last_any_publication_at"):
            timestamp = merged.get(field)
            if timestamp is not None and not cls._valid_timestamp(timestamp):
                raise ValueError(f"destination {field} timestamp is invalid")
        for field in ("last_fingerprint", "last_manual_fingerprint", "last_any_fingerprint"):
            fingerprint = merged.get(field)
            if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint):
                raise ValueError(f"destination {field} fingerprint is invalid")
        return merged

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version", 1) != 1
                or not isinstance(payload.get("destinations", {}), dict)
            ):
                raise ValueError("managed-destination state schema is invalid")
            destinations = {
                key: self._normalize_destination(key, record)
                for key, record in payload.get("destinations", {}).items()
            }
            return {"schema_version": 1, "destinations": destinations}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._backup_corrupt_file(self.path)
            logging.exception("Managed-destination state is unreadable; preserving it and using an empty allow-list")
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
                logging.warning("Could not fsync managed-destination state directory %s", self.path.parent)
        finally:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                logging.warning("Could not remove temporary managed-destination state file")

    @staticmethod
    def _require_admin(actor_id: int, admin_ids: set[int] | frozenset[int]) -> None:
        if actor_id not in admin_ids:
            raise PermissionError("managed destinations can only be changed by configured admins")

    def list_destinations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                deepcopy(record)
                for _, record in sorted(self._data["destinations"].items(), key=lambda item: int(item[0]))
            ]

    def get_destination(self, chat_id: int) -> dict[str, Any] | None:
        with self._lock:
            record = self._data["destinations"].get(str(chat_id))
            return deepcopy(record) if record is not None else None

    def register_destination(
        self,
        chat_id: int,
        name: str,
        destination_type: str,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
        bot_status: str = "administrator",
        can_post_messages: bool = True,
    ) -> dict[str, Any]:
        self._require_admin(actor_id, admin_ids)
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id >= 0:
            raise ValueError("managed destination must use a non-private Telegram chat ID")
        clean_name = name.strip() if isinstance(name, str) else ""
        if not clean_name or len(clean_name) > 128:
            raise ValueError("destination name must contain 1 to 128 characters")
        if destination_type not in DESTINATION_TYPES:
            raise ValueError("destination type must be group, supergroup, or channel")
        if bot_status not in {"creator", "administrator", "member", "restricted"}:
            raise ValueError("bot must be an active member of the destination")
        if type(can_post_messages) is not bool or not can_post_messages:
            raise ValueError("bot must have permission to publish in the destination")
        key = str(chat_id)
        with self._lock:
            previous = deepcopy(self._data)
            record = self._data["destinations"].get(key)
            if record is None:
                record = _default_destination(chat_id, clean_name, destination_type)
            else:
                record = {
                    **record,
                    "name": clean_name,
                    "type": destination_type,
                }
            record["bot_status"] = bot_status
            record["can_post_messages"] = can_post_messages
            self._data["destinations"][key] = record
            try:
                self._save()
            except Exception:
                self._data = previous
                raise
            return deepcopy(record)

    def observe_bot_membership(
        self,
        *,
        chat_id: int,
        name: str,
        destination_type: str,
        bot_status: str,
        can_post_messages: bool,
        observed_at: datetime,
        force_disable_schedule: bool = False,
    ) -> dict[str, Any]:
        """Record a Bot API membership update; a newly detected chat stays disabled."""
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id >= 0:
            raise ValueError("destination must use a non-private Telegram chat ID")
        clean_name = name.strip() if isinstance(name, str) else ""
        if not clean_name or len(clean_name) > 128 or destination_type not in DESTINATION_TYPES:
            raise ValueError("destination identity is invalid")
        if bot_status not in {"unknown", "creator", "administrator", "member", "restricted", "left", "kicked"}:
            raise ValueError("bot membership status is invalid")
        if type(can_post_messages) is not bool:
            raise ValueError("posting-right flag must be boolean")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("membership timestamp must be timezone-aware")
        key = str(chat_id)
        timestamp = observed_at.astimezone(timezone.utc).isoformat()
        allowed = bot_status in ACTIVE_BOT_STATUSES and can_post_messages
        with self._lock:
            previous = deepcopy(self._data)
            record = self._data["destinations"].get(key)
            if record is None:
                record = _default_destination(chat_id, clean_name, destination_type)
            record.update({
                "name": clean_name,
                "type": destination_type,
                "bot_status": bot_status,
                "can_post_messages": can_post_messages,
                "membership_seen_at": timestamp,
            })
            if not allowed or force_disable_schedule:
                record["enabled"] = False
                record["farming_interval_hours"] = None
            self._data["destinations"][key] = record
            try:
                self._save()
            except Exception:
                self._data = previous
                raise
            return deepcopy(record)

    def disable_destination(
        self,
        chat_id: int,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
    ) -> dict[str, Any]:
        self._require_admin(actor_id, admin_ids)
        key = str(chat_id)
        with self._lock:
            record = self._data["destinations"].get(key)
            if record is None:
                raise KeyError(f"managed destination {chat_id} is not registered")
            previous = deepcopy(self._data)
            record["enabled"] = False
            record["farming_interval_hours"] = None
            try:
                self._save()
            except Exception:
                self._data = previous
                raise
            return deepcopy(record)

    def mark_posting_unavailable(self, chat_id: int) -> dict[str, Any]:
        """Fail closed after Telegram rejects a post with Forbidden."""
        key = str(chat_id)
        with self._lock:
            record = self._data["destinations"].get(key)
            if record is None:
                raise KeyError(f"managed destination {chat_id} is not registered")
            previous = deepcopy(self._data)
            record["can_post_messages"] = False
            record["enabled"] = False
            record["farming_interval_hours"] = None
            try:
                self._save()
            except Exception:
                self._data = previous
                raise
            return deepcopy(record)

    def set_schedule(
        self,
        chat_id: int,
        interval_hours: int | None,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
    ) -> dict[str, Any]:
        self._require_admin(actor_id, admin_ids)
        if interval_hours is not None and (
            isinstance(interval_hours, bool) or interval_hours not in SCHEDULE_PRESETS_HOURS
        ):
            raise ValueError("schedule must be OFF, 3, 6, 12, or 24 hours")
        key = str(chat_id)
        with self._lock:
            record = self._data["destinations"].get(key)
            if record is None:
                raise KeyError(f"managed destination {chat_id} is not registered")
            if interval_hours is not None and destination_connection_state(record) != "connected":
                raise ValueError("Бот не может публиковать в этом чате.")
            previous = deepcopy(self._data)
            record["farming_interval_hours"] = interval_hours
            record["enabled"] = interval_hours is not None
            try:
                self._save()
            except Exception:
                self._data = previous
                raise
            return deepcopy(record)

    def record_successful_publication(self, chat_id: int, fingerprint: str, published_at: datetime) -> None:
        if published_at.tzinfo is None or not fingerprint:
            raise ValueError("publication record requires a fingerprint and aware timestamp")
        key = str(chat_id)
        with self._lock:
            record = self._data["destinations"].get(key)
            if record is None:
                raise KeyError(f"managed destination {chat_id} is not registered")
            previous = deepcopy(self._data)
            record["last_fingerprint"] = fingerprint
            record["last_publication_at"] = published_at.astimezone(timezone.utc).isoformat()
            record["last_any_fingerprint"] = fingerprint
            record["last_any_publication_at"] = published_at.astimezone(timezone.utc).isoformat()
            try:
                self._save()
            except Exception:
                self._data = previous
                raise

    def record_successful_manual_publication(
        self,
        chat_id: int,
        fingerprint: str,
        published_at: datetime,
    ) -> None:
        if published_at.tzinfo is None or not fingerprint:
            raise ValueError("manual publication record requires a fingerprint and aware timestamp")
        key = str(chat_id)
        with self._lock:
            record = self._data["destinations"].get(key)
            if record is None:
                raise KeyError(f"managed destination {chat_id} is not registered")
            previous = deepcopy(self._data)
            record["last_manual_fingerprint"] = fingerprint
            record["last_manual_publication_at"] = published_at.astimezone(timezone.utc).isoformat()
            record["last_any_fingerprint"] = fingerprint
            record["last_any_publication_at"] = published_at.astimezone(timezone.utc).isoformat()
            try:
                self._save()
            except Exception:
                self._data = previous
                raise


@dataclass(frozen=True)
class PublicationSummary:
    sent: int = 0
    failed: int = 0
    skipped: int = 0


def farming_publication_card(snapshot: DataSnapshot) -> tuple[str, str] | None:
    metrics: list[PoolMetrics] = []
    for pool in FARMING_POOLS:
        item = snapshot.farming.get(pool.address)
        if (
            item is None
            or not is_primary_farming_usable(
                item,
                anomaly=f"farming:{pool.address}" in snapshot.anomalies,
            )
            or item.pool.url != pool.url
        ):
            return None
        metrics.append(item)
    card = render_farming_text(metrics, anomalies=snapshot.anomalies)
    return card, hashlib.sha256(card.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChannelPublicationPreview:
    token: str
    admin_id: int
    chat_id: int
    created_at: datetime
    snapshot_timestamp: datetime
    fingerprint: str


@dataclass(frozen=True)
class ChannelPublicationResult:
    status: str
    published: bool = False


class ChannelPublicationCoordinator:
    """One-use preview/confirm flow for a single managed publication target."""

    def __init__(self, *, preview_ttl: timedelta = timedelta(minutes=5)) -> None:
        if preview_ttl.total_seconds() <= 0:
            raise ValueError("preview TTL must be positive")
        self.preview_ttl = preview_ttl
        self._lock = threading.RLock()
        self._pending: dict[str, ChannelPublicationPreview] = {}

    @staticmethod
    def _require_admin(actor_id: int, admin_ids: set[int] | frozenset[int]) -> None:
        ManagedDestinationStore._require_admin(actor_id, admin_ids)

    @staticmethod
    def _can_publish(destination: dict[str, Any] | None) -> bool:
        return bool(destination and destination_connection_state(destination) == "connected")

    def create_preview(
        self,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
        chat_id: int,
        snapshot: DataSnapshot,
        destinations: ManagedDestinationStore,
        now: datetime,
    ) -> ChannelPublicationPreview:
        self._require_admin(actor_id, admin_ids)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("publication preview timestamp must be timezone-aware")
        destination = destinations.get_destination(chat_id)
        if not self._can_publish(destination):
            raise ValueError("У бота нет права публикации в этом канале.")
        rendered = farming_publication_card(snapshot)
        if rendered is None:
            raise ValueError("Карточка фарминга сейчас недоступна для публикации.")
        preview = ChannelPublicationPreview(
            token=secrets.token_hex(8),
            admin_id=actor_id,
            chat_id=chat_id,
            created_at=now.astimezone(timezone.utc),
            snapshot_timestamp=snapshot.fetched_at,
            fingerprint=rendered[1],
        )
        with self._lock:
            self._pending[preview.token] = preview
        return preview

    def _consume(self, token: str, actor_id: int, now: datetime) -> ChannelPublicationPreview | None:
        with self._lock:
            preview = self._pending.get(token)
            if preview is None or preview.admin_id != actor_id:
                return None
            self._pending.pop(token, None)
            if now.astimezone(timezone.utc) - preview.created_at > self.preview_ttl:
                return None
            return preview

    def cancel(
        self,
        token: str,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
        now: datetime,
    ) -> bool:
        self._require_admin(actor_id, admin_ids)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("cancel timestamp must be timezone-aware")
        return self._consume(token, actor_id, now) is not None

    async def confirm(
        self,
        token: str,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
        snapshot_provider: Callable[[], Awaitable[DataSnapshot]],
        bot: Any,
        destinations: ManagedDestinationStore,
        now: datetime,
    ) -> ChannelPublicationResult:
        self._require_admin(actor_id, admin_ids)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("publication timestamp must be timezone-aware")
        preview = self._consume(token, actor_id, now)
        if preview is None:
            return ChannelPublicationResult("invalid")
        destination = destinations.get_destination(preview.chat_id)
        if not self._can_publish(destination):
            return ChannelPublicationResult("unavailable")
        snapshot = await snapshot_provider()
        rendered = farming_publication_card(snapshot)
        if (
            rendered is None
            or snapshot.fetched_at != preview.snapshot_timestamp
            or rendered[1] != preview.fingerprint
        ):
            return ChannelPublicationResult("stale")
        try:
            await _send_with_retry(bot, destination, rendered[0])
        except Forbidden:
            try:
                destinations.mark_posting_unavailable(preview.chat_id)
            except Exception:
                logging.exception(
                    "Could not mark managed destination unavailable after a posting-right error. chat_id=%s",
                    preview.chat_id,
                )
            return ChannelPublicationResult("unavailable")
        except (RetryAfter, TelegramError):
            logging.exception("Telegram rejected a manual channel publication. chat_id=%s", preview.chat_id)
            return ChannelPublicationResult("failed")
        try:
            destinations.record_successful_manual_publication(
                preview.chat_id,
                preview.fingerprint,
                now.astimezone(timezone.utc),
            )
        except Exception:
            logging.exception("Manual channel post sent but its state could not be saved. chat_id=%s", preview.chat_id)
            return ChannelPublicationResult("failed", published=True)
        return ChannelPublicationResult("complete", published=True)


async def _send_with_retry(bot: Any, destination: dict[str, Any], text: str) -> None:
    keyboard = channel_post_keyboard() if destination["type"] == "channel" else group_response_keyboard()
    for attempt in range(2):
        try:
            await bot.send_message(
                chat_id=destination["chat_id"],
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except RetryAfter as exc:
            if attempt == 1:
                raise
            delay = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
            await asyncio.sleep(max(0.0, delay))


async def dispatch_managed_publications(
    store: ManagedDestinationStore,
    bot: Any,
    snapshot: DataSnapshot,
    *,
    now: datetime,
    global_paused: bool = False,
) -> PublicationSummary:
    if now.tzinfo is None:
        raise ValueError("publication scheduler timestamp must be timezone-aware")
    if global_paused:
        return PublicationSummary()
    destinations = [item for item in store.list_destinations() if item["enabled"]]
    if not destinations:
        return PublicationSummary()
    eligible = farming_publication_card(snapshot)
    if eligible is None:
        return PublicationSummary(skipped=len(destinations))
    card, fingerprint = eligible
    sent = failed = skipped = 0
    current = now.astimezone(timezone.utc)
    for destination in destinations:
        last_fingerprint = destination.get("last_any_fingerprint") or destination["last_fingerprint"]
        if last_fingerprint == fingerprint:
            skipped += 1
            continue
        last_publication = destination["last_publication_at"]
        interval = timedelta(hours=destination["farming_interval_hours"])
        if last_publication is not None:
            published_at = datetime.fromisoformat(last_publication).astimezone(timezone.utc)
            if current - published_at < interval:
                skipped += 1
                continue
        try:
            await _send_with_retry(bot, destination, card)
        except Forbidden:
            try:
                store.mark_posting_unavailable(destination["chat_id"])
            except Exception:
                logging.exception(
                    "Could not mark managed destination unavailable after a scheduled posting-right error. chat_id=%s",
                    destination["chat_id"],
                )
            logging.info("Managed destination blocked the bot. chat_id=%s", destination["chat_id"])
            failed += 1
            continue
        except (RetryAfter, TelegramError):
            logging.exception("Telegram rejected a managed publication. chat_id=%s", destination["chat_id"])
            failed += 1
            continue
        except Exception:
            logging.exception("Managed publication failed. chat_id=%s", destination["chat_id"])
            failed += 1
            continue
        try:
            store.record_successful_publication(destination["chat_id"], fingerprint, current)
        except Exception:
            logging.exception(
                "Managed post was sent but its publication state could not be saved. chat_id=%s",
                destination["chat_id"],
            )
            failed += 1
            continue
        sent += 1
    return PublicationSummary(sent=sent, failed=failed, skipped=skipped)
