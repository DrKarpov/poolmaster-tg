import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from bot import PoolmasterBot, telegram_call_with_retry
from farming import FARMING_POOLS, PoolMetrics, RateMetric, SourceStatus
from inline_mode import INLINE_CACHE_SECONDS, build_inline_results
from price import PriceMetrics
from snapshot import DataSnapshot
from telegram.error import RetryAfter


NOW = datetime(2026, 10, 17, 19, 0, tzinfo=timezone.utc)


def _snapshot(*, stale=False, unavailable_price=False):
    farming = {}
    for pool in FARMING_POOLS:
        kind = "APY" if pool.dex == "STON.fi" else "APR"
        rate = RateMetric(Decimal("12.5"), kind, "test")
        farming[pool.address] = PoolMetrics(
            pool,
            (rate,),
            (),
            rate,
            None,
            NOW,
            SourceStatus.OK if not unavailable_price else SourceStatus.PARTIAL,
            True,
            is_stale=stale,
        )
    price = PriceMetrics(
        None if unavailable_price else Decimal("0.02"),
        None,
        None,
        None,
        "JETTONUSDT",
        "USDT",
        "MEXC spot",
        NOW,
        NOW if not unavailable_price else None,
        SourceStatus.UNAVAILABLE if unavailable_price else SourceStatus.OK,
        is_stale=stale,
    )
    return DataSnapshot(NOW, farming, price, {"farming": "ok", "price": price.status.value}, {})


def test_inline_results_include_price_and_farming_from_shared_snapshot_with_public_keyboards():
    results = build_inline_results(_snapshot())

    assert [result.id for result in results] == ["jetton-price", "jetton-farming"]
    assert [result.title for result in results] == ["Цена JETTON", "Фарминг JETTON"]
    price_text = results[0].input_message_content.message_text
    farming_text = results[1].input_message_content.message_text
    assert "0.02 USDT" in price_text
    assert "MEXC Spot" in price_text
    assert "Доходность: <b>12.50%</b>" in farming_text
    assert "DeDust JETTON/GRAM" in farming_text
    assert "STON.fi JETTON/GRAM" in farming_text
    assert "JETTON/TON" not in farming_text
    assert "APR" not in farming_text
    assert "APY" not in farming_text
    assert "Price" not in results[0].title
    assert "Farming" not in results[1].title
    price_buttons = [
        button
        for row in results[0].reply_markup.inline_keyboard
        for button in row
    ]
    assert len(price_buttons) == 1
    assert price_buttons[0].callback_data == "inline:price:refresh"
    assert price_buttons[0].url is None

    farming_buttons = [
        button
        for row in results[1].reply_markup.inline_keyboard
        for button in row
    ]
    assert farming_buttons
    assert all(button.url and button.callback_data is None for button in farming_buttons)


def test_inline_results_preserve_stale_and_unavailable_labels():
    stale_results = build_inline_results(_snapshot(stale=True))
    assert "временно не обновлялась" in stale_results[0].input_message_content.message_text.lower()
    assert "временно не обновляются" in stale_results[1].input_message_content.message_text.lower()

    unavailable_results = build_inline_results(_snapshot(unavailable_price=True))
    assert "временно недоступны" in unavailable_results[0].input_message_content.message_text.lower()
    assert "Доходность: <b>12.50%</b>" in unavailable_results[1].input_message_content.message_text


def test_inline_price_is_a_complete_fresh_quote_without_chart_copy():
    snapshot = _snapshot()
    quote = replace(
        snapshot.price,
        price_change_24h_percent=Decimal("-0.40"),
        high_price_24h=Decimal("0.02500"),
        low_price_24h=Decimal("0.01800"),
        quote_volume_24h=Decimal("1234.50"),
    )
    result = build_inline_results(replace(snapshot, price=quote))[0]
    text = result.input_message_content.message_text

    assert "0.02 USDT" in text
    assert "Источник: MEXC Spot" in text
    assert "Обновлено:" in text
    assert "Изменение за 24 ч: -0.40%" in text
    assert "Диапазон за 24 ч:" in text
    assert "Объём за 24 ч: 1,234.5 USDT" in text
    assert "свеч" not in text.lower()
    assert "график" not in text.lower()
    assert "12 ч" not in text.lower()


