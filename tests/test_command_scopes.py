import asyncio
from types import SimpleNamespace

import bot as bot_module
from bot import PoolmasterBot
from farming import FARMING_POOLS, IdentityCheck
from telegram import (
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ChatAdministratorRights,
)
from price import PriceIdentityCheck


def test_user_and_admin_command_menus_use_separate_scopes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("ADMIN_USER_IDS", "111,222")
    monkeypatch.setenv("ADMIN_STATE_PATH", str(tmp_path / "admin.json"))
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("USER_STATE_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("MANAGED_DESTINATIONS_PATH", str(tmp_path / "destinations.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))
    bot = PoolmasterBot()

    async def identity_checks():
        return {pool.address: IdentityCheck(True, "test identity") for pool in FARMING_POOLS}

    async def price_identity():
        return PriceIdentityCheck(True, "test identity")

    monkeypatch.setattr(bot_module, "verify_farming_pool_identities", identity_checks)
    monkeypatch.setattr(bot, "check_price_identity", price_identity)
    command_sets = []

    class FakeBotApi:
        username = "poolmaster_test"

        async def get_me(self):
            return SimpleNamespace(id=987654321, username=self.username)

        async def get_my_default_administrator_rights(self, *, for_channels=None):
            return ChatAdministratorRights(
                is_anonymous=False,
                can_manage_chat=False,
                can_delete_messages=False,
                can_manage_video_chats=False,
                can_restrict_members=False,
                can_promote_members=False,
                can_change_info=False,
                can_invite_users=False,
                can_post_stories=False,
                can_edit_stories=False,
                can_delete_stories=False,
                can_post_messages=False,
                can_edit_messages=False,
                can_pin_messages=False,
                can_manage_topics=False,
                can_manage_direct_messages=False,
                can_manage_tags=False,
            )

        async def set_my_default_administrator_rights(self, **_kwargs):
            return True

        async def set_my_commands(self, commands, *, scope=None):
            command_sets.append((commands, scope))

    class FakeApplication:
        bot = FakeBotApi()

        def create_task(self, task):
            task.close()

    asyncio.run(bot.post_init(FakeApplication()))

    assert len(command_sets) == 4
    default_commands, default_scope = command_sets[0]
    assert isinstance(default_scope, BotCommandScopeDefault)
    assert {command.command: command.description for command in default_commands} == {
        "start": "Меню",
        "farming": "Фарминг",
        "price": "Цена",
    }
    assert "admin" not in {command.command for command in default_commands}
    assert "push_farming" not in {command.command for command in default_commands}

    group_commands, group_scope = command_sets[1]
    assert isinstance(group_scope, BotCommandScopeAllGroupChats)
    assert {command.command for command in group_commands} == {"farming", "price"}

    scoped = {
        scope.chat_id: {command.command: command.description for command in commands}
        for commands, scope in command_sets
        if isinstance(scope, BotCommandScopeChat)
    }
    assert set(scoped) == {111, 222}
    expected_admin = {
        "start": "Меню",
        "farming": "Фарминг",
        "price": "Цена",
        "admin": "Админ-панель",
    }
    assert all(commands == expected_admin for commands in scoped.values())
    assert all(isinstance(scope, BotCommandScopeChat) for _commands, scope in command_sets[2:])
