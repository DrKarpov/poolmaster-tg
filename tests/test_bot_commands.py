import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
import bot as bot_module
from bot import PoolmasterBot, _alert_reference_signature, _alert_snapshot_is_recent, _alert_snapshot_ready
from farming import FARMING_POOLS, IdentityCheck, PoolMetrics, RateMetric, SourceStatus
from price import PriceIdentityCheck, PriceMetrics
from snapshot import DataSnapshot
from telegram.ext import InlineQueryHandler, MessageHandler, filters
from telegram import BotCommandScopeChat, ChatAdministratorRights
from telegram.error import TelegramError


def _make_bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))
    return PoolmasterBot()


def _set_existing_language(bot, *user_ids):
    for user_id in user_ids:
        bot.user_alert_store.set_language(user_id, "ru")


def _admin_rights(*, can_manage_chat=False, can_post_messages=False):
    return ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=can_manage_chat,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_post_messages=can_post_messages,
        can_edit_messages=False,
        can_pin_messages=False,
        can_manage_topics=False,
        can_manage_direct_messages=False,
        can_manage_tags=False,
        api_kwargs={"can_send_welcome_messages": False},
    )


def _minimal_admin_rights(*, for_channels):
    return _admin_rights(
        can_manage_chat=not for_channels,
        can_post_messages=for_channels,
    )


def _reported_minimal_admin_rights(*, for_channels):
    return ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_post_messages=True if for_channels else None,
        can_edit_messages=None,
        can_pin_messages=None,
        can_manage_topics=None,
        can_manage_direct_messages=None,
        can_manage_tags=None,
    )


def _patch_post_init_market_checks(bot, monkeypatch):
    async def identity_checks():
        return {pool.address: IdentityCheck(True, "test identity") for pool in FARMING_POOLS}

    async def price_identity():
        return PriceIdentityCheck(True, "test identity")

    monkeypatch.setattr(bot_module, "verify_farming_pool_identities", identity_checks)
    monkeypatch.setattr(bot, "check_price_identity", price_identity)


def _run_post_init(bot, api, *, monkeypatch):
    _patch_post_init_market_checks(bot, monkeypatch)

    class FakeApplication:
        bot = api

    asyncio.run(bot.post_init(FakeApplication()))


def test_post_init_uses_runtime_bot_identity_and_configures_minimal_default_rights(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_USER_IDS", "101")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "configured_default")
    bot = _make_bot(tmp_path, monkeypatch)
    updates = []

    class FakeBotApi:
        async def get_me(self):
            return SimpleNamespace(id=987654321, username="@runtime_test_bot")

        async def get_my_default_administrator_rights(self, *, for_channels=None):
            return _admin_rights()

        async def set_my_default_administrator_rights(self, *, rights, for_channels=None):
            updates.append((for_channels, rights))

        async def set_my_commands(self, *_args, **_kwargs):
            return None

    _run_post_init(bot, FakeBotApi(), monkeypatch=monkeypatch)

    assert bot.bot_id == 987654321
    assert bot.bot_username == "runtime_test_bot"
    assert len(updates) == 2
    assert updates[0][0] is True
    assert updates[0][1] == _minimal_admin_rights(for_channels=True)
    assert updates[1][0] is False
    assert updates[1][1] == _minimal_admin_rights(for_channels=False)


def test_post_init_does_not_mutate_matching_default_rights(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "101")
    bot = _make_bot(tmp_path, monkeypatch)
    current_reads = []
    updates = []

    class FakeBotApi:
        async def get_me(self):
            return SimpleNamespace(id=987654321, username="runtime_test_bot")

        async def get_my_default_administrator_rights(self, *, for_channels=None):
            current_reads.append(for_channels)
            return _reported_minimal_admin_rights(for_channels=for_channels)

        async def set_my_default_administrator_rights(self, **kwargs):
            updates.append(kwargs)

        async def set_my_commands(self, *_args, **_kwargs):
            return None

    _run_post_init(bot, FakeBotApi(), monkeypatch=monkeypatch)

    assert current_reads == [True, False]
    assert updates == []


def test_post_init_disables_new_welcome_message_default_right_for_both_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "101")
    bot = _make_bot(tmp_path, monkeypatch)
    updates = []

    class FakeBotApi:
        async def get_me(self):
            return SimpleNamespace(id=987654321, username="runtime_test_bot")

        async def get_my_default_administrator_rights(self, *, for_channels=None):
            rights = _reported_minimal_admin_rights(for_channels=for_channels)
            return ChatAdministratorRights.de_json(
                {**rights.to_dict(), "can_send_welcome_messages": True}, None
            )

        async def set_my_default_administrator_rights(self, *, rights, for_channels=None):
            updates.append((for_channels, rights))

        async def set_my_commands(self, *_args, **_kwargs):
            return None

    _run_post_init(bot, FakeBotApi(), monkeypatch=monkeypatch)

    assert [for_channels for for_channels, _rights in updates] == [True, False]
    assert all(rights.to_dict()["can_send_welcome_messages"] is False for _, rights in updates)


