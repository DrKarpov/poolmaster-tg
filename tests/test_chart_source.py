import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from chart import TIMEFRAMES
from chart_source import (
    MEXC_CHART_WINDOWS,
    ChartSourceEmpty,
    ChartSourceInvalidResponse,
    ChartSourceRateLimited,
    ChartSourceTimeout,
    aggregate_mexc_chart_window,
    fetch_mexc_spot_ohlcv,
    normalize_mexc_klines,
)


NOW = datetime(2026, 10, 17, 22, 0, tzinfo=timezone.utc)
BASE_TIME = int(NOW.timestamp())


def _rows(interval: str, count: int, *, start: int | None = None):
    window = MEXC_CHART_WINDOWS[interval]
    first = start if start is not None else BASE_TIME - BASE_TIME % window.output_interval_seconds
    first -= first % window.source_interval_seconds
    return [
        [
            (first + index * window.source_interval_seconds) * 1000,
            "0.02",
            "0.022",
            "0.019",
            "0.021",
            "100.5",
            (first + (index + 1) * window.source_interval_seconds) * 1000,
            "2.1105",
        ]
        for index in range(count)
    ]


def test_normalize_mexc_klines_preserves_documented_ohlcv_and_sorts_by_open_time():
    payload = _rows("5m", 2)
    candles = normalize_mexc_klines(list(reversed(payload)), "5m")

    assert [item.open_time for item in candles] == [int(payload[0][0] / 1000), int(payload[1][0] / 1000)]
    assert candles[0].open == Decimal("0.02")
    assert candles[0].high == Decimal("0.022")
    assert candles[0].low == Decimal("0.019")
    assert candles[0].close == Decimal("0.021")
    assert candles[0].base_volume == Decimal("100.5")
    assert candles[0].quote_volume == Decimal("2.1105")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.__setitem__(2, "0.0205"),  # high below close
        lambda row: row.__setitem__(5, "-1"),  # negative base volume
        lambda row: row.__setitem__(7, "nan"),  # non-finite quote volume
        lambda row: row.__setitem__(6, row[0] + 1),  # close time not an interval boundary
        lambda row: row.__setitem__(0, row[0] + 1000),  # open not aligned to interval
    ],
)
def test_normalize_rejects_invalid_market_data(mutate):
    row = _rows("5m", 1)[0]
    mutate(row)

    with pytest.raises(ChartSourceInvalidResponse):
        normalize_mexc_klines([row], "5m")


def test_normalize_rejects_duplicate_and_missing_source_candles():
    rows = _rows("5m", 3)

    with pytest.raises(ChartSourceInvalidResponse, match="duplicate"):
        normalize_mexc_klines([rows[0], rows[0]], "5m")
    with pytest.raises(ChartSourceInvalidResponse, match="gap"):
        normalize_mexc_klines([rows[0], rows[2]], "5m")


@pytest.mark.parametrize("code", list(MEXC_CHART_WINDOWS))
def test_every_chart_window_has_exact_count_range_and_utc_aligned_candles(code):
    window = MEXC_CHART_WINDOWS[code]
    raw = normalize_mexc_klines(_rows(code, window.request_limit), window.source_interval)
    result = aggregate_mexc_chart_window(raw, code)
    chart_timeframe = TIMEFRAMES[code]

    assert len(result) == window.output_count
    assert window.request_limit <= 1000
    assert window.range_seconds == window.output_count * window.output_interval_seconds
    assert all(candle.open_time % window.output_interval_seconds == 0 for candle in result)
    assert all(
        current.open_time - previous.open_time == window.output_interval_seconds
        for previous, current in zip(result, result[1:])
    )
    assert chart_timeframe.limit // chart_timeframe.combine == window.output_count
    assert result[-1].open_time + window.output_interval_seconds - result[0].open_time == window.range_seconds


def test_10m_window_requests_extra_candles_to_match_its_eight_hour_label():
    window = MEXC_CHART_WINDOWS["10m"]
    chart_timeframe = TIMEFRAMES["10m"]

    assert chart_timeframe.limit == 96
    assert window.request_limit == 97
    assert window.range_seconds == 8 * 60 * 60


def test_aggregation_sums_quote_volume_and_all_ohlc_fields():
    window = MEXC_CHART_WINDOWS["10m"]
    raw_rows = _rows("10m", window.request_limit)
    raw_rows[2][1:] = ["0.020", "0.021", "0.019", "0.0205", "100", raw_rows[2][6], "2.0"]
    raw_rows[3][1:] = ["0.0205", "0.023", "0.020", "0.022", "150", raw_rows[3][6], "3.3"]
    source = normalize_mexc_klines(raw_rows, "5m")
    result = aggregate_mexc_chart_window(source, "10m")
    first = result[0]

    assert window.combine == 2
    assert first.open == Decimal("0.020")
    assert first.high == Decimal("0.023")
    assert first.low == Decimal("0.019")
    assert first.close == Decimal("0.022")
    assert first.base_volume == Decimal("250")
    assert first.quote_volume == Decimal("5.3")


def test_aggregate_returns_empty_error_when_fewer_than_requested_output_candles():
    source = normalize_mexc_klines(_rows("5m", 4), "5m")

    with pytest.raises(ChartSourceEmpty):
        aggregate_mexc_chart_window(source, "5m")


def test_fetch_requests_public_spot_klines_with_documented_interval_and_no_credentials():
    window = MEXC_CHART_WINDOWS["10m"]
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["symbol"] == "JETTONUSDT"
        assert request.url.params["interval"] == "5m"
        assert request.url.params["limit"] == str(window.request_limit)
        assert "X-MEXC-APIKEY" not in request.headers
        return httpx.Response(200, json=_rows("10m", window.request_limit))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_mexc_spot_ohlcv(client, "10m")

    candles = asyncio.run(run())

    assert len(requests) == 1
    assert len(candles) == window.output_count


def test_fetch_turns_rate_limit_and_timeout_into_safe_source_errors():
    def rate_limited(_request):
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"code": 429})

    def timed_out(_request):
        raise httpx.ReadTimeout("synthetic timeout")

    async def run(handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_mexc_spot_ohlcv(client, "5m")

    with pytest.raises(ChartSourceRateLimited) as rate_error:
        asyncio.run(run(rate_limited))
    assert rate_error.value.retry_after_seconds == 7

    with pytest.raises(ChartSourceTimeout):
        asyncio.run(run(timed_out))


def test_fetch_marks_empty_and_malformed_response_without_guessing_values():
    async def run(payload):
        def handler(_request):
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_mexc_spot_ohlcv(client, "5m")

    with pytest.raises(ChartSourceEmpty):
        asyncio.run(run([]))
    with pytest.raises(ChartSourceInvalidResponse):
        asyncio.run(run({"error": "unexpected"}))
