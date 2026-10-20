import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
import pytest

from bot import PoolmasterBot
from farming import FARMING_POOLS, PoolMetrics, RateMetric, SourceStatus
from price import PriceIdentityCheck, PriceMetrics
from snapshot import DataSnapshot
from user_ui import private_callback_is_owned, render_user_screen


def _snapshot():
    now = datetime.now(timezone.utc)
    pool = FARMING_POOLS[0]
    farming = PoolMetrics(
        pool=pool,
        farm_rates=(RateMetric(Decimal("12.5"), "APR", "apr_rewards"),),
        pool_rates=(),
        total_rate=RateMetric(Decimal("12.5"), "APR", "apr"),
        tvl_usd=None,
        fetched_at=now,
        status=SourceStatus.OK,
        identity_valid=True,
    )
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
        farming={pool.address: farming},
        price=price,
        source_statuses={"farming": "ok", "price": "ok"},
        anomalies={},
    )


def test_price_screen_labels_quote_and_stale_status():
    snapshot = _snapshot()
    snapshot = snapshot.__class__(
        fetched_at=snapshot.fetched_at,
        farming=snapshot.farming,
        price=PriceMetrics(
            **{
                **snapshot.price.__dict__,
                "is_stale": True,
            }
        ),
        source_statuses=snapshot.source_statuses,
        anomalies=snapshot.anomalies,
    )

    text = render_user_screen("price", snapshot)

    assert "0.01865 USDT" in text
    assert "MEXC Spot" in text
    assert "временно не обновлялась" in text.lower()
    assert "Bid/Ask" not in text
    assert "Объём" not in text


def test_bounded_stale_price_fallback_is_visible_on_price_and_home_screens():
    snapshot = _snapshot()
    stale_price = PriceMetrics(
        **{
            **snapshot.price.__dict__,
            "status": SourceStatus.TIMEOUT,
            "status_detail": "last confirmed price shown; source is temporarily unavailable",
            "is_stale": True,
        }
    )
    stale_snapshot = DataSnapshot(
        fetched_at=snapshot.fetched_at,
        farming=snapshot.farming,
        price=stale_price,
        source_statuses={"farming": "ok", "price": "timeout"},
        anomalies={},
    )

    price_text = render_user_screen("price", stale_snapshot)
    home_text = render_user_screen("home", stale_snapshot)

    assert "0.01865 USDT" in price_text
    assert "временно не обновлялась" in price_text.lower()
    assert "0.01865 USDT" in home_text
    assert "временно не обновлялась" in home_text.lower()


def test_home_screen_uses_human_update_label_and_hides_source_internals():
    text = render_user_screen("home", _snapshot())

    assert "Обновлено:" in text
    assert "Источник" not in text
    assert "Снимок" not in text
    assert "snapshot" not in text.lower()


def test_group_price_card_hides_bid_ask_and_volume():
    text = render_user_screen("price", _snapshot())

    assert "0.01865 USDT" in text
    assert "MEXC Spot" in text
    assert "Bid/Ask" not in text
    assert "Объём" not in text


def test_farming_screen_uses_neutral_primary_yield_and_stale_label():
    snapshot = _snapshot()
    pool, = snapshot.farming.values()
    stale_pool = PoolMetrics(
        **{**pool.__dict__, "is_stale": True, "status": SourceStatus.TIMEOUT}
    )
    snapshot = snapshot.__class__(
        fetched_at=snapshot.fetched_at,
        farming={pool.pool.address: stale_pool},
        price=snapshot.price,
        source_statuses=snapshot.source_statuses,
        anomalies=snapshot.anomalies,
    )

    text = render_user_screen("farming", snapshot)

    assert "Доходность: <b>12.50%</b>" in text
    assert "APR" not in text
    assert "данные временно не обновляются" in text.lower()