def test_post_init_sanitizes_default_rights_api_errors_and_continues_startup(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("ADMIN_USER_IDS", "101")
    bot = _make_bot(tmp_path, monkeypatch)
    command_calls = []
    successful_updates = []

    class FakeBotApi:
        async def get_me(self):
            return SimpleNamespace(id=987654321, username="runtime_test_bot")

        async def get_my_default_administrator_rights(self, *, for_channels=None):
            return _admin_rights()

        async def set_my_default_administrator_rights(self, *, rights, for_channels=None):
            if for_channels:
                raise TelegramError("sensitive API response detail")
            successful_updates.append((for_channels, rights))

        async def set_my_commands(self, *_args, **_kwargs):
            command_calls.append(True)

    with caplog.at_level(logging.WARNING):
        _run_post_init(bot, FakeBotApi(), monkeypatch=monkeypatch)

    assert successful_updates == [(False, _minimal_admin_rights(for_channels=False))]
    assert command_calls
    assert "error_type=TelegramError" in caplog.text
    assert "sensitive API response detail" not in caplog.text


def test_alert_preview_readiness_and_signature_accept_partial_primary_yield():
    now = datetime.now(timezone.utc)
    pool = FARMING_POOLS[0]
    metric = PoolMetrics(
        pool=pool,
        farm_rates=(RateMetric(Decimal("12.5"), "APR", "apr_rewards"),),
        pool_rates=(),
        total_rate=None,
        tvl_usd=None,
        fetched_at=now,
        status=SourceStatus.PARTIAL,
        identity_valid=True,
    )
    price = PriceMetrics(
        price_usdt=Decimal("0.02"),
        bid_usdt=None,
        ask_usdt=None,
        quote_volume_24h=None,
        symbol="JETTONUSDT",
        quote_currency="USDT",
        source="MEXC Spot",
        fetched_at=now,
        source_timestamp=now,
        status=SourceStatus.OK,
    )
    snapshot = DataSnapshot(now, {pool.address: metric}, price, {}, {})

    assert _alert_snapshot_ready("yield", snapshot, set()) is True
    assert _alert_snapshot_is_recent("yield", snapshot, 60) is True
    assert _alert_reference_signature("yield", snapshot, set()) == {
        pool.address: {"value": "12.5", "kind": "APR", "source_field": "apr_rewards"}
    }


def test_text_command_parser_accepts_our_bot_mention(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)

    parsed = bot.parse_text_command("/farming@JETTONPOOL_BOT")

    assert parsed == ("farming", "JETTONPOOL_BOT")
    assert bot.is_command_for_this_bot(parsed[1])


def test_main_suppresses_httpx_request_logs_before_startup(monkeypatch):
    root_logger = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(root_logger, "level", logging.INFO)
    monkeypatch.setattr(httpx_logger, "level", logging.NOTSET)
    observed_levels = []

    class FakePoolmasterBot:
        def run(self):
            observed_levels.append(httpx_logger.getEffectiveLevel())

    monkeypatch.setattr(bot_module, "PoolmasterBot", FakePoolmasterBot)

    bot_module.main()

    assert observed_levels == [logging.WARNING]


def test_admin_start_sets_admin_command_scope_after_private_chat_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "101")
    bot = _make_bot(tmp_path, monkeypatch)
    calls = []

    class FakeBotApi:
        async def set_my_commands(self, commands, *, scope=None):
            calls.append((commands, scope))

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot, "delete_command_message", no_op)
    monkeypatch.setattr(bot.stats_store, "track", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "send_user_screen", no_op)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=101),
        effective_chat=SimpleNamespace(id=101, type="private"),
        effective_message=SimpleNamespace(),
    )

    asyncio.run(bot.start(update, SimpleNamespace(bot=FakeBotApi())))

    assert len(calls) == 1
    commands, scope = calls[0]
    assert isinstance(scope, BotCommandScopeChat)
    assert scope.chat_id == 101
    assert {command.command: command.description for command in commands} == {
        "start": "Меню",
        "farming": "Фарминг",
        "price": "Цена",
        "admin": "Админ-панель",
    }


