import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from telegram import Message
from telegram.error import TelegramError, TimedOut

from test_user_ui import _make_bot, _UIQuery, _snapshot, _async_return
from widget_transport import WidgetApiError, WidgetTransport


class MenuMessage:
    def __init__(self, events, *, widget=True, fail_reply=False):
        self.chat=SimpleNamespace(id=7,type='private')
        self.message_id=42
        self.api_kwargs={'widget':{'public_data':'{}'}} if widget else {}
        self.events=events
        self.fail_reply=fail_reply

    async def reply_text(self,text,**kwargs):
        if self.fail_reply:
            raise TelegramError('simulated destination failure')
        self.events.append(('text',text,kwargs))
        return SimpleNamespace(message_id=62)

    async def delete(self):
        self.events.append(('delete',self.message_id))


def setup(tmp_path,monkeypatch,*,language='ru',failure=None):
    bot=_make_bot(tmp_path,monkeypatch,language=language)
    bot.settings=replace(bot.settings,coin_widgets_enabled=True)
    bot.snapshot_manager=SimpleNamespace(get_snapshot=_async_return(_snapshot()))
    events=[]
    async def call(self,method,payload,**kwargs):
        assert method=='sendWidget'
        events.append(('widget',payload))
        if failure:raise failure
        return {'chat':{'id':7,'type':'private'},'message_id':51,'widget':{'can_send_queries':False}}
    monkeypatch.setattr(WidgetTransport,'_call',call)
    return bot,events


def query(events,data,*,widget=True):
    result=_UIQuery(data)
    result.message=MenuMessage(events,widget=widget)
    return result


async def no_snapshot(**kwargs):
    raise AssertionError('the local home coin must not require market data')


async def no_op(*args,**kwargs):
    return None


def test_start_with_selected_language_opens_coin_without_market_dependency(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch)
    bot.snapshot_manager=SimpleNamespace(get_snapshot=no_snapshot)
    message=MenuMessage(events,widget=False)
    update=SimpleNamespace(effective_chat=message.chat,effective_message=message,
        effective_user=SimpleNamespace(id=7,username='test',first_name='Tester',last_name=None))
    asyncio.run(bot.start(update,SimpleNamespace(bot=SimpleNamespace(set_my_commands=no_op))))
    assert [e[0] for e in events]==['widget']
    assert events[0][1]['caption']==''


def test_start_still_requires_language_before_coin(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch,language=None)
    bot.snapshot_manager=SimpleNamespace(get_snapshot=no_snapshot)
    message=MenuMessage(events,widget=False)
    update=SimpleNamespace(effective_chat=message.chat,effective_message=message,
        effective_user=SimpleNamespace(id=7,username='test',first_name='Tester',last_name=None))
    asyncio.run(bot.start(update,None))
    assert [e[0] for e in events]==['text']
    assert events[0][1]=='Выберите язык / Choose a language:'


@pytest.mark.parametrize('action',['ui:home','ui:language:set:en'])
def test_home_and_language_selection_send_coin_before_removing_old_screen(tmp_path,monkeypatch,action):
    bot,events=setup(tmp_path,monkeypatch)
    bot.snapshot_manager=SimpleNamespace(get_snapshot=no_snapshot)
    q=query(events,action,widget=False)
    api=SimpleNamespace(set_my_commands=no_op)
    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=q),SimpleNamespace(user_data={},bot=api)))
    assert [e[0] for e in events]==['widget','delete']
    assert q.edits==[] and len(q.answers)==1
    if action.endswith(':en'):
        assert events[0][1]['reply_markup']['inline_keyboard'][0][0]['text']=='Price'


@pytest.mark.parametrize('action,visible',[
    ('ui:farming','Доходность'),('ui:alerts','Уведомления'),('ui:language','язык'),
])
def test_widget_menu_can_open_text_sections(tmp_path,monkeypatch,action,visible):
    bot,events=setup(tmp_path,monkeypatch)
    q=query(events,action)
    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=q),SimpleNamespace(user_data={})))
    assert [e[0] for e in events]==['text','delete']
    assert visible.lower() in events[0][1].lower()
    assert q.edits==[] and len(q.answers)==1


def test_known_widget_rejection_keeps_legacy_home_available(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch,failure=WidgetApiError('not_allowed'))
    q=query(events,'ui:home',widget=False)
    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=q),SimpleNamespace(user_data={})))
    assert [e[0] for e in events]==['widget']
    assert len(q.edits)==1 and 'Выберите раздел' in q.edits[0][0]


def test_uncertain_home_send_does_not_duplicate_or_remove_current_screen(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch,failure=WidgetApiError('network',unknown=True))
    bot.snapshot_manager=SimpleNamespace(get_snapshot=no_snapshot)
    q=query(events,'ui:home',widget=False)
    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=q),SimpleNamespace(user_data={})))
    assert [e[0] for e in events]==['widget']
    assert q.edits==[]