@pytest.mark.parametrize("language", ["ru", "en"])
def test_farming_cards_display_gram_for_both_exchanges(language):
    snapshot = _snapshot()
    metrics = [
        PoolMetrics(
            pool=pool,
            farm_rates=(RateMetric(Decimal("12.5"), "APR", "apr_rewards"),),
            pool_rates=(),
            total_rate=RateMetric(Decimal("12.5"), "APR", "apr"),
            tvl_usd=None,
            fetched_at=snapshot.fetched_at,
            status=SourceStatus.OK,
            identity_valid=True,
        )
        for pool in FARMING_POOLS
    ]
    farming_snapshot = snapshot.__class__(
        fetched_at=snapshot.fetched_at,
        farming={item.pool.address: item for item in metrics},
        price=snapshot.price,
        source_statuses=snapshot.source_statuses,
        anomalies=snapshot.anomalies,
    )

    text = render_user_screen("farming", farming_snapshot, language=language)

    assert "DeDust JETTON/GRAM" in text
    assert "STON.fi JETTON/GRAM" in text
    assert "JETTON/TON" not in text


def test_farming_screen_hides_unconfirmed_primary_yield_but_keeps_tvl():
    snapshot = _snapshot()
    pool_id, metrics = next(iter(snapshot.farming.items()))
    metrics = PoolMetrics(
        **{
            **metrics.__dict__,
            "tvl_usd": Decimal("1234"),
        }
    )
    snapshot = snapshot.__class__(
        fetched_at=snapshot.fetched_at,
        farming={pool_id: metrics},
        price=snapshot.price,
        source_statuses=snapshot.source_statuses,
        anomalies={f"farming:{pool_id}": "unconfirmed"},
    )

    text = render_user_screen("farming", snapshot)

    assert "Доходность: <b>12.50%</b>" not in text
    assert "доходность временно недоступна" in text.lower()
    assert "TVL: $1,234" in text


def test_farming_screen_keeps_partial_primary_yield_without_tvl():
    snapshot = _snapshot()
    pool_id, metrics = next(iter(snapshot.farming.items()))
    partial = PoolMetrics(
        **{
            **metrics.__dict__,
            "status": SourceStatus.PARTIAL,
            "pool_rates": (),
            "total_rate": None,
            "tvl_usd": None,
        }
    )
    snapshot = snapshot.__class__(
        fetched_at=snapshot.fetched_at,
        farming={pool_id: partial},
        price=snapshot.price,
        source_statuses=snapshot.source_statuses,
        anomalies={},
    )

    text = render_user_screen("farming", snapshot)

    assert "Доходность: <b>12.50%</b>" in text
    assert "Общий TVL пулов временно недоступен" in text


def test_farming_screen_shows_total_tvl_for_all_displayed_pools_in_english():
    snapshot = _snapshot()
    metrics = []
    for index, pool in enumerate(FARMING_POOLS):
        base = snapshot.farming.get(pool.address)
        metrics.append(
            PoolMetrics(
                pool=pool,
                farm_rates=base.farm_rates if base else (),
                pool_rates=(),
                total_rate=base.total_rate if base else None,
                tvl_usd=Decimal((index + 1) * 1000),
                fetched_at=snapshot.fetched_at,
                status=SourceStatus.OK,
                identity_valid=True,
            )
        )

    text = render_user_screen("farming", snapshot.__class__(
        fetched_at=snapshot.fetched_at,
        farming={info.pool.address: info for info in metrics},
        price=snapshot.price,
        source_statuses=snapshot.source_statuses,
        anomalies=snapshot.anomalies,
    ), language="en")

    assert text.splitlines()[-1] == "<b>Total TVL: $10,000</b>"


