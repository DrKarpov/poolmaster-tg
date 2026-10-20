import asyncio
from io import BytesIO
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import bot as bot_module
from bot import PoolmasterBot
from farming import SourceStatus
from farming_history import FarmingHistoryStore, FarmingHistorySummary
from localization import translate
from price import PriceMetrics
from snapshot import SnapshotManager


class _HistoryMessage:
    def __init__(self, events):
        self.chat = SimpleNamespace(id=7, type="private")
        self.message_id = 42
        self.events = events
        self.replies = []

    async def delete(self):
        self.events.append("delete")

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _HistoryQuery:
    def __init__(self, data, events):
        self.data = data
        self.from_user = SimpleNamespace(id=7)
        self.events = events
        self.message = _HistoryMessage(events)
        self.answers = []
        self.media_edits = []

    async def answer(self, text="", **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_media(self, *, media, reply_markup=None):
        self.events.append("edit_media")
        self.media_edits.append((media, reply_markup))


def _bot(tmp_path, monkeypatch, *, language="ru"):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setenv("AUTO_PUSH_STATE_PATH", str(tmp_path / "auto-push.json"))
    bot = PoolmasterBot()
    bot.user_alert_store.set_language(7, language)
    return bot


def _chart(caption):
    image = BytesIO(b"synthetic farming history PNG")
    image.name = "farming-history.png"
    return SimpleNamespace(caption=caption, image=image)


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_history_callback_replaces_farming_text_with_photo(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)
    events = []
    summary = SimpleNamespace(revision=1)

    class SnapshotReader:
        last_history_write_error = None

        async def get_snapshot(self):
            events.append("snapshot")
            return object()

    class HistoryStore:
        def get_summary(self, *, as_of):
            events.append("summary")
            assert as_of.tzinfo is not None
            return summary

    class Telegram:
        def __init__(self):
            self.photos = []

        async def send_photo(self, **kwargs):
            events.append("send_photo")
            self.photos.append(kwargs)

    telegram = Telegram()
    bot.snapshot_manager = SnapshotReader()
    bot.farming_history_store = HistoryStore()

    def render(received, language, *, as_of):
        events.append("render")
        assert received is summary
        assert language == "ru"
        assert as_of.tzinfo is not None
        return _chart("История доходности")

    monkeypatch.setattr(bot_module, "build_farming_history_chart", render, raising=False)
    query = _HistoryQuery("ui:farming:history", events)
    context = SimpleNamespace(user_data={}, bot=telegram)

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), context))

    assert events == ["snapshot", "summary", "render", "send_photo", "delete"]
    assert query.answers == [("", {})]
    assert telegram.photos[0]["chat_id"] == 7
    assert telegram.photos[0]["caption"] == "История доходности"
    callbacks = _callbacks(telegram.photos[0]["reply_markup"])
    assert callbacks == ["ui:farming:history:back"]
    button = telegram.photos[0]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Назад"
    assert button.style == "danger"


def test_group_farming_command_sends_the_weekly_history_chart(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)
    summary = SimpleNamespace(revision=7)
    events = []

    class SnapshotReader:
        last_history_write_error = None

        async def get_snapshot(self):
            events.append("snapshot")
            return SimpleNamespace(farming={}, anomalies={})

    class HistoryStore:
        def get_summary(self, *, as_of):
            events.append("summary")
            assert as_of.tzinfo is not None
            return summary

    class Message:
        async def reply_photo(self, **kwargs):
            events.append("reply_photo")
            self.photo = kwargs

        async def reply_text(self, *_args, **_kwargs):
            pytest.fail("group /farming should return the history chart, not text only")

    message = Message()
    bot.snapshot_manager = SnapshotReader()
    bot.farming_history_store = HistoryStore()
    bot._allow_group_refresh = lambda _chat: True
    bot.stats_store.track = lambda *_args, **_kwargs: None
    bot.delete_command_message = lambda *_args: asyncio.sleep(0)

    def render(received, language, *, as_of):
        events.append("render")
        assert received is summary
        assert language == "ru"
        assert as_of.tzinfo is not None
        return _chart("График истории farming")

    monkeypatch.setattr(bot_module, "build_farming_history_chart", render)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
        effective_user=SimpleNamespace(id=111, username=None, first_name="Tester", last_name=None),
        effective_message=message,
    )

    asyncio.run(bot.farming(update, None))

    assert events == ["snapshot", "summary", "render", "reply_photo"]
    assert message.photo["caption"] == "График истории farming"
    assert message.photo["reply_markup"] is None


