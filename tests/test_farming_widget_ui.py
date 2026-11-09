import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from farming_history import FarmingHistoryStore
from farming_widget_controller import FarmingWidgetController
from chart_widget_state import ChartWidgetSessionStore
from widget_transport import WidgetApiError
from widget_updates import ChartWidgetQuery
from test_chart_widget_ui import RecordingTransport
from test_farming_widget_data import Snapshots
from test_farming_history import SEED_CSV


def controller(tmp_path):
    history=FarmingHistoryStore(tmp_path/'history.sqlite3');history.seed_csv(SEED_CSV)
    snapshots=Snapshots()
    # Controller uses the real wall clock, matching the source receipt times.
    from dataclasses import replace
    snapshots.metrics=[replace(m,fetched_at=datetime.now(timezone.utc)) for m in snapshots.metrics]
    transport=RecordingTransport()
    c=FarmingWidgetController(transport=transport,history=history,snapshots=snapshots,
        sessions=ChartWidgetSessionStore(tmp_path/'sessions.sqlite3'),queries_enabled=True,cooldown_seconds=0)
    return c,transport


def test_open_sends_empty_caption_and_back_only(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'en')
        assert t.calls[0][3]==''
        buttons=t.calls[-1][-1]['inline_keyboard']
        assert len(buttons)==1 and buttons[0][0]['callback_data']=='ui:farming:history:back'
        assert buttons[0][0]['text']=='Back'
        assert c.sessions.get(ctx).timeframe=='7D'
    asyncio.run(run())


