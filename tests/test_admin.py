import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from telegram import MessageEntity
from telegram.error import TelegramError, TimedOut

from bot import PoolmasterBot
from farming import FARMING_POOLS, IdentityCheck, PoolMetrics, RateMetric, SourceStatus
from price import PriceIdentityCheck, PriceMetrics
from snapshot import DataSnapshot


NOW = datetime(2026, 10, 17, 19, 0, tzinfo=timezone.utc)


def _make_bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "TESTPOOLMASTERBOT")
    monkeypatch.setenv("ADMIN_USER_IDS", "111,222")
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("USER_STATE_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("ADMIN_STATE_PATH", str(tmp_path / "admin.json"))
    monkeypatch.setenv("MANAGED_DESTINATIONS_PATH", str(tmp_path / "destinations.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))
    return PoolmasterBot()


def _snapshot():
    farming = {}
    for pool in FARMING_POOLS:
        kind = "APY" if pool.dex == "STON.fi" else "APR"
        rate = RateMetric(Decimal("12.5"), kind, "test")
        farming[pool.address] = PoolMetrics(pool, (rate,), (), rate, None, NOW, SourceStatus.OK, True)
    price = PriceMetrics(
        Decimal("0.02"), None, None, None, "JETTONUSDT", "USDT", "MEXC spot", NOW, NOW, SourceStatus.OK
    )
    return DataSnapshot(NOW, farming, price, {"farming": "ok", "price": "ok"}, {})


def _update(user_id=111, *, chat_id=None, chat_type="private", data="admin:home"):
    actual_chat_id = user_id if chat_id is None else chat_id
    message = _Message(actual_chat_id, chat_type)
    query = _Query(user_id, message, data)
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=message.chat,
        effective_message=message,
    ), query, message


class _Message:
    def __init__(self, chat_id, chat_type="private"):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.message_id = 500
        self.replies = []
        self.deleted = False

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))

    async def delete(self):
        self.deleted = True

    async def reply_document(self, **kwargs):
        self.replies.append((None, kwargs))


class _Query:
    def __init__(self, user_id, message, data):
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def test_admin_command_opens_private_single_frame_dashboard_only_for_configured_id(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    snapshot = _snapshot()

    class SnapshotReader:
        async def get_snapshot(self, **_kwargs):
            return snapshot

    bot.snapshot_manager = SnapshotReader()
    bot.user_alert_store.ensure_user(123)
    message = _Message(111)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111, username=None, first_name="Admin", last_name=None),
        effective_chat=message.chat,
        effective_message=message,
    )

    asyncio.run(bot.admin(update, SimpleNamespace(args=[])))

    text, kwargs = message.replies[0]
    assert "POOLMASTER ADMIN" in text
    assert kwargs["reply_markup"].inline_keyboard


def test_non_admin_and_group_admin_cannot_open_admin_dashboard(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    message = _Message(333)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=333),
        effective_chat=message.chat,
        effective_message=message,
    )
    asyncio.run(bot.admin(update, SimpleNamespace(args=[])))
    assert "только администратору" in message.replies[0][0]

    group_message = _Message(-100555, "supergroup")
    group_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=group_message.chat,
        effective_message=group_message,
    )
    asyncio.run(bot.admin(group_update, SimpleNamespace(args=[])))
    assert "только в личном чате" in group_message.replies[0][0]


@pytest.mark.parametrize("command", ["stats", "stats_full"])
def test_admin_statistics_commands_do_not_disclose_user_data_in_groups(
    command, tmp_path, monkeypatch
):
    bot = _make_bot(tmp_path, monkeypatch)
    message = _Message(-100555, "supergroup")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=message.chat,
        effective_message=message,
    )
    monkeypatch.setattr(bot, "delete_command_message", lambda _update: asyncio.sleep(0))
    monkeypatch.setattr(bot.stats_store, "summary", lambda: pytest.fail("must not disclose group statistics"))
    monkeypatch.setattr(bot.stats_store, "load", lambda: pytest.fail("must not export group statistics"))

    asyncio.run(getattr(bot, command)(update, SimpleNamespace()))

    assert len(message.replies) == 1
    assert "личный чат" in message.replies[0][0]


def test_removed_group_debug_command_has_no_product_response(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    message = _Message(-100555, "supergroup")
    message.text = "/group_debug"
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=333),
        effective_chat=message.chat,
        effective_message=message,
    )

    asyncio.run(bot.command_fallback(update, SimpleNamespace()))

    assert message.replies == []