def test_inline_result_marks_missing_farming_metrics_unavailable():
    snapshot = _snapshot()
    empty_snapshot = DataSnapshot(NOW, {}, snapshot.price, {"farming": "unavailable", "price": "ok"}, {})

    text = build_inline_results(empty_snapshot)[1].input_message_content.message_text

    assert "данные фарминга временно недоступны" in text.lower()


def test_inline_farming_uses_partial_primary_but_hides_unconfirmed_primary():
    snapshot = _snapshot()
    pool_id = FARMING_POOLS[0].address
    partial = replace(
        snapshot.farming[pool_id],
        status=SourceStatus.PARTIAL,
        pool_rates=(),
        total_rate=None,
        tvl_usd=None,
    )
    snapshot = replace(snapshot, farming={**snapshot.farming, pool_id: partial})

    partial_text = build_inline_results(snapshot)[1].input_message_content.message_text
    unconfirmed = replace(snapshot, anomalies={f"farming:{pool_id}": "unconfirmed"})
    unconfirmed_text = build_inline_results(unconfirmed)[1].input_message_content.message_text

    assert "DeDust JETTON/GRAM" in partial_text
    assert "Доходность: <b>12.50%</b>" in partial_text
    unconfirmed_dedust = unconfirmed_text.split("DeDust JETTON/USDT", 1)[0]
    assert "Доходность: <b>12.50%</b>" not in unconfirmed_dedust


def test_missing_snapshot_returns_explicit_unavailable_results_with_short_cache():
    results = build_inline_results(None)
    assert all("временно недоступны" in result.input_message_content.message_text.lower() for result in results)
    assert INLINE_CACHE_SECONDS <= 10


def test_inline_handler_reads_shared_snapshot_once_and_answers_with_short_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("ADMIN_STATE_PATH", str(tmp_path / "admin.json"))
    monkeypatch.setenv("USER_STATE_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("MANAGED_DESTINATIONS_PATH", str(tmp_path / "destinations.json"))
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))
    bot = PoolmasterBot()
    snapshot = _snapshot()
    calls = []

    class SnapshotReader:
        async def get_snapshot(self, **kwargs):
            calls.append(kwargs)
            return snapshot

    class Query:
        def __init__(self):
            self.answers = []

        async def answer(self, results, **kwargs):
            self.answers.append((results, kwargs))

    query = Query()
    bot.snapshot_manager = SnapshotReader()
    update = SimpleNamespace(inline_query=query)

    asyncio.run(bot.inline_query(update, None))

    assert calls == [{}]
    results, kwargs = query.answers[0]
    assert len(results) == 2
    assert kwargs["cache_time"] == INLINE_CACHE_SECONDS
    assert kwargs["is_personal"] is False


def test_inline_handler_returns_safe_unavailable_cards_if_snapshot_manager_fails(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.setenv("ADMIN_STATE_PATH", str(tmp_path / "admin.json"))
    monkeypatch.setenv("USER_STATE_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("MANAGED_DESTINATIONS_PATH", str(tmp_path / "destinations.json"))
    monkeypatch.setenv("SUBSCRIBERS_PATH", str(tmp_path / "subscribers.json"))
    monkeypatch.setenv("STATS_PATH", str(tmp_path / "stats.json"))
    bot = PoolmasterBot()

    class SnapshotReader:
        async def get_snapshot(self, **_kwargs):
            raise RuntimeError("synthetic failure")

    class Query:
        async def answer(self, results, **kwargs):
            self.results = results
            self.kwargs = kwargs

    query = Query()
    bot.snapshot_manager = SnapshotReader()

    asyncio.run(bot.inline_query(SimpleNamespace(inline_query=query), None))

    assert len(query.results) == 2
    assert all("временно недоступны" in result.input_message_content.message_text.lower() for result in query.results)
    assert "Could not read accepted snapshot for inline query" in caplog.text


def test_telegram_retry_helper_accepts_timedelta_retry_after(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")

    async def no_wait(_delay):
        return None

    monkeypatch.setattr("bot.asyncio.sleep", no_wait)
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryAfter(timedelta(seconds=0))
        return "answered"

    assert asyncio.run(telegram_call_with_retry(operation)) == "answered"
    assert attempts == 2
