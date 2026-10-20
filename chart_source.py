from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


MEXC_SPOT_BASE_URL = "https://api.mexc.com/api/v3"
MEXC_SPOT_SYMBOL = "JETTONUSDT"
MEXC_INTERVAL_SECONDS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "60m": 60 * 60,
    "4h": 4 * 60 * 60,
}


class ChartSourceError(RuntimeError):
    """Base class for safe-to-display chart-source failures."""


class ChartSourceUnavailable(ChartSourceError):
    pass


class ChartSourceTimeout(ChartSourceUnavailable):
    pass


class ChartSourceRateLimited(ChartSourceUnavailable):
    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("MEXC spot chart source returned HTTP 429")
        self.retry_after_seconds = retry_after_seconds


class ChartSourceInvalidResponse(ChartSourceError):
    pass


class ChartSourceEmpty(ChartSourceError):
    pass


@dataclass(frozen=True)
class MexcChartWindow:
    code: str
    source_interval: str
    source_interval_seconds: int
    output_interval_seconds: int
    output_count: int
    combine: int = 1

    @property
    def request_limit(self) -> int:
        # Include enough source bars to keep the requested number of UTC-aligned
        # output candles even when the latest output candle is still forming.
        return self.output_count * self.combine + self.combine - 1

    @property
    def range_seconds(self) -> int:
        return self.output_count * self.output_interval_seconds


MEXC_CHART_WINDOWS = {
    "5m": MexcChartWindow("5m", "5m", 300, 300, 24),
    "10m": MexcChartWindow("10m", "5m", 300, 600, 48, combine=2),
    "15m": MexcChartWindow("15m", "15m", 900, 900, 24),
    "30m": MexcChartWindow("30m", "15m", 900, 1800, 24, combine=2),
    "1h": MexcChartWindow("1h", "60m", 3600, 3600, 24),
    "12h": MexcChartWindow("12h", "4h", 14400, 43200, 24, combine=3),
}


@dataclass(frozen=True)
class MexcSpotCandle:
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal


def _decimal(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ChartSourceInvalidResponse(f"MEXC kline {field} is not numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ChartSourceInvalidResponse(f"MEXC kline {field} is invalid") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise ChartSourceInvalidResponse(f"MEXC kline {field} is outside the accepted range")
    return parsed


def _timestamp_seconds(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % 1000:
        raise ChartSourceInvalidResponse(f"MEXC kline {field} is not a millisecond timestamp")
    return value // 1000


def normalize_mexc_klines(payload: Any, source_interval: str) -> list[MexcSpotCandle]:
    interval_seconds = MEXC_INTERVAL_SECONDS.get(source_interval)
    if interval_seconds is None:
        raise ValueError(f"unsupported MEXC spot interval: {source_interval}")
    if not isinstance(payload, list):
        raise ChartSourceInvalidResponse("MEXC kline response is not a list")
    if not payload:
        return []

    candles: list[MexcSpotCandle] = []
    for row in payload:
        if not isinstance(row, list) or len(row) != 8:
            raise ChartSourceInvalidResponse("MEXC kline row does not match the documented eight-field schema")
        open_time = _timestamp_seconds(row[0], "open time")
        close_time = _timestamp_seconds(row[6], "close time")
        if open_time % interval_seconds or close_time - open_time != interval_seconds:
            raise ChartSourceInvalidResponse("MEXC kline timestamp does not match its interval")
        open_price = _decimal(row[1], "open")
        high_price = _decimal(row[2], "high")
        low_price = _decimal(row[3], "low")
        close_price = _decimal(row[4], "close")
        base_volume = _decimal(row[5], "base volume", allow_zero=True)
        quote_volume = _decimal(row[7], "quote volume", allow_zero=True)
        if high_price < max(open_price, close_price) or low_price > min(open_price, close_price) or high_price < low_price:
            raise ChartSourceInvalidResponse("MEXC kline OHLC values are inconsistent")
        candles.append(
            MexcSpotCandle(
                open_time,
                close_time,
                open_price,
                high_price,
                low_price,
                close_price,
                base_volume,
                quote_volume,
            )
        )

    candles.sort(key=lambda candle: candle.open_time)
    for previous, current in zip(candles, candles[1:]):
        if current.open_time == previous.open_time:
            raise ChartSourceInvalidResponse("MEXC kline response contains duplicate candles")
        if current.open_time - previous.open_time != interval_seconds:
            raise ChartSourceInvalidResponse("MEXC kline response contains a time gap")
    return candles


def aggregate_mexc_chart_window(
    candles: list[MexcSpotCandle],
    timeframe_code: str,
) -> list[MexcSpotCandle]:
    window = MEXC_CHART_WINDOWS.get(timeframe_code)
    if window is None:
        raise ValueError(f"unsupported chart timeframe: {timeframe_code}")
    if not candles:
        raise ChartSourceEmpty("MEXC returned no spot candles")

    buckets: dict[int, list[MexcSpotCandle]] = {}
    for candle in candles:
        bucket_time = candle.open_time // window.output_interval_seconds * window.output_interval_seconds
        buckets.setdefault(bucket_time, []).append(candle)

    grouped: list[MexcSpotCandle] = []
    bucket_times = sorted(buckets)
    last_bucket_time = bucket_times[-1]
    for bucket_time in bucket_times:
        group = buckets[bucket_time]
        if group[0].open_time != bucket_time:
            # The request may start in the middle of an output window. Drop
            # that left-edge fragment; later groups must be complete.
            if bucket_time == bucket_times[0]:
                continue
            raise ChartSourceInvalidResponse("MEXC aggregate window starts between output-candle boundaries")
        if len(group) > window.combine:
            raise ChartSourceInvalidResponse("MEXC aggregate window contains too many source candles")
        if len(group) != window.combine and bucket_time != last_bucket_time:
            raise ChartSourceInvalidResponse("MEXC aggregate window is missing a source candle")
        grouped.append(
            MexcSpotCandle(
                open_time=bucket_time,
                close_time=group[-1].close_time,
                open=group[0].open,
                high=max(candle.high for candle in group),
                low=min(candle.low for candle in group),
                close=group[-1].close,
                base_volume=sum((candle.base_volume for candle in group), Decimal(0)),
                quote_volume=sum((candle.quote_volume for candle in group), Decimal(0)),
            )
        )

    result = grouped[-window.output_count:]
    if len(result) != window.output_count:
        raise ChartSourceEmpty(
            f"MEXC returned {len(result)} aligned {timeframe_code} candles; {window.output_count} are required"
        )
    for previous, current in zip(result, result[1:]):
        if current.open_time - previous.open_time != window.output_interval_seconds:
            raise ChartSourceInvalidResponse("MEXC output candle window is not contiguous")
    return result


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


async def fetch_mexc_spot_ohlcv(
    client: httpx.AsyncClient,
    timeframe_code: str,
    *,
    timeout_seconds: float = 12,
) -> list[MexcSpotCandle]:
    window = MEXC_CHART_WINDOWS.get(timeframe_code)
    if window is None:
        raise ValueError(f"unsupported chart timeframe: {timeframe_code}")
    try:
        response = await client.get(
            f"{MEXC_SPOT_BASE_URL}/klines",
            params={
                "symbol": MEXC_SPOT_SYMBOL,
                "interval": window.source_interval,
                "limit": window.request_limit,
            },
            timeout=timeout_seconds,
        )
        if response.status_code == 429:
            raise ChartSourceRateLimited(_retry_after_seconds(response))
        response.raise_for_status()
        payload = response.json()
    except ChartSourceRateLimited:
        raise
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise ChartSourceTimeout("MEXC spot chart request timed out") from exc
    except httpx.HTTPError as exc:
        raise ChartSourceUnavailable("MEXC spot chart source is unavailable") from exc
    except ValueError as exc:
        raise ChartSourceInvalidResponse("MEXC spot chart response is not valid JSON") from exc

    candles = normalize_mexc_klines(payload, window.source_interval)
    if not candles:
        raise ChartSourceEmpty("MEXC returned no spot candles")
    return aggregate_mexc_chart_window(candles, timeframe_code)
