"""Exercise the real PTB update queue and application shutdown without networking."""
import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import patch

from telegram import Update
from telegram.ext import ApplicationBuilder

import bot as bot_module
from bot import PoolmasterBot
from config import load_settings
from test_application_smoke import OfflineTelegramRequest


class OfflineBuilder(ApplicationBuilder):
    def connection_pool_size(self,size):return self
    def pool_timeout(self,timeout):return self


def make_application(handler,limit=16):
    b=object.__new__(PoolmasterBot)
    b.settings=load_settings({'UPDATE_CONCURRENCY':str(limit)})
    b.token='123456:synthetic-test-token';b.start=handler
    async def background(_):await asyncio.Event().wait()
    b.alert_loop=b.managed_publication_loop=b.farming_history_loop=background
    errors=[]
    async def error(update,context):errors.append(context.error)
    b.error_handler=error
    request=OfflineTelegramRequest()
    def builder():return OfflineBuilder().request(request).get_updates_request(OfflineTelegramRequest())
    with patch.object(bot_module,'ApplicationBuilder',builder):app=b.build_application()
    return app,errors,request


def command(uid,sequence=1,telegram_bot=None):
    return Update.de_json({'update_id':uid*10+sequence,'message':{
        'message_id':sequence,'date':int(datetime.now(timezone.utc).timestamp()),
        'chat':{'id':uid,'type':'private'},'from':{'id':uid,'is_bot':False,'first_name':'Offline'},
        'text':'/start','entities':[{'type':'bot_command','offset':0,'length':6}]}},telegram_bot)


def test_actual_queue_handles_three_hundred_users_with_bounded_parallelism():
    async def run():
        active=peak=0;seen={i:[] for i in range(1,301)}
        async def handler(update,context):
            nonlocal active,peak
            active+=1;peak=max(active,peak)
            await asyncio.sleep(.001)
            seen[update.effective_user.id].append(update.message.message_id)
            active-=1
        app,errors,_=make_application(handler)
        await app.initialize();await app.start()
        try:
            for sequence in (1,2):
                for uid in seen:await app.update_queue.put(command(uid,sequence,app.bot))
            await asyncio.wait_for(app.update_queue.join(),5)
            await asyncio.wait_for(app._drain_work(),5)
            assert 1<peak<=16
            assert all(v==[1,2] for v in seen.values())
        finally:
            await app.stop();await app.shutdown()
        assert errors==[] and active==0 and not app._work_tasks
    asyncio.run(run())


def test_real_stop_drains_accepted_work_while_another_user_can_progress():
    async def run():
        started=asyncio.Event();release=asyncio.Event();other=asyncio.Event();seen=[]
        async def handler(update,context):
            if update.effective_user.id==1:
                if update.message.message_id==1:started.set();await release.wait()
                seen.append(update.message.message_id)
                await context.bot.send_message(chat_id=1,text='completed offline')
            else:other.set()
        app,errors,request=make_application(handler,2)
        await app.initialize();await app.start()
        stopping=None
        try:
            await app.update_queue.put(command(1,1,app.bot));await asyncio.wait_for(started.wait(),2)
            await app.update_queue.put(command(1,2,app.bot));await app.update_queue.put(command(2,telegram_bot=app.bot))
            await asyncio.wait_for(other.wait(),1)
            stopping=asyncio.create_task(app.stop())
            await asyncio.sleep(0);assert not stopping.done()
        finally:
            release.set()
            if stopping is None:await app.stop()
            else:await stopping
            await app.shutdown()
        assert seen==[1,2] and request.methods.count('sendMessage')==2 and errors==[]
    asyncio.run(run())
