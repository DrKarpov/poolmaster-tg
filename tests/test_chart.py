import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from time import monotonic

from PIL import Image

import chart
from chart import (
    CachedChart,
    PUBLIC_TIMEFRAMES,
    TIMEFRAMES,
    cache_chart_result,
    chart_cache_is_usable,
    combine_candles,
    fetch_chart,
    load_disk_chart_cache,
    make_chart_result,
    normalize_chart_caption,
    save_disk_chart_cache,
    stale_chart_from_cache,
)
from chart_source import ChartSourceRateLimited, ChartSourceTimeout, MexcSpotCandle


def test_combine_candles_preserves_open_close_extremes_and_total_volume():
    candles = [
        [1000, 10.0, 12.0, 9.0, 11.0, 4.0],
        [1300, 11.0, 13.0, 8.0, 12.0, 6.0],
    ]

    combined = combine_candles(candles, 2)

    assert combined == [[1000, 10.0, 13.0, 8.0, 12.0, 10.0]]


def test_legacy_chart_cache_caption_keeps_geckoterminal_price_in_usd():
    legacy = (
        "<b>JETTON chart</b>\n"
        "Цена USD: <b>$0.0200</b>\n"
        "Пул: DeDust JETTON/TON\n"
        "Свеча: 5 минут\n"
        "Диапазон: 2 часа\n"
        "Source: GeckoTerminal OHLCV"
    )

    caption = normalize_chart_caption(legacy)

    assert "Цена JETTON в USD:" in caption
    assert "USDT" not in caption


def _mexc_candles():
    first_open = int(datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp())
    candles = []
    for index in range(24):
        open_price = Decimal("0.020") + Decimal(index) / Decimal("100000")
        close_price = open_price + Decimal("0.00001")
        candles.append(
            MexcSpotCandle(
                first_open + index * 300,
                first_open + (index + 1) * 300,
                open_price,
                close_price + Decimal("0.00001"),
                open_price - Decimal("0.00001"),
                close_price,
                Decimal("100"),
                Decimal("2.1105"),
            )
        )
    return candles


def test_chart_fetch_uses_mexc_spot_and_quotes_price_and_volume_in_usdt(tmp_path, monkeypatch):
    monkeypatch.setattr(chart, "CHART_CACHE_DIR", tmp_path)
    monkeypatch.setattr(chart, "CHART_CACHE", {})
    monkeypatch.setattr(chart, "CHART_FETCH_TASKS", {})
    calls = []

    class OfflineClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(chart.httpx, "AsyncClient", lambda **_kwargs: OfflineClient())

    async def fake_fetch(_client, timeframe_code):
        calls.append(timeframe_code)
        return _mexc_candles()

    async def prohibited_gecko_request(*_args):
        raise AssertionError("the chart renderer must only request the canonical MEXC spot market")

    monkeypatch.setattr(chart, "fetch_mexc_spot_ohlcv", fake_fetch, raising=False)
    monkeypatch.setattr(chart, "request_geckoterminal_ohlcv", prohibited_gecko_request, raising=False)

    result = asyncio.run(fetch_chart("mexc", "5m"))

    assert calls == ["5m"]
    assert "Цена JETTON/USDT:" in result.caption
    assert "MEXC Spot" in result.caption
    assert "50.65 USDT" in result.caption
    assert "2026-10-18 13:55 UTC" in result.caption
    assert "GeckoTerminal" not in result.caption


def test_mexc_source_timeout_uses_only_a_bounded_stale_chart(tmp_path, monkeypatch):
    monkeypatch.setattr(chart, "CHART_CACHE_DIR", tmp_path)
    monkeypatch.setattr(chart, "CHART_STALE_MAX_AGE_SECONDS", 1800)
    calls = []
    cached = CachedChart(
        expires_monotonic=0,
        cached_at_epoch=datetime.now(timezone.utc).timestamp() - 10,
        caption="Источник: MEXC Spot",
        image_bytes=b"verified cached image",
        image_name="jetton_chart_mexc_5m.png",
    )

    class OfflineClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(chart.httpx, "AsyncClient", lambda **_kwargs: OfflineClient())

    async def timeout_fetch(_client, timeframe_code):
        calls.append(timeframe_code)
        raise ChartSourceTimeout("synthetic timeout")

    async def prohibited_gecko_request(*_args):
        raise AssertionError("a MEXC timeout must not trigger a DEX source request")

    monkeypatch.setattr(chart, "fetch_mexc_spot_ohlcv", timeout_fetch, raising=False)
    monkeypatch.setattr(chart, "request_geckoterminal_ohlcv", prohibited_gecko_request, raising=False)
    monkeypatch.setattr(chart, "load_disk_chart_cache", lambda _key: None)

    result = asyncio.run(chart._fetch_chart_uncached("mexc", "5m", cached))

    assert calls == ["5m"]
    assert result.image.getvalue() == b"verified cached image"
    assert "MEXC Spot" in result.caption
    assert "последний успешный график" in result.caption.lower()