def test_farming_screen_marks_partial_and_unavailable_total_tvl():
    snapshot = _snapshot()
    available_pool, invalid_pool, unavailable_pool, no_tvl_pool = FARMING_POOLS
    metrics = [
        PoolMetrics(available_pool, (), (), None, Decimal("1250"), snapshot.fetched_at, SourceStatus.OK, True),
        PoolMetrics(invalid_pool, (), (), None, Decimal("9000"), snapshot.fetched_at, SourceStatus.OK, False),
        PoolMetrics(unavailable_pool, (), (), None, Decimal("8000"), snapshot.fetched_at, SourceStatus.TIMEOUT, True),
        PoolMetrics(no_tvl_pool, (), (), None, None, snapshot.fetched_at, SourceStatus.OK, True),
    ]
    farming_snapshot = snapshot.__class__(
        fetched_at=snapshot.fetched_at,
        farming={info.pool.address: info for info in metrics},
        price=snapshot.price,
        source_statuses=snapshot.source_statuses,
        anomalies=snapshot.anomalies,
    )

    partial = render_user_screen("farming", farming_snapshot)
    unavailable = render_user_screen(
        "farming",
        farming_snapshot.__class__(
            fetched_at=snapshot.fetched_at,
            farming={info.pool.address: PoolMetrics(
                info.pool, (), (), None, None, info.fetched_at, info.status, info.identity_valid
            ) for info in metrics},
            price=snapshot.price,
            source_statuses=snapshot.source_statuses,
            anomalies=snapshot.anomalies,
        ),
    )

    assert partial.splitlines()[-1] == "<b>Общий TVL доступных пулов: $1,250</b>"
    assert unavailable.splitlines()[-1] == "<b>Общий TVL пулов временно недоступен.</b>"


def test_private_callback_requires_private_chat_and_matching_user():
    owned = SimpleNamespace(
        from_user=SimpleNamespace(id=7),
        message=SimpleNamespace(chat=SimpleNamespace(id=7, type="private")),
    )
    foreign = SimpleNamespace(
        from_user=SimpleNamespace(id=8),
        message=SimpleNamespace(chat=SimpleNamespace(id=7, type="private")),
    )
    group = SimpleNamespace(
        from_user=SimpleNamespace(id=7),
        message=SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup")),
    )

    assert private_callback_is_owned(owned)
    assert not private_callback_is_owned(foreign)
    assert not private_callback_is_owned(group)


def test_notifications_screen_shows_saved_thresholds_and_personal_pause_only():
    now = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)
    state = {
        "farming_enabled": True,
        "farming_change_threshold_pp": "3",
        "price_enabled": True,
        "price_threshold_pct": "5",
        "price_baseline": "0.0200",
        "paused_until": (now + timedelta(hours=24)).isoformat(),
    }

    text = render_user_screen("alerts", _snapshot(), user_state=state, now=now)

    assert "Доходность" in text
    assert "Доходность: <b>включены</b> · порог ±3 пункта · рост и снижение" in text
    assert "доходность — в пунктах между значениями" in text
    assert "с 12,5% до 15,5% = 3 пункта" in text
    assert "Цена" in text
    assert "±5%" in text
    assert "2026-10-18 14:00 UTC" in text
    assert "APR" not in text
    assert "APY" not in text
    assert "Farming alerts" not in text
    assert "Price alerts" not in text
    assert "Глобальный статус" not in text


def test_first_private_start_asks_for_language_without_loading_dashboard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setenv("AUTO_PUSH_STATE_PATH", str(tmp_path / "auto_push.json"))
    bot = PoolmasterBot()
    snapshot_calls = []

    async def get_snapshot():
        snapshot_calls.append(True)
        return _snapshot()

    bot.snapshot_manager = SimpleNamespace(get_snapshot=get_snapshot)
    calls = []

    class Message:
        async def reply_text(self, text, **kwargs):
            calls.append((text, kwargs))

    user = SimpleNamespace(id=7, username="test", first_name="Tester", last_name=None)
    message = Message()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=user,
        effective_message=message,
    )

    asyncio.run(bot.start(update, None))

    assert len(calls) == 1
    text, kwargs = calls[0]
    assert text == "Выберите язык / Choose a language:"
    assert kwargs["reply_markup"] is not None
    assert {
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    } == {"ui:language:set:ru", "ui:language:set:en", "ui:home"}
    assert snapshot_calls == []


