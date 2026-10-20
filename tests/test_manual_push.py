import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telegram import MessageEntity
from telegram.error import Forbidden

from admin_state import AdminStateStore
from managed_destinations import ManagedDestinationStore
from manual_push import ManualPushCoordinator
from user_state import UserAlertStore


NOW = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
ADMIN_IDS = {111, 222}


def _stores(tmp_path):
    users = UserAlertStore(tmp_path / "users.json")
    users.register_private_user(
        SimpleNamespace(id=123, username="quiet_user", first_name="Quiet", last_name="User"),
        observed_at=NOW,
    )
    users.register_private_user(
        SimpleNamespace(id=456, username="blocked_user", first_name="Blocked", last_name="User"),
        observed_at=NOW,
    )
    users.ensure_user(789)  # Has preferences storage but never registered as a private user.
    destinations = ManagedDestinationStore(tmp_path / "destinations.json")
    destinations.register_destination(
        -100123,
        "Test channel",
        "channel",
        actor_id=111,
        admin_ids=ADMIN_IDS,
    )
    return users, destinations


def _begin(flow, admin_id=111):
    flow.begin_input(actor_id=admin_id, admin_ids=ADMIN_IDS)


def _preview(flow, *, admin_id=111, source_message_id=501, kind="text", text="Update"):
    _begin(flow, admin_id)
    return flow.create_preview(
        actor_id=admin_id,
        admin_ids=ADMIN_IDS,
        source_chat_id=admin_id,
        source_message_id=source_message_id,
        kind=kind,
        text=text if kind == "text" else None,
        caption=text if kind == "photo" else None,
        photo_file_id="telegram-photo-file-id" if kind == "photo" else None,
        recipient_count=2,
        now=NOW,
    )


def test_only_configured_admin_can_begin_manual_broadcast():
    flow = ManualPushCoordinator()

    with pytest.raises(PermissionError):
        flow.begin_input(actor_id=333, admin_ids=ADMIN_IDS)

    flow.begin_input(actor_id=222, admin_ids=ADMIN_IDS)
    assert flow.is_waiting_for_content(222)
    assert not flow.is_waiting_for_content(111)


@pytest.mark.parametrize(
    ("kind", "text"),
    [("text", "A plain text notice"), ("photo", "Photo caption")],
)
def test_broadcast_preview_keeps_only_source_reference_and_one_use_token(kind, text):
    flow = ManualPushCoordinator()
    preview = _preview(flow, kind=kind, text=text)

    assert preview.kind == kind
    assert preview.source_chat_id == 111
    assert preview.source_message_id == 501
    assert preview.recipient_count == 2
    assert not hasattr(preview, "text")
    assert not hasattr(preview, "caption")
    assert not flow.is_waiting_for_content(111)


@pytest.mark.parametrize(
    ("kind", "text", "caption"),
    [("video", None, None), ("text", "x" * 4097, None), ("photo", None, "x" * 1025)],
)
def test_unsupported_or_overlong_content_is_rejected_before_confirmation(kind, text, caption):
    flow = ManualPushCoordinator()
    _begin(flow)

    with pytest.raises(ValueError):
        flow.create_preview(
            actor_id=111,
            admin_ids=ADMIN_IDS,
            source_chat_id=111,
            source_message_id=502,
            kind=kind,
            text=text,
            caption=caption,
            recipient_count=2,
            now=NOW,
        )

    assert flow.is_waiting_for_content(111)


def test_cancel_consumes_a_preview_without_delivering(tmp_path):
    users, _destinations = _stores(tmp_path)
    flow = ManualPushCoordinator()
    preview = _preview(flow)

    assert flow.cancel(preview.token, actor_id=111, admin_ids=ADMIN_IDS, now=NOW)
    assert not flow.cancel(preview.token, actor_id=111, admin_ids=ADMIN_IDS, now=NOW)
    assert users.list_active_user_ids() == [123, 456]


def test_later_identical_content_creates_a_new_campaign():
    flow = ManualPushCoordinator()
    first = _preview(flow, source_message_id=501, text="Same message")
    assert flow.cancel(first.token, actor_id=111, admin_ids=ADMIN_IDS, now=NOW)

    second = _preview(flow, source_message_id=502, text="Same message")
    assert second.token != first.token
    assert second.source_message_id == 502


def test_confirm_sends_photo_to_active_users_and_configured_admins_without_controls_or_channel_sends(tmp_path):
    users, destinations = _stores(tmp_path)
    users.set_farming_threshold(123, None)
    users.set_price_threshold(123, None)
    users.pause_user(123, now=NOW)
    users.set_farming_threshold(456, None)
    users.mark_blocked(456, observed_at=NOW)
    admin_state = AdminStateStore(tmp_path / "admin.json", initial_paused=True)
    flow = ManualPushCoordinator()
    preview = _preview(flow, kind="photo", text="Caption")

    class DeliveryBot:
        def __init__(self):
            self.photos = []

        async def send_photo(self, **kwargs):
            self.photos.append(kwargs)

    telegram = DeliveryBot()
    result = asyncio.run(
        flow.confirm(
            preview.token,
            actor_id=111,
            admin_ids=ADMIN_IDS,
            bot=telegram,
            users=users,
            admin_state=admin_state,
            now=NOW,
        )
    )

    assert result.status == "complete"
    assert result.delivered == 3
    assert result.blocked == 0
    assert [item["chat_id"] for item in telegram.photos] == [111, 123, 222]
    assert all(item["photo"] == "telegram-photo-file-id" for item in telegram.photos)
    assert all(item["caption"] == "Caption" for item in telegram.photos)
    assert all("reply_markup" not in item for item in telegram.photos)
    assert destinations.list_destinations()[0]["chat_id"] == -100123
    assert admin_state.is_global_auto_push_paused() is True
    assert admin_state.last_manual_push_at() == NOW