def test_mexc_rate_limit_returns_branded_unavailable_chart_without_dex_fallback(monkeypatch):
    class OfflineClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(chart.httpx, "AsyncClient", lambda **_kwargs: OfflineClient())

    async def limited_fetch(_client, timeframe_code):
        assert timeframe_code == "5m"
        raise ChartSourceRateLimited(1)

    def prohibited_dex_fallback(*_args):
        raise AssertionError("a MEXC failure must not trigger a DEX source request")

    monkeypatch.setattr(chart, "fetch_mexc_spot_ohlcv", limited_fetch)
    monkeypatch.setattr(chart, "request_geckoterminal_ohlcv", prohibited_dex_fallback, raising=False)

    result = asyncio.run(chart._fetch_chart_uncached("mexc", "5m", None))

    assert "MEXC временно ограничил запросы" in result.caption
    assert "JETTON/USDT" in result.caption
    with Image.open(BytesIO(result.image.getvalue())) as image:
        assert image.size == (1100, 700)


def test_every_existing_chart_label_matches_its_exact_candle_count_and_range():
    base_seconds = {"minute": 60, "hour": 3600, "day": 86400}
    expected_seconds = {
        "5m": 2 * 3600,
        "10m": 8 * 3600,
        "15m": 6 * 3600,
        "30m": 12 * 3600,
        "1h": 24 * 3600,
        "12h": 12 * 24 * 3600,
    }
    expected_counts = {code: 24 for code in TIMEFRAMES}
    expected_counts["10m"] = 48

    for code, timeframe in TIMEFRAMES.items():
        output_candles = timeframe.limit // timeframe.combine
        exact_range = (
            output_candles
            * timeframe.aggregate
            * timeframe.combine
            * base_seconds[timeframe.unit]
        )
        assert output_candles == expected_counts[code]
        assert exact_range == expected_seconds[code]


def test_chart_cache_enforces_configured_maximum_stale_age(monkeypatch):
    monkeypatch.setattr(chart, "CHART_STALE_MAX_AGE_SECONDS", 1800)
    saved_at = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc).timestamp()
    entry = CachedChart(monotonic(), saved_at, "chart", b"image", "chart.png")

    assert chart_cache_is_usable(entry, now_epoch=saved_at + 1800)
    assert not chart_cache_is_usable(entry, now_epoch=saved_at + 1801)
    assert stale_chart_from_cache(entry, now_epoch=saved_at + 1801) is None
    assert stale_chart_from_cache(entry, now_epoch=saved_at + 1800).caption.endswith(
        "Показан последний успешный график: свежие данные временно недоступны."
    )


def test_disk_cache_rejects_entries_older_than_maximum_stale_age(tmp_path, monkeypatch):
    monkeypatch.setattr(chart, "CHART_CACHE_DIR", tmp_path)
    monkeypatch.setattr(chart, "CHART_STALE_MAX_AGE_SECONDS", 1800)
    saved_at = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)
    key = ("dedust", "5m")

    save_disk_chart_cache(key, "old chart", b"image", "chart.png", cached_at=saved_at)

    assert load_disk_chart_cache(key, now=saved_at + timedelta(seconds=1800)) is not None
    assert load_disk_chart_cache(key, now=saved_at + timedelta(seconds=1801)) is None


def test_chart_memory_cache_round_trip_preserves_cached_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(chart, "CHART_CACHE_DIR", tmp_path)
    monkeypatch.setattr(chart, "CHART_CACHE", {})
    key = ("dedust", "5m")
    result = make_chart_result("chart caption", b"png-bytes", "chart.png")
    saved_at = datetime.now(timezone.utc)

    cached = cache_chart_result(key, result, cached_at=saved_at)

    assert cached.caption == "chart caption"
    assert chart.CHART_CACHE[key].cached_at_epoch == saved_at.timestamp()
    assert chart.CHART_CACHE[key].image_bytes == b"png-bytes"


def test_chart_requests_for_same_key_share_one_in_flight_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(chart, "CHART_CACHE_DIR", tmp_path)
    monkeypatch.setattr(chart, "CHART_CACHE", {})
    monkeypatch.setattr(chart, "CHART_FETCH_TASKS", {})
    calls = []

    async def fake_fetch(pool_code, timeframe_code, _cached):
        calls.append((pool_code, timeframe_code))
        await asyncio.sleep(0.01)
        return make_chart_result("shared", b"shared-image", "shared.png")

    monkeypatch.setattr(chart, "_fetch_chart_uncached", fake_fetch)

    async def run():
        return await asyncio.gather(fetch_chart("mexc", "5m"), fetch_chart("mexc", "5m"))

    first, second = asyncio.run(run())

    assert calls == [("mexc", "5m")]
    assert first is not second
    assert first.image is not second.image
    assert first.image.getvalue() == second.image.getvalue() == b"shared-image"


