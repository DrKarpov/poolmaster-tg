import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from chart_widget import ChartPreferences, encode_window, load_widget_html
from test_chart_widget import window


def runtime(parts, actions=(), *, width=300,stored_view=None):
    node=shutil.which('node')
    if node is None: pytest.skip('Node is needed for the renderer boundary test')
    content=load_widget_html(language='ru',interactive=True)
    script=re.search(r'<script>(.*?)</script>',content,re.S).group(1)
    payload={'script':script,'publicData':parts.public_data,'chatData':parts.chat_data,'actions':actions,'width':width,'storedView':stored_view}
    result=subprocess.run([node,str(Path(__file__).with_name('widget_runtime.cjs'))],
        input=json.dumps(payload),text=True,capture_output=True,check=True,timeout=15)
    return json.loads(result.stdout)


def test_shipped_js_decodes_real_state_and_requests_selected_frame():
    parts=encode_window(window(),ChartPreferences(),1)
    output=runtime(parts,[{'kind':'tap','index':5}])
    assert output['states'][0]['rows'][-1][4]==pytest.approx(.01623)
    assert output['states'][0]['rows'][-1][5]==pytest.approx(187623.973849)
    assert output['requests']==['p1:tf:1M:1']


def test_expired_state_clears_rendered_series_after_revision_pair_arrives():
    parts=encode_window(window(),ChartPreferences(),1)
    public=json.loads(parts.public_data); private=json.loads(parts.chat_data)
    public.update(r=2,e='expired',c='',q='')
    private.update(r=2,c='',n=0,w=0,h=False)
    result=runtime(parts,[{'kind':'data','publicData':json.dumps(public),'chatData':parts.chat_data},
        {'kind':'data','publicData':json.dumps(public),'chatData':json.dumps(private)}])
    assert result['states'][1]['revision']==1
    assert result['states'][2]['candles']==0
    assert result['info']=='' and result['price']==''


def test_forward_is_dated_and_has_no_enabled_controls():
    from chart_widget import WidgetStateParts
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(WidgetStateParts(parts.public_data,'null',parts.visible_start))
    assert all(result['disabled'])
    assert result['status']=='copy' and '2026-' in result['info']
    assert result['statusText']==''


def test_old_refresh_timer_cannot_cancel_a_new_action():
    parts=encode_window(window(),ChartPreferences(),1)
    next_parts=encode_window(window(),ChartPreferences(),2)
    result=runtime(parts,[{'kind':'tap','index':6},
        {'kind':'data','publicData':next_parts.public_data,'chatData':next_parts.chat_data},
        {'kind':'tap','index':5},{'kind':'timer','id':1}])
    assert result['requests']==['p1:refresh:1','p1:tf:1M:2']
    assert result['status']=='pending'
    assert result['statusText']==''


def test_paging_beyond_buffer_limit_keeps_requested_older_candles_and_allows_return():
    from dataclasses import replace
    source=window()
    initial=encode_window(source,ChartPreferences(),1)
    actions=[]
    for page in range(1,7):
        older=replace(source,candles=tuple(replace(c,open_time=c.open_time-page*24*3600) for c in source.candles))
        parts=encode_window(older,ChartPreferences(),page+1)
        actions.extend([{'kind':'drag','dx':1000},
            {'kind':'data','publicData':parts.public_data,'chatData':parts.chat_data}])
    actions.append({'kind':'drag','dx':-1000})
    result=runtime(initial,actions)
    oldest=result['states'][-2]
    assert oldest['rows'][0][0]==source.candles[0].open_time-6*24*3600
    assert oldest['candles']<=120
    assert result['states'][-1]['right']>oldest['right']


def test_selecting_active_frame_returns_from_history_to_latest():
    from dataclasses import replace
    source=window()
    initial=encode_window(source,ChartPreferences(),1)
    older=replace(source,candles=tuple(replace(c,open_time=c.open_time-24*3600) for c in source.candles))
    history=encode_window(older,ChartPreferences(),2)
    latest=encode_window(source,ChartPreferences(),3)
    public=json.loads(latest.public_data);public['u']=3
    result=runtime(initial,[{'kind':'drag','dx':1000},
        {'kind':'data','publicData':history.public_data,'chatData':history.chat_data},
        {'kind':'tap','index':1},
        {'kind':'data','publicData':json.dumps(public),'chatData':latest.chat_data}])
    final=result['states'][-1]
    assert final['right']==final['candles']
    assert final['rows'][final['right']-1][0]==source.candles[-1].open_time


