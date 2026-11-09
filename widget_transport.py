"""Small beta Bot API seam. Sending is never retried after uncertain delivery."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import httpx


def wire_text(value: str, limit: int) -> str:
    if not isinstance(value, str) or '\0' in value or len(value.encode('utf-8')) > limit:
        raise ValueError('widget field exceeds its UTF-8 text limit')
    return value


@dataclass(frozen=True)
class WidgetMessageContext:
    chat_id: int | None
    message_id: int | None
    owner_id: int
    inline_message_id: str | None = None

    def coordinates(self) -> dict:
        if self.inline_message_id:
            if self.chat_id is not None or self.message_id is not None:
                raise ValueError('ambiguous widget context')
            return {'inline_message_id': self.inline_message_id}
        if (type(self.chat_id) is not int or self.chat_id <= 0
                or type(self.message_id) is not int or self.message_id <= 0
                or self.chat_id != self.owner_id):
            raise ValueError('private widget context is invalid')
        return {'chat_id': self.chat_id, 'message_id': self.message_id}


@dataclass(frozen=True)
class WidgetReceipt:
    chat_id: int
    message_id: int
    can_send_queries: bool | None
    can_send_background_queries: bool | None


class WidgetApiError(RuntimeError):
    def __init__(self, kind: str, *, unknown: bool = False, retry_after: int | None = None):
        super().__init__('Telegram widget request: ' + kind)
        self.kind = kind
        self.unknown = unknown
        self.retry_after = retry_after


class WidgetTransport:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None):
        self._token = token
        self._client = client

    async def _call(self, method: str, parameters: dict, *, beta: bool = True):
        prefix = 'beta/' if beta else ''
        url = f'https://api.telegram.org/{prefix}bot{self._token}/{method}'
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3)) as client:
                    response = await client.post(url, json=parameters)
            else:
                response = await self._client.post(url, json=parameters, timeout=10)
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            # HTTPX exceptions can carry the complete secret-bearing URL.
            raise WidgetApiError('network_or_invalid_receipt', unknown=True) from None
        if not isinstance(payload, dict) or type(payload.get('ok')) is not bool:
            raise WidgetApiError('invalid_receipt', unknown=True)
        if payload['ok'] and response.is_success and 'result' in payload:
            return payload['result']
        code = payload.get('error_code', response.status_code)
        description = str(payload.get('description', '')).lower()
        if code == 400 and 'message is not modified' in description:
            return False
        kind = ('rate_limited' if code == 429 else 'forbidden' if code == 403
                else 'not_allowed' if 'widget_not_allowed' in description else 'rejected')
        retry = payload.get('parameters', {})
        retry = retry.get('retry_after') if isinstance(retry, dict) else None
        raise WidgetApiError(kind, unknown=not isinstance(code, int) or code >= 500,
                             retry_after=retry if type(retry) is int else None)

    async def send_private(self, chat_id: int, html: str, public_data: str, chat_data: str,
                           caption: str, reply_markup: dict, *, allow_queries: bool = True,
                           width: int = 0, height: int = 0) -> WidgetReceipt:
        import zlib
        if type(chat_id) is not int or chat_id <= 0:
            raise ValueError('widget destination must be a private chat')
        wire_text(html, 32768)
        if len(zlib.compress(html.encode(), 9)) > 4096:
            raise ValueError('compressed widget exceeds 4096 bytes')
        result = await self._call('sendWidget', {
            'chat_id': chat_id, 'html': html, 'public_data': wire_text(public_data, 1024),
            'chat_data': wire_text(chat_data, 1024), 'caption': caption, 'parse_mode': 'HTML',
            'width': width, 'height': height, 'is_shared': False, 'allow_queries': allow_queries,
            'reply_markup': reply_markup,
        })
        if (not isinstance(result, dict) or type(result.get('message_id')) is not int
                or result['message_id'] <= 0 or not isinstance(result.get('chat'), dict)
                or result['chat'].get('id') != chat_id or result['chat'].get('type') != 'private'
                or not isinstance(result.get('widget'), dict)):
            raise WidgetApiError('invalid_receipt', unknown=True)
        widget = result['widget']
        logging.info('Widget receipt requested_queries=%s query_flag_present=%s can_send_queries=%s widget_fields=%s',
            allow_queries, 'can_send_queries' in widget, widget.get('can_send_queries'), sorted(widget))
        capability = lambda key: widget.get(key) if type(widget.get(key)) is bool else None
        return WidgetReceipt(chat_id, result['message_id'], capability('can_send_queries'),
                             capability('can_send_background_queries'))

    async def edit_state(self, context: WidgetMessageContext, public_data: str, chat_data: str) -> bool:
        result = await self._call('editMessageWidgetData', {**context.coordinates(),
            'public_data': wire_text(public_data, 1024), 'chat_data': wire_text(chat_data, 1024)})
        return result is not False

    async def edit_caption(self, context: WidgetMessageContext, caption: str,
                           *, reply_markup: dict | None = None) -> bool:
        parameters = {**context.coordinates(), 'caption': caption, 'parse_mode': 'HTML'}
        if reply_markup is not None:
            parameters['reply_markup'] = reply_markup
        result = await self._call('editMessageCaption', parameters, beta=False)
        return result is not False

    async def answer(self, query_id: str, data: str | None) -> None:
        payload = {'widget_query_id': query_id}
        if data is not None:
            payload['data'] = wire_text(data, 1024)
        await self._call('answerWidgetQuery', payload)

    async def edit_markup(self, context: WidgetMessageContext, markup: dict) -> None:
        await self._call('editMessageReplyMarkup', {**context.coordinates(), 'reply_markup': markup}, beta=False)