def test_run_logs_the_configured_bot_username(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "TESTPOOLMASTERBOT")
    bot = _make_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot, "build_application", lambda: SimpleNamespace(run_polling=lambda: None))

    with caplog.at_level(logging.INFO):
        bot.run()

    assert "Open Telegram and send /farming to @TESTPOOLMASTERBOT." in caplog.text
    assert "@JETTONPOOL_BOT" not in caplog.text


def test_bot_startup_migrates_legacy_private_subscribers_after_backup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    legacy_path = tmp_path / "subscribers.json"
    legacy_path.write_text('{"subscribers": [123, -100555]}', encoding="utf-8")
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(legacy_path))
    monkeypatch.setenv("USER_STATE_PATH", str(tmp_path / "user_state.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))

    bot = PoolmasterBot()

    assert bot.user_alert_store.list_user_ids() == [123]
    backups = list(tmp_path.glob("subscribers.json.backup-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"subscribers": [123, -100555]}'


def test_private_start_registers_user_identity_in_alert_store(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot.stats_store, "track", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "delete_command_message", lambda *_args: asyncio.sleep(0))
    monkeypatch.setattr(bot, "send_user_screen", lambda *_args: asyncio.sleep(0))
    user = SimpleNamespace(id=7, username="test_user", first_name="Test", last_name="User")
    update = SimpleNamespace(
        effective_user=user,
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_message=SimpleNamespace(),
    )

    asyncio.run(bot.start(update, SimpleNamespace(bot=SimpleNamespace())))

    record = bot.user_alert_store.get_user(7)
    assert record["registered"] is True
    assert record["username"] == "test_user"
    assert bot.user_alert_store.list_active_user_ids() == [7]


def test_group_start_has_no_menu_response_or_pool_links(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot.stats_store, "track", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot, "delete_command_message", lambda *_args: asyncio.sleep(0))
    replies = []

    async def reply_text(text, **kwargs):
        replies.append((text, kwargs))

    message = SimpleNamespace(reply_text=reply_text)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7, username="group_user", first_name="Group", last_name=None),
        effective_chat=SimpleNamespace(id=-100777, type="supergroup"),
        effective_message=message,
    )

    asyncio.run(bot.start(update, SimpleNamespace(bot=SimpleNamespace())))

    assert bot.user_alert_store.list_registered_user_ids() == []
    assert replies == []


def test_application_does_not_register_web_app_updates(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)

    class CapturingApplication:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, *_args, **_kwargs):
            self.handlers.append(handler)

        def add_error_handler(self, *_args, **_kwargs):
            pass

        def run_polling(self):
            pass

    class CapturingBuilder:
        def token(self, _token):
            return self

        def application_class(self, _application_class, kwargs=None):
            return self

        def post_init(self, _callback):
            return self

        def build(self):
            self.application = CapturingApplication()
            return self.application

    builder = CapturingBuilder()
    monkeypatch.setattr(bot_module, "ApplicationBuilder", lambda: builder)

    bot.run()

    commands = {
        command
        for handler in builder.application.handlers
        if hasattr(handler, "commands")
        for command in handler.commands
    }
    assert {"start", "price", "farming", "settings", "chart"} <= commands
    assert "push_farming" not in commands
    assert "auto_status" not in commands

    web_app_handlers = [
        handler
        for handler in builder.application.handlers
        if isinstance(handler, MessageHandler)
        and handler.filters == filters.StatusUpdate.WEB_APP_DATA
    ]
    assert web_app_handlers == []
    assert any(isinstance(handler, InlineQueryHandler) for handler in builder.application.handlers)


def test_startup_identity_mismatch_is_recorded_without_blocking_other_pools(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    checks = {
        pool.address: IdentityCheck(True, "pool and asset metadata match")
        for pool in FARMING_POOLS
    }
    checks[FARMING_POOLS[1].address] = IdentityCheck(False, "USDT asset kind mismatch")
    checks[FARMING_POOLS[2].address] = IdentityCheck(None, "identity metadata source unavailable")

    async def fake_verify():
        return checks

    monkeypatch.setattr(bot_module, "verify_farming_pool_identities", fake_verify)

    asyncio.run(bot.initialize_pool_identity_checks())

    assert bot.invalid_pool_ids == {FARMING_POOLS[1].address}
    assert bot.pool_identity_checks[FARMING_POOLS[2].address].valid is None


def test_startup_invalid_identity_is_applied_before_cache_or_delivery(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.invalid_pool_ids = {FARMING_POOLS[0].address}
    metrics = [
        PoolMetrics(
            pool=pool,
            farm_rates=(),
            pool_rates=(),
            total_rate=None,
            tvl_usd=None,
            fetched_at=datetime.now(timezone.utc),
            status=SourceStatus.OK,
            identity_valid=True,
        )
        for pool in FARMING_POOLS[:2]
    ]

    checked = bot.apply_pool_identity_gate(metrics)

    assert checked[0].identity_valid is False
    assert checked[0].status == SourceStatus.INVALID_RESPONSE
    assert checked[1].identity_valid is True
    assert checked[1].status == SourceStatus.OK


def test_farming_shortcut_reads_the_shared_snapshot(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 7)
    pool = FARMING_POOLS[0]
    metrics = PoolMetrics(
        pool=pool,
        farm_rates=(RateMetric(Decimal("12.5"), "APR", "apr_rewards"),),
        pool_rates=(),
        total_rate=None,
        tvl_usd=None,
        fetched_at=datetime.now(timezone.utc),
        status=SourceStatus.OK,
        identity_valid=True,
    )
    price = PriceMetrics(
        price_usdt=Decimal("0.01865"),
        bid_usdt=None,
        ask_usdt=None,
        quote_volume_24h=None,
        symbol="JETTONUSDT",
        quote_currency="USDT",
        source="MEXC spot",
        fetched_at=datetime.now(timezone.utc),
        source_timestamp=datetime.now(timezone.utc),
        status=SourceStatus.OK,
    )
    snapshot = DataSnapshot(
        fetched_at=datetime.now(timezone.utc),
        farming={pool.address: metrics},
        price=price,
        source_statuses={"farming": "ok", "price": "ok"},
        anomalies={},
    )
    snapshot_calls = 0
    replies = []

    class SnapshotReader:
        async def get_snapshot(self, *, force=False):
            nonlocal snapshot_calls
            snapshot_calls += 1
            return snapshot

    class Message:
        async def reply_text(self, text, **_kwargs):
            replies.append(text)

    bot.snapshot_manager = SnapshotReader()
    monkeypatch.setattr(bot_module, "fetch_all_farming_info", lambda: pytest.fail("shortcut fetched outside snapshot"))
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7, username=None, first_name="Tester", last_name=None),
        effective_message=Message(),
    )

    asyncio.run(bot.send_farming_info(update))

    assert snapshot_calls == 1
    assert "Доходность: <b>12.50%</b>" in replies[-1]


def test_global_pause_is_read_from_persistent_admin_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_PUSH_ENABLED", "true")
    monkeypatch.setenv("GLOBAL_AUTO_PUSH_PAUSED", "true")
    bot = _make_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bot_module,
        "verify_farming_pool_identities",
        lambda: _resolved({pool.address: IdentityCheck(True, "ok") for pool in FARMING_POOLS}),
    )
    monkeypatch.setattr(
        bot_module,
        "verify_mexc_market_identity",
        lambda: _resolved(True, identity=True),
    )
    scheduled = []

    class FakeBotApi:
        username = "poolmaster_test"

        async def get_me(self):
            return SimpleNamespace(id=987654321, username=self.username)

        async def get_my_default_administrator_rights(self, *, for_channels=None):
            return _admin_rights()

        async def set_my_default_administrator_rights(self, **_kwargs):
            return True

        async def set_my_commands(self, _commands, **_kwargs):
            return None

    class FakeApplication:
        bot = FakeBotApi()

        def create_task(self, task):
            scheduled.append(task.cr_code.co_name)
            task.close()

    asyncio.run(bot.post_init(FakeApplication()))

    assert scheduled == []
    assert bot.admin_state_store.is_global_auto_push_paused() is True