def test_chart_memory_cache_serves_fresh_entry_without_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(chart, "CHART_CACHE_DIR", tmp_path)
    key = ("mexc", "5m")
    saved_at = datetime.now(timezone.utc).timestamp()
    entry = CachedChart(monotonic() + 60, saved_at, "fresh", b"fresh-image", "fresh.png")
    monkeypatch.setattr(chart, "CHART_CACHE", {key: entry})

    calls = []

    async def unexpected_fetch(*args):
        calls.append(args)
        return make_chart_result("cache miss", b"fresh-image", "fresh.png")

    monkeypatch.setattr(chart, "_fetch_chart_uncached", unexpected_fetch)

    result = asyncio.run(fetch_chart("mexc", "5m", max_cache_age_seconds=10))

    assert result.caption == "fresh"
    assert result.image.getvalue() == b"fresh-image"
    assert calls == []


def test_chart_memory_cache_refetches_after_requested_maximum_age(tmp_path, monkeypatch):
    monkeypatch.setattr(chart, "CHART_CACHE_DIR", tmp_path)
    monkeypatch.setattr(chart, "CHART_FETCH_TASKS", {})
    key = ("mexc", "1h", "ru")
    cached_at = datetime.now(timezone.utc).timestamp() - 11
    entry = CachedChart(monotonic() + 60, cached_at, "cached", b"old-image", "old.png")
    monkeypatch.setattr(chart, "CHART_CACHE", {key: entry})
    calls = []

    async def fetch_fresh(pool_code, timeframe_code, stale_cache):
        calls.append((pool_code, timeframe_code, stale_cache))
        return make_chart_result("fresh", b"new-image", "new.png")

    monkeypatch.setattr(chart, "_fetch_chart_uncached", fetch_fresh)

    result = asyncio.run(fetch_chart("mexc", "1h", max_cache_age_seconds=10))

    assert result.caption == "fresh"
    assert calls == [("mexc", "1h", entry)]


def test_chart_caption_distinguishes_data_refresh_from_candle_start(tmp_path, monkeypatch):
    monkeypatch.setattr(chart, "CHART_CACHE_DIR", tmp_path)
    monkeypatch.setattr(chart, "CHART_CACHE", {})
    monkeypatch.setattr(chart, "CHART_FETCH_TASKS", {})

    class OfflineClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(chart.httpx, "AsyncClient", lambda **_kwargs: OfflineClient())

    async def fake_fetch(_client, _timeframe_code):
        return _mexc_candles()

    monkeypatch.setattr(chart, "fetch_mexc_spot_ohlcv", fake_fetch)

    result = asyncio.run(fetch_chart("mexc", "5m", force_refresh=True))

    assert "Обновлено:" in result.caption
    assert "Начало свечи:" in result.caption
    assert "Последняя свеча:" not in result.caption

    english = asyncio.run(fetch_chart("mexc", "5m", force_refresh=True, language="en"))

    assert "Updated:" in english.caption
    assert "Candle start:" in english.caption


def test_explicit_chart_refresh_bypasses_fresh_memory_and_disk_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(chart, "CHART_CACHE_DIR", tmp_path)
    key = ("mexc", "1h")
    entry = CachedChart(monotonic() + 60, datetime.now(timezone.utc).timestamp(), "cached", b"old", "old.png")
    monkeypatch.setattr(chart, "CHART_CACHE", {key: entry})
    monkeypatch.setattr(chart, "CHART_FETCH_TASKS", {})
    disk_calls = []
    monkeypatch.setattr(chart, "load_disk_chart_cache", lambda *args, **kwargs: disk_calls.append(args))
    fetch_calls = []

    async def fresh(pool_code, timeframe_code, _cached):
        fetch_calls.append((pool_code, timeframe_code))
        return make_chart_result("fresh", b"new", "new.png")

    monkeypatch.setattr(chart, "_fetch_chart_uncached", fresh)

    result = asyncio.run(fetch_chart("mexc", "1h", force_refresh=True))

    assert result.caption == "fresh"
    assert result.image.getvalue() == b"new"
    assert fetch_calls == [("mexc", "1h")]
    assert disk_calls == []


def test_public_timeframes_are_limited_and_unknown_internal_code_defaults_to_1h(monkeypatch):
    assert PUBLIC_TIMEFRAMES == ("1h", "12h")
    monkeypatch.setattr(chart, "CHART_CACHE", {})
    monkeypatch.setattr(chart, "CHART_FETCH_TASKS", {})
    monkeypatch.setattr(chart, "load_disk_chart_cache", lambda *_args, **_kwargs: None)
    calls = []

    async def fake_fetch(pool_code, timeframe_code, _cached):
        calls.append((pool_code, timeframe_code))
        return make_chart_result("fallback", b"image", "chart.png")

    monkeypatch.setattr(chart, "_fetch_chart_uncached", fake_fetch)

    asyncio.run(fetch_chart("mexc", "invalid"))

    assert calls == [("mexc", "1h")]
