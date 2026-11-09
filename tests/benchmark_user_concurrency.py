"""Opt-in offline load comparison: python tests/benchmark_user_concurrency.py.

Uses temporary state, a synthetic token and fake Telegram/market transports.
Reports handler/store throughput; does not model live API rate limits.
"""
import os,sys,asyncio,json,time,tempfile,resource,sqlite3
sys.dont_write_bytecode=True
os.environ['PYTHON_DOTENV_DISABLED']='1'
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timezone
from dataclasses import replace
from unittest.mock import patch
repo=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(repo),str(repo/'tests')]
import httpx
import bot as bm
from bot import PoolmasterBot
from user_state import _default_user
from price import PriceIdentityCheck
from snapshot import SnapshotManager
from chart_data import ChartDataService
from test_chart_widget import window
from test_snapshot import _price,_farming
from farming import FARMING_POOLS
from test_application_smoke import OfflineTelegramRequest
from test_concurrent_application import OfflineBuilder
from telegram import Update

async def scenario(limit,users):
 with tempfile.TemporaryDirectory(prefix='poolmaster-load-') as folder:
  root=Path(folder);previous=Path.cwd();os.chdir(root)
  values={'TELEGRAM_BOT_TOKEN':'123456:synthetic-test-token','PYTHON_DOTENV_DISABLED':'1','ADMIN_USER_IDS':'',
   'UPDATE_CONCURRENCY':str(limit),'PRICE_WIDGETS_ENABLED':'true','PRICE_WIDGET_QUERIES_ENABLED':'true',
   'FARMING_WIDGETS_ENABLED':'false','COIN_WIDGETS_ENABLED':'false','PRICE_WIDGET_BETA_UPDATES':'false',
   'GLOBAL_AUTO_PUSH_PAUSED':'true','TELEGRAM_BOT_USERNAME':'OFFLINE_POOLMASTER'}
  for key,name in {'AUTO_PUSH_STATE_PATH':'admin_state.json','SUBSCRIBERS_PATH':'subscribers.json',
   'STATS_PATH':'stats.json','MANAGED_DESTINATIONS_PATH':'destinations.json','USER_STATE_PATH':'user_state.json',
   'FARMING_CACHE_PATH':'farming_cache.json','FARMING_HISTORY_PATH':'history.sqlite3',
   'SNAPSHOT_CACHE_PATH':'snapshot.json','CHART_CACHE_DIR':'charts'}.items():values[key]=str(root/'data'/name)
  try:
   root.joinpath('data').mkdir()
   root.joinpath('data/user_state.json').write_text(json.dumps({'schema_version':2,'migrations':{},'users':{
    str(uid):{**_default_user(uid),'registered':True,'language':'ru'} for uid in range(1,users+1)}}))
   with patch.dict(os.environ,values):b=PoolmasterBot()
   counts={'candles':0,'price':0,'farming':0,'widgets':0};ids=0
   async def prices():counts['price']+=1;await asyncio.sleep(.05);return _price(fetched_at=datetime.now(timezone.utc))
   async def farms():counts['farming']+=1;await asyncio.sleep(.05);return [_farming(fetched_at=datetime.now(timezone.utc),pool=p) for p in FARMING_POOLS]
   b.snapshot_manager=SnapshotManager(farms,prices)
   b.price_identity_check=PriceIdentityCheck(True,'offline benchmark')
   async def reject_legacy(*args,**kwargs):raise AssertionError('unexpected legacy fallback in offline benchmark')
   b.fetch_verified_mexc_chart=reject_legacy
   source=window(count=55);end=int(time.time())//3600*3600
   candles=[replace(c,open_time=end-(54-i)*3600,close_time=end-(53-i)*3600) for i,c in enumerate(source.candles)]
   async def load(frame,before):counts['candles']+=1;await asyncio.sleep(.05);return candles
   async def http(request):
    nonlocal ids
    await asyncio.sleep(.02)
    body=json.loads(request.content)
    if request.url.path.endswith('/sendWidget'):
     ids+=1;counts['widgets']+=1
     result={'chat':{'id':body['chat_id'],'type':'private'},'message_id':ids,'widget':{'can_send_queries':True,'can_send_background_queries':True}}
    else:result=True
    return httpx.Response(200,json={'ok':True,'result':result})
   async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
    controller=b.price_widget_controller();controller.data=ChartDataService(loader=load);controller.transport._client=client
    async def background(_):await asyncio.Event().wait()
    b.alert_loop=b.managed_publication_loop=b.farming_history_loop=background
    errors=[]
    async def onerror(update,context):errors.append(type(context.error).__name__)
    b.error_handler=onerror
    completed=0;active=peak=0;durations=[];all_done=asyncio.Event();original=b.user_ui_callback
    async def handler(update,context):
     nonlocal completed,active,peak
     active+=1;peak=max(peak,active)
     try:await original(update,context)
     finally:
      active-=1;completed+=1;durations.append(time.perf_counter()-start)
      if completed%50==0:print(json.dumps({'progress':completed,'limit':limit,'seconds':round(time.perf_counter()-start,2)}),flush=True)
      if completed==users:all_done.set()
    b.user_ui_callback=handler
    def builder():return OfflineBuilder().request(OfflineTelegramRequest()).get_updates_request(OfflineTelegramRequest())
    with patch.object(bm,'ApplicationBuilder',builder):app=b.build_application()
    await app.initialize();await app.start()
    start=time.perf_counter()
    try:
     for uid in range(1,users+1):
      update=Update.de_json({'update_id':uid,'callback_query':{'id':str(uid),'from':{'id':uid,'is_bot':False,'first_name':'Load'},'chat_instance':'offline','data':'ui:price','message':{'message_id':10000+uid,'date':int(time.time()),'chat':{'id':uid,'type':'private'},'from':{'id':99,'is_bot':True,'first_name':'Bot'},'text':'Menu'}}},app.bot)
      await app.update_queue.put(update)
     try:await asyncio.wait_for(all_done.wait(),120)
     except TimeoutError:
      print(json.dumps({'timeout_completed':completed,'counts':counts,'errors':errors[:10],'queued':app.update_queue.qsize(),'lanes':len(app.update_processor._lanes),'active':app.update_processor.current_concurrent_updates}),flush=True)
      raise
     elapsed=time.perf_counter()-start
    finally:await app.stop();await app.shutdown()
    with sqlite3.connect(controller.sessions.path.resolve().as_uri()+'?mode=ro',uri=True) as db:stored=db.execute('select count(*) from widget_sessions').fetchone()[0]
    ordered=sorted(durations)
    result={'users':users,'work_limit':limit,'completed':completed,'stored_sessions':stored,'peak_active':peak,
     'elapsed_seconds':round(elapsed,3),'median_seconds':round(ordered[len(ordered)//2],3),'p95_seconds':round(ordered[int(users*.95)-1],3),
     'counts':counts,'errors':errors,'peak_process_rss_mb':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1)}
    assert completed==users and stored==users and counts['widgets']==users and peak<=limit and not errors
    return result
  finally:os.chdir(previous)
async def main():
 results=[]
 for limit,users in ((16,300),(1,300)):
  r=await scenario(limit,users);results.append(r);print(json.dumps(r),flush=True)
 print(json.dumps({'report':results}),flush=True)
if __name__=='__main__':asyncio.run(main())