def test_narrow_plot_draws_only_one_readable_time_tick():
    result=runtime(encode_window(window(),ChartPreferences(),1),width=180)
    ticks=[text for text,x,y in result['labels'] if y>=232 and re.fullmatch(r'\d\d:\d\d',text)]
    assert len(ticks)==1


@pytest.mark.parametrize('frame,times,want',[
    ('30m',('2026-11-08T08:00','2026-11-08T08:30','2026-11-08T09:00'),['08:00','08:30','09:00']),
    ('1h',('2026-11-08T23:00','2026-11-09T00:00','2026-11-09T01:00'),['23:00','9 ноя','01:00']),
    ('4h',('2026-11-08T00:00','2026-11-08T04:00','2026-11-08T08:00'),['8 ноя','04:00','08:00']),
    ('1d',('2026-11-07T00:00','2026-11-08T00:00','2026-11-09T00:00'),['7 ноя','8 ноя','9 ноя']),
    ('1w',('2026-10-26T00:00','2026-11-02T00:00','2026-11-09T00:00'),['26 окт','2 ноя','9 ноя']),
    ('1M',('2026-09-01T00:00','2026-10-01T00:00','2026-11-01T00:00'),['сен','окт','ноя']),
])
def test_axis_uses_frame_appropriate_utc_labels(frame,times,want):
    from dataclasses import replace
    from datetime import datetime,timezone
    source=window(frame,count=3)
    stamps=[int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()) for t in times]
    source=replace(source,candles=tuple(replace(c,open_time=t,close_time=t+1)
        for c,t in zip(source.candles,stamps)))
    result=runtime(encode_window(source,ChartPreferences(frame),1))
    labels=[text for text,x,y in result['labels'] if y>=232]
    assert labels==want


def dense_parts(frame='30m',revision=1):
    from dataclasses import replace
    from datetime import datetime,timezone
    from decimal import Decimal
    start=int(datetime(2026,11,7,21,30,tzinfo=timezone.utc).timestamp())
    source=window(frame,count=54)
    source=replace(source,visible_count=48,candles=tuple(replace(c,open_time=start+j*1800,
        quote_volume=Decimal('1.23')) for j,c in enumerate(source.candles)))
    return encode_window(source,ChartPreferences(frame),revision)


def test_daily_intraday_window_uses_natural_hours_and_midnight_date():
    result=runtime(dense_parts())
    labels=[t for t,x,y in result['labels'] if y>=232]
    assert labels==['06:00','12:00','18:00','9 ноя']


def test_narrow_screen_limits_initial_candle_density_and_preserves_real_tail():
    parts=dense_parts()
    normal=runtime(parts)['states'][0]
    narrow=runtime(parts,width=180)['states'][0]
    assert normal['visible']==48
    assert narrow['visible']<=28
    assert narrow['rows'][-1]==normal['rows'][-1]


def test_time_axis_is_single_row_inside_thin_band_and_clear_of_volume():
    result=runtime(dense_parts())
    labels=[(t,x,y) for t,x,y in result['labels'] if y>=232]
    assert labels and len({y for t,x,y in labels})==1
    assert all(246<=y<=254 for t,x,y in labels)
    backgrounds=[v for v in result['fills'] if v[2]==300 and v[1]>=227 and v[3]>1]
    assert len(backgrounds)==1
    x,y,w,h=backgrounds[0]
    assert y>227 and h<=24 and y+h<=268
    assert any(s==[0,18,235,166] for s in result['strokes'])


def test_old_automatic_viewport_does_not_override_new_frame_default():
    result=runtime(dense_parts(),stored_view={'f':'30m','n':24,'t':0})
    assert result['states'][0]['visible']==48


def test_initial_zero_layout_waits_for_real_width_before_selecting_density():
    result=runtime(dense_parts(),[{'kind':'resize','width':300}],width=0)
    assert result['states'][-1]['visible']==48


