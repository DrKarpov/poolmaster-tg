import asyncio
from types import SimpleNamespace

from telegram import Update

from update_processing import KeyedUpdateProcessor


class Host:
    def __init__(self):
        self.errors=[]

    def create_work_task(self, coroutine, **kwargs):
        return asyncio.create_task(coroutine, name=kwargs.get('name'))

    async def process_error(self, *, update, error):
        self.errors.append(error)


def user(uid):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),effective_chat=None,callback_query=None)


def processor(limit=4):
    p=KeyedUpdateProcessor(limit);host=Host();p.bind(host)
    return p,host


def test_blocked_user_and_large_backlog_do_not_block_another_user():
    async def run():
        p,_=processor(2);started=asyncio.Event();release=asyncio.Event();other=asyncio.Event();order=[]
        async def slow(i):
            if i==0:started.set();await release.wait()
            order.append(i)
        async def fast():other.set()
        try:
            for i in range(50):await p.process_update(user(1),slow(i))
            await started.wait()
            await p.process_update(user(2),fast())
            await asyncio.wait_for(other.wait(),1)
            assert order==[]
        finally:
            release.set();await p.shutdown()
        assert order==list(range(50))
    asyncio.run(run())


def test_three_hundred_users_preserve_order_and_bound_active_work():
    async def run():
        p,_=processor(16);active=peak=0;done={i:[] for i in range(300)}
        async def work(uid,sequence):
            nonlocal active,peak
            active+=1;peak=max(active,peak)
            await asyncio.sleep(.001)
            done[uid].append(sequence);active-=1
        for sequence in range(3):
            for uid in range(300):await p.process_update(user(uid),work(uid,sequence))
        await p.shutdown()
        assert 1<peak<=16 and active==0
        assert all(v==[0,1,2] for v in done.values())
        assert p.current_concurrent_updates==0
    asyncio.run(run())


def test_widget_work_is_ordered_per_message_and_back_remains_available():
    async def run():
        p,_=processor(3);started=asyncio.Event();release=asyncio.Event();back=asyncio.Event();seen=[]
        native=Update(1,api_kwargs={'widget_query':{'id':'q','from':{'id':7},'chat':{'id':7,'type':'private'},'message_id':41,'data':'p1:refresh:1'}})
        legacy=user(7);legacy.callback_query=SimpleNamespace(data='wchart:refresh',message=SimpleNamespace(chat=SimpleNamespace(id=7),message_id=41))
        async def load():started.set();await release.wait();seen.append('load')
        async def next_action():seen.append('next')
        async def go_back():back.set()
        try:
            await p.process_update(native,load());await started.wait()
            await p.process_update(legacy,next_action())
            await p.process_update(user(7),go_back())
            await asyncio.wait_for(back.wait(),1)
            assert seen==[]
        finally:
            release.set();await p.shutdown()
        assert seen==['load','next']
    asyncio.run(run())


def test_failed_handler_releases_work_permit_and_keeps_its_queue_alive():
    async def run():
        p,host=processor(1);seen=[]
        async def failed():raise RuntimeError('expected boundary failure')
        async def next_action():seen.append('ok')
        await p.process_update(user(1),failed())
        await p.process_update(user(1),next_action())
        await p.shutdown()
        assert seen==['ok'] and len(host.errors)==1
    asyncio.run(run())


def test_cancelling_a_queued_lane_closes_unstarted_handlers():
    import inspect
    async def run():
        p,_=processor(1)
        async def work():await asyncio.sleep(0)
        first,second=work(),work()
        await p.process_update(user(1),first)
        await p.process_update(user(1),second)
        for task in tuple(p._workers):task.cancel()
        await p.shutdown()
        assert inspect.getcoroutinestate(first)=='CORO_CLOSED'
        assert inspect.getcoroutinestate(second)=='CORO_CLOSED'
    asyncio.run(run())


def test_shared_inline_message_is_serialized_across_different_viewers():
    async def run():
        p,_=processor(2);seen=[];release=asyncio.Event()
        one,two=user(1),user(2)
        one.callback_query=SimpleNamespace(inline_message_id='same-public-message')
        two.callback_query=SimpleNamespace(inline_message_id='same-public-message')
        async def first():await release.wait();seen.append(1)
        async def second():seen.append(2)
        await p.process_update(one,first());await p.process_update(two,second())
        for _ in range(3):await asyncio.sleep(0)
        before=list(seen);release.set();await p.shutdown()
        assert before==[] and seen==[1,2]
    asyncio.run(run())


def test_expired_callback_ack_does_not_discard_an_accepted_queued_action():
    from telegram.error import BadRequest
    from update_processing import callback_for_queue
    async def run():
        class Query:
            message=object()
            async def answer(self,*args,**kwargs):
                raise BadRequest('Query is too old and response timeout expired or query ID is invalid')
        original=Query();query=callback_for_queue(original)
        assert query.message is original.message
        assert await query.answer() is False
    asyncio.run(run())


def test_unrelated_callback_errors_are_not_hidden():
    import pytest
    from telegram.error import BadRequest
    from update_processing import callback_for_queue
    async def run():
        class Query:
            async def answer(self):raise BadRequest('Unexpected Telegram validation failure')
        with pytest.raises(BadRequest,match='Unexpected'):
            await callback_for_queue(Query()).answer()
    asyncio.run(run())
