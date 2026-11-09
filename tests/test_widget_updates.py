import asyncio
import json

import pytest
from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest

from widget_updates import WidgetQueryHandler, WidgetUpdatesRequest, parse_widget_query


def payload():
    return {'update_id': 9, 'widget_query': {'id': 'q', 'from': {'id': 41, 'is_bot': False,
        'first_name': 'A'}, 'chat': {'id': 41, 'type': 'private'}, 'message_id': 7,
        'chat_instance': '123', 'data': 'p1:refresh:1'}}


class OfflineRequest(BaseRequest):
    @property
    def read_timeout(self): return 1
    async def initialize(self): pass
    async def shutdown(self): pass
    async def do_request(self, url, method, **kwargs):
        return 200, json.dumps({'ok': True, 'result': {'id': 1, 'is_bot': True,
            'first_name': 'Pool', 'username': 'pool_bot'}}).encode()


def test_raw_beta_update_reaches_our_handler_in_real_ptb_lifecycle():
    received = []
    async def callback(update, context):
        received.append(parse_widget_query(update))
    async def run():
        app = Application.builder().token('1:synthetic').request(OfflineRequest()).build()
        app.add_handler(WidgetQueryHandler(callback))
        async with app:
            await app.process_update(Update.de_json(payload(), app.bot))
            await app.process_update(Update.de_json({'update_id': 10}, app.bot))
    asyncio.run(run())
    assert len(received) == 1
    q = received[0]
    assert (q.id, q.actor_id, q.context.chat_id, q.context.message_id, q.data) == ('q', 41, 41, 7, 'p1:refresh:1')


@pytest.mark.parametrize('change', [
    {'data': 'я' * 33}, {'from': {'id': True}}, {'message_id': True},
    {'chat': {'id': -1001, 'type': 'supergroup'}}, {'data': ''},
])
def test_invalid_or_nonprivate_query_cannot_become_private_context(change):
    data = payload()
    data['widget_query'].update(change)
    assert parse_widget_query(Update.de_json(data, None)) is None


def test_inline_context_is_recognized_for_future_fenced_route():
    data = payload()
    q = data['widget_query']
    q.pop('chat'); q.pop('message_id'); q['inline_message_id'] = 'inline-1'
    parsed = parse_widget_query(Update.de_json(data, None))
    assert parsed.context.inline_message_id == 'inline-1'
    assert parsed.context.chat_id is None


def test_beta_poll_transport_rewrites_only_getupdates(monkeypatch):
    urls = []
    async def request(self, url, method, **kwargs):
        urls.append(url)
        return 200, b'{}'
    from telegram.request import HTTPXRequest
    monkeypatch.setattr(HTTPXRequest, 'do_request', request)
    async def run():
        transport = WidgetUpdatesRequest(beta=True)
        try:
            await transport.do_request('https://api.telegram.org/bot1:synthetic/getUpdates', 'POST')
            await transport.do_request('https://api.telegram.org/bot1:synthetic/sendMessage', 'POST')
        finally:
            await transport.shutdown()
    asyncio.run(run())
    assert urls == ['https://api.telegram.org/beta/bot1:synthetic/getUpdates',
                    'https://api.telegram.org/bot1:synthetic/sendMessage']
