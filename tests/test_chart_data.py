import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from chart_data import ChartDataService, fetch_widget_candles
from chart_source import ChartSourceInvalidResponse, ChartSourceTimeout, normalize_mexc_klines, period_end


def epoch(year, month, day=1, hour=0):
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


def row(open_time, duration):
    return [open_time*1000, '0.01622', '0.01624', '0.01620', '0.01623', '123',
            (open_time+duration)*1000, '1.23456']


@pytest.mark.parametrize('frame,source,duration,limit,start', [
    ('30m', '30m', 1800,55,epoch(2026,11,29,20)+1800),
    ('1h', '60m', 3600,55,epoch(2026,11,28,17)),
    ('4h', '4h', 14400,49,epoch(2026,11,22,20)),
    ('1d', '1d', 86400,37,epoch(2026,10,25)),
    ('1w', '1W', 604800,33,epoch(2026,4,14)),
    ('1M', '1M', 2592000,31,epoch(2024,5)),
])
def test_all_widget_frames_use_native_interval_and_both_history_bounds(frame, source, duration,limit,start):
    candle_start = epoch(2026, 11, 2) if frame == '1w' else epoch(2026, 11)
    def handler(request):
        assert dict(request.url.params) == {'symbol': 'JETTONUSDT', 'interval': source,
            'limit': str(limit), 'startTime':str(start*1000), 'endTime': str(epoch(2026,12)*1000-1)}
        return httpx.Response(200, json=[row(candle_start, duration)])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_widget_candles(client, frame, before_open_time=epoch(2026,12))
    result = asyncio.run(run())
    assert result[0].quote_volume == Decimal('1.23456')


@pytest.mark.parametrize('frame,interval,seconds,count',[
    ('30m','30m',1800,48),('1h','60m',3600,48),('4h','4h',14400,42),
    ('1d','1d',86400,30),('1w','1W',604800,26),('1M','1M',2592000,24),
])
def test_full_windows_load_frame_specific_visible_candles_and_ma7_context(frame,interval,seconds,count):
    import json
    from chart_widget import ChartPreferences,encode_window,decode_window
    if frame=='1M':
        stamps=[epoch((2026*12+10-i)//12,(2026*12+10-i)%12+1) for i in reversed(range(count+7))]
    else:
        end=epoch(2026,11,2) if frame=='1w' else epoch(2026,11,8,8 if frame=='4h' else 0 if frame=='1d' else 9)
        stamps=[end-i*seconds for i in reversed(range(count+7))]
    candles=normalize_mexc_klines([row(t,seconds) for t in stamps],interval)
    async def run():
        async def loader(frame,before):return candles
        service=ChartDataService(loader=loader,clock=lambda:datetime(2026,11,8,9,tzinfo=timezone.utc))
        result=await service.load_window(frame)
        assert result.visible_count==count and result.warmup_count==6
        assert len(result.candles)==count+6 and result.candles[-1]==candles[-1]
        parts=encode_window(result,ChartPreferences(frame),1)
        assert json.loads(parts.chat_data)['n']==count
        assert len(parts.public_data.encode()) <= 900
        assert len(parts.chat_data.encode()) <= 1000
        assert decode_window(parts).candles[-1].quote_volume==Decimal('1.23456')
    asyncio.run(run())


def test_calendar_months_accept_nominal_source_close_but_use_real_calendar_end():
    candles = normalize_mexc_klines([row(epoch(2024,2),2592000), row(epoch(2024,3),2592000)], '1M')
    assert period_end(candles[0].open_time, '1M') == epoch(2024,3)
    assert period_end(candles[1].open_time, '1M') == epoch(2024,4)
    assert candles[0].close_time == epoch(2024,3,2)
    assert period_end(epoch(2026,12), '1M') == epoch(2027,1)


def test_week_uses_monday_anchor_and_detects_missing_month():
    assert normalize_mexc_klines([row(epoch(2026,11,2),604800)], '1W')
    with pytest.raises(ChartSourceInvalidResponse):
        normalize_mexc_klines([row(epoch(2026,11,1),604800)], '1W')
    with pytest.raises(ChartSourceInvalidResponse):
        normalize_mexc_klines([row(epoch(2026,9),2592000),row(epoch(2026,11),2592000)], '1M')


def test_latest_cache_refresh_singleflight_history_and_bounded_stale():
    calls = []
    now = [datetime(2026,11,8,9,tzinfo=timezone.utc)]
    fail = [False]
    async def loader(frame, before):
        calls.append((frame,before))
        await asyncio.sleep(0)
        if fail[0]: raise ChartSourceTimeout('timeout')
        start=epoch(2026,11,7,3)
        rows=[row(start+i*3600,3600) for i in range(30)]
        return normalize_mexc_klines(rows,'60m')
    async def run():
        service=ChartDataService(loader=loader, clock=lambda:now[0], stale_max_age_seconds=60)
        first=await service.load_window('1h')
        assert first.visible_count==30 and first.warmup_count==0
        assert first.candles[0].open==Decimal('0.01622')
        assert await service.load_window('1h') is first
        a,b=await asyncio.gather(service.load_window('1h',force_refresh=True),service.load_window('1h',force_refresh=True))
        assert a is b and len(calls)==2
        await service.load_window('1h',before_open_time=epoch(2026,11,7,3))
        assert len(calls)==3
        fail[0]=True
        now[0]=datetime(2026,11,8,9,0,30,tzinfo=timezone.utc)
        stale=await service.load_window('1h',force_refresh=True)
        assert stale.is_stale and stale.fetched_at==first.fetched_at
        now[0]=datetime(2026,11,8,9,2,tzinfo=timezone.utc)
        with pytest.raises(ChartSourceTimeout):
            await service.load_window('1h',force_refresh=True)
    asyncio.run(run())


def test_short_monthly_history_is_drawable_and_empty_history_has_end():
    async def loader(frame,before):
        return [] if before else normalize_mexc_klines([row(epoch(2026,11),2592000)],'1M')
    async def run():
        service=ChartDataService(loader=loader,clock=lambda:datetime(2026,11,8,tzinfo=timezone.utc))
        window=await service.load_window('1M')
        assert window.visible_count==1 and window.warmup_count==0 and not window.has_earlier
        assert window.forming_open_time==epoch(2026,11)
        end=await service.load_window('1M',before_open_time=epoch(2026,11))
        assert not end.has_earlier and not end.candles
    asyncio.run(run())


def test_failed_fetch_cannot_extend_stale_deadline():
    from datetime import timedelta
    now = [datetime(2026, 11, 8, 9, tzinfo=timezone.utc)]
    failing = [False]
    async def loader(frame, before):
        if failing[0]:
            now[0] += timedelta(seconds=10)
            raise ChartSourceTimeout('source timed out')
        return normalize_mexc_klines([row(epoch(2026, 11, 8, 9), 3600)], '60m')
    async def run():
        service = ChartDataService(loader=loader, clock=lambda: now[0], stale_max_age_seconds=60)
        await service.load_window('1h')
        now[0] += timedelta(seconds=55)
        failing[0] = True
        with pytest.raises(ChartSourceTimeout):
            await service.load_window('1h', force_refresh=True)
    asyncio.run(run())
