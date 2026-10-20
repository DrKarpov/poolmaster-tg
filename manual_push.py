from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from telegram.error import Forbidden, RetryAfter, TelegramError

from admin_state import AdminStateStore
from user_state import UserAlertStore


@dataclass(frozen=True)
class ManualPushPreview:
    token: str
    admin_id: int
    created_at: datetime
    source_chat_id: int
    source_message_id: int
    kind: str
    recipient_count: int


@dataclass(frozen=True)
class ManualPushResult:
    status: str
    delivered: int = 0
    blocked: int = 0
    failed: int = 0
    state_failures: int = 0


@dataclass(frozen=True)
class _ManualPushPayload:
    kind: str
    text: str | None = None
    text_entities: tuple[Any, ...] = ()
    photo_file_id: str | None = None
    caption: str | None = None
    caption_entities: tuple[Any, ...] = ()


class ManualPushCoordinator:
    """One-use admin broadcasts with in-memory payloads while preview is pending."""

    def __init__(self, *, preview_ttl: timedelta = timedelta(minutes=10)) -> None:
        if preview_ttl.total_seconds() <= 0:
            raise ValueError("preview TTL must be positive")
        self.preview_ttl = preview_ttl
        self._lock = threading.RLock()
        self._pending: dict[str, ManualPushPreview] = {}
        self._payloads: dict[str, _ManualPushPayload] = {}
        self._active_by_admin: dict[int, str] = {}
        self._awaiting_content: set[int] = set()

    @staticmethod
    def recipient_ids(
        users: UserAlertStore,
        admin_ids: set[int] | frozenset[int],
    ) -> list[int]:
        """Broadcast to registered users and every configured admin once."""
        return sorted(set(users.list_active_user_ids()) | set(admin_ids))

    @staticmethod
    def _require_admin(actor_id: int, admin_ids: set[int] | frozenset[int]) -> None:
        if actor_id not in admin_ids:
            raise PermissionError("manual broadcast requires a configured admin")

    def begin_input(self, *, actor_id: int, admin_ids: set[int] | frozenset[int]) -> None:
        self._require_admin(actor_id, admin_ids)
        with self._lock:
            previous = self._active_by_admin.pop(actor_id, None)
            if previous:
                self._pending.pop(previous, None)
                self._payloads.pop(previous, None)
            self._awaiting_content.add(actor_id)

    def is_waiting_for_content(self, actor_id: int) -> bool:
        with self._lock:
            return actor_id in self._awaiting_content

    def cancel_input(self, actor_id: int, *, admin_ids: set[int] | frozenset[int]) -> None:
        self._require_admin(actor_id, admin_ids)
        with self._lock:
            self._awaiting_content.discard(actor_id)

    def create_preview(
        self,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
        source_chat_id: int,
        source_message_id: int,
        kind: str,
        text: str | None,
        caption: str | None,
        photo_file_id: str | None = None,
        text_entities: Sequence[Any] | None = None,
        caption_entities: Sequence[Any] | None = None,
        recipient_count: int,
        now: datetime,
    ) -> ManualPushPreview:
        self._require_admin(actor_id, admin_ids)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("broadcast timestamp must be timezone-aware")
        if source_chat_id != actor_id or source_message_id <= 0:
            raise ValueError("broadcast content must come from the admin private chat")
        if isinstance(recipient_count, bool) or recipient_count < 0:
            raise ValueError("recipient count is invalid")
        if kind == "text":
            if not text or len(text) > 4096 or caption is not None:
                raise ValueError("Отправьте текст не длиннее 4096 символов или фото с подписью до 1024 символов.")
        elif kind == "photo":
            if (
                text is not None
                or not photo_file_id
                or (caption is not None and len(caption) > 1024)
            ):
                raise ValueError("Подпись к фото не должна превышать 1024 символа.")
        else:
            raise ValueError("Поддерживаются только текст и фото с подписью.")

        with self._lock:
            if actor_id not in self._awaiting_content:
                raise ValueError("Сначала откройте раздел «Рассылка».")
            self._awaiting_content.discard(actor_id)
            previous = self._active_by_admin.pop(actor_id, None)
            if previous:
                self._pending.pop(previous, None)
                self._payloads.pop(previous, None)
            preview = ManualPushPreview(
                token=secrets.token_hex(8),
                admin_id=actor_id,
                created_at=now.astimezone(timezone.utc),
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                kind=kind,
                recipient_count=recipient_count,
            )
            self._pending[preview.token] = preview
            self._payloads[preview.token] = _ManualPushPayload(
                kind=kind,
                text=text,
                text_entities=tuple(text_entities or ()),
                photo_file_id=photo_file_id,
                caption=caption,
                caption_entities=tuple(caption_entities or ()),
            )
            self._active_by_admin[actor_id] = preview.token
            return preview

    def _consume(
        self,
        token: str,
        actor_id: int,
        now: datetime,
    ) -> tuple[ManualPushPreview, _ManualPushPayload] | None:
        with self._lock:
            preview = self._pending.get(token)
            if preview is None or preview.admin_id != actor_id:
                return None
            self._pending.pop(token, None)
            payload = self._payloads.pop(token, None)
            if self._active_by_admin.get(actor_id) == token:
                self._active_by_admin.pop(actor_id, None)
            if payload is None or now.astimezone(timezone.utc) - preview.created_at > self.preview_ttl:
                return None
            return preview, payload

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

    def replace_preview_source(
        self,
        token: str,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
        source_message_id: int,
    ) -> ManualPushPreview:
        self._require_admin(actor_id, admin_ids)
        if source_message_id <= 0:
            raise ValueError("preview message ID must be positive")
        with self._lock:
            preview = self._pending.get(token)
            if preview is None or preview.admin_id != actor_id:
                raise ValueError("broadcast preview is no longer active")
            replacement = ManualPushPreview(
                token=preview.token,
                admin_id=preview.admin_id,
                created_at=preview.created_at,
                source_chat_id=preview.source_chat_id,
                source_message_id=source_message_id,
                kind=preview.kind,
                recipient_count=preview.recipient_count,
            )
            self._pending[token] = replacement
            return replacement

    async def confirm(
        self,
        token: str,
        *,
        actor_id: int,
        admin_ids: set[int] | frozenset[int],
        bot: Any,
        users: UserAlertStore,
        admin_state: AdminStateStore,
        now: datetime,
    ) -> ManualPushResult:
        self._require_admin(actor_id, admin_ids)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("confirm timestamp must be timezone-aware")
        campaign = self._consume(token, actor_id, now)
        if campaign is None:
            return ManualPushResult(status="invalid")
        _preview, payload = campaign

        delivered = blocked = failed = state_failures = 0
        for user_id in self.recipient_ids(users, admin_ids):
            try:
                await self._send_with_retry(bot, user_id, payload)
            except Forbidden:
                blocked += 1
                try:
                    users.mark_blocked(user_id, observed_at=now)
                except Exception:
                    logging.exception("Blocked broadcast recipient state could not be saved. user_id=%s", user_id)
                    state_failures += 1
            except (RetryAfter, TelegramError):
                logging.exception("Telegram rejected a confirmed private broadcast. user_id=%s", user_id)
                failed += 1
            except Exception:
                logging.exception("Confirmed private broadcast failed. user_id=%s", user_id)
                failed += 1
            else:
                delivered += 1

        if delivered:
            try:
                admin_state.record_manual_push(now)
            except Exception:
                logging.exception("Broadcast was delivered but its audit timestamp could not be saved")
                state_failures += 1
        logging.info(
            "Confirmed private-user broadcast completed. admin_id=%s delivered=%s blocked=%s failed=%s",
            actor_id,
            delivered,
            blocked,
            failed,
        )
        return ManualPushResult(
            status="complete",
            delivered=delivered,
            blocked=blocked,
            failed=failed,
            state_failures=state_failures,
        )

    @staticmethod
    async def _send_with_retry(bot: Any, user_id: int, payload: _ManualPushPayload) -> None:
        for attempt in range(2):
            try:
                if payload.kind == "photo":
                    kwargs: dict[str, Any] = {
                        "chat_id": user_id,
                        "photo": payload.photo_file_id,
                    }
                    if payload.caption is not None:
                        kwargs["caption"] = payload.caption
                    if payload.caption_entities:
                        kwargs["caption_entities"] = list(payload.caption_entities)
                    await bot.send_photo(**kwargs)
                else:
                    kwargs = {"chat_id": user_id, "text": payload.text}
                    if payload.text_entities:
                        kwargs["entities"] = list(payload.text_entities)
                    await bot.send_message(**kwargs)
                return
            except RetryAfter as exc:
                if attempt == 1:
                    raise
                delay = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
                await asyncio.sleep(max(0.0, delay))