def test_new_user_can_choose_english_and_get_localized_home(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, language=None)
    bot.snapshot_manager = SimpleNamespace(get_snapshot=_async_return(_snapshot()))

    class BotApi:
        async def set_my_commands(self, *_args, **_kwargs):
            return None

    query = _UIQuery("ui:language:set:en")
    context = SimpleNamespace(user_data={}, bot=BotApi())

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), context))

    user = bot.user_alert_store.get_user(7)
    assert user["language"] == "en"
    assert query.answers[0][0] == "Language saved."
    assert "Choose a section below." in query.edits[0][0]
    assert "Выберите раздел ниже." not in query.edits[0][0]
    assert any(
        button.text == "Price"
        for row in query.edits[0][1]["reply_markup"].inline_keyboard
        for button in row
    )


def test_farming_navigation_edits_same_private_message_and_reads_shared_snapshot(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    snapshot = _snapshot()
    calls = []

    class SnapshotReader:
        async def get_snapshot(self, *, force=False):
            calls.append(force)
            return snapshot

    class Query:
        data = "ui:farming"
        from_user = SimpleNamespace(id=7)
        message = SimpleNamespace(chat=SimpleNamespace(id=7, type="private"), message_id=42)

        def __init__(self):
            self.answered = 0
            self.edits = []

        async def answer(self, *_args, **_kwargs):
            self.answered += 1

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    bot.snapshot_manager = SnapshotReader()
    query = Query()
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(user_data={})

    asyncio.run(bot.user_ui_callback(update, context))

    assert calls == [False]
    assert query.answered == 1
    assert len(query.edits) == 1
    assert "Доходность: <b>12.50%</b>" in query.edits[0][0]
    assert query.edits[0][1]["reply_markup"] is not None


@pytest.mark.parametrize("callback_data", ("ui:price", "ui:chart"))
def test_price_button_and_legacy_chart_alias_open_1h_chart_in_one_frame(tmp_path, monkeypatch, callback_data):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.price_identity_check = PriceIdentityCheck(True, "test market verified")
    calls = []
    sent_photos = []

    async def fake_chart(timeframe):
        calls.append(timeframe)
        image = BytesIO(b"png")
        image.name = f"jetton_chart_mexc_{timeframe}.png"
        return SimpleNamespace(image=image, caption="MEXC Spot JETTON/USDT")

    async def send_photo(**kwargs):
        sent_photos.append(kwargs)

    monkeypatch.setattr(bot, "fetch_verified_mexc_chart", fake_chart)
    query = _UIQuery(callback_data)
    deleted = []

    async def delete():
        deleted.append(True)

    query.message.delete = delete
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_photo=send_photo))

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), context))

    assert calls == ["1h"]
    assert deleted == [True]
    assert query.answers == [("", {})]
    assert len(sent_photos) == 1
    assert sent_photos[0]["caption"] == "MEXC Spot JETTON/USDT"
    assert sent_photos[0]["reply_markup"] is not None


def test_private_price_chart_button_fails_closed_when_identity_is_unverified(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    photos = []

    async def unexpected_fetch(*_args):
        raise AssertionError("an unverified market must not fetch or serve a cached chart")

    monkeypatch.setattr("bot.fetch_chart", unexpected_fetch)
    query = _UIQuery("ui:price")
    query.message.delete = _async_return(None)

    async def send_photo(**kwargs):
        photos.append(kwargs)

    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_photo=send_photo))
    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), context))

    assert len(photos) == 1
    assert "не пройдена" in photos[0]["caption"].lower()


def test_private_price_chart_button_rejects_group_and_foreign_owner(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    fetches = []

    async def fake_fetch(*args):
        fetches.append(args)

    monkeypatch.setattr("bot.fetch_chart", fake_fetch)
    group_query = _UIQuery("ui:price", chat_id=-100555, chat_type="supergroup")
    foreign_query = _UIQuery("ui:price")
    foreign_query.from_user = SimpleNamespace(id=8)

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=group_query), SimpleNamespace(user_data={})))
    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=foreign_query), SimpleNamespace(user_data={})))

    assert fetches == []
    assert group_query.answers[0][1]["show_alert"] is True
    assert foreign_query.answers[0][1]["show_alert"] is True