def test_global_user_alert_pause_does_not_pause_channel_autopost(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBAL_AUTO_PUSH_PAUSED", "true")
    bot = _make_bot(tmp_path, monkeypatch)
    bot.managed_destination_store.register_destination(
        -100555,
        "Paused test channel",
        "channel",
        actor_id=123456789,
        admin_ids={123456789},
    )
    bot.managed_destination_store.set_schedule(
        -100555,
        3,
        actor_id=123456789,
        admin_ids={123456789},
    )

    now = datetime.now(timezone.utc)
    farming = {}
    for pool in FARMING_POOLS:
        rate = RateMetric(Decimal("5"), "APR", "apr_rewards")
        farming[pool.address] = PoolMetrics(
            pool,
            (rate,),
            (),
            rate,
            Decimal("1000"),
            now,
            SourceStatus.OK,
            True,
        )
    snapshot = DataSnapshot(
        now,
        farming,
        PriceMetrics(Decimal("0.02"), None, None, None, "JETTONUSDT", "USDT", "MEXC", now, now, SourceStatus.OK),
        {"farming": "ok", "price": "ok"},
        {},
    )
    snapshot_calls = []

    class SnapshotReader:
        async def get_snapshot(self, **_kwargs):
            snapshot_calls.append(True)
            return snapshot

    bot.snapshot_manager = SnapshotReader()

    class FakeTelegramBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)

    telegram = FakeTelegramBot()

    async def stop_after_one_cycle(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(bot_module.asyncio, "sleep", stop_after_one_cycle)
    application = SimpleNamespace(bot=telegram)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.alert_loop(application))
    assert snapshot_calls == []

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.managed_publication_loop(application))
    assert snapshot_calls == [True]
    assert [message["chat_id"] for message in telegram.messages] == [-100555]