def test_month_axis_marks_year_changes_at_january():
    from dataclasses import replace
    from datetime import datetime,timezone
    from decimal import Decimal
    source=window('1M',count=30)
    stamps=[int(datetime(2024+(5+i)//12,(5+i)%12+1,1,tzinfo=timezone.utc).timestamp()) for i in range(30)]
    source=replace(source,candles=tuple(replace(c,open_time=t,quote_volume=Decimal('1.23')) for c,t in zip(source.candles,stamps)))
    result=runtime(encode_window(source,ChartPreferences('1M'),1))
    assert [t for t,x,y in result['labels'] if y>=232]==['2025','июл','2026','июл']


def test_four_hour_frame_switches_to_dates_when_a_week_is_visible():
    from dataclasses import replace
    from datetime import datetime,timezone
    from decimal import Decimal
    source=window('4h',count=48);start=int(datetime(2026,11,1,tzinfo=timezone.utc).timestamp())
    source=replace(source,visible_count=42,candles=tuple(replace(c,open_time=start+j*14400,quote_volume=Decimal('1.23')) for j,c in enumerate(source.candles)))
    result=runtime(encode_window(source,ChartPreferences('4h'),1))
    labels=[t for t,x,y in result['labels'] if y>=232]
    assert 3<=len(labels)<=4
    assert all(':' not in t for t in labels)


@pytest.mark.parametrize('frame,start,spacing,count',[
    ('1d','2026-12-16',86400,30),('1w','2026-12-07',604800,8),
])
def test_new_year_boundary_is_labeled_even_outside_the_regular_stride(frame,start,spacing,count):
    from dataclasses import replace
    from datetime import datetime,timezone
    from decimal import Decimal
    source=window(frame,count=count);stamp=int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    source=replace(source,visible_count=count,candles=tuple(replace(c,open_time=stamp+j*spacing,quote_volume=Decimal('1.23')) for j,c in enumerate(source.candles)))
    result=runtime(encode_window(source,ChartPreferences(frame),1))
    assert '2027' in [t for t,x,y in result['labels'] if y>=232]


def test_date_mode_has_only_one_tick_per_day_at_wide_intermediate_zoom():
    from dataclasses import replace
    from datetime import datetime,timezone
    from decimal import Decimal
    source=window('4h',count=24);stamp=int(datetime(2026,11,1,tzinfo=timezone.utc).timestamp())
    source=replace(source,candles=tuple(replace(c,open_time=stamp+j*14400,quote_volume=Decimal('1.23')) for j,c in enumerate(source.candles)))
    result=runtime(encode_window(source,ChartPreferences('4h'),1),width=700)
    labels=[t for t,x,y in result['labels'] if y>=232]
    assert len(labels)==len(set(labels))
    assert labels==['1 ноя','2 ноя','3 ноя','4 ноя']


def test_natural_ticks_move_with_candles_when_panning():
    original=runtime(dense_parts())
    moved=runtime(dense_parts(),[{'kind':'drag','dx':5*3}])
    before=next(x for t,x,y in original['labels'] if t=='12:00')
    after=next(x for t,x,y in moved['labels'] if t=='12:00')
    assert after>before


@pytest.mark.parametrize('invalid',[0,-5,'broken'])
def test_invalid_saved_zoom_cannot_disable_narrow_screen_density(invalid):
    result=runtime(dense_parts(),width=180,stored_view={'v':2,'f':'30m','z':{'30m':invalid}})
    assert result['states'][0]['visible']<=28


def test_zoom_choice_survives_frame_switch_and_refresh():
    from dataclasses import replace
    source=window('1M',count=3)
    monthly=encode_window(source,ChartPreferences('1M'),2)
    result=runtime(dense_parts(),[
        {'kind':'drag','dx':60,'y':265},
        {'kind':'data','publicData':monthly.public_data,'chatData':monthly.chat_data},
        {'kind':'data','publicData':dense_parts(revision=3).public_data,'chatData':dense_parts(revision=3).chat_data},
        {'kind':'data','publicData':dense_parts(revision=4).public_data,'chatData':dense_parts(revision=4).chat_data}])
    assert result['states'][1]['visible']==28
    assert result['states'][-1]['visible']==28


def test_dragging_plot_pans_without_changing_time_scale_or_losing_latest_data():
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(parts,[{'kind':'drag','dx':15}])
    first,last=result['states']
    assert last['right']<first['right']
    assert last['visible']==first['visible']==24
    assert last['rows'][-1]==first['rows'][-1]


def test_dragging_time_axis_zooms_with_latest_candle_anchored():
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(parts,[{'kind':'drag','dx':30,'y':245}])
    first,last=result['states']
    assert last['visible']<first['visible']
    assert last['right']==last['candles']
    assert last['rows'][-1]==first['rows'][-1]
    assert not result['requests']


def test_zooming_out_loads_history_without_hiding_latest_or_resetting_scale():
    source=window()
    parts=encode_window(source,ChartPreferences(),1)
    result=runtime(parts,[{'kind':'drag','dx':-60,'y':245},{'kind':'tick','ms':0},
        {'kind':'reply','data':reserve_page(parts)}])
    assert len(result['requests'])==1 and result['requests'][0].startswith('p1:history:')
    assert result['states'][-1]['visible']==result['states'][1]['visible']>24
    assert result['states'][-1]['right']==result['states'][-1]['candles']
    assert result['states'][-1]['rows'][-1][0]==source.candles[-1].open_time


def test_axis_gesture_returns_latest_after_old_pages_evicted_it():
    from dataclasses import replace
    source=window()
    parts=encode_window(source,ChartPreferences(),1)
    actions=[]
    for page in range(1,7):
        older=replace(source,candles=tuple(replace(c,open_time=c.open_time-page*24*3600) for c in source.candles))
        state=encode_window(older,ChartPreferences(),page+1)
        actions.extend([{'kind':'drag','dx':1000},
            {'kind':'data','publicData':state.public_data,'chatData':state.chat_data}])
    actions.append({'kind':'drag','dx':30,'y':245})
    result=runtime(parts,actions)
    assert result['requests'][-1]=='p1:latest:7'


def test_latest_refresh_follows_new_candle_when_more_context_becomes_available():
    from dataclasses import replace
    initial=window(count=20)
    next_window=window(count=30)
    next_window=replace(next_window,candles=tuple(replace(c,open_time=c.open_time-9*3600) for c in next_window.candles))
    first=encode_window(initial,ChartPreferences(),1)
    fresh=encode_window(next_window,ChartPreferences(),2)
    result=runtime(first,[{'kind':'data','publicData':fresh.public_data,'chatData':fresh.chat_data}])
    assert result['states'][-1]['right']==result['states'][-1]['candles']
    assert result['states'][-1]['rows'][-1][0]==next_window.candles[-1].open_time


def test_history_loading_is_quiet_and_zoom_remains_local_while_waiting():
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(parts,[{'kind':'drag','dx':-60,'y':245},{'kind':'tick','ms':0},
        {'kind':'drag','dx':30,'y':245}])
    assert len(result['requests'])==1 and result['requests'][0].startswith('p1:history:')
    assert result['status']=='ok'
    assert result['states'][-1]['visible']<result['states'][-2]['visible']
    assert result['price']==json.loads(parts.public_data)['q']


def test_history_arriving_during_a_gesture_waits_until_release():
    source=window()
    initial=encode_window(source,ChartPreferences(),1)
    result=runtime(initial,[{'kind':'drag','dx':-60,'y':245},{'kind':'tick','ms':0},
        {'kind':'down','y':245},{'kind':'move','dx':-15,'y':245},
        {'kind':'reply','data':reserve_page(initial)},
        {'kind':'move','dx':-30,'y':245},{'kind':'up','dx':-30,'y':245}])
    assert result['states'][5]['candles']==result['states'][0]['candles']
    assert result['states'][-1]['candles']>result['states'][0]['candles']
    assert result['states'][-1]['revision']==1
    assert result['states'][-1]['visible']==result['states'][-2]['visible']
    final=result['states'][-1]
    assert final['rows'][final['right']-1][0]==source.candles[-1].open_time


def test_pointer_moves_coalesce_paint_and_save_only_after_release():
    initial=encode_window(window(),ChartPreferences(),1)
    result=runtime(initial,[{'kind':'down','y':245},
        {'kind':'move','dx':15,'y':245},{'kind':'move','dx':30,'y':245},
        {'kind':'animation'},{'kind':'up','dx':30,'y':245}])
    before,moved,painted,finished=[result['measurements'][i] for i in (0,3,4,5)]
    assert moved['draws']==before['draws'] and moved['frames']==1
    assert moved['writes']==before['writes']
    assert painted['draws']==before['draws']+1
    assert painted['writes']==before['writes']
    assert finished['writes']==before['writes']+1
    assert finished['frames']==0


def reserve_page(parts, revision=1):
    from dataclasses import replace
    from chart_widget import encode_history_page
    source=window()
    older=replace(source,candles=tuple(replace(c,open_time=c.open_time-30*3600) for c in source.candles))
    return json.loads(encode_history_page(older,revision,json.loads(parts.public_data)['t']))


def test_prefetch_starts_before_gestures_without_disabling_buttons():
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(parts,[{'kind':'tick','ms':0}])
    assert len(result['requests'])==1 and result['requests'][0].startswith('p1:history:')
    assert not any(result['disabled']) and result['status']=='ok'


def test_background_result_adds_candles_without_changing_revision_quote_or_tail():
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(parts,[{'kind':'tick','ms':0},{'kind':'reply','data':reserve_page(parts)}])
    first,last=result['states'][0],result['states'][-1]
    assert last['candles']>first['candles'] and last['revision']==first['revision']
    assert last['rows'][last['right']-1]==first['rows'][-1]
    assert result['price']==json.loads(parts.public_data)['q']
    assert not any(result['disabled'])


def test_frame_press_during_prefetch_is_prioritized_and_not_lost():
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(parts,[{'kind':'tick','ms':0},{'kind':'tap','index':5},
        {'kind':'reply','data':reserve_page(parts)},{'kind':'tick','ms':5200}])
    assert result['requests'][0].startswith('p1:history:')
    assert result['requests'][1:]==['p1:tf:1M:1']
    assert not any(result['measurements'][2]['disabled'])


def test_hidden_widget_does_not_prefetch_and_full_reserve_stops_queries():
    parts=dense_parts()
    result=runtime(parts,[{'kind':'visibility','hidden':True},{'kind':'tick','ms':10000}])
    assert not result['requests']
    initial=encode_window(window(),ChartPreferences(),1)
    page=reserve_page(initial);page['h']=False
    done=runtime(initial,[{'kind':'tick','ms':0},{'kind':'reply','data':page},{'kind':'tick','ms':60000}])
    assert len(done['requests'])==1


def test_new_open_uses_latest_candles_but_keeps_saved_frame_zoom():
    parts=encode_window(window(),ChartPreferences(),1)
    first=json.loads(parts.public_data)['t']
    result=runtime(parts,stored_view={'v':2,'f':'1h','t':first,'z':{'1h':16}})
    current=result['states'][0]
    assert current['visible']==16 and current['right']==current['candles']


def test_later_direct_frame_press_replaces_an_older_queued_press():
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(parts,[{'kind':'tick','ms':0},{'kind':'tap','index':5},
        {'kind':'reply','data':reserve_page(parts)},{'kind':'tap','index':2},
        {'kind':'tick','ms':5200}])
    assert result['requests'][1:]==['p1:tf:4h:1']


def test_background_reserve_stops_at_target_and_keeps_latest_when_buffer_is_full():
    from dataclasses import replace
    from chart_widget import encode_history_page,decode_window
    source=window()
    parts=encode_window(source,ChartPreferences(),1)
    boundary=source.candles[0].open_time;actions=[{'kind':'tick','ms':0}]
    count=len(decode_window(parts).candles);pages=0
    while count<120:
        older=replace(source,candles=tuple(replace(c,open_time=c.open_time+boundary-source.candles[0].open_time-30*3600) for c in source.candles))
        page=json.loads(encode_history_page(older,1,boundary))
        boundary=page['t']+int(page['c'].split(',')[0],36)
        actions.extend([{'kind':'reply','index':pages,'data':page},{'kind':'tick','ms':5200}])
        count+=len(page['c'].split(';'));pages+=1
    result=runtime(parts,actions+[{'kind':'tick','ms':60000}],stored_view={'v':2,'z':{'1h':96}})
    final=result['states'][-1]
    assert final['candles']==120 and final['right']==final['candles']
    assert final['rows'][-1][0]==source.candles[-1].open_time
    assert len(result['requests'])==pages


def test_missing_label_metadata_after_server_rollback_still_renders():
    from chart_widget import WidgetStateParts
    parts=encode_window(window(),ChartPreferences(),1)
    public=json.loads(parts.public_data);public.pop('k',None)
    result=runtime(WidgetStateParts(json.dumps(public),parts.chat_data,parts.visible_start))
    assert result['states'][0]['candles']>0 and not any(result['disabled'])


def test_manual_pending_state_watchdog_survives_pointer_down():
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(parts,[{'kind':'tap','index':6},{'kind':'down'},
        {'kind':'visibility','hidden':True},{'kind':'tick','ms':16000}])
    assert all(result['measurements'][1]['disabled'])
    assert not any(result['disabled']) and result['status']=='error'


def test_arriving_revision_releases_manual_pending_watchdog():
    parts=encode_window(window(),ChartPreferences(),1)
    fresh=encode_window(window(),ChartPreferences(),2)
    result=runtime(parts,[{'kind':'tap','index':6},
        {'kind':'data','publicData':fresh.public_data,'chatData':fresh.chat_data},
        {'kind':'tick','ms':16000}])
    assert result['states'][-1]['revision']==2
    assert not any(result['disabled']) and result['status']=='ok'


def test_slow_physical_history_request_keeps_single_request_lock():
    parts=encode_window(window(),ChartPreferences(),1)
    result=runtime(parts,[{'kind':'tick','ms':0},{'kind':'tap','index':5},
        {'kind':'tick','ms':20000},{'kind':'reply','data':reserve_page(parts)},
        {'kind':'tick','ms':0}])
    assert len(result['requests'])==2
    assert result['requests'][-1]=='p1:tf:1M:1'
    assert not any(result['measurements'][3]['disabled'])