def test_refresh_callback_uses_shared_freshness_policy(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    snapshot = _snapshot()
    calls = []

    class SnapshotReader:
        async def refresh_farming(self, **kwargs):
            calls.append("farming")
            return snapshot

    query = _UIQuery("ui:refresh:farming")
    order = []

    async def answer(text="", **_kwargs):
        order.append("answer")
        query.answers.append((text, {}))

    async def edit_message_text(*_args, **_kwargs):
        order.append("edit")

    query.answer = answer
    query.edit_message_text = edit_message_text

    bot.snapshot_manager = SnapshotReader()
    context = SimpleNamespace(user_data={})

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), context))

    assert calls == ["farming"]
    assert order[0] == "answer"
    assert query.answers[0][0] == "Обновляю…"


def test_refresh_callback_is_throttled_per_user_with_clear_retry_message(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)

    class SnapshotReader:
        async def refresh_farming(self, **_kwargs):
            raise AssertionError("throttled refresh should not fetch")

    bot.snapshot_manager = SnapshotReader()
    query = _UIQuery("ui:refresh:farming")
    bot.cooldowns.allow(("user_refresh", 7, "farming"), 10)

    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), SimpleNamespace(user_data={})))

    assert query.edits == []
    assert query.answers[0][0] == "Обновить можно раз в 10 секунд."


def test_private_alert_threshold_selection_saves_and_seeds_user_baseline(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    snapshot = _snapshot()
    bot.snapshot_manager = SimpleNamespace(get_snapshot=_async_return(snapshot))
    query = _UIQuery("ui:set:apr:3")
    update = SimpleNamespace(callback_query=query)

    asyncio.run(bot.user_ui_callback(update, SimpleNamespace(user_data={})))

    user = bot.user_alert_store.get_user(7)
    pool_id = FARMING_POOLS[0].address
    assert user["farming_enabled"] is True
    assert user["farming_change_threshold_pp"] == "3"
    assert user["last_notified_yield"][pool_id] == {
        "value": "12.5",
        "kind": "APR",
        "source_field": "apr_rewards",
    }
    assert len(query.edits) == 1
    assert "Доходность: <b>включены</b> · порог ±3 пункта · рост и снижение" in query.edits[0][0]


def test_private_pause_button_persists_24_hour_pause_and_shows_resume(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.user_alert_store.ensure_user(7)
    bot.snapshot_manager = SimpleNamespace(get_snapshot=_async_return(_snapshot()))
    query = _UIQuery("ui:pause24")

    asyncio.run(
        bot.user_ui_callback(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={}),
        )
    )

    user = bot.user_alert_store.get_user(7)
    paused_until = datetime.fromisoformat(user["paused_until"])
    assert paused_until > datetime.now(timezone.utc) + timedelta(hours=23, minutes=59)
    assert paused_until <= datetime.now(timezone.utc) + timedelta(hours=24, seconds=2)
    assert "Возобновить уведомления" in [
        button.text
        for row in query.edits[0][1]["reply_markup"].inline_keyboard
        for button in row
    ]


def test_group_callback_cannot_create_or_change_private_user_state(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    before = bot.user_alert_store.load_users()
    query = _UIQuery("ui:set:apr:1", chat_id=-100555, chat_type="supergroup")
    update = SimpleNamespace(callback_query=query)

    asyncio.run(bot.user_ui_callback(update, SimpleNamespace(user_data={})))

    assert bot.user_alert_store.load_users() == before
    assert query.edits == []
    assert query.answers[0][1]["show_alert"] is True


def _make_bot(tmp_path, monkeypatch, *, language="ru"):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setenv("AUTO_PUSH_STATE_PATH", str(tmp_path / "auto_push.json"))
    bot = PoolmasterBot()
    if language is not None:
        bot.user_alert_store.set_language(7, language)
    return bot


def _async_return(value):
    async def result(**_kwargs):
        return value

    return result


class _UIQuery:
    def __init__(self, data, *, chat_id=7, chat_type="private"):
        self.data = data
        self.from_user = SimpleNamespace(id=7)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id, type=chat_type), message_id=42)
        self.answers = []
        self.edits = []

    async def answer(self, text="", **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