def test_post_init_does_not_start_background_loops_before_application_start(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_PUSH_ENABLED", "true")
    monkeypatch.setenv("GLOBAL_AUTO_PUSH_PAUSED", "false")
    bot = _make_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bot_module,
        "verify_farming_pool_identities",
        lambda: _resolved({pool.address: IdentityCheck(True, "ok") for pool in FARMING_POOLS}),
    )
    monkeypatch.setattr(
        bot_module,
        "verify_mexc_market_identity",
        lambda: _resolved(True, identity=True),
    )
    scheduled = []

    class FakeBotApi:
        username = "poolmaster_test"

        async def get_me(self):
            return SimpleNamespace(id=987654321, username=self.username)

        async def get_my_default_administrator_rights(self, *, for_channels=None):
            return _admin_rights()

        async def set_my_default_administrator_rights(self, **_kwargs):
            return True

        async def set_my_commands(self, _commands, **_kwargs):
            return None

    class FakeApplication:
        bot = FakeBotApi()

        def create_task(self, task):
            scheduled.append(task.cr_code.co_name)
            task.close()

    asyncio.run(bot.post_init(FakeApplication()))

    assert scheduled == []


def _resolved(value, *, identity=False):
    async def result():
        if identity:
            return PriceIdentityCheck(value, "test identity result")
        return value

    return result()