def test_foreign_user_and_foreign_private_message_cannot_use_admin_callbacks(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)

    for user_id, chat_id in ((333, 333), (111, 333)):
        update, query, _message = _update(user_id, chat_id=chat_id, data="admin:global:resume")
        asyncio.run(bot.admin_ui_callback(update, SimpleNamespace(bot=object())))
        assert query.answers[-1][1]["show_alert"] is True
        assert bot.admin_state_store.is_global_auto_push_paused() is True


def test_every_mutating_admin_callback_is_rejected_before_dispatch_for_non_admin(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.managed_destination_store.register_destination(
        -100123,
        "Test Channel",
        "channel",
        actor_id=111,
        admin_ids=bot.admin_ids,
    )
    callbacks = (
        "admin:global:resume",
        "admin:dest:set:-100123:6",
        "admin:push:preview",
        "admin:push:confirm:guessabletoken",
    )

    for callback in callbacks:
        update, query, _message = _update(333, data=callback)
        asyncio.run(bot.admin_ui_callback(update, SimpleNamespace(bot=object())))
        assert query.answers[-1][1]["show_alert"] is True
        assert query.edits == []

    assert bot.admin_state_store.is_global_auto_push_paused() is True
    assert bot.managed_destination_store.get_destination(-100123)["farming_interval_hours"] is None


def test_error_handler_does_not_log_update_payload_or_error_message(tmp_path, monkeypatch, caplog):
    bot = _make_bot(tmp_path, monkeypatch)
    secret = "synthetic-secret-in-message"
    update = SimpleNamespace(update_id=42, message=SimpleNamespace(text=f"/admin {secret}"))
    context = SimpleNamespace(error=RuntimeError(secret))

    asyncio.run(bot.error_handler(update, context))

    assert "update_id=42" in caplog.text
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


def test_admin_callback_persists_global_resume_and_renders_updated_state(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)

    class SnapshotReader:
        async def get_snapshot(self, **_kwargs):
            return _snapshot()

    bot.snapshot_manager = SnapshotReader()
    update, query, _message = _update(111, data="admin:global:resume")

    asyncio.run(bot.admin_ui_callback(update, SimpleNamespace(bot=object())))

    assert bot.admin_state_store.is_global_auto_push_paused() is False
    assert "Автоуведомления пользователям: <b>включены</b>" in query.edits[-1][0]


def test_admin_status_refresh_callback_is_throttled_per_admin(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    calls = []

    class SnapshotReader:
        async def refresh(self, **kwargs):
            calls.append("refresh")
            return _snapshot()

    bot.snapshot_manager = SnapshotReader()
    first_update, first_query, _ = _update(111, data="admin:refresh:status")
    second_update, second_query, _ = _update(111, data="admin:refresh:status")

    asyncio.run(bot.admin_ui_callback(first_update, SimpleNamespace(bot=object())))
    asyncio.run(bot.admin_ui_callback(second_update, SimpleNamespace(bot=object())))

    assert calls == ["refresh"]
    assert first_query.answers[0][0] == "Обновляю…"
    assert second_query.answers[0][0] == "Обновить можно раз в 10 секунд."


def test_admin_chart_duplicate_action_is_removed(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    update, query, _message = _update(111, data="admin:chart")
    calls = []

    asyncio.run(bot.admin_ui_callback(update, SimpleNamespace(bot=object())))

    assert calls == []
    assert "Неизвестное действие" in query.edits[-1][0]


def test_destination_schedule_callback_changes_only_registered_destination(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.managed_destination_store.register_destination(
        -100123,
        "Test Channel",
        "channel",
        actor_id=111,
        admin_ids=bot.admin_ids,
    )

    class SnapshotReader:
        async def get_snapshot(self, **_kwargs):
            return _snapshot()

    bot.snapshot_manager = SnapshotReader()
    update, _query, _message = _update(111, data="admin:dest:set:-100123:6")

    asyncio.run(bot.admin_ui_callback(update, SimpleNamespace(bot=object())))

    saved = bot.managed_destination_store.get_destination(-100123)
    assert saved["enabled"] is True
    assert saved["farming_interval_hours"] == 6


def test_turn_off_schedule_callback_shows_updated_disabled_state_in_place(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.managed_destination_store.register_destination(
        -100123,
        "Test Channel",
        "channel",
        actor_id=111,
        admin_ids=bot.admin_ids,
    )
    bot.managed_destination_store.set_schedule(
        -100123,
        6,
        actor_id=111,
        admin_ids=bot.admin_ids,
    )

    class SnapshotReader:
        async def get_snapshot(self, **_kwargs):
            return _snapshot()

    bot.snapshot_manager = SnapshotReader()
    update, query, _message = _update(111, data="admin:dest:set:-100123:off")

    asyncio.run(bot.admin_ui_callback(update, SimpleNamespace(bot=object())))

    saved = bot.managed_destination_store.get_destination(-100123)
    assert saved["enabled"] is False
    assert saved["farming_interval_hours"] is None
    assert "Автопост: <b>выключен</b>" in query.edits[-1][0]
    assert any(
        button.callback_data == "admin:dest:set:-100123:off" and button.style == "danger"
        for row in query.edits[-1][1]["reply_markup"].inline_keyboard
        for button in row
    )


def test_legacy_disable_autopost_callback_keeps_destination_settings_open(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.managed_destination_store.register_destination(
        -100123,
        "Test Channel",
        "channel",
        actor_id=111,
        admin_ids=bot.admin_ids,
    )
    bot.managed_destination_store.set_schedule(
        -100123,
        6,
        actor_id=111,
        admin_ids=bot.admin_ids,
    )

    class SnapshotReader:
        async def get_snapshot(self, **_kwargs):
            return _snapshot()

    bot.snapshot_manager = SnapshotReader()
    update, query, _message = _update(111, data="admin:dest:disable:-100123")

    asyncio.run(bot.admin_ui_callback(update, SimpleNamespace(bot=object())))

    saved = bot.managed_destination_store.get_destination(-100123)
    assert saved["enabled"] is False
    assert saved["farming_interval_hours"] is None
    assert "Автопост: <b>выключен</b>" in query.edits[-1][0]
    assert any(
        button.callback_data == "admin:dest:set:-100123:off" and button.style == "danger"
        for row in query.edits[-1][1]["reply_markup"].inline_keyboard
        for button in row
    )


@pytest.mark.parametrize("content_kind", ["text", "photo"])
def test_manual_broadcast_preview_and_confirm_preserve_content_entities_and_admin_recipients(
    content_kind, tmp_path, monkeypatch
):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.user_alert_store.register_private_user(
        SimpleNamespace(id=123, username="test_user", first_name="Test", last_name="User"),
        observed_at=NOW,
    )

    class FakeTelegramBot:
        def __init__(self):
            self.copies = []
            self.markup_edits = []
            self.messages = []
            self.photos = []

        async def copy_message(self, **kwargs):
            self.copies.append(kwargs)
            if kwargs["chat_id"] == 111:
                return SimpleNamespace(message_id=900)

        async def edit_message_reply_markup(self, **kwargs):
            self.markup_edits.append(kwargs)

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)

        async def send_photo(self, **kwargs):
            self.photos.append(kwargs)

    telegram = FakeTelegramBot()
    context = SimpleNamespace(bot=telegram, user_data={})
    home_update, _home_query, _home_message = _update(111, data="admin:push")
    asyncio.run(bot.admin_ui_callback(home_update, context))
    assert bot.manual_push_coordinator.is_waiting_for_content(111)

    source = _Message(111)
    source.message_id = 501
    if content_kind == "text":
        source.text = "A notice link"
        source.entities = [
            MessageEntity(type="bold", offset=0, length=1),
            MessageEntity(type="text_link", offset=9, length=4, url="https://example.com"),
        ]
        source.photo = None
        source.caption = None
    else:
        source.text = None
        source.photo = [SimpleNamespace(file_id="telegram-file-id")]
        source.caption = "Photo caption link"
        source.caption_entities = [
            MessageEntity(type="bold", offset=0, length=5),
            MessageEntity(type="text_link", offset=13, length=4, url="https://example.com"),
        ]
    input_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=source.chat,
        effective_message=source,
    )
    asyncio.run(bot.manual_broadcast_content(input_update, context))

    assert len(telegram.copies) == 1
    assert telegram.copies[0] == {
        "chat_id": 111,
        "from_chat_id": 111,
        "message_id": 501,
    }
    assert source.deleted is True
    preview_markup = telegram.markup_edits[0]["reply_markup"]
    preview_buttons = [button for row in preview_markup.inline_keyboard for button in row]
    assert [button.text for button in preview_buttons] == ["Отправить всем", "Отмена"]
    confirm_data = next(button.callback_data for button in preview_buttons if button.text == "Отправить всем")
    confirm_update, confirm_query, _confirm_message = _update(111, data=confirm_data)
    confirm_query.message.message_id = 900
    asyncio.run(bot.admin_ui_callback(confirm_update, context))

    assert len(telegram.copies) == 1
    assert telegram.copies[0] == {
        "chat_id": 111,
        "from_chat_id": 111,
        "message_id": 501,
    }
    if content_kind == "text":
        broadcast_messages = [item for item in telegram.messages if item.get("text") == source.text]
        assert [item["chat_id"] for item in broadcast_messages] == [111, 123, 222]
        assert all(item["entities"] == source.entities for item in broadcast_messages)
    else:
        assert [item["chat_id"] for item in telegram.photos] == [111, 123, 222]
        assert all(item["photo"] == "telegram-file-id" for item in telegram.photos)
        assert all(item["caption"] == source.caption for item in telegram.photos)
        assert all(item["caption_entities"] == source.caption_entities for item in telegram.photos)
    assert telegram.messages[-1]["text"] == "Отправлено: 3\nЗаблокировали бота: 0\nОшибки: 0"


def test_destination_binding_requires_bot_posting_rights_and_starts_off(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    message = _Message(111)

    class BindingBot:
        def __init__(self, can_post):
            self.can_post = can_post

        async def get_chat(self, _chat_id):
            return SimpleNamespace(id=-100123, type="channel", title="Test Channel")

        async def get_me(self):
            return SimpleNamespace(id=999)

        async def get_chat_member(self, _chat_id, _bot_id):
            return SimpleNamespace(status="administrator", can_post_messages=self.can_post)

    async def run(can_post):
        await bot.bind_managed_destination(
            message,
            SimpleNamespace(bot=BindingBot(can_post)),
            111,
            "-100123",
        )

    asyncio.run(run(False))
    assert bot.managed_destination_store.list_destinations() == []

    asyncio.run(run(True))
    destination = bot.managed_destination_store.get_destination(-100123)
    assert destination["name"] == "Test Channel"
    assert destination["type"] == "channel"
    assert destination["enabled"] is False
    assert destination["farming_interval_hours"] is None


def test_destination_binding_rejects_group_that_disallows_member_messages(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    message = _Message(111)

    class BindingBot:
        async def get_chat(self, _chat_id):
            return SimpleNamespace(
                id=-100123,
                type="supergroup",
                title="Read Only Group",
                permissions=SimpleNamespace(can_send_messages=False),
            )

        async def get_me(self):
            return SimpleNamespace(id=999)

        async def get_chat_member(self, _chat_id, _bot_id):
            return SimpleNamespace(status="member")

    asyncio.run(
        bot.bind_managed_destination(message, SimpleNamespace(bot=BindingBot()), 111, "-100123")
    )

    assert bot.managed_destination_store.list_destinations() == []


def test_emergency_bind_rejects_new_group_member_when_send_permission_is_unknown(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    message = _Message(111)

    class BindingBot:
        async def get_chat(self, _chat_id):
            return SimpleNamespace(id=-100123, type="supergroup", title="Unknown Permissions")

        async def get_me(self):
            return SimpleNamespace(id=999)

        async def get_chat_member(self, _chat_id, _bot_id):
            return SimpleNamespace(status="member")

    asyncio.run(
        bot.bind_managed_destination(
            message,
            SimpleNamespace(bot=BindingBot()),
            111,
            "-100123",
        )
    )

    assert bot.managed_destination_store.list_destinations() == []
    assert "права администратора" in message.replies[-1][0]


def test_existing_legacy_member_uses_previously_verified_permission_when_chat_permissions_omitted(
    tmp_path, monkeypatch
):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    bot.managed_destination_store.register_destination(
        -100900,
        "Legacy Group",
        "supergroup",
        actor_id=111,
        admin_ids=bot.admin_ids,
        bot_status="member",
        can_post_messages=True,
    )
    bot.managed_destination_store.set_schedule(-100900, 6, actor_id=111, admin_ids=bot.admin_ids)
    update, _chat, _member = _membership_update(
        actor_id=333,
        chat_type="supergroup",
        status="member",
    )
    api = _membership_api(-100900, "supergroup", "member")

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    destination = bot.managed_destination_store.get_destination(-100900)
    assert destination["can_post_messages"] is True
    assert destination["enabled"] is True
    assert destination["farming_interval_hours"] == 6


def test_new_group_member_without_confirmed_send_permission_stays_off(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    update, _chat, _member = _membership_update(
        actor_id=111,
        chat_type="supergroup",
        status="member",
    )
    api = _membership_api(-100900, "supergroup", "member")

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    destination = bot.managed_destination_store.get_destination(-100900)
    assert destination["can_post_messages"] is False
    assert destination["enabled"] is False
    assert destination["farming_interval_hours"] is None


def test_emergency_bind_accepts_positive_input_when_telegram_resolves_private_group_id(
    tmp_path, monkeypatch
):
    bot = _make_bot(tmp_path, monkeypatch)
    observed = []

    class BindingBot:
        async def get_chat(self, chat_reference):
            observed.append(chat_reference)
            return SimpleNamespace(
                id=-1007654321,
                type="supergroup",
                title="Resolved Private Group",
                permissions=SimpleNamespace(can_send_messages=True),
            )

        async def get_me(self):
            return SimpleNamespace(id=999)

        async def get_chat_member(self, _chat_id, bot_id):
            return SimpleNamespace(status="administrator", user=SimpleNamespace(id=bot_id))

    message = _Message(111)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=message.chat,
        effective_message=message,
    )
    context = SimpleNamespace(bot=BindingBot(), args=["bind", "4290224921"])

    asyncio.run(bot.admin(update, context))

    destination = bot.managed_destination_store.get_destination(-1007654321)
    assert observed == [4290224921]
    assert destination["type"] == "supergroup"
    assert destination["name"] == "Resolved Private Group"
    assert destination["enabled"] is False
    assert "Чат добавлен" in message.replies[-1][0]


class _MembershipApi:
    def __init__(self, target, member, *, failure=None, send_failure=None):
        self.target = target
        self.member = member
        self.failure = failure
        self.send_failure = send_failure
        self.calls = []
        self.messages = []

    async def get_chat(self, chat_id):
        self.calls.append(("get_chat", chat_id))
        if self.failure:
            raise self.failure
        return self.target

    async def get_chat_member(self, chat_id, bot_id):
        self.calls.append(("get_chat_member", chat_id, bot_id))
        if self.failure:
            raise self.failure
        return self.member

    async def send_message(self, **kwargs):
        if self.send_failure:
            raise self.send_failure
        self.messages.append(kwargs)


def _membership_update(*, actor_id, chat_type="channel", status="administrator", chat_id=-100900,
                       title="Detected Destination", chat_permissions=None, **member_fields):
    chat = SimpleNamespace(
        id=chat_id,
        type=chat_type,
        title=title,
        username=None,
        permissions=chat_permissions,
    )
    member = SimpleNamespace(
        status=status,
        user=SimpleNamespace(id=999),
        **member_fields,
    )
    return SimpleNamespace(
        my_chat_member=SimpleNamespace(
            chat=chat,
            new_chat_member=member,
            from_user=SimpleNamespace(id=actor_id) if actor_id is not None else None,
            date=NOW,
        )
    ), chat, member


def _membership_api(
    chat_id,
    chat_type,
    status,
    *,
    title="Verified Destination",
    chat_permissions=None,
    failure=None,
    send_failure=None,
    **fields,
):
    target = SimpleNamespace(
        id=chat_id,
        type=chat_type,
        title=title,
        username=None,
        permissions=chat_permissions,
    )
    member = SimpleNamespace(status=status, user=SimpleNamespace(id=999), **fields)
    return _MembershipApi(target, member, failure=failure, send_failure=send_failure)


@pytest.mark.parametrize(
    ("chat_type", "status", "member_fields"),
    [
        ("channel", "administrator", {"can_post_messages": True}),
        ("supergroup", "administrator", {}),
    ],
)
def test_configured_admin_adds_verified_channel_or_group_without_enabling_schedule(
    chat_type, status, member_fields, tmp_path, monkeypatch
):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    update, _chat, _member = _membership_update(
        actor_id=111, chat_type=chat_type, status=status, **member_fields
    )
    api = _membership_api(-100900, chat_type, status, **member_fields)

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    candidate = bot.managed_destination_store.get_destination(-100900)
    assert candidate["type"] == chat_type
    assert candidate["name"] == "Verified Destination"
    assert candidate["bot_status"] == status
    assert candidate["can_post_messages"] is True
    assert candidate["enabled"] is False
    assert candidate["farming_interval_hours"] is None
    assert api.calls == [("get_chat", -100900), ("get_chat_member", -100900, 999)]


@pytest.mark.parametrize("chat_type", ["channel", "group"])
def test_unconfigured_actor_cannot_add_new_managed_destination(chat_type, tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    update, _chat, _member = _membership_update(
        actor_id=333, chat_type=chat_type, status="administrator", can_post_messages=True
    )
    api = _membership_api(-100900, chat_type, "administrator", can_post_messages=True)

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    assert bot.managed_destination_store.list_destinations() == []
    assert api.calls == []
    assert api.messages == []


def test_new_verified_destination_with_insufficient_rights_is_off_candidate(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    update, _chat, _member = _membership_update(
        actor_id=111, status="administrator", can_post_messages=False
    )
    api = _membership_api(-100900, "channel", "administrator", can_post_messages=False)

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    candidate = bot.managed_destination_store.get_destination(-100900)
    assert candidate["bot_status"] == "administrator"
    assert candidate["can_post_messages"] is False
    assert candidate["enabled"] is False
    assert candidate["farming_interval_hours"] is None


def test_new_configured_admin_candidate_stays_unverified_and_off_when_api_fails(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    update, _chat, _member = _membership_update(
        actor_id=111, status="administrator", can_post_messages=True
    )
    api = _membership_api(
        -100900,
        "channel",
        "administrator",
        failure=TelegramError("private details"),
    )

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    candidate = bot.managed_destination_store.get_destination(-100900)
    assert candidate["bot_status"] == "unknown"
    assert candidate["can_post_messages"] is False
    assert candidate["enabled"] is False
    assert candidate["farming_interval_hours"] is None


def test_new_destination_confirms_once_only_to_the_actor_with_settings_callback(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    update, _chat, _member = _membership_update(
        actor_id=111,
        chat_type="supergroup",
        status="administrator",
    )
    api = _membership_api(-100900, "supergroup", "administrator")

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))
    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    assert len(api.messages) == 1
    assert api.messages[0]["chat_id"] == 111
    assert "Группа" in api.messages[0]["text"]
    assert "Автопост пока выключен" in api.messages[0]["text"]
    buttons = [
        button
        for row in api.messages[0]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert any(
        button.text == "Открыть настройки"
        and button.callback_data == "admin:dest:open:-100900"
        for button in buttons
    )


def test_confirmation_delivery_failure_does_not_undo_destination_registration(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    update, _chat, _member = _membership_update(actor_id=111, status="administrator", can_post_messages=True)
    api = _membership_api(
        -100900,
        "channel",
        "administrator",
        can_post_messages=True,
        send_failure=TelegramError("private chat unavailable"),
    )

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    candidate = bot.managed_destination_store.get_destination(-100900)
    assert candidate is not None
    assert candidate["can_post_messages"] is True
    assert candidate["enabled"] is False


def test_random_actor_updates_existing_destination_and_rights_loss_turns_schedule_off(
    tmp_path, monkeypatch
):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    bot.managed_destination_store.register_destination(
        -100900,
        "Old name",
        "channel",
        actor_id=111,
        admin_ids=bot.admin_ids,
    )
    bot.managed_destination_store.set_schedule(-100900, 6, actor_id=111, admin_ids=bot.admin_ids)
    update, _chat, _member = _membership_update(
        actor_id=333,
        status="administrator",
        title="Renamed destination",
        can_post_messages=False,
    )
    api = _membership_api(
        -100900,
        "channel",
        "administrator",
        title="Renamed destination",
        can_post_messages=False,
    )

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    destination = bot.managed_destination_store.get_destination(-100900)
    assert destination["name"] == "Renamed destination"
    assert destination["can_post_messages"] is False
    assert destination["enabled"] is False
    assert destination["farming_interval_hours"] is None
    assert api.calls == [("get_chat", -100900), ("get_chat_member", -100900, 999)]


def test_queued_rights_loss_disables_schedule_even_if_permissions_were_restored_before_polling(
    tmp_path, monkeypatch
):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    bot.managed_destination_store.register_destination(
        -100900,
        "Destination",
        "channel",
        actor_id=111,
        admin_ids=bot.admin_ids,
    )
    bot.managed_destination_store.set_schedule(-100900, 12, actor_id=111, admin_ids=bot.admin_ids)

    # Telegram's queued update says posting rights were lost, but getChatMember
    # already reflects a later restoration by the time this update is handled.
    update, _chat, _member = _membership_update(
        actor_id=333,
        status="administrator",
        can_post_messages=False,
    )
    api = _membership_api(-100900, "channel", "administrator", can_post_messages=True)

    asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    destination = bot.managed_destination_store.get_destination(-100900)
    assert destination["bot_status"] == "administrator"
    assert destination["can_post_messages"] is True
    assert destination["enabled"] is False
    assert destination["farming_interval_hours"] is None


def test_rights_restoration_and_duplicate_updates_never_restore_schedule(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    bot.managed_destination_store.register_destination(
        -100900,
        "Destination",
        "channel",
        actor_id=111,
        admin_ids=bot.admin_ids,
    )
    bot.managed_destination_store.set_schedule(-100900, 12, actor_id=111, admin_ids=bot.admin_ids)

    removed, _chat, _member = _membership_update(
        actor_id=333, status="left", can_post_messages=False
    )
    removed_api = _membership_api(-100900, "channel", "left")
    asyncio.run(bot.my_chat_member(removed, SimpleNamespace(bot=removed_api)))

    restored, _chat, _member = _membership_update(
        actor_id=333, status="administrator", can_post_messages=True
    )
    restored_api = _membership_api(-100900, "channel", "administrator", can_post_messages=True)
    asyncio.run(bot.my_chat_member(restored, SimpleNamespace(bot=restored_api)))
    asyncio.run(bot.my_chat_member(restored, SimpleNamespace(bot=restored_api)))

    destination = bot.managed_destination_store.get_destination(-100900)
    assert destination["bot_status"] == "administrator"
    assert destination["can_post_messages"] is True
    assert destination["enabled"] is False
    assert destination["farming_interval_hours"] is None
    assert len(bot.managed_destination_store.list_destinations()) == 1
    assert removed_api.calls == [("get_chat", -100900), ("get_chat_member", -100900, 999)]
    assert restored_api.calls == [
        ("get_chat", -100900),
        ("get_chat_member", -100900, 999),
        ("get_chat", -100900),
        ("get_chat_member", -100900, 999),
    ]


def test_existing_membership_api_failure_applies_event_and_logs_no_destination_pii(
    tmp_path, monkeypatch, caplog
):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.bot_id = 999
    bot.managed_destination_store.register_destination(
        -100900,
        "Secret Channel Name",
        "channel",
        actor_id=111,
        admin_ids=bot.admin_ids,
    )
    bot.managed_destination_store.set_schedule(-100900, 3, actor_id=111, admin_ids=bot.admin_ids)
    update, _chat, _member = _membership_update(
        actor_id=333,
        status="left",
        title="Secret Channel Name",
        can_post_messages=False,
    )
    api = _membership_api(
        -100900,
        "channel",
        "left",
        failure=TelegramError("must not leak"),
    )

    with caplog.at_level("WARNING"):
        asyncio.run(bot.my_chat_member(update, SimpleNamespace(bot=api)))

    destination = bot.managed_destination_store.get_destination(-100900)
    assert destination["bot_status"] == "left"
    assert destination["enabled"] is False
    assert destination["farming_interval_hours"] is None
    assert "TelegramError" in caplog.text
    assert "-100900" not in caplog.text
    assert "Secret Channel Name" not in caplog.text
    assert "must not leak" not in caplog.text


def test_admin_manual_channel_publish_requires_preview_and_targets_only_selected_channel(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    for chat_id, name in ((-100901, "Channel One"), (-100902, "Channel Two")):
        bot.managed_destination_store.register_destination(
            chat_id,
            name,
            "channel",
            actor_id=111,
            admin_ids=bot.admin_ids,
        )
    bot.user_alert_store.register_private_user(
        SimpleNamespace(id=123, username="test_user", first_name="Test", last_name="User"),
        observed_at=NOW,
    )

    class SnapshotReader:
        async def get_snapshot(self, **_kwargs):
            return _snapshot()

    bot.snapshot_manager = SnapshotReader()

    class ChannelBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)

    telegram = ChannelBot()
    context = SimpleNamespace(bot=telegram)
    preview_update, preview_query, _ = _update(111, data="admin:dest:publish:-100901")
    asyncio.run(bot.admin_ui_callback(preview_update, context))

    assert telegram.messages == []
    assert "Предпросмотр публикации в чате «Channel One»" in preview_query.edits[-1][0]
    assert "DeDust JETTON/GRAM" in preview_query.edits[-1][0]
    assert "STON.fi JETTON/GRAM" in preview_query.edits[-1][0]
    buttons = [button for row in preview_query.edits[-1][1]["reply_markup"].inline_keyboard for button in row]
    assert [button.text for button in buttons] == ["Опубликовать", "Отмена"]
    confirm_data = next(button.callback_data for button in buttons if button.text == "Опубликовать")
    confirm_update, confirm_query, _ = _update(111, data=confirm_data)
    asyncio.run(bot.admin_ui_callback(confirm_update, context))

    assert [message["chat_id"] for message in telegram.messages] == [-100901]
    assert "Публикация отправлена." in confirm_query.edits[-1][0]


@pytest.mark.parametrize("flaky_operation", ["get_chat", "get_me", "get_chat_member"])
def test_emergency_admin_bind_retries_transient_read_timeouts(
    flaky_operation, tmp_path, monkeypatch
):
    bot = _make_bot(tmp_path, monkeypatch)
    target = SimpleNamespace(id=-100901, type="channel", title="Bound Channel", username="bound_channel")
    calls = {name: 0 for name in ("get_chat", "get_me", "get_chat_member")}

    class BindingBot:
        async def get_chat(self, _chat_ref):
            calls["get_chat"] += 1
            if flaky_operation == "get_chat" and calls["get_chat"] == 1:
                raise TimedOut("temporary network timeout")
            return target

        async def get_me(self):
            calls["get_me"] += 1
            if flaky_operation == "get_me" and calls["get_me"] == 1:
                raise TimedOut("temporary network timeout")
            return SimpleNamespace(id=999)

        async def get_chat_member(self, _chat_id, _bot_id):
            calls["get_chat_member"] += 1
            if flaky_operation == "get_chat_member" and calls["get_chat_member"] == 1:
                raise TimedOut("temporary network timeout")
            return SimpleNamespace(status="administrator", can_post_messages=True)

    message = _Message(111)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=message.chat,
        effective_message=message,
    )
    context = SimpleNamespace(bot=BindingBot(), args=["bind", "-100901"])

    asyncio.run(bot.admin(update, context))

    destination = bot.managed_destination_store.get_destination(-100901)
    assert destination is not None
    assert destination["enabled"] is False
    assert calls[flaky_operation] == 2


def test_emergency_admin_bind_reports_temporary_failure_when_retry_budget_is_exhausted(
    tmp_path, monkeypatch
):
    bot = _make_bot(tmp_path, monkeypatch)
    calls = 0

    class BindingBot:
        async def get_chat(self, _chat_ref):
            nonlocal calls
            calls += 1
            raise TimedOut("temporary network timeout")

    message = _Message(111)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=message.chat,
        effective_message=message,
    )
    context = SimpleNamespace(bot=BindingBot(), args=["bind", "-100901"])

    asyncio.run(bot.admin(update, context))

    assert calls == 2
    assert bot.managed_destination_store.list_destinations() == []
    assert message.replies[-1][0] == "Telegram временно недоступен. Чат не добавлен — попробуйте ещё раз через минуту."


def test_non_admin_cannot_use_emergency_admin_bind(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    message = _Message(333)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=333),
        effective_chat=message.chat,
        effective_message=message,
    )

    asyncio.run(bot.admin(update, SimpleNamespace(bot=object(), args=["bind", "-100901"])))

    assert bot.managed_destination_store.list_destinations() == []
    assert "только администратору" in message.replies[-1][0]


def test_users_export_callback_sends_registered_private_users_as_csv_only_to_admin(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.user_alert_store.register_private_user(
        SimpleNamespace(id=123, username="test_name", first_name="First", last_name="Last"),
        observed_at=NOW,
    )
    bot.user_alert_store.ensure_user(777)
    bot.user_alert_store.mark_blocked(123, observed_at=NOW)
    sent = []

    async def reply_document(**kwargs):
        sent.append(kwargs)

    update, query, message = _update(111, data="admin:users:export")
    message.reply_document = reply_document
    asyncio.run(bot.admin_ui_callback(update, SimpleNamespace(bot=object())))

    assert len(sent) == 1
    assert sent[0]["filename"] == "poolmaster_users.csv"
    payload = sent[0]["document"].getvalue().decode("utf-8-sig")
    assert "telegram_id,username,first_name,last_name,first_seen,last_seen" in payload
    assert "123,test_name,First,Last," in payload
    assert "777," not in payload
    assert query.answers[0] == (None, {})


def test_removed_legacy_push_farming_command_has_no_handler_method(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    assert not hasattr(bot, "push_farming")
