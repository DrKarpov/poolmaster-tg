import asyncio
import json
import warnings
from datetime import datetime, timezone
from decimal import Decimal

from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User
from telegram.ext import Application, ApplicationBuilder as RealApplicationBuilder
from telegram.request import BaseRequest

import bot as bot_module
from bot import PoolmasterBot
from price import PriceMetrics
from snapshot import DataSnapshot
from farming import SourceStatus


class OfflineTelegramRequest(BaseRequest):
    def __init__(self):
        self.methods = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def do_request(
        self,
        url,
        method,
        request_data=None,
        read_timeout=None,
        write_timeout=None,
        connect_timeout=None,
        pool_timeout=None,
    ):
        api_method = url.rsplit("/", 1)[-1]
        self.methods.append(api_method)
        if api_method == "getMe":
            result = {
                "id": 99,
                "is_bot": True,
                "first_name": "POOLMASTER",
                "username": "POOLMASTER_TEST",
                "can_join_groups": True,
                "can_read_all_group_messages": True,
                "supports_inline_queries": True,
            }
        elif api_method == "getMyDefaultAdministratorRights":
            result = {
                "is_anonymous": False,
                "can_manage_chat": False,
                "can_delete_messages": False,
                "can_manage_video_chats": False,
                "can_restrict_members": False,
                "can_promote_members": False,
                "can_change_info": False,
                "can_invite_users": False,
                "can_post_stories": False,
                "can_edit_stories": False,
                "can_delete_stories": False,
            }
        elif api_method in {"sendMessage", "sendPhoto", "editMessageText"}:
            result = {
                "message_id": 77,
                "date": int(datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()),
                "chat": {"id": 7, "type": "private", "first_name": "Tester"},
                "from": {
                    "id": 99,
                    "is_bot": True,
                    "first_name": "POOLMASTER",
                    "username": "POOLMASTER_TEST",
                },
                "text": "POOLMASTER test frame",
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode("utf-8")


def _snapshot():
    now = datetime.now(timezone.utc)
    price = PriceMetrics(
        price_usdt=Decimal("0.01865"),
        bid_usdt=Decimal("0.01860"),
        ask_usdt=Decimal("0.01870"),
        quote_volume_24h=Decimal("1234"),
        symbol="JETTONUSDT",
        quote_currency="USDT",
        source="MEXC spot",
        fetched_at=now,
        source_timestamp=now,
        status=SourceStatus.OK,
    )
    return DataSnapshot(
        fetched_at=now,
        farming={},
        price=price,
        source_statuses={"farming": "unavailable", "price": "ok"},
        anomalies={},
    )


def test_real_ptb_lifecycle_routes_private_start_and_callback_without_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    monkeypatch.setenv("GLOBAL_AUTO_PUSH_PAUSED", "true")
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setenv("USER_STATE_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("AUTO_PUSH_STATE_PATH", str(tmp_path / "admin.json"))

    bot = PoolmasterBot()
    bot.user_alert_store.set_language(7, "ru")
    snapshot = _snapshot()

    async def get_snapshot(**_kwargs):
        return snapshot

    async def verified_pool_identities():
        return None

    async def verified_price_identity():
        return True

    started = set()
    cancelled = set()
    started_event = asyncio.Event()
    cancelled_event = asyncio.Event()
    background_tasks = set()

    def blocking_background_loop(name):
        async def run_until_stopped(application):
            assert application.running
            background_tasks.add(asyncio.current_task())
            started.add(name)
            if len(started) == 3:
                started_event.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(name)
                if len(cancelled) == 3:
                    cancelled_event.set()

        return run_until_stopped

    bot.snapshot_manager.get_snapshot = get_snapshot
    bot.initialize_pool_identity_checks = verified_pool_identities
    bot.check_price_identity = verified_price_identity

    async def test_chart(_timeframe):
        from io import BytesIO
        from types import SimpleNamespace

        image = BytesIO(b"chart")
        image.name = "test-chart.png"
        return SimpleNamespace(image=image, caption="MEXC Spot JETTON/USDT")

    bot.fetch_verified_mexc_chart = test_chart
    bot.alert_loop = blocking_background_loop("alerts")
    bot.managed_publication_loop = blocking_background_loop("managed")
    bot.farming_history_loop = blocking_background_loop("farming-history")
    errors = []

    async def capture_error(_update, context):
        errors.append(context.error)

    bot.error_handler = capture_error

    telegram_request = OfflineTelegramRequest()
    updates_request = OfflineTelegramRequest()

    def offline_builder():
        return (
            RealApplicationBuilder()
            .request(telegram_request)
            .get_updates_request(updates_request)
        )

    monkeypatch.setattr(bot_module, "ApplicationBuilder", offline_builder)
    application = bot.build_application()
    build_calls = []

    def reuse_built_application():
        build_calls.append(True)
        return application

    monkeypatch.setattr(bot, "build_application", reuse_built_application)
    processed = []
    stopped_background_tasks = []

    def mocked_polling(application):
        async def lifecycle():
            await application.initialize()
            await application.post_init(application)
            await application.start()
            try:
                await asyncio.wait_for(started_event.wait(), timeout=2)
                user = User(id=7, first_name="Tester", is_bot=False)
                chat = Chat(id=7, type="private", first_name="Tester")
                start_message = Message(
                    message_id=1,
                    date=datetime(2026, 10, 17, tzinfo=timezone.utc),
                    chat=chat,
                    from_user=user,
                    text="/start",
                    entities=[MessageEntity("bot_command", 0, 6)],
                )
                start_message.set_bot(application.bot)
                await application.process_update(Update(update_id=1, message=start_message))

                callback_message = Message(
                    message_id=77,
                    date=datetime(2026, 10, 17, tzinfo=timezone.utc),
                    chat=chat,
                    from_user=User(
                        id=99,
                        first_name="POOLMASTER",
                        is_bot=True,
                        username="POOLMASTER_TEST",
                    ),
                    text="POOLMASTER test frame",
                )
                callback_message.set_bot(application.bot)
                callback = CallbackQuery(
                    id="offline-callback",
                    from_user=user,
                    chat_instance="offline-chat",
                    message=callback_message,
                    data="ui:price",
                )
                callback.set_bot(application.bot)
                await application.process_update(Update(update_id=2, callback_query=callback))
                processed.extend((1, 2))
            finally:
                await application.stop()
                stopped_background_tasks.append(cancelled_event.is_set())
                pending_background_tasks = [task for task in background_tasks if not task.done()]
                for task in pending_background_tasks:
                    task.cancel()
                if pending_background_tasks:
                    await asyncio.gather(*pending_background_tasks, return_exceptions=True)
                await application.shutdown()

        asyncio.run(lifecycle())

    monkeypatch.setattr(Application, "run_polling", mocked_polling)

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        bot.run()

    assert build_calls == [True]
    assert processed == [1, 2]
    assert errors == []
    assert stopped_background_tasks == [True]
    assert cancelled == {"alerts", "managed", "farming-history"}
    assert not [warning for warning in caught_warnings if "Application.create_task" in str(warning.message)]
    assert "getMe" in telegram_request.methods
    assert telegram_request.methods.count("getMyDefaultAdministratorRights") == 2
    assert telegram_request.methods.count("setMyDefaultAdministratorRights") == 2
    assert "setMyCommands" in telegram_request.methods
    assert telegram_request.methods.count("sendMessage") == 1
    assert telegram_request.methods.count("deleteMessage") == 1
    assert telegram_request.methods.count("sendPhoto") == 1
    assert telegram_request.methods.count("answerCallbackQuery") == 1
    assert updates_request.methods == []