def test_group_farming_refresh_is_throttled_per_chat(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    calls = []

    async def capture_farming(update):
        calls.append(update.effective_user.id)

    bot.send_farming_info = capture_farming

    chat = SimpleNamespace(id=-100123, type="supergroup")
    message = SimpleNamespace()
    first = SimpleNamespace(
        effective_chat=chat,
        effective_message=message,
        effective_user=SimpleNamespace(id=111, username=None, first_name="One", last_name=None),
    )
    second = SimpleNamespace(
        effective_chat=chat,
        effective_message=message,
        effective_user=SimpleNamespace(id=222, username=None, first_name="Two", last_name=None),
    )

    asyncio.run(bot.farming(first, None))
    asyncio.run(bot.farming(second, None))

    assert calls == [111]


def test_third_party_group_farming_does_not_create_managed_destination(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    calls = []

    async def capture_farming(update):
        calls.append(update.effective_chat.id)

    bot.send_farming_info = capture_farming
    chat = SimpleNamespace(id=-100555, type="supergroup")
    message = SimpleNamespace()
    update = SimpleNamespace(
        effective_chat=chat,
        effective_message=message,
        effective_user=SimpleNamespace(id=333, username="ordinary_user", first_name="User", last_name=None),
    )

    asyncio.run(bot.farming(update, None))

    assert calls == [-100555]
    assert bot.managed_destination_store.list_destinations() == []


def test_chart_command_uses_default_1h_and_is_throttled_per_chat(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 111)
    bot.price_identity_check = PriceIdentityCheck(True, "test market verified")
    calls = []

    async def fake_fetch_chart(pool, timeframe, *, max_cache_age_seconds=None):
        calls.append((pool, timeframe, max_cache_age_seconds))
        return SimpleNamespace(image=object(), caption="chart")

    monkeypatch.setattr(bot_module, "fetch_chart", fake_fetch_chart)

    class Message:
        async def reply_photo(self, **_kwargs):
            pass

    chat = SimpleNamespace(id=123, type="private")
    update = SimpleNamespace(
        effective_chat=chat,
        effective_message=Message(),
        effective_user=SimpleNamespace(id=111, username=None, first_name="One", last_name=None),
    )

    asyncio.run(bot.chart(update, None))
    asyncio.run(bot.chart(update, None))

    assert calls == [("mexc", "1h", 10)]


def test_chart_command_fails_closed_when_mexc_identity_is_unverified(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 111)
    photos = []

    async def unexpected_fetch(*_args):
        raise AssertionError("an unverified market must not fetch or serve a cached chart")

    monkeypatch.setattr(bot_module, "fetch_chart", unexpected_fetch)

    class Message:
        async def reply_photo(self, **kwargs):
            photos.append(kwargs)

    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=123, type="private"),
        effective_message=Message(),
        effective_user=SimpleNamespace(id=111, username=None, first_name="One", last_name=None),
    )

    asyncio.run(bot.chart(update, None))

    assert len(photos) == 1
    assert "не пройдена" in photos[0]["caption"].lower()


def test_chart_callbacks_are_throttled_per_chat(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 111, 222)
    bot.price_identity_check = PriceIdentityCheck(True, "test market verified")
    calls = []

    async def fake_fetch_chart(pool, timeframe):
        calls.append((pool, timeframe))
        image = BytesIO(b"png")
        image.name = "chart.png"
        return SimpleNamespace(image=image, caption="chart")

    monkeypatch.setattr(bot_module, "fetch_chart", fake_fetch_chart)

    class Query:
        def __init__(self, data, user_id):
            self.data = data
            self.from_user = SimpleNamespace(id=user_id)
            self.message = SimpleNamespace(chat=SimpleNamespace(id=user_id, type="private"))
            self.answers = []

        async def answer(self, text=None, **_kwargs):
            self.answers.append(text)

        async def edit_message_media(self, **_kwargs):
            pass

    first_query = Query("chart:mexc:1h", user_id=111)
    second_query = Query("chart:mexc:12h", user_id=222)
    asyncio.run(bot.chart_callback(SimpleNamespace(callback_query=first_query), None))
    asyncio.run(bot.chart_callback(SimpleNamespace(callback_query=second_query), None))

    assert calls == [("mexc", "1h"), ("mexc", "12h")]
    assert first_query.answers[0].startswith("Обновляю")
    assert second_query.answers[0].startswith("Обновляю")


