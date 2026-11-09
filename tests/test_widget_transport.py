import asyncio

import httpx
import pytest

from widget_transport import WidgetTransport, WidgetApiError, WidgetMessageContext


def run_transport(handler, action):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await action(WidgetTransport('123:synthetic-secret', client=client))
    return asyncio.run(run())


def test_private_send_includes_caption_and_keyboard_and_checks_receipt():
    def handler(request):
        import json
        assert request.url.path == '/beta/bot123:synthetic-secret/sendWidget'
        body = json.loads(request.content)
        assert body['chat_id'] == 41
        assert body['allow_queries'] is True and body['is_shared'] is False
        assert body['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'chart:back'
        assert body['caption'] == 'caption'
        return httpx.Response(200, json={'ok': True, 'result': {
            'chat': {'id': 41, 'type': 'private'}, 'message_id': 7,
            'widget': {'can_send_queries': True, 'can_send_background_queries': False},
        }})
    result = run_transport(handler, lambda t: t.send_private(41, '<p>x</p>', '{}', '{}', 'caption',
        {'inline_keyboard': [[{'text': 'Назад', 'callback_data': 'chart:back'}]]}))
    assert result.message_id == 7 and result.can_send_queries
    assert not result.can_send_background_queries


def test_missing_beta_capability_fields_mean_unknown_not_denied():
    def handler(request):
        return httpx.Response(200,json={'ok':True,'result':{
            'chat':{'id':41,'type':'private'},'message_id':7,
            'widget':{'html':'<p>x</p>','public_data':'{}','chat_data':'{}'}}})
    receipt=run_transport(handler,lambda t:t.send_private(41,'<p>x</p>','{}','{}','',{}))
    assert receipt.can_send_queries is None
    assert receipt.can_send_background_queries is None


@pytest.mark.parametrize('result', [
    {'chat': {'id': 99, 'type': 'private'}, 'message_id': 7, 'widget': {}},
    {'chat': {'id': 41, 'type': 'supergroup'}, 'message_id': 7, 'widget': {}},
    {'chat': {'id': 41, 'type': 'private'}, 'widget': {}},
])
def test_bad_receipt_is_unknown_delivery_and_not_retried(result):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'ok': True, 'result': result})
    with pytest.raises(WidgetApiError) as e:
        run_transport(handler, lambda t: t.send_private(41, 'x', '', '', '', {}))
    assert e.value.unknown and len(calls) == 1


def test_timeout_does_not_retry_or_expose_token():
    def handler(request):
        raise httpx.ReadTimeout(str(request.url))
    with pytest.raises(WidgetApiError) as e:
        run_transport(handler, lambda t: t.answer('q', 'ok'))
    assert e.value.unknown and 'synthetic-secret' not in str(e.value)
    assert e.value.__cause__ is None


@pytest.mark.parametrize('status,description,kind', [
    (400, 'Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED', 'not_allowed'),
    (429, 'Too Many Requests', 'rate_limited'),
    (403, 'Forbidden', 'forbidden'),
])
def test_explicit_errors_are_classified(status, description, kind):
    def handler(request):
        return httpx.Response(status, json={'ok': False, 'error_code': status,
            'description': description, 'parameters': {'retry_after': 12}})
    with pytest.raises(WidgetApiError) as e:
        run_transport(handler, lambda t: t.answer('q', None))
    assert e.value.kind == kind and not e.value.unknown
    assert e.value.retry_after == 12


def test_state_and_answer_names_and_noop_are_idempotent():
    import json
    calls = []
    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith('editMessageWidgetData'):
            return httpx.Response(400, json={'ok': False, 'error_code': 400,
                'description': 'Bad Request: message is not modified'})
        return httpx.Response(200, json={'ok': True, 'result': True})
    async def action(t):
        changed = await t.edit_state(WidgetMessageContext(41, 7, 41), '{"r":2}', '{"r":2}')
        await t.answer('q', '{"r":2}')
        return changed
    assert run_transport(handler, action) is False
    assert calls[-1][1] == {'widget_query_id': 'q', 'data': '{"r":2}'}
    assert calls[0][1] == {'chat_id': 41, 'message_id': 7, 'public_data': '{"r":2}', 'chat_data': '{"r":2}'}


def test_caption_uses_standard_endpoint_and_inline_edits_have_only_inline_id():
    import json
    calls = []
    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'ok': True, 'result': True})
    async def action(t):
        where = WidgetMessageContext(None, None, 41, 'inline-1')
        await t.edit_state(where, '{}', '{}')
        await t.edit_caption(where, 'fresh')
    run_transport(handler, action)
    assert calls[0][1] == {'inline_message_id': 'inline-1', 'public_data': '{}', 'chat_data': '{}'}
    assert calls[1][0] == '/bot123:synthetic-secret/editMessageCaption'


def test_wire_size_is_measured_in_utf8_bytes():
    def prohibited(request):
        raise AssertionError('oversized answer reached Telegram')
    with pytest.raises(ValueError):
        run_transport(prohibited, lambda t: t.answer('q', 'я' * 513))


def test_caption_and_back_are_sent_in_the_same_standard_api_edit():
    import json
    markup={'inline_keyboard':[[{'text':'Назад','callback_data':'chart:back','style':'danger'}]]}
    def handler(request):
        assert request.url.path=='/bot123:synthetic-secret/editMessageCaption'
        body=json.loads(request.content)
        assert body['caption']=='fresh' and body['reply_markup']==markup
        assert body['chat_id']==41 and body['message_id']==7
        return httpx.Response(200,json={'ok':True,'result':{'message_id':7,'caption':'fresh','reply_markup':markup}})
    assert run_transport(handler,lambda t:t.edit_caption(WidgetMessageContext(41,7,41),'fresh',reply_markup=markup)) is True
