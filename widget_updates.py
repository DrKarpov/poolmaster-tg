"""Forward beta updates through the existing PTB poller, never a second one."""
from dataclasses import dataclass
from telegram import Update
from telegram.ext import BaseHandler
from telegram.request import HTTPXRequest

from widget_transport import WidgetMessageContext


class WidgetUpdatesRequest(HTTPXRequest):
    def __init__(self, *, beta: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._widget_beta = beta

    async def do_request(self, url: str, method: str, **kwargs):
        prefix = 'https://api.telegram.org/'
        if self._widget_beta and url.startswith(prefix + 'bot') and url.endswith('/getUpdates'):
            url = prefix + 'beta/' + url[len(prefix):]
        return await super().do_request(url, method, **kwargs)


@dataclass(frozen=True)
class ChartWidgetQuery:
    id: str
    actor_id: int
    context: WidgetMessageContext
    data: str


def raw_widget_query(update: object) -> dict | None:
    if not isinstance(update, Update):
        return None
    value = update.api_kwargs.get('widget_query')
    return value if isinstance(value, dict) else None


def parse_widget_query(update: Update) -> ChartWidgetQuery | None:
    q = raw_widget_query(update)
    if q is None:
        return None
    actor = q.get('from', {})
    actor = actor.get('id') if isinstance(actor, dict) else None
    data, query_id = q.get('data'), q.get('id')
    if (type(actor) is not int or actor <= 0 or not isinstance(query_id, str) or not query_id
            or not isinstance(data, str) or not data or '\0' in data):
        return None
    try:
        if len(data.encode('utf-8')) > 64:
            return None
    except UnicodeError:
        return None
    inline_id = q.get('inline_message_id')
    if isinstance(inline_id, str) and inline_id and 'chat' not in q and 'message_id' not in q:
        where = WidgetMessageContext(None, None, actor, inline_id)
    else:
        chat = q.get('chat', {})
        message_id = q.get('message_id')
        if (not isinstance(chat, dict) or chat.get('type') != 'private'
                or type(chat.get('id')) is not int or chat['id'] != actor
                or type(message_id) is not int or message_id <= 0):
            return None
        where = WidgetMessageContext(chat['id'], message_id, actor)
    return ChartWidgetQuery(query_id, actor, where, data)


class WidgetQueryHandler(BaseHandler):
    def check_update(self, update: object) -> bool:
        return raw_widget_query(update) is not None