def test_chart_refresh_answers_before_forced_mexc_fetch(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 111)
    bot.price_identity_check = PriceIdentityCheck(True, "verified")
    order = []

    async def fake_fetch_chart(pool, timeframe, *, force_refresh=False):
        order.append(("fetch", pool, timeframe, force_refresh))
        image = BytesIO(b"png")
        image.name = "chart.png"
        return SimpleNamespace(image=image, caption="refreshed")

    monkeypatch.setattr(bot_module, "fetch_chart", fake_fetch_chart)

    class Query:
        data = "chart:mexc:refresh"
        from_user = SimpleNamespace(id=111)
        message = SimpleNamespace(chat=SimpleNamespace(id=111, type="private"), message_id=42)

        async def answer(self, text=None, **_kwargs):
            order.append(("answer", text))

        async def edit_message_media(self, **_kwargs):
            order.append(("edit",))

    asyncio.run(bot.chart_callback(SimpleNamespace(callback_query=Query()), SimpleNamespace()))

    assert order[0] == ("answer", "Обновляю график…")
    assert order[1] == ("fetch", "mexc", "1h", True)
    assert order[2] == ("edit",)


@pytest.mark.parametrize(("callback_data", "timeframe"), [
    ("chart:mexc:refresh:1h", "1h"),
    ("chart:mexc:refresh:12h", "12h"),
])
def test_chart_refresh_preserves_selected_timeframe(tmp_path, monkeypatch, callback_data, timeframe):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 111)
    bot.price_identity_check = PriceIdentityCheck(True, "verified")
    calls = []

    async def fake_fetch_chart(pool, requested, *, force_refresh=False):
        calls.append((pool, requested, force_refresh))
        image = BytesIO(b"png")
        image.name = "chart.png"
        return SimpleNamespace(image=image, caption="refreshed")

    monkeypatch.setattr(bot_module, "fetch_chart", fake_fetch_chart)

    class Query:
        data = callback_data
        from_user = SimpleNamespace(id=111)
        message = SimpleNamespace(chat=SimpleNamespace(id=111, type="private"), message_id=42)

        async def answer(self, *_args, **_kwargs):
            pass

        async def edit_message_media(self, **_kwargs):
            pass

    asyncio.run(bot.chart_callback(SimpleNamespace(callback_query=Query()), SimpleNamespace()))

    assert calls == [("mexc", timeframe, True)]


def test_chart_refresh_uses_ten_second_cooldown_toast(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 111)
    bot.price_identity_check = PriceIdentityCheck(True, "verified")
    bot.cooldowns.allow(("chart_refresh", 111), 10)

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("throttled chart refresh must not fetch")

    monkeypatch.setattr(bot_module, "fetch_chart", unexpected_fetch)

    class Query:
        data = "chart:mexc:refresh"
        from_user = SimpleNamespace(id=111)
        message = SimpleNamespace(chat=SimpleNamespace(id=111, type="private"), message_id=42)

        def __init__(self):
            self.answers = []

        async def answer(self, text=None, **_kwargs):
            self.answers.append(text)

    query = Query()
    asyncio.run(bot.chart_callback(SimpleNamespace(callback_query=query), SimpleNamespace()))

    assert query.answers == ["Обновить можно раз в 10 секунд."]


def test_chart_callback_fails_closed_when_mexc_identity_is_invalid(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 111)
    bot.price_identity_check = PriceIdentityCheck(False, "test mismatch")
    edits = []

    async def unexpected_fetch(*_args):
        raise AssertionError("an invalid market must not fetch or serve a cached chart")

    monkeypatch.setattr(bot_module, "fetch_chart", unexpected_fetch)

    class Query:
        data = "chart:mexc:1h"
        from_user = SimpleNamespace(id=111)
        message = SimpleNamespace(chat=SimpleNamespace(id=111, type="private"))

        async def answer(self, *_args, **_kwargs):
            pass

        async def edit_message_media(self, **kwargs):
            edits.append(kwargs)

    asyncio.run(bot.chart_callback(SimpleNamespace(callback_query=Query()), None))

    assert len(edits) == 1
    assert "не пройдена" in edits[0]["media"].caption.lower()