@pytest.mark.parametrize(("freshness_seconds", "expected_fetches"), [(3600, 1), (1, 2)])
def test_history_open_uses_snapshot_manager_freshness(tmp_path, monkeypatch, freshness_seconds, expected_fetches):
    bot = _bot(tmp_path, monkeypatch)
    events = []
    calls = {"farming": 0, "price": 0}
    now = datetime.now(timezone.utc)
    initial_refresh_at = now if freshness_seconds > 1 else now - timedelta(seconds=2)
    store = FarmingHistoryStore(tmp_path / "history.sqlite3")

    async def fetch_farming():
        calls["farming"] += 1
        events.append("fetch_farming")
        return []

    async def fetch_price():
        calls["price"] += 1
        events.append("fetch_price")
        return PriceMetrics(
            price_usdt=Decimal("0.018"),
            bid_usdt=Decimal("0.018"),
            ask_usdt=Decimal("0.018"),
            quote_volume_24h=Decimal("0"),
            symbol="JETTONUSDT",
            quote_currency="USDT",
            source="synthetic test",
            fetched_at=datetime.now(timezone.utc),
            source_timestamp=now,
            status=SourceStatus.OK,
        )

    manager = SnapshotManager(
        fetch_farming,
        fetch_price,
        farming_history_store=store,
        farming_freshness_seconds=freshness_seconds,
        price_freshness_seconds=freshness_seconds,
    )
    bot.snapshot_manager = manager
    bot.farming_history_store = store

    class Telegram:
        photos = []

        async def send_photo(self, **kwargs):
            events.append("send_photo")
            self.photos.append(kwargs)

    telegram = Telegram()

    def render(summary, language, *, as_of):
        events.append("render")
        assert language == "ru"
        assert as_of.tzinfo is not None
        return _chart("История доходности")

    monkeypatch.setattr(bot_module, "build_farming_history_chart", render, raising=False)
    query = _HistoryQuery("ui:farming:history", events)

    async def run():
        await manager.refresh(now=initial_refresh_at)
        events.clear()
        await bot.user_ui_callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}, bot=telegram),
        )

    asyncio.run(run())

    assert calls == {"farming": expected_fetches, "price": expected_fetches}
    assert events[-3:] == ["render", "send_photo", "delete"]
    assert _callbacks(telegram.photos[0]["reply_markup"]) == ["ui:farming:history:back"]


def test_history_open_surfaces_persistence_failure_and_only_offers_back(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)
    events = []

    class SnapshotReader:
        last_history_write_error = OSError("history disk unavailable")

        async def get_snapshot(self):
            events.append("snapshot")
            return object()

    class Telegram:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            events.append("send_error")
            self.messages.append(kwargs)

    class HistoryStore:
        def get_summary(self, *, as_of):
            raise AssertionError("history must not be read when its latest write failed")

    telegram = Telegram()
    bot.snapshot_manager = SnapshotReader()
    bot.farming_history_store = HistoryStore()
    query = _HistoryQuery("ui:farming:history", events)

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), SimpleNamespace(user_data={}, bot=telegram)))

    assert events == ["snapshot", "send_error", "delete"]
    assert telegram.messages[0]["text"] == translate("ru", "farming_history.error.persistence")
    assert _callbacks(telegram.messages[0]["reply_markup"]) == ["ui:farming:history:back"]
    assert query.media_edits == []


def test_history_render_failure_keeps_farming_message_usable(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)
    events = []

    class HistoryStore:
        def get_summary(self, *, as_of):
            return object()

    class SnapshotReader:
        last_history_write_error = None

        async def get_snapshot(self):
            events.append("snapshot")
            return object()

    class Telegram:
        async def send_message(self, **kwargs):
            events.append("send_error")

    bot.farming_history_store = HistoryStore()
    bot.snapshot_manager = SnapshotReader()

    def fail_render(_summary, **_kwargs):
        events.append("render")
        raise ValueError("synthetic render failure")

    monkeypatch.setattr(bot_module, "build_farming_history_chart", fail_render, raising=False)
    query = _HistoryQuery("ui:farming:history", events)

    asyncio.run(
        bot.user_ui_callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}, bot=Telegram()),
        )
    )

    assert events == ["snapshot", "render", "send_error"]


def test_history_back_restores_farming_screen(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)
    events = []

    class SnapshotReader:
        async def get_snapshot(self):
            events.append("snapshot")
            return object()

    class Telegram:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            events.append("send_message")
            self.messages.append(kwargs)

    telegram = Telegram()
    bot.snapshot_manager = SnapshotReader()
    monkeypatch.setattr(
        bot.user_alert_store,
        "seed_missing_baselines",
        lambda user_id, _snapshot: bot.user_alert_store.get_user(user_id),
    )
    monkeypatch.setattr(
        bot_module,
        "render_user_screen",
        lambda screen, *_args, **_kwargs: events.append(f"render:{screen}") or "Farming card",
    )
    query = _HistoryQuery("ui:farming:history:back", events)

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), SimpleNamespace(user_data={}, bot=telegram)))

    assert events == ["snapshot", "render:farming", "delete", "send_message"]
    assert query.answers == [("", {})]
    assert telegram.messages[0]["text"] == "Farming card"
    assert telegram.messages[0]["chat_id"] == 7
    assert "ui:farming:history" in _callbacks(telegram.messages[0]["reply_markup"])


def test_history_callbacks_keep_private_owner_guard(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)
    events = []
    query = _HistoryQuery("ui:farming:history", events)
    query.message.chat.type = "supergroup"
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace())

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), context))

    assert query.answers[0][1]["show_alert"] is True
    assert events == []


def test_empty_history_uses_localized_refresh_and_back_controls(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch, language="en")
    events = []
    empty = FarmingHistorySummary(series=(), period_start=None, period_end=None, overall_mean_pct=None)

    class HistoryStore:
        def get_summary(self, *, as_of):
            return empty

    class SnapshotReader:
        last_history_write_error = None

        async def get_snapshot(self):
            return object()

    class Telegram:
        photos = []

        async def send_photo(self, **kwargs):
            self.photos.append(kwargs)

    telegram = Telegram()
    bot.farming_history_store = HistoryStore()
    bot.snapshot_manager = SnapshotReader()
    query = _HistoryQuery("ui:farming:history", events)

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), SimpleNamespace(user_data={}, bot=telegram)))

    photo = telegram.photos[0]
    assert "No data is saved for completed weeks yet." in photo["caption"]
    assert "00:00 UTC" in photo["caption"]
    assert "00:05 UTC" in photo["caption"]
    buttons = [button for row in photo["reply_markup"].inline_keyboard for button in row]
    assert [button.text for button in buttons] == ["Back"]
    assert buttons[0].style == "danger"
