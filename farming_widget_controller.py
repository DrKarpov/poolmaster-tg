"""Private farming widget lifecycle on the existing transport and durable sessions."""
import json
import logging
import sqlite3
import time
from dataclasses import replace

from chart_widget import compact
from farming_widget import encode_farming_view, load_farming_widget_html
from farming_widget_data import load_weekly_view
from keyboards import farming_history_keyboard
from localization import normalize_language
from widget_transport import WidgetApiError, WidgetMessageContext


def _flags(parts, **values):
    public=json.loads(parts.public_data)
    public.update(values)
    private=json.loads(parts.chat_data)
    if 'r' in values:
        private['r']=values['r']
    return replace(parts,public_data=compact(public),chat_data=compact(private))


class FarmingWidgetController:
    def __init__(self, *, transport, history, snapshots, sessions, queries_enabled, cooldown_seconds=10):
        self.transport,self.history,self.snapshots,self.sessions=transport,history,snapshots,sessions
        self.queries_enabled=queries_enabled
        self.cooldown_seconds=cooldown_seconds
        self._refresh_at={}

    async def open(self, chat_id, language):
        language=normalize_language(language)
        view=await load_weekly_view(self.history,self.snapshots)
        parts=_flags(encode_farming_view(view,1),i=int(self.queries_enabled),g=language)
        markup=farming_history_keyboard(language).to_dict()
        receipt=await self.transport.send_private(chat_id,
            load_farming_widget_html(language=language,interactive=self.queries_enabled),
            parts.public_data,parts.chat_data,'',markup,allow_queries=self.queries_enabled)
        where=WidgetMessageContext(receipt.chat_id,receipt.message_id,chat_id)
        try:
            self.sessions.register(where,'7D',parts,claimed=True)
            try:
                if receipt.can_send_queries is False:
                    parts=_flags(parts,i=0)
                    await self.transport.edit_state(where,parts.public_data,parts.chat_data)
                await self.transport.edit_markup(where,markup)
                self.sessions.finish(where,'7D',1,None,parts)
            finally:
                self.sessions.release(where)
        except Exception:
            # Already sent: neither caller nor retry may create a duplicate PNG.
            raise WidgetApiError('post_delivery_setup',unknown=True) from None
        return where

    async def close(self, context):
        self.sessions.close(context)

    async def _deliver(self, transition):
        if transition is None or self.sessions.get(transition.context) is None:
            return
        where,parts=transition.context,transition.parts
        await self.transport.edit_state(where,parts.public_data,parts.chat_data)
        if self.sessions.get(where) is None:
            return
        language=json.loads(parts.public_data).get('g','ru')
        # The beta clears the native keyboard on state edits.
        await self.transport.edit_markup(where,farming_history_keyboard(language).to_dict())
        if self.sessions.get(where) is not None:
            self.sessions.finish(where,'7D',transition.revision,None,parts)

    async def handle(self, query):
        async def answer(status, revision=None):
            await self.transport.answer(query.id,compact(dict(status=status,**({'revision':revision} if revision else {}))))
        where=query.context
        tokens=query.data.split(':')
        if (not self.queries_enabled or query.actor_id!=where.owner_id or where.inline_message_id
                or len(tokens)!=3 or tokens[:2]!=['fw1','refresh']
                or not tokens[2].isdigit() or len(tokens[2])>10):
            await answer('reopen');return
        session=self.sessions.get(where)
        pending=self.sessions.pending(where)
        revision=int(tokens[2])
        if (session is None or not json.loads(session.parts.public_data).get('i',1)
                or revision not in (session.revision,pending.revision if pending else session.revision)):
            await answer('reopen');return
        now=time.monotonic()
        if pending is None and now-self._refresh_at.get(query.actor_id,0)<self.cooldown_seconds:
            await answer('cooldown');return
        if self.sessions.claim(where,session.revision) is None:
            await answer('cooldown');return
        try:
            await answer('pending',pending.revision if pending else session.revision+1)
            if pending is not None:
                await self._deliver(pending);return
            if len(self._refresh_at)>=4096:
                self._refresh_at={k:v for k,v in self._refresh_at.items() if now-v<max(60,self.cooldown_seconds)}
            self._refresh_at[query.actor_id]=now
            try:
                view=await load_weekly_view(self.history,self.snapshots,force=True)
                if view.status!='ok':
                    parts=_flags(session.parts,e='unavailable',r=session.revision+1)
                else:
                    old=json.loads(session.parts.public_data)
                    parts=_flags(encode_farming_view(view,session.revision+1),i=old.get('i',1),g=old.get('g','ru'))
            except (ValueError,OSError,sqlite3.Error):
                logging.warning('Farming widget source unavailable; last state retained')
                parts=_flags(session.parts,e='unavailable',r=session.revision+1)
            if self.sessions.get(where) is None:
                return
            transition=self.sessions.prepare(where,session.revision,'7D',session.revision+1,None,parts)
            await self._deliver(transition)
        except WidgetApiError:
            logging.warning('Farming widget edit incomplete; durable transition retained')
        finally:
            self.sessions.release(where)
