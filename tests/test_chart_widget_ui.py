import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chart_data import ChartDataService
from chart_source import ChartSourceTimeout
from chart_widget import ChartPreferences, load_widget_html
from chart_widget_controller import ChartWidgetController
from chart_widget_state import ChartWidgetSessionStore
from price import PriceMetrics
from farming import SourceStatus
from user_state import UserAlertStore
from widget_transport import WidgetApiError, WidgetReceipt, WidgetMessageContext
from widget_updates import ChartWidgetQuery
from test_chart_widget import window


class RecordingTransport:
    def __init__(self):
        self.calls=[]
        self.caption_error=False
        self.queries=True
    async def send_private(self, chat_id, html, public_data, chat_data, caption, reply_markup, **kwargs):
        self.calls.append(('send',chat_id,json.loads(public_data),caption,reply_markup))
        return WidgetReceipt(chat_id,len(self.calls)+10,self.queries,False)
    async def edit_state(self,ctx,public,private):
        self.calls.append(('state',ctx,json.loads(public),json.loads(private)))
        return True
    async def answer(self,id,data):
        self.calls.append(('answer',id,json.loads(data) if data else None))
    async def edit_caption(self,ctx,caption,*,reply_markup=None):
        self.calls.append(('caption',ctx,caption,reply_markup))
        if self.caption_error: raise WidgetApiError('rejected')
    async def edit_markup(self,ctx,markup):
        self.calls.append(('markup',ctx,markup))


def controller(tmp_path):
    now=datetime(2026,11,8,tzinfo=timezone.utc)
    price=PriceMetrics(Decimal('0.01623'),None,None,Decimal('1234'),'JETTONUSDT','USDT',
        'MEXC spot',now,now,SourceStatus.OK,price_change_24h_percent=Decimal('0.2'),
        high_price_24h=Decimal('0.017'),low_price_24h=Decimal('0.016'))
    class Snapshots:
        refreshed=0
        value=type('Snapshot',(),{'price':price,'anomalies':{}})()
        async def get_snapshot(self): return self.value
        async def refresh_price(self):
            self.refreshed+=1
            return await self.get_snapshot()
    class Data:
        def __init__(self): self.calls=[]; self.delay=None; self.fail=False
        async def load_window(self,frame,**kwargs):
            self.calls.append((frame,kwargs))
            if self.delay: await self.delay.wait()
            if self.fail: raise ChartSourceTimeout('source down')
            return window(frame)
    transport=RecordingTransport()
    c=ChartWidgetController(transport=transport,data=Data(),users=UserAlertStore(tmp_path/'users.json'),
        sessions=ChartWidgetSessionStore(tmp_path/'widgets.sqlite3'),snapshots=Snapshots(),
        identity_valid=lambda:True,queries_enabled=True,cooldown_seconds=0)
    return c,transport


def test_widget_bundle_fits_with_compatibility_and_both_languages():
    import zlib
    for lang in ('ru','en'):
        content=load_widget_html(language=lang,interactive=True)
        assert len(content.encode())<=32768 and len(zlib.compress(content.encode(),9))<=4096
        assert '__LOGO__' not in content and '__QUERY__' not in content


def test_price_header_starts_with_brand_text_without_logo_or_placeholder():
    import re
    content=load_widget_html(language='ru',interactive=True)
    header=re.search(r'<header>(.*?)</header>',content,re.S).group(1)
    assert header.startswith('<b>JETTON POOL MASTER</b>')
    assert '<svg' not in header and '<img' not in header
    assert '__LOGO__' not in content
    assert 'padding:10px' in content


