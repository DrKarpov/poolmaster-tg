import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chart_data import ChartWindow
from chart_source import MexcSpotCandle
from chart_widget import ChartPreferences, WidgetStateParts, encode_window, decode_window
from chart_widget_state import ChartWidgetSessionStore
from user_state import UserAlertStore
from widget_transport import WidgetMessageContext


def window(frame='1h', count=30):
    start=1794009600
    candles=tuple(MexcSpotCandle(start+i*3600,start+(i+1)*3600,
        Decimal('0.01622'),Decimal('0.01624'),Decimal('0.01620'),Decimal('0.01623'),
        Decimal('123.1'),Decimal('187623.973849')) for i in range(count))
    return ChartWindow(frame,candles,min(24,count),datetime.now(timezone.utc),
                       candles[-1].open_time,None,True)


def test_codec_roundtrip_exact_ohlcv_and_real_timestamps_under_utf8_budget():
    source=window()
    parts=encode_window(source,ChartPreferences(),7)
    decoded=decode_window(parts)
    assert len(parts.public_data.encode())<=900 and len(parts.chat_data.encode())<=1000
    assert decoded.timeframe=='1h' and decoded.revision==7
    assert decoded.candles[-1].close==Decimal('0.01623')
    assert decoded.candles[-1].quote_volume==Decimal('187623.973849')
    assert decoded.candles[0].open_time==source.candles[-len(decoded.candles)].open_time
    assert decoded.warmup_count==6


def test_codec_keeps_month_gaps_exact_and_large_values_as_decimal_strings():
    source=window('1M',3)
    times=(1706745600,1709251200,1711929600) # Feb, March, April 2024 UTC
    candles=tuple(replace(c,open_time=t,close_time=t+2592000,
        quote_volume=Decimal('123456789012345678901234567890.123456789')) for c,t in zip(source.candles,times))
    parts=encode_window(replace(source,candles=candles),ChartPreferences('1M'),1)
    decoded=decode_window(parts)
    assert [c.open_time for c in decoded.candles]==list(times)
    assert decoded.candles[-1].quote_volume==candles[-1].quote_volume


def test_mixed_revisions_cannot_decode_into_a_chart():
    a=encode_window(window(),ChartPreferences(),1)
    b=encode_window(window(),ChartPreferences(),2)
    with pytest.raises(ValueError):
        decode_window(WidgetStateParts(a.public_data,b.chat_data,a.visible_start))


def test_chart_preference_survives_restart_and_is_private(tmp_path):
    store=UserAlertStore(tmp_path/'users.json')
    store.set_language(41,'en')
    store.set_chart_preferences(41,ChartPreferences('1M'))
    again=UserAlertStore(tmp_path/'users.json')
    assert again.get_chart_preferences(41).timeframe=='1M'
    assert again.get_chart_preferences(42).timeframe=='1h'
    assert again.get_user(41)['language']=='en'
    with pytest.raises(ValueError):
        ChartPreferences('1m')


def test_sessions_are_owned_restartable_busy_and_closed_by_back(tmp_path):
    path=tmp_path/'widgets.sqlite3'
    ctx=WidgetMessageContext(41,7,41)
    parts=encode_window(window(),ChartPreferences(),1)
    store=ChartWidgetSessionStore(path)
    store.register(ctx,'1h',parts)
    session=store.claim(ctx,1)
    assert session is not None and store.claim(ctx,1) is None
    assert store.claim(WidgetMessageContext(41,7,99),1) is None
    restarted=ChartWidgetSessionStore(path)
    assert restarted.claim(ctx,1) is not None
    restarted.close(ctx)
    assert restarted.get(ctx) is None and restarted.claim(ctx,1) is None


def test_old_user_record_gains_default_without_changing_alerts(tmp_path):
    store=UserAlertStore(tmp_path/'users.json')
    store.ensure_user(41)
    data=json.loads(store.path.read_text())
    data['users']['41'].pop('chart_preferences',None)
    data['users']['41']['price_baseline']='0.01622'
    store.path.write_text(json.dumps(data))
    reloaded=UserAlertStore(store.path)
    assert reloaded.get_chart_preferences(41).timeframe=='1h'
    assert reloaded.get_user(41)['price_baseline']=='0.01622'


def test_identical_shared_window_reuses_exact_encoding_without_repacking(monkeypatch):
    import chart_widget
    packed=[]
    original=chart_widget._pack
    def count(value,scale):
        packed.append(1)
        return original(value,scale)
    monkeypatch.setattr(chart_widget,'_pack',count)
    source=window(count=54)
    labels=['30m','1h','4h','1D','1W','1M',*chart_widget.AXIS_MONTHS['en']]
    first=encode_window(source,ChartPreferences(),8123,labels=labels)
    work=len(packed)
    second=encode_window(source,ChartPreferences(),8123,labels=tuple(labels))
    assert first==second and work>0 and len(packed)==work
    next_revision=encode_window(source,ChartPreferences(),8124,labels=labels)
    assert json.loads(next_revision.public_data)['r']==8124
    assert len(packed)>work


def test_session_operations_close_sqlite_connections_promptly(tmp_path,monkeypatch):
    import sqlite3
    import chart_widget_state
    closed=[]
    class TrackedConnection(sqlite3.Connection):
        def close(self):closed.append(1);super().close()
    connect=sqlite3.connect
    def tracked(*args,**kwargs):return connect(*args,**kwargs,factory=TrackedConnection)
    monkeypatch.setattr(chart_widget_state.sqlite3,'connect',tracked)
    store=ChartWidgetSessionStore(tmp_path/'sessions.sqlite3')
    context=WidgetMessageContext(7,1,7)
    store.register(context,'1h',encode_window(window(),ChartPreferences(),1))
    for _ in range(20):assert store.get(context) is not None
    assert len(closed)==22


def test_new_session_can_start_claimed_without_a_second_write(tmp_path):
    store=ChartWidgetSessionStore(tmp_path/'sessions.sqlite3')
    context=WidgetMessageContext(7,1,7)
    store.register(context,'1h',encode_window(window(),ChartPreferences(),1),claimed=True)
    assert store.claim(context,1) is None
    store.release(context,1)
    assert store.claim(context,1) is not None