def test_price_back_restores_coin_before_closing_and_deleting_chart(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch)
    bot.snapshot_manager=SimpleNamespace(get_snapshot=no_snapshot)
    bot._price_widget_controller=SimpleNamespace(sessions=SimpleNamespace(close=lambda _:events.append(('close',))))
    q=query(events,'chart:back')
    async def delete_message(**kwargs):events.append(('delete',kwargs['message_id']))
    api=SimpleNamespace(delete_message=delete_message)
    asyncio.run(bot.chart_callback(SimpleNamespace(callback_query=q),SimpleNamespace(bot=api)))
    assert [e[0] for e in events]==['widget','close','delete']


def test_uncertain_home_send_from_price_back_keeps_chart_recoverable(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch,failure=WidgetApiError('network',unknown=True))
    bot.snapshot_manager=SimpleNamespace(get_snapshot=no_snapshot)
    bot._price_widget_controller=SimpleNamespace(sessions=SimpleNamespace(close=lambda _:events.append(('close',))))
    q=query(events,'chart:back')
    async def delete_message(**kwargs):events.append(('delete',))
    asyncio.run(bot.chart_callback(SimpleNamespace(callback_query=q),SimpleNamespace(bot=SimpleNamespace(delete_message=delete_message))))
    assert [e[0] for e in events]==['widget']


def test_failed_widget_to_text_send_preserves_the_current_menu(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch)
    q=query(events,'ui:farming');q.message.fail_reply=True
    with pytest.raises(TelegramError):
        asyncio.run(bot.edit_private_menu_message(q,'target screen',reply_markup=None))
    assert events==[] and q.edits==[]


@pytest.mark.parametrize('action',['ui:farming','ui:alerts','ui:language'])
def test_ambiguous_text_destination_does_not_trigger_another_send(tmp_path,monkeypatch,action):
    bot,events=setup(tmp_path,monkeypatch)
    q=query(events,action)
    attempts=[]
    async def reply_text(text,**kwargs):
        attempts.append(text)
        if len(attempts)==1:
            raise TimedOut('simulated ambiguous delivery')
        events.append(('text',text))
    q.message.reply_text=reply_text
    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=q),SimpleNamespace(user_data={})))
    assert len(attempts)==1,'uncertain destination must not trigger a second control screen'
    assert events==[] and q.edits==[]


def test_real_ptb_unknown_widget_field_is_handled_as_media(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch)
    async def send_message(**kwargs):
        events.append(('text',kwargs['text']));return SimpleNamespace(message_id=61)
    async def delete_message(**kwargs):events.append(('delete',kwargs['message_id']))
    api=SimpleNamespace(send_message=send_message,delete_message=delete_message)
    message=Message.de_json({'message_id':42,'date':1794130000,'chat':{'id':7,'type':'private'},'widget':{'public_data':'{}'}},api)
    q=_UIQuery('ui:farming');q.message=message
    asyncio.run(bot.edit_private_menu_message(q,'Farming',reply_markup=None))
    assert events==[('text','Farming'),('delete',42)] and q.edits==[]


def test_foreign_and_group_callbacks_cannot_open_coin_menu(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch)
    async def run():
        for q in [_UIQuery('ui:home',chat_id=8),_UIQuery('ui:home',chat_id=-100,chat_type='supergroup')]:
            await bot.user_ui_callback(SimpleNamespace(callback_query=q),SimpleNamespace(user_data={}))
            assert q.answers[0][1].get('show_alert') is True
    asyncio.run(run())
    assert events==[]


def test_coin_menu_price_button_keeps_existing_price_widget_route(tmp_path,monkeypatch):
    from test_chart_widget_ui import controller
    bot,events=setup(tmp_path,monkeypatch)
    bot.settings=replace(bot.settings,price_widgets_enabled=True)
    c,transport=controller(tmp_path)
    bot._price_widget_controller=c
    original=transport.send_private
    async def send(*args,**kwargs):
        receipt=await original(*args,**kwargs);events.append(('price',));return receipt
    transport.send_private=send
    q=query(events,'ui:price')
    asyncio.run(bot.user_ui_callback(SimpleNamespace(callback_query=q),SimpleNamespace(user_data={})))
    assert [e[0] for e in events]==['price','delete']
    assert transport.calls[0][-1]['inline_keyboard'][0][0]['callback_data']=='chart:back'


def test_group_start_with_coin_enabled_does_not_send_private_game(tmp_path,monkeypatch):
    bot,events=setup(tmp_path,monkeypatch)
    update=SimpleNamespace(effective_chat=SimpleNamespace(id=-100,type='supergroup'),
        effective_message=MenuMessage(events),effective_user=SimpleNamespace(id=7))
    asyncio.run(bot.start(update,None))
    assert events==[]