def test_private_open_and_timeframe_are_personal_and_back_only(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        a=await c.open(41,'ru')
        b=await c.open(42,'en')
        assert t.calls[0][-1]['inline_keyboard']==[[{'text':'Назад','callback_data':'chart:back','style':'danger'}]]
        await c.handle(ChartWidgetQuery('q',41,a,'p1:tf:1M:1'))
        assert c.users.get_chart_preferences(41).timeframe=='1M'
        assert c.users.get_chart_preferences(42).timeframe=='1h'
        assert c.sessions.get(b).timeframe=='1h'
        assert c.sessions.get(a).revision==2
    asyncio.run(run())


def test_bad_owner_stale_revision_and_unknown_action_cannot_change_chart(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        for actor,data in [(42,'p1:tf:1M:1'),(41,'p1:tf:1m:1'),(41,'p1:refresh:0'),(41,'other:refresh:1')]:
            await c.handle(ChartWidgetQuery('q',actor,ctx,data))
        assert c.sessions.get(ctx).revision==1
        assert not any(x[0]=='state' for x in t.calls)
    asyncio.run(run())


def test_refresh_bypasses_cache_and_updates_quote_caption_and_widget(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'en')
        t.calls.clear()
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:1'))
        assert c.data.calls[-1][1]['force_refresh'] is True
        assert c.snapshots.refreshed==1
        assert any(x[0]=='state' and x[2]['r']==2 for x in t.calls)
        caption=[x[2] for x in t.calls if x[0]=='caption'][-1]
        assert '24' in caption and 'Candle' not in caption and 'Range' not in caption
        assert [x[0] for x in t.calls].index('answer') < [x[0] for x in t.calls].index('caption')
    asyncio.run(run())


def test_back_during_slow_refresh_never_resurrects_message(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        initial=len(t.calls)
        c.data.delay=asyncio.Event()
        task=asyncio.create_task(c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:1')))
        await asyncio.sleep(0)
        c.sessions.close(ctx)
        c.data.delay.set()
        await task
        assert c.sessions.get(ctx) is None
        assert not any(x[0] in ('state','caption') for x in t.calls[initial:])
        assert len([x for x in t.calls if x[0]=='send'])==1
    asyncio.run(run())


def test_caption_failure_is_partial_and_durable(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        t.caption_error=True
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:1'))
        session=c.sessions.get(ctx)
        assert session.pending_caption
        assert json.loads(session.parts.public_data)['e']=='caption_pending'
        t.caption_error=False
        await c.handle(ChartWidgetQuery('q2',41,ctx,'p1:refresh:2'))
        assert c.sessions.get(ctx).pending_caption is None
    asyncio.run(run())


def test_source_failure_keeps_last_candles_and_reports_error(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        old=json.loads(c.sessions.get(ctx).parts.public_data)
        c.data.fail=True
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:1'))
        new=json.loads(c.sessions.get(ctx).parts.public_data)
        assert new['e']=='unavailable' and new['c']==old['c'] and new['a']==old['a']
    asyncio.run(run())


def test_widget_status_and_query_answers_include_localized_text(tmp_path,monkeypatch):
    monkeypatch.setattr('chart_widget_controller.load_widget_html',lambda **kwargs:'<div>chart</div>')
    c,t=controller(tmp_path)
    c.users.set_language(41,'en')
    async def run():
        ctx=await c.open(41,'en')
        c.data.fail=True
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:1'))
        answer=next(x[2] for x in t.calls if x[0]=='answer')
        assert answer['text']=='Updating…'
        assert json.loads(c.sessions.get(ctx).parts.public_data)['m']=='Source unavailable'
    asyncio.run(run())


def test_missing_server_query_permission_restores_working_inline_controls(tmp_path):
    c,t=controller(tmp_path)
    t.queries=False
    async def run():
        ctx=await c.open(41,'ru')
        markups=[x[-1] for x in t.calls if x[0]=='markup']
        assert any(button.get('callback_data')=='wchart:refresh' for row in markups[-1]['inline_keyboard'] for button in row)
        assert json.loads(c.sessions.get(ctx).parts.public_data)['i']==0
    asyncio.run(run())


@pytest.mark.parametrize('queries,caption_error',[(False,False),(True,True)])
def test_initial_status_edit_restores_keyboard_after_state(queries,caption_error,tmp_path):
    c,t=controller(tmp_path)
    t.queries=queries
    t.caption_error=caption_error
    async def run():
        await c.open(41,'ru')
        state_index=max(i for i,call in enumerate(t.calls) if call[0]=='state')
        markup_index=max(i for i,call in enumerate(t.calls) if call[0]=='markup')
        assert markup_index>state_index
        assert any(button['callback_data']=='chart:back'
            for row in t.calls[markup_index][-1]['inline_keyboard'] for button in row)
    asyncio.run(run())


def test_bot_open_classifies_uncertain_send_without_static_duplicate(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from bot import PoolmasterBot
    bot=PoolmasterBot.__new__(PoolmasterBot)
    class Failed:
        async def open(self,chat_id,language): raise WidgetApiError('network',unknown=True)
    bot.settings=SimpleNamespace(price_widgets_enabled=True)
    monkeypatch.setattr(bot,'price_widget_controller',lambda:Failed())
    assert asyncio.run(bot.try_open_price_widget(41,'ru'))=='uncertain'


def test_bot_uses_legacy_on_explicit_widget_denial(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from bot import PoolmasterBot
    bot=PoolmasterBot.__new__(PoolmasterBot)
    class Denied:
        async def open(self,chat_id,language): raise WidgetApiError('not_allowed')
    bot.settings=SimpleNamespace(price_widgets_enabled=True)
    monkeypatch.setattr(bot,'price_widget_controller',lambda:Denied())
    assert asyncio.run(bot.try_open_price_widget(41,'ru'))=='legacy'


def test_expired_snapshot_is_not_retained_as_usable_candles(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        session=c.sessions.get(ctx)
        a=json.loads(session.parts.public_data); a['a']=1
        from chart_widget import WidgetStateParts
        c.sessions.finish(ctx,'1h',1,None,WidgetStateParts(json.dumps(a),session.parts.chat_data,session.parts.visible_start))
        c.data.fail=True
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:1'))
        parts=c.sessions.get(ctx).parts
        assert json.loads(parts.public_data)['e']=='expired'
        assert json.loads(parts.public_data)['c']=='' and json.loads(parts.chat_data)['c']==''
    asyncio.run(run())


def test_fresh_candles_and_unavailable_quote_are_reported_as_partial(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        c.snapshots.value.price=replace(c.snapshots.value.price,status=SourceStatus.TIMEOUT,price_usdt=None)
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:1'))
        assert json.loads(c.sessions.get(ctx).parts.public_data)['e']=='quote_unavailable'
        assert 'недоступна' in [x[2] for x in t.calls if x[0]=='caption'][-1]
    asyncio.run(run())


def test_error_after_confirmed_send_never_causes_a_static_duplicate(tmp_path):
    c,t=controller(tmp_path)
    t.queries=False
    async def fail(*args): raise WidgetApiError('not_allowed')
    t.edit_markup=fail
    with pytest.raises(WidgetApiError) as e:
        asyncio.run(c.open(41,'ru'))
    assert e.value.unknown


@pytest.mark.parametrize('uncertain', [False, True])
def test_real_private_price_route_deletes_menu_only_after_delivery(tmp_path, monkeypatch, uncertain):
    from types import SimpleNamespace
    from test_user_ui import _make_bot, _UIQuery
    bot = _make_bot(tmp_path, monkeypatch)
    bot.settings = replace(bot.settings, price_widgets_enabled=True)
    c, transport = controller(tmp_path)
    bot._price_widget_controller = c
    if uncertain:
        async def lost_receipt(*args, **kwargs):
            raise WidgetApiError('network_or_invalid_receipt', unknown=True)
        transport.send_private = lost_receipt
    deleted = []
    query = _UIQuery('ui:price')
    async def delete():
        assert transport.calls[0][0] == 'send'
        deleted.append(True)
    query.message.delete = delete
    # No send_photo boundary: an unexpected legacy replacement must fail.
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace())
    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=query), context))
    assert deleted == ([] if uncertain else [True])


@pytest.mark.parametrize('applied', [False,True])
def test_uncertain_edit_recovers_durably_after_restart_at_either_revision(tmp_path,applied):
    c,t=controller(tmp_path)
    original=t.edit_state
    async def ambiguous(ctx,public,private):
        if applied:
            await original(ctx,public,private)
        raise WidgetApiError('network_or_invalid_receipt',unknown=True)
    async def run():
        ctx=await c.open(41,'ru')
        t.edit_state=ambiguous
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:tf:1M:1'))
        # Repeated ambiguous delivery must not erase the intended transition.
        await c.handle(ChartWidgetQuery('q2',41,ctx,'p1:refresh:'+('2' if applied else '1')))
        restarted=ChartWidgetSessionStore(c.sessions.path)
        c.sessions=restarted
        t.edit_state=original
        calls=len(c.data.calls)
        await c.handle(ChartWidgetQuery('q3',41,ctx,'p1:refresh:'+('2' if applied else '1')))
        assert c.sessions.get(ctx).revision==2
        assert c.sessions.get(ctx).timeframe=='1M'
        assert c.users.get_chart_preferences(41).timeframe=='1M'
        assert len(c.data.calls)==calls  # reconcile exact intent, don't fetch an unrelated page
        assert not any(x[0]=='answer' and x[2]['status']=='reopen' for x in t.calls)
        assert [x[2] for x in t.calls if x[0]=='answer'][-1]['revision']==2
    asyncio.run(run())


def test_back_discards_uncertain_transition_before_recovery(tmp_path):
    c,t=controller(tmp_path)
    async def ambiguous(*args): raise WidgetApiError('network',unknown=True)
    async def run():
        ctx=await c.open(41,'ru')
        t.edit_state=ambiguous
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:tf:1M:1'))
        c.sessions.close(ctx)
        assert c.sessions.pending(ctx) is None
        await c.handle(ChartWidgetQuery('q2',41,ctx,'p1:refresh:2'))
        assert t.calls[-1][0]=='answer' and t.calls[-1][2]['status']=='reopen'
    asyncio.run(run())


def test_timeframe_selection_changes_navigation_epoch_only_for_selection(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        original=json.loads(c.sessions.get(ctx).parts.public_data)['u']
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:tf:1h:1'))
        selected=json.loads(c.sessions.get(ctx).parts.public_data)['u']
        assert selected!=original
        await c.handle(ChartWidgetQuery('q2',41,ctx,'p1:refresh:2'))
        assert json.loads(c.sessions.get(ctx).parts.public_data)['u']==selected
    asyncio.run(run())


def test_native_fallback_reopen_response_is_visible_to_user(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from test_user_ui import _make_bot, _UIQuery
    bot=_make_bot(tmp_path,monkeypatch)
    bot.settings=replace(bot.settings,price_widgets_enabled=True)
    c,t=controller(tmp_path)
    bot._price_widget_controller=c
    ctx=asyncio.run(c.open(7,'ru'))
    query=_UIQuery('wchart:refresh')
    query.id='callback-id'
    query.message.message_id=ctx.message_id
    async def action(parsed,*,answer): await answer('{"status":"reopen"}')
    c.handle=action
    asyncio.run(bot.widget_inline_callback(SimpleNamespace(callback_query=query),SimpleNamespace()))
    assert query.answers[0][0]=='Откройте «Цена» заново'


def test_recovering_old_uncertain_transition_does_not_republish_expired_candles(tmp_path,monkeypatch):
    c,t=controller(tmp_path)
    original=t.edit_state
    async def ambiguous(*args): raise WidgetApiError('network',unknown=True)
    async def run():
        ctx=await c.open(41,'ru')
        t.edit_state=ambiguous
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:tf:1M:1'))
        import time
        later=time.time()+3600
        monkeypatch.setattr(time,'time',lambda:later)
        t.edit_state=original
        await c.handle(ChartWidgetQuery('q2',41,ctx,'p1:refresh:1'))
        parts=c.sessions.get(ctx).parts
        assert json.loads(parts.public_data)['e']=='expired'
        assert json.loads(parts.public_data)['c']==json.loads(parts.chat_data)['c']==''
    asyncio.run(run())


def test_caption_and_uncertain_state_recovery_clears_partial_status(tmp_path):
    c,t=controller(tmp_path)
    original=t.edit_state
    async def ambiguous(*args): raise WidgetApiError('network',unknown=True)
    async def run():
        ctx=await c.open(41,'ru')
        t.caption_error=True
        t.edit_state=ambiguous
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:1'))
        t.caption_error=False
        t.edit_state=original
        await c.handle(ChartWidgetQuery('q2',41,ctx,'p1:refresh:1'))
        session=c.sessions.get(ctx)
        assert session.revision==2 and session.pending_caption is None
        assert json.loads(session.parts.public_data)['e']=='ok'
    asyncio.run(run())


def test_new_action_waits_until_keyboard_install_finishes(tmp_path):
    c,t=controller(tmp_path)
    t.queries=False
    async def run():
        ctx=await c.open(41,'ru')
        running=[]
        async def markup(*args):
            if running: return
            c.data.delay=asyncio.Event()
            running.append(asyncio.create_task(c.handle(ChartWidgetQuery('q2',41,ctx,'p1:refresh:2'))))
            for _ in range(5): await asyncio.sleep(0)
        t.edit_markup=markup
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:tf:1M:1'))
        try:
            assert [x[2]['status'] for x in t.calls if x[0]=='answer'][-1]=='cooldown'
            assert len(c.data.calls)==2
            assert c.sessions.claim(ctx,2) is not None
        finally:
            c.data.delay.set()
            await running[0]
    asyncio.run(run())


def test_unknown_capability_does_not_disable_requested_private_widget_controls(tmp_path):
    c,t=controller(tmp_path)
    t.queries=None
    async def run():
        ctx=await c.open(41,'ru')
        assert json.loads(c.sessions.get(ctx).parts.public_data)['i']==1
        assert all(len(call[-1]['inline_keyboard'])==1 for call in t.calls if call[0]=='markup')
        assert len(t.calls[0][-1]['inline_keyboard'])==1  # Back only
    asyncio.run(run())


def test_axis_latest_action_resets_history_cursor_without_changing_preference(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        await c.handle(ChartWidgetQuery('q1',41,ctx,'p1:earlier:1'))
        historical=c.sessions.get(ctx)
        assert historical.before_open_time is not None
        assert json.loads(historical.parts.public_data)['l']==0
        await c.handle(ChartWidgetQuery('q2',41,ctx,'p1:latest:2'))
        current=c.sessions.get(ctx)
        assert current.revision==3 and current.before_open_time is None
        assert json.loads(current.parts.public_data)['l']==1
        assert c.data.calls[-1][1]['before_open_time'] is None
        assert c.users.get_chart_preferences(41).timeframe=='1h'
    asyncio.run(run())


def test_back_and_caption_are_installed_even_when_sendwidget_omits_them(tmp_path):
    c,t=controller(tmp_path)
    t.queries=None
    async def run():
        ctx=await c.open(41,'ru')
        keyboards=[call[-1]['inline_keyboard'] for call in t.calls
                   if call[0] in ('caption','markup') and isinstance(call[-1],dict)]
        assert keyboards and all(rows==[[{'text':'Назад','callback_data':'chart:back','style':'danger'}]] for rows in keyboards)
        captions=[call[2] for call in t.calls if call[0]=='caption']
        assert captions and '24' in captions[-1]
        assert c.sessions.get(ctx) is not None
    asyncio.run(run())


def test_back_keyboard_is_restored_after_each_interactive_state_edit(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        t.calls.clear()
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:1'))
        kinds=[x[0] for x in t.calls]
        assert 'markup' in kinds and kinds.index('state')<kinds.index('markup')
        keyboard=[x[2] for x in t.calls if x[0]=='markup'][-1]['inline_keyboard']
        assert keyboard==[[{'text':'Назад','callback_data':'chart:back','style':'danger'}]]
    asyncio.run(run())


@pytest.mark.parametrize('language,back_label',[('ru','Назад'),('en','Back')])
def test_initial_open_retains_back_when_caption_edit_clears_keyboard(tmp_path,language,back_label):
    class CaptionClearingTransport(RecordingTransport):
        visible_markup = None
        transitions = None
        async def send_private(self,*args,**kwargs):
            receipt=await super().send_private(*args,**kwargs)
            self.visible_markup=args[5]
            self.transitions=[self.visible_markup]
            return receipt
        async def edit_caption(self,ctx,caption,*,reply_markup=None):
            await super().edit_caption(ctx,caption,reply_markup=reply_markup)
            # A caption edit replaces its keyboard, including omission.
            self.visible_markup=reply_markup
            self.transitions.append(self.visible_markup)
        async def edit_markup(self,ctx,markup):
            await super().edit_markup(ctx,markup)
            self.visible_markup=markup
            self.transitions.append(self.visible_markup)
    c,_=controller(tmp_path)
    transport=CaptionClearingTransport()
    c.transport=transport
    async def run():
        ctx=await c.open(41,language)
        assert c.sessions.get(ctx).revision==1
        assert all(transport.transitions),'Back must never disappear between initial send and final setup'
        assert transport.visible_markup is not None,'Back must exist before the first user action'
        assert transport.visible_markup['inline_keyboard']==[[{
            'text':back_label,'callback_data':'chart:back','style':'danger'}]]
    asyncio.run(run())


@pytest.mark.parametrize('action',['tf:1M','earlier','latest'])
def test_chart_navigation_does_not_replace_header_quote_with_another_candle(tmp_path,action):
    from chart_widget import decode_window
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        initial=json.loads(c.sessions.get(ctx).parts.public_data)['q']
        original=c.data.load_window
        async def changed_window(frame,**kwargs):
            source=await original(frame,**kwargs)
            value=Decimal('0.01000') if kwargs.get('before_open_time') else Decimal('0.01699')
            shift=48*3600 if kwargs.get('before_open_time') else 0
            return replace(source,candles=tuple(replace(bar,open_time=bar.open_time-shift,
                close_time=bar.close_time-shift,open=value,high=value+Decimal('.00001'),
                low=value-Decimal('.00001'),close=value) for bar in source.candles))
        c.data.load_window=changed_window
        t.calls.clear()
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:'+action+':1'))
        parts=c.sessions.get(ctx).parts
        assert decode_window(parts).candles[-1].close != Decimal(initial),'chart navigation must still load its candles'
        assert json.loads(parts.public_data)['q']==initial,'navigation must not refresh the displayed market quote'
        assert not any(call[0]=='caption' for call in t.calls)
    asyncio.run(run())


@pytest.mark.parametrize('historical',[False,True])
def test_manual_refresh_can_change_the_header_quote(tmp_path,historical):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        original=c.data.load_window
        async def changed_window(frame,**kwargs):
            source=await original(frame,**kwargs)
            value=Decimal('0.01699')
            return replace(source,candles=tuple(replace(bar,open=value,high=value+Decimal('.00001'),
                low=value-Decimal('.00001'),close=value) for bar in source.candles))
        c.data.load_window=changed_window
        revision=1
        if historical:
            await c.handle(ChartWidgetQuery('older',41,ctx,'p1:earlier:1'))
            revision=2
        async def fresh_quote():
            c.snapshots.value.price=replace(c.snapshots.value.price,price_usdt=Decimal('0.01655'))
            return c.snapshots.value
        c.snapshots.refresh_price=fresh_quote
        await c.handle(ChartWidgetQuery('q',41,ctx,'p1:refresh:'+str(revision)))
        assert Decimal(json.loads(c.sessions.get(ctx).parts.public_data)['q'])==Decimal('0.01655')
        assert '0.01655' in [call[2] for call in t.calls if call[0]=='caption'][-1]
    asyncio.run(run())


def test_initial_header_quote_matches_caption_not_the_candle_close(tmp_path):
    c,t=controller(tmp_path)
    c.snapshots.value.price=replace(c.snapshots.value.price,price_usdt=Decimal('0.01640'))
    async def run():
        ctx=await c.open(41,'ru')
        assert Decimal(json.loads(c.sessions.get(ctx).parts.public_data)['q'])==Decimal('0.01640')
        assert '0.01640' in [call[2] for call in t.calls if call[0]=='caption'][-1]
    asyncio.run(run())


def test_history_reserve_is_exact_read_only_and_keeps_session_cursor(tmp_path):
    from chart_widget import decode_window, WidgetStateParts, compact
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');before=c.sessions.get(ctx)
        boundary=before.parts.visible_start
        older=replace(window(),candles=tuple(replace(v,open_time=v.open_time-30*3600) for v in window().candles))
        async def load(frame,**kwargs):
            assert kwargs=={'before_open_time':boundary}
            return older
        c.data.load_window=load;t.calls.clear()
        await c.handle(ChartWidgetQuery('reserve',41,ctx,f'p1:history:{boundary}:1'))
        assert [v[0] for v in t.calls]==['answer']
        page=t.calls[0][2]
        assert page['status']=='history' and page['r']==1 and page['b']==boundary
        assert len(json.dumps(page,separators=(',',':')).encode())<=1024
        decoded=decode_window(WidgetStateParts(compact(dict(page,v=1)),compact(dict(v=1,r=page['r'],f=page['f'],t=page['t'],w=0,c='')),0)).candles
        assert decoded and decoded[-1].close==older.candles[-1].close
        assert decoded[-1].quote_volume==older.candles[-1].quote_volume
        assert decoded[-1].open_time<boundary
        assert c.sessions.get(ctx)==before and c.sessions.pending(ctx) is None
        assert c.snapshots.refreshed==0
    asyncio.run(run())


@pytest.mark.parametrize('data', ['p1:history:999999999999:1','p1:history:0:1','p1:history:no:1','p1:history:1:0'])
def test_history_rejects_invalid_cursor_or_revision(tmp_path,data):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');t.calls.clear();c.data.calls.clear()
        await c.handle(ChartWidgetQuery('reserve',41,ctx,data))
        assert t.calls[-1][2]['status']=='reopen'
        assert not c.data.calls and c.sessions.get(ctx).revision==1
    asyncio.run(run())


def test_history_completion_after_back_is_discarded(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');boundary=c.sessions.get(ctx).parts.visible_start
        c.data.delay=asyncio.Event();t.calls.clear()
        task=asyncio.create_task(c.handle(ChartWidgetQuery('reserve',41,ctx,f'p1:history:{boundary}:1')))
        await asyncio.sleep(.01);c.sessions.close(ctx);c.data.delay.set();await task
        assert [v[0] for v in t.calls]==['answer']
        assert t.calls[0][2]['status']=='reopen'
    asyncio.run(run())


def test_history_rate_limit_is_per_message_and_does_not_lock_manual_actions(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');boundary=c.sessions.get(ctx).parts.visible_start
        source=window()
        async def load(frame,**kwargs):
            if kwargs.get('before_open_time'):
                return replace(source,candles=tuple(replace(v,open_time=v.open_time-30*3600) for v in source.candles))
            return window(frame)
        c.data.load_window=load;t.calls.clear()
        await c.handle(ChartWidgetQuery('a',41,ctx,f'p1:history:{boundary}:1'))
        await c.handle(ChartWidgetQuery('b',41,ctx,f'p1:history:{boundary}:1'))
        assert [v[2]['status'] for v in t.calls]==['history','cooldown']
        await c.handle(ChartWidgetQuery('frame',41,ctx,'p1:tf:1M:1'))
        assert c.sessions.get(ctx).timeframe=='1M'
    asyncio.run(run())


def test_history_answer_after_frame_change_is_discarded(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');boundary=c.sessions.get(ctx).parts.visible_start
        waiting=asyncio.Event();entered=asyncio.Event()
        async def load(frame,**kwargs):
            if kwargs.get('before_open_time'):
                entered.set();await waiting.wait()
            return window(frame)
        c.data.load_window=load;t.calls.clear()
        task=asyncio.create_task(c.handle(ChartWidgetQuery('history',41,ctx,f'p1:history:{boundary}:1')))
        await entered.wait()
        await c.handle(ChartWidgetQuery('frame',41,ctx,'p1:tf:1M:1'))
        waiting.set();await task
        answer=next(v[2] for v in t.calls if v[0]=='answer' and v[1]=='history')
        assert answer['status']=='reopen'
        assert c.sessions.get(ctx).timeframe=='1M' and c.sessions.get(ctx).revision==2
    asyncio.run(run())


def test_history_empty_page_and_source_failure_never_edit_the_message(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');boundary=c.sessions.get(ctx).parts.visible_start
        async def empty(frame,**kwargs):return replace(window(frame),candles=(),visible_count=0,has_earlier=False)
        c.data.load_window=empty;t.calls.clear()
        await c.handle(ChartWidgetQuery('empty',41,ctx,f'p1:history:{boundary}:1'))
        assert [v[0] for v in t.calls]==['answer']
        assert t.calls[0][2]['c']=='' and t.calls[0][2]['h'] is False
        c._history_at.clear()
        async def fail(frame,**kwargs):raise ChartSourceTimeout('source down')
        c.data.load_window=fail;t.calls.clear()
        await c.handle(ChartWidgetQuery('failed',41,ctx,f'p1:history:{boundary}:1'))
        assert [v[0] for v in t.calls]==['answer'] and t.calls[0][2]['status']=='unavailable'
        assert c.sessions.get(ctx).revision==1
    asyncio.run(run())