def test_refresh_forces_source_fetch_updates_kpis_and_restores_back(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');t.calls.clear()
        await c.handle(ChartWidgetQuery('q',41,ctx,'fw1:refresh:1'))
        state=json.loads(c.sessions.get(ctx).parts.public_data)
        assert state['q']=='50.00' and state['l']=='2000.00' and state['r']==2
        assert [x[0] for x in t.calls]==['answer','state','markup']
        assert t.calls[-1][-1]['inline_keyboard'][0][0]['callback_data']=='ui:farming:history:back'
        await c.handle(ChartWidgetQuery('q2',41,ctx,'fw1:refresh:2'))
        assert [x[0] for x in t.calls][-2:]==['state','markup']
    asyncio.run(run())


def test_foreign_owner_and_stale_revision_do_not_mutate(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru')
        for actor,data in [(42,'fw1:refresh:1'),(41,'fw1:refresh:0'),(41,'p1:refresh:1'),(41,'fw1:tf:3D:1')]:
            await c.handle(ChartWidgetQuery('q',actor,ctx,data))
        assert c.sessions.get(ctx).revision==1
        assert not any(x[0]=='state' for x in t.calls)
    asyncio.run(run())


def test_back_during_request_prevents_late_reopen(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');started=asyncio.Event();resume=asyncio.Event()
        original=c.snapshots.refresh_farming
        async def refresh():
            started.set();await resume.wait();return await original()
        c.snapshots.refresh_farming=refresh;t.calls.clear()
        task=asyncio.create_task(c.handle(ChartWidgetQuery('q',41,ctx,'fw1:refresh:1')))
        await started.wait();await c.close(ctx);resume.set();await task
        assert c.sessions.get(ctx) is None
        assert [x[0] for x in t.calls]==['answer']
    asyncio.run(run())


@pytest.mark.parametrize('failure',['network','incomplete'])
def test_failed_refresh_keeps_last_good_data_and_error_dot(tmp_path,failure):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');old=json.loads(c.sessions.get(ctx).parts.public_data)
        async def refresh():
            if failure=='network': raise OSError('source down')
            c.snapshots.metrics=[];return await c.snapshots.get_snapshot()
        c.snapshots.refresh_farming=refresh
        await c.handle(ChartWidgetQuery('q',41,ctx,'fw1:refresh:1'))
        new=json.loads(c.sessions.get(ctx).parts.public_data)
        assert new['e']=='unavailable' and new['a']==old['a'] and new['q']==old['q'] and new['l']==old['l'] and new['c']==old['c']
        assert t.calls[-1][0]=='markup'
    asyncio.run(run())


def test_pending_edit_recovers_after_restart(tmp_path):
    c,t=controller(tmp_path)
    async def run():
        ctx=await c.open(41,'ru');original=t.edit_markup
        async def failed(*args):raise WidgetApiError('network',unknown=True)
        t.edit_markup=failed
        await c.handle(ChartWidgetQuery('q',41,ctx,'fw1:refresh:1'))
        assert c.sessions.pending(ctx) is not None
        c.sessions=ChartWidgetSessionStore(c.sessions.path);t.edit_markup=original
        await c.handle(ChartWidgetQuery('q2',41,ctx,'fw1:refresh:2'))
        assert c.sessions.pending(ctx) is None and c.sessions.get(ctx).revision==2
        assert len([x for x in t.calls if x[0]=='send'])==1
    asyncio.run(run())


def test_initial_state_edit_retains_back_when_queries_denied(tmp_path):
    c,t=controller(tmp_path);t.queries=False
    async def run():
        ctx=await c.open(41,'ru')
        assert json.loads(c.sessions.get(ctx).parts.public_data)['i']==0
        assert [x[0] for x in t.calls][-2:]==['state','markup']
    asyncio.run(run())


def test_farming_and_price_queries_are_separate(tmp_path,monkeypatch):
    from bot import PoolmasterBot
    from telegram import Update
    bot=object.__new__(PoolmasterBot)
    bot.settings=SimpleNamespace(price_widgets_enabled=True,farming_widgets_enabled=True)
    routes=[]
    class Handler:
        def __init__(self,label):self.label=label
        async def handle(self,q):routes.append(self.label)
    monkeypatch.setattr(bot,'price_widget_controller',lambda:Handler('price'))
    monkeypatch.setattr(bot,'farming_widget_controller',lambda:Handler('farm'))
    async def run():
        for data in ('fw1:refresh:1','p1:refresh:1'):
            update=Update.de_json(dict(update_id=1,widget_query={'id':'q','from':{'id':41},'chat':{'id':41,'type':'private'},'message_id':5,'data':data}),None)
            await bot.widget_query(update,None)
        assert routes==['farm','price']
    asyncio.run(run())


@pytest.mark.parametrize('outcome',['sent','uncertain','legacy'])
def test_private_history_widget_and_safe_legacy_fallback(tmp_path,monkeypatch,outcome):
    from test_farming_history_ui import _bot,_HistoryQuery,_chart
    import bot as bot_module
    bot=_bot(tmp_path,monkeypatch)
    from dataclasses import replace
    bot.settings=replace(bot.settings,farming_widgets_enabled=True)
    events=[];query=_HistoryQuery('ui:farming:history',events)
    async def open_widget(*args):events.append('widget');return outcome
    monkeypatch.setattr(bot,'try_open_farming_widget',open_widget)
    class Snapshots:
        last_history_write_error=None
        async def get_snapshot(self):return object()
    bot.snapshot_manager=Snapshots()
    bot.farming_history_store=SimpleNamespace(get_summary=lambda **kwargs:object())
    monkeypatch.setattr(bot_module,'build_farming_history_chart',lambda *args,**kwargs:_chart('legacy'))
    class Telegram:
        async def send_photo(self,**kwargs):events.append('photo')
    asyncio.run(bot.send_private_farming_history_photo(query,SimpleNamespace(bot=Telegram()),'ru'))
    assert events==({'sent':['widget','delete'],'uncertain':['widget'],'legacy':['widget','photo','delete']}[outcome])


def test_feature_off_preserves_legacy(tmp_path,monkeypatch):
    from test_farming_history_ui import _bot
    bot=_bot(tmp_path,monkeypatch)
    assert bot.settings.farming_widgets_enabled is False
    monkeypatch.setattr(bot,'farming_widget_controller',lambda:pytest.fail('feature off must not initialize controller'))
    assert asyncio.run(bot.try_open_farming_widget(7,'ru'))=='legacy'


def test_refresh_uses_real_snapshot_pipeline_and_persists_daily_observations(tmp_path):
    from snapshot import SnapshotManager
    from test_farming_history import _metric
    from test_snapshot import _price
    import sqlite3
    c,t=controller(tmp_path)
    count=0
    async def fetch_farming():
        nonlocal count
        count+=1
        now=datetime.now(timezone.utc)
        return [_metric(i,str(10 if count==1 else 40),now) for i in range(4)]
    async def fetch_price():return _price(fetched_at=datetime.now(timezone.utc))
    c.snapshots=SnapshotManager(fetch_farming,fetch_price,farming_history_store=c.history)
    async def run():
        ctx=await c.open(41,'ru')
        await c.handle(ChartWidgetQuery('q',41,ctx,'fw1:refresh:1'))
        assert json.loads(c.sessions.get(ctx).parts.public_data)['q']=='40.00'
        with sqlite3.connect(c.history.path) as db:
            values=db.execute('SELECT value_pct FROM observations WHERE date_utc=?',(datetime.now(timezone.utc).date().isoformat(),)).fetchall()
        assert values==[('40',)]*4
    asyncio.run(run())
