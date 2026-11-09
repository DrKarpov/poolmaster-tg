"""Real OHLCV pages for the widget; legacy PNG uses the same source validator."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from chart_source import (
    MEXC_SPOT_BASE_URL, MEXC_SPOT_SYMBOL, MexcSpotCandle,
    ChartSourceError, ChartSourceEmpty, ChartSourceTimeout, ChartSourceUnavailable,
    ChartSourceRateLimited, ChartSourceInvalidResponse, normalize_mexc_klines, period_end,
    MEXC_INTERVAL_SECONDS,
)

WIDGET_TIMEFRAMES = ('30m', '1h', '4h', '1d', '1w', '1M')
FRAME_COUNTS = dict(zip(WIDGET_TIMEFRAMES, (48, 48, 42, 30, 26, 24)))
SOURCE_INTERVAL = dict(zip(WIDGET_TIMEFRAMES, ('30m', '60m', '4h', '1d', '1W', '1M')))
FRAME_LABELS = {
    'ru': ('30м', '1ч', '4ч', '1Д', '1Н', '1М'),
    'en': ('30m', '1h', '4h', '1D', '1W', '1M'),
}


@dataclass(frozen=True)
class ChartWindow:
    timeframe: str
    candles: tuple[MexcSpotCandle, ...]
    visible_count: int
    fetched_at: datetime
    data_asof: int | None
    forming_open_time: int | None
    has_earlier: bool
    is_stale: bool = False

    @property
    def warmup_count(self) -> int:
        return len(self.candles) - self.visible_count


async def fetch_widget_candles(client: httpx.AsyncClient, timeframe: str,
                               *, before_open_time: int | None = None) -> list[MexcSpotCandle]:
    if timeframe not in WIDGET_TIMEFRAMES:
        raise ValueError('unsupported widget timeframe')
    params = {'symbol': MEXC_SPOT_SYMBOL, 'interval': SOURCE_INTERVAL[timeframe], 'limit': FRAME_COUNTS[timeframe] + 7}
    if before_open_time is not None:
        if type(before_open_time) is not int or before_open_time <= 0:
            raise ValueError('invalid history boundary')
        # MEXC may ignore endTime alone. Bound both ends; monthly pages use
        # calendar months, never a nominal 30-day subtraction.
        if timeframe == '1M':
            before = datetime.fromtimestamp(before_open_time,timezone.utc)
            month = before.year*12 + before.month-1 - params['limit']
            start = int(before.replace(year=month//12,month=month%12+1,day=1,
                hour=0,minute=0,second=0,microsecond=0).timestamp())
        else:
            start = before_open_time - params['limit'] * MEXC_INTERVAL_SECONDS[SOURCE_INTERVAL[timeframe]]
        params['startTime'] = max(0,start) * 1000
        params['endTime'] = before_open_time * 1000 - 1
    try:
        response = await client.get(MEXC_SPOT_BASE_URL + '/klines', params=params, timeout=8)
        if response.status_code == 429:
            raise ChartSourceRateLimited()
        response.raise_for_status()
        payload = response.json()
    except (httpx.TimeoutException, TimeoutError):
        raise ChartSourceTimeout('MEXC widget source timeout') from None
    except httpx.HTTPError:
        raise ChartSourceUnavailable('MEXC widget source unavailable') from None
    except ValueError:
        raise ChartSourceInvalidResponse('MEXC widget source is not JSON') from None
    candles = normalize_mexc_klines(payload, SOURCE_INTERVAL[timeframe])
    if before_open_time is not None and any(c.open_time >= before_open_time for c in candles):
        raise ChartSourceInvalidResponse('MEXC ignored the historical boundary')
    return candles


class ChartDataService:
    def __init__(self, *, loader: Callable[[str, int | None], Awaitable[list[MexcSpotCandle]]] | None = None,
                 clock: Callable[[], datetime] | None = None, stale_max_age_seconds: int = 1800):
        self._loader = loader or self._load
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stale_max_age = stale_max_age_seconds
        self._cache: OrderedDict[tuple, ChartWindow] = OrderedDict()
        self._pending: dict[tuple, asyncio.Task] = {}

    @staticmethod
    async def _load(timeframe: str, before: int | None):
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3)) as client:
            return await fetch_widget_candles(client, timeframe, before_open_time=before)

    async def load_window(self, timeframe: str, *, before_open_time: int | None = None,
                          force_refresh: bool = False) -> ChartWindow:
        if timeframe not in WIDGET_TIMEFRAMES:
            raise ValueError('unsupported widget timeframe')
        key = (timeframe, before_open_time)
        cached = self._cache.get(key)
        now = self._clock()
        age = (now - cached.fetched_at).total_seconds() if cached else float('inf')
        if cached and not force_refresh and 0 <= age < (30 if before_open_time is None else 300):
            self._cache.move_to_end(key)
            return cached
        task = self._pending.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._fetch(key))
            self._pending[key] = task
        try:
            return await asyncio.shield(task)
        except ChartSourceError:
            age = (self._clock() - cached.fetched_at).total_seconds() if cached else float('inf')
            if cached and 0 <= age <= self._stale_max_age:
                return replace(cached, is_stale=True)
            raise
        finally:
            if task.done() and self._pending.get(key) is task:
                self._pending.pop(key, None)

    async def _fetch(self, key: tuple) -> ChartWindow:
        timeframe, before = key
        candles = await self._loader(timeframe, before)
        now = self._clock()
        if not candles and before is None:
            raise ChartSourceEmpty('MEXC returned no widget candles')
        now_epoch = int(now.timestamp())
        if candles and candles[-1].open_time > now_epoch + 30:
            raise ChartSourceInvalidResponse('MEXC candle is in the future')
        end = period_end(candles[-1].open_time, SOURCE_INTERVAL[timeframe]) if candles else 0
        if before is None and candles and end < now_epoch - 30:
            raise ChartSourceUnavailable('MEXC latest candle window is out of date')
        desired = FRAME_COUNTS[timeframe]
        selected = tuple(candles[-(desired + 6):])
        visible = min(desired, len(selected))
        result = ChartWindow(timeframe, selected, visible, now,
            selected[-1].open_time if selected else None,
            selected[-1].open_time if selected and now_epoch < end else None,
            len(candles) > visible)
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > 128:
            self._cache.popitem(last=False)
        return result