def test_private_chart_back_replaces_photo_with_home_screen(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 111)
    deleted = []
    sent = []

    async def delete_message(**kwargs):
        deleted.append(kwargs)

    async def send_message(**kwargs):
        sent.append(kwargs)

    class Query:
        data = "chart:back"
        from_user = SimpleNamespace(id=111)
        message = SimpleNamespace(chat=SimpleNamespace(id=111, type="private"), message_id=42)

        async def answer(self, *_args, **_kwargs):
            self.answered = True

    monkeypatch.setattr("bot.render_user_screen", lambda *_args, **_kwargs: "POOLMASTER home")
    query = Query()
    context = SimpleNamespace(bot=SimpleNamespace(delete_message=delete_message, send_message=send_message))

    asyncio.run(bot.chart_callback(SimpleNamespace(callback_query=query), context))

    assert query.answered is True
    assert deleted == [{"chat_id": 111, "message_id": 42}]
    assert sent[0]["chat_id"] == 111
    assert sent[0]["text"] == "POOLMASTER home"
    assert [button.text for row in sent[0]["reply_markup"].inline_keyboard for button in row] == [
        "Цена", "Фарминг", "Уведомления", "Язык / Language"
    ]


def test_private_price_command_opens_default_1h_chart(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _set_existing_language(bot, 111)
    calls = []
    photos = []

    async def fake_chart(timeframe, *, max_cache_age_seconds=None):
        calls.append((timeframe, max_cache_age_seconds))
        return SimpleNamespace(image=object(), caption="chart")

    class Message:
        async def reply_photo(self, **kwargs):
            photos.append(kwargs)

    bot.fetch_verified_mexc_chart = fake_chart
    async def no_op(*_args, **_kwargs):
        return None

    bot.send_user_screen = no_op
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=111, type="private"),
        effective_user=SimpleNamespace(id=111, username=None, first_name="Admin", last_name=None),
        effective_message=Message(),
    )

    asyncio.run(bot.price_command(update, None))

    assert calls == [("1h", 10)]
    assert photos[0]["caption"] == "chart"
    assert photos[0]["reply_markup"] is not None


def test_group_price_command_sends_the_verified_1h_chart_without_private_controls(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    calls = []
    photos = []

    async def fake_chart(timeframe, *, max_cache_age_seconds=None):
        calls.append((timeframe, max_cache_age_seconds))
        return SimpleNamespace(image="fresh-chart", caption="Verified MEXC chart")

    async def no_op(*_args, **_kwargs):
        return None

    class Message:
        async def reply_photo(self, **kwargs):
            photos.append(kwargs)

        async def reply_text(self, *_args, **_kwargs):
            pytest.fail("group /price should send the chart, not a text-only price card")

    bot.fetch_verified_mexc_chart = fake_chart
    bot.delete_command_message = no_op
    bot._allow_group_refresh = lambda _chat: True
    bot.stats_store.track = lambda *_args, **_kwargs: None
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
        effective_user=SimpleNamespace(id=111, username=None, first_name="Tester", last_name=None),
        effective_message=Message(),
    )

    asyncio.run(bot.price(update, None))

    assert calls == [("1h", 10)]
    assert photos == [{
        "photo": "fresh-chart",
        "caption": "Verified MEXC chart",
        "parse_mode": bot_module.ParseMode.HTML,
        "reply_markup": None,
    }]


def test_chart_keyboard_has_only_two_timeframes_refresh_and_back(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)

    markup = bot.chart_keyboard("mexc", "1h")
    rows = markup.inline_keyboard
    buttons = [button for row in rows for button in row]

    callbacks = {button.callback_data for button in buttons if button.callback_data}
    assert callbacks == {"chart:mexc:1h", "chart:mexc:12h", "chart:mexc:refresh:1h", "chart:back"}
    assert [[button.text for button in row] for row in rows] == [["• 1ч", "12ч"], ["Обновить"], ["Назад"]]
    assert all(button.url is None for button in buttons)
    assert bot.chart_keyboard("mexc", "12h").inline_keyboard[1][0].callback_data == "chart:mexc:refresh:12h"