def test_photo_broadcast_preserves_photo_caption_entities_and_includes_configured_admins(tmp_path):
    users, _destinations = _stores(tmp_path)
    users.mark_blocked(456, observed_at=NOW)
    entities = (
        MessageEntity(type="bold", offset=0, length=4),
        MessageEntity(type="text_link", offset=7, length=4, url="https://example.com"),
    )
    flow = ManualPushCoordinator()
    _begin(flow)
    preview = flow.create_preview(
        actor_id=111,
        admin_ids=ADMIN_IDS,
        source_chat_id=111,
        source_message_id=501,
        kind="photo",
        text=None,
        caption="News link",
        photo_file_id="telegram-photo-file-id",
        caption_entities=entities,
        text_entities=None,
        recipient_count=3,
        now=NOW,
    )

    class DeliveryBot:
        def __init__(self):
            self.photos = []

        async def send_photo(self, **kwargs):
            self.photos.append(kwargs)

    telegram = DeliveryBot()
    result = asyncio.run(
        flow.confirm(
            preview.token,
            actor_id=111,
            admin_ids=ADMIN_IDS,
            bot=telegram,
            users=users,
            admin_state=AdminStateStore(tmp_path / "admin.json"),
            now=NOW,
        )
    )

    assert result.delivered == 3
    assert [item["chat_id"] for item in telegram.photos] == [111, 123, 222]
    assert all(item["photo"] == "telegram-photo-file-id" for item in telegram.photos)
    assert all(item["caption"] == "News link" for item in telegram.photos)
    assert all(item["caption_entities"] == list(entities) for item in telegram.photos)


def test_text_broadcast_preserves_message_entities_for_configured_admins(tmp_path):
    users, _destinations = _stores(tmp_path)
    users.mark_blocked(456, observed_at=NOW)
    entities = (
        MessageEntity(type="bold", offset=0, length=6),
        MessageEntity(type="text_link", offset=7, length=4, url="https://example.com"),
    )
    flow = ManualPushCoordinator()
    _begin(flow)
    preview = flow.create_preview(
        actor_id=111,
        admin_ids=ADMIN_IDS,
        source_chat_id=111,
        source_message_id=501,
        kind="text",
        text="Update link",
        caption=None,
        photo_file_id=None,
        caption_entities=None,
        text_entities=entities,
        recipient_count=3,
        now=NOW,
    )

    class DeliveryBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)

    telegram = DeliveryBot()
    result = asyncio.run(
        flow.confirm(
            preview.token,
            actor_id=111,
            admin_ids=ADMIN_IDS,
            bot=telegram,
            users=users,
            admin_state=AdminStateStore(tmp_path / "admin.json"),
            now=NOW,
        )
    )

    assert result.delivered == 3
    assert [item["chat_id"] for item in telegram.messages] == [111, 123, 222]
    assert all(item["text"] == "Update link" for item in telegram.messages)
    assert all(item["entities"] == list(entities) for item in telegram.messages)


def test_forbidden_user_is_marked_blocked_and_not_retried(tmp_path):
    users, _destinations = _stores(tmp_path)
    flow = ManualPushCoordinator()
    preview = _preview(flow)

    class DeliveryBot:
        def __init__(self):
            self.attempts = []

        async def send_message(self, **kwargs):
            self.attempts.append(kwargs["chat_id"])
            if kwargs["chat_id"] == 123:
                raise Forbidden("user blocked the bot")

    telegram = DeliveryBot()
    result = asyncio.run(
        flow.confirm(
            preview.token,
            actor_id=111,
            admin_ids=ADMIN_IDS,
            bot=telegram,
            users=users,
            admin_state=AdminStateStore(tmp_path / "admin.json"),
            now=NOW,
        )
    )

    assert result.blocked == 1
    assert result.delivered == 3
    assert telegram.attempts == [111, 123, 222, 456]
    assert users.get_user(123)["blocked"] is True
    assert users.list_active_user_ids() == [456]


def test_double_confirm_only_copies_once(tmp_path):
    users, _destinations = _stores(tmp_path)
    flow = ManualPushCoordinator()
    preview = _preview(flow)

    class DeliveryBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)

    telegram = DeliveryBot()
    params = {
        "actor_id": 111,
        "admin_ids": ADMIN_IDS,
        "bot": telegram,
        "users": users,
        "admin_state": AdminStateStore(tmp_path / "admin.json"),
        "now": NOW,
    }
    async def confirm_twice():
        return await asyncio.gather(
            flow.confirm(preview.token, **params),
            flow.confirm(preview.token, **params),
        )

    first, second = asyncio.run(confirm_twice())

    assert sorted((first.status, second.status)) == ["complete", "invalid"]
    assert len(telegram.messages) == 4


def test_expired_preview_cannot_be_confirmed(tmp_path):
    users, _destinations = _stores(tmp_path)
    flow = ManualPushCoordinator(preview_ttl=timedelta(minutes=5))
    preview = _preview(flow)

    result = asyncio.run(
        flow.confirm(
            preview.token,
            actor_id=111,
            admin_ids=ADMIN_IDS,
            bot=SimpleNamespace(copy_message=lambda **_kwargs: None),
            users=users,
            admin_state=AdminStateStore(tmp_path / "admin.json"),
            now=NOW + timedelta(minutes=6),
        )
    )

    assert result.status == "invalid"
