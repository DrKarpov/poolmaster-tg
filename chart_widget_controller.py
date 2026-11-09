"""Private widget lifecycle and actions; Telegram delivery stays in its seam."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import html
import json
import logging
import time

from chart_data import WIDGET_TIMEFRAMES, FRAME_LABELS
from chart_source import ChartSourceError
from chart_widget import ChartPreferences, WidgetStateParts, AXIS_MONTHS, compact, encode_window, encode_history_page, load_widget_html
from farming import SourceStatus
from keyboards import price_widget_keyboard
from localization import translate
from widget_transport import WidgetApiError, WidgetMessageContext


def build_widget_caption(price, *, language: str = 'ru', anomaly: bool = False) -> str:
    ru = language != 'en'
    title = '<b>JETTON/USDT · MEXC Spot</b>'
    if anomaly or price.status != SourceStatus.OK or price.price_usdt is None:
        return title + ('\nКотировка временно недоступна.' if ru else '\nQuote temporarily unavailable.')
    number = lambda v: html.escape(str(v))
    lines = [title, ('Цена: ' if ru else 'Price: ') + '<b>' + number(price.price_usdt) + ' USDT</b>']
    if price.price_change_24h_percent is not None:
        lines.append(('Изменение за 24 ч: ' if ru else 'Change, 24h: ') + number(price.price_change_24h_percent) + '%')
    if price.high_price_24h is not None and price.low_price_24h is not None:
        lines.append(('Макс./мин. за 24 ч: ' if ru else 'High/low, 24h: ') + number(price.high_price_24h) + ' / ' + number(price.low_price_24h) + ' USDT')
    if price.quote_volume_24h is not None:
        lines.append(('Объём за 24 ч: ' if ru else 'Volume, 24h: ') + number(price.quote_volume_24h) + ' USDT')
    stamp = price.source_timestamp
    if stamp:
        lines.append(('Данные котировки: ' if ru else 'Quote data: ') + stamp.strftime('%Y-%m-%d %H:%M:%S UTC'))
    if price.is_stale:
        lines.append('Сохранённые данные; обновление недоступно.' if ru else 'Saved data; update unavailable.')
    return '\n'.join(lines)


def state_status(parts, *, status=None, interactive=None, revision=None,language=None):
    a, b = json.loads(parts.public_data), json.loads(parts.chat_data)
    if status is not None: a['e'] = status
    if interactive is not None: a['i'] = int(interactive)
    if revision is not None: a['r'] = b['r'] = revision
    if language is not None or status is not None:
        a.pop('m',None)
        if a.get('e')!='ok':
            a['m']=translate(language or 'ru','widget.'+a['e'])
            if len(compact(a).encode())>900:a.pop('m')
    return WidgetStateParts(compact(a),compact(b),parts.visible_start)


def state_quote(parts, quote):
    # The header is the market quote, independent of the displayed candle page.
    public=json.loads(parts.public_data)
    public['q']='' if quote is None else str(quote)
    encoded=compact(public)
    if len(encoded.encode())>900:
        raise ValueError('quote exceeds the widget state budget')
    return replace(parts,public_data=encoded)


class ChartWidgetController:
    def __init__(self, *, transport, data, users, sessions, snapshots, identity_valid,
                 queries_enabled: bool, cooldown_seconds: int = 10, stale_max_age_seconds: int = 1800):
        self.transport, self.data, self.users, self.sessions = transport,data,users,sessions
        self.snapshots, self.identity_valid = snapshots,identity_valid
        self.queries_enabled = queries_enabled
        self.cooldown_seconds = cooldown_seconds
        self.stale_max_age_seconds = stale_max_age_seconds
        self._refresh_at = {}
        self._history_at = {}
        self._history_busy = set()

    @staticmethod
    def _quote_unavailable(snapshot):
        return ('price' in snapshot.anomalies or snapshot.price.status != SourceStatus.OK
                or snapshot.price.price_usdt is None or snapshot.price.is_stale)

    def _identity(self):
        if self.identity_valid() is not True:
            raise ChartSourceError('verified market identity is required')

    async def open(self, chat_id: int, language: str) -> WidgetMessageContext:
        self._identity()
        prefs = self.users.get_chart_preferences(chat_id)
        window, snapshot = await asyncio.gather(self.data.load_window(prefs.timeframe),self.snapshots.get_snapshot())
        self._identity()
        parts = state_status(encode_window(window,prefs,1,labels=(*FRAME_LABELS[language],*AXIS_MONTHS[language])),interactive=self.queries_enabled,language=language)
        parts = state_quote(parts,None if self._quote_unavailable(snapshot) else snapshot.price.price_usdt)
        if self._quote_unavailable(snapshot) and not window.is_stale:
            parts = state_status(parts,status='quote_unavailable',language=language)
        caption = build_widget_caption(snapshot.price,language=language,anomaly='price' in snapshot.anomalies)
        receipt = await self.transport.send_private(chat_id,load_widget_html(language=language,interactive=self.queries_enabled),
            parts.public_data,parts.chat_data,caption,
            price_widget_keyboard(language,prefs.timeframe,interactive=self.queries_enabled).to_dict(),
            allow_queries=self.queries_enabled)
        where = WidgetMessageContext(receipt.chat_id,receipt.message_id,chat_id)
        try:
            self.sessions.register(where,prefs.timeframe,parts,claimed=True)
            try:
                interactive=self.queries_enabled and receipt.can_send_queries is not False
                markup=price_widget_keyboard(language,prefs.timeframe,interactive=interactive).to_dict()
                pending_caption=None
                sent_parts=parts
                if not interactive:
                    parts=state_status(parts,interactive=False)
                try:
                    # Preserve Back in the same edit: separate caption/markup
                    # calls briefly remove it and make the chat jump.
                    await self.transport.edit_caption(where,caption,reply_markup=markup)
                except WidgetApiError:
                    pending_caption=caption
                    parts=state_status(parts,status='caption_pending',language=language)
                # Some beta sends omit metadata. Persist setup before editing
                # flags/status; the message already exists and must not duplicate.
                self.sessions.finish(where,prefs.timeframe,1,None,parts,pending_caption)
                if parts!=sent_parts and self.sessions.claim(where,1) is not None:
                    await self.transport.edit_state(where,parts.public_data,parts.chat_data)
                    if self.sessions.get(where) is not None:
                        await self.transport.edit_markup(where,price_widget_keyboard(language,prefs.timeframe,interactive=interactive).to_dict())
            finally:
                self.sessions.release(where,1)
        except Exception:
            # The message exists. A later setup error must never select a second
            # send (PNG fallback) as if sendWidget had been refused.
            raise WidgetApiError('post_delivery_setup',unknown=True) from None
        return where

    async def _deliver(self, transition, expected_revision):
        if transition is None:
            return
        where=transition.context
        language=self.users.get_user(where.owner_id).get('language') or 'ru'
        parts,caption=transition.parts,transition.pending_caption
        if self.sessions.get(where) is None:
            return
        public,private=json.loads(parts.public_data),json.loads(parts.chat_data)
        age=time.time()-public['a']
        if not (0<=age<=self.stale_max_age_seconds) or self.identity_valid() is not True:
            public.update(e='expired',c='',q='')
            private.update(c='',n=0,w=0,h=False)
            parts=WidgetStateParts(compact(public),compact(private),parts.visible_start)
            caption=None
        parts=state_status(parts,language=language)
        delivered=parts
        if caption:
            try:
                await self.transport.edit_caption(where,caption)
                caption=None
            except WidgetApiError:
                delivered=state_status(parts,status='caption_pending',language=language)
        # Also persist caption outcome before publishing state. If the process
        # dies after Telegram applies the edit, either client revision can
        # recover this exact transition through an idempotent re-delivery.
        transition=self.sessions.prepare(where,expected_revision,transition.timeframe,
            transition.revision,transition.before_open_time,parts,caption,
            save_preference=transition.save_preference)
        if transition is None:
            return
        await self.transport.edit_state(where,delivered.public_data,delivered.chat_data)
        if self.sessions.get(where) is None:
            return
        # Beta state edits clear the keyboard. Restore the same card's controls
        # before releasing its transition lock, including interactive Back.
        await self.transport.edit_markup(where,price_widget_keyboard(language,transition.timeframe,
            interactive=bool(json.loads(delivered.public_data).get('i',1))).to_dict())
        if self.sessions.get(where) is None:
            return
        self.sessions.finish(where,transition.timeframe,transition.revision,
            transition.before_open_time,delivered,caption)
        if transition.save_preference and json.loads(delivered.public_data).get('e')!='expired':
            self.users.set_chart_preferences(where.owner_id,ChartPreferences(transition.timeframe))

    async def _history(self, query, session, before, respond, answer):
        where=query.context
        public=json.loads(session.parts.public_data)
        key=(where.chat_id,where.message_id)
        if not 0 < before <= session.parts.visible_start:
            await respond('reopen'); return
        if not 0 <= time.time()-public['a'] <= self.stale_max_age_seconds:
            await respond('expired'); return
        now=time.monotonic()
        if key in self._history_busy or now-self._history_at.get(key,-10) < 1:
            await respond('cooldown'); return
        if len(self._history_at)>=4096:
            self._history_at={k:v for k,v in self._history_at.items() if now-v<60}
        self._history_at[key]=now
        self._history_busy.add(key)
        try:
            self._identity()
            window=await self.data.load_window(session.timeframe,before_open_time=before)
            self._identity()
            current=self.sessions.get(where)
            if current is None or current.revision!=session.revision or current.timeframe!=session.timeframe:
                await respond('reopen'); return
            if not 0 <= time.time()-public['a'] <= self.stale_max_age_seconds:
                await respond('expired'); return
            if window.is_stale:
                await respond('unavailable'); return
            value=encode_history_page(window,session.revision,before)
            if answer is not None: await answer(value)
            else: await self.transport.answer(query.id,value)
        except (ChartSourceError,ValueError):
            await respond('unavailable')
        except WidgetApiError:
            logging.warning('Widget history answer failed; message retained')
        finally:
            self._history_busy.discard(key)

    async def handle(self, query, *, answer=None):
        language='ru'
        async def respond(status, revision=None):
            value=compact({'status':status,'text':translate(language,'widget.'+status),
                **({'revision':revision} if revision is not None else {})})
            if answer is not None: await answer(value)
            else: await self.transport.answer(query.id,value)
        where=query.context
        if query.actor_id!=where.owner_id or where.inline_message_id:
            await respond('reopen'); return
        language=self.users.get_user(query.actor_id).get('language') or 'ru'
        tokens=query.data.split(':')
        if (len(tokens) not in (3,4) or tokens[0]!='p1'
                or not tokens[-1].isdigit() or len(tokens[-1])>10):
            await respond('reopen'); return
        action=tokens[1]
        if not ((action in ('refresh','earlier','latest') and len(tokens)==3)
                or (action=='tf' and len(tokens)==4 and tokens[2] in WIDGET_TIMEFRAMES)
                or (action=='history' and len(tokens)==4 and tokens[2].isdigit() and len(tokens[2])<=13)):
            await respond('reopen'); return
        revision=int(tokens[-1])
        session=self.sessions.get(where)
        pending=self.sessions.pending(where)
        if session is None or revision not in (session.revision,pending.revision if pending else session.revision):
            await respond('reopen'); return
        if action=='history':
            if pending is not None:
                await respond('cooldown'); return
            await self._history(query,session,int(tokens[2]),respond,answer)
            return
        if pending is None and action=='refresh':
            previous=self._refresh_at.get(query.actor_id,0)
            if time.monotonic()-previous < self.cooldown_seconds:
                await respond('cooldown'); return
        if pending is None and action=='earlier' and not json.loads(session.parts.chat_data)['h']:
            await respond('end'); return
        session=self.sessions.claim(where,session.revision)
        if session is None:
            await respond('cooldown'); return
        language=self.users.get_user(query.actor_id).get('language') or 'ru'
        frame=tokens[2] if action=='tf' else session.timeframe
        before=None if action in ('tf','latest') else session.parts.visible_start if action=='earlier' else session.before_open_time
        next_revision=session.revision+1
        try:
            # The widget's promise must resolve within 15 seconds, even while
            # MEXC anomaly confirmation takes longer. State events finish it.
            await respond('pending',next_revision)
            if pending is not None:
                await self._deliver(pending,session.revision)
                return
            self._identity()
            if action=='refresh':
                if len(self._refresh_at) >= 4096:
                    cutoff=time.monotonic()-max(60,self.cooldown_seconds)
                    self._refresh_at={k:v for k,v in self._refresh_at.items() if v>=cutoff}
                self._refresh_at[query.actor_id]=time.monotonic()
                window,snapshot=await asyncio.gather(
                    self.data.load_window(frame,before_open_time=before,force_refresh=True),
                    self.snapshots.refresh_price())
            else:
                window=await self.data.load_window(frame,before_open_time=before)
                snapshot=None
            self._identity()
            if self.sessions.get(where) is None: return
            if not window.candles:
                parts=state_status(session.parts,revision=next_revision)
                private=json.loads(parts.chat_data); private['h']=False
                parts=replace(parts,chat_data=compact(private))
                frame,before=session.timeframe,session.before_open_time
            else:
                navigation=next_revision if action in ('tf','latest') else json.loads(session.parts.public_data).get('u',1)
                parts=encode_window(window,ChartPreferences(frame),next_revision,navigation=navigation,latest=before is None,labels=(json.loads(session.parts.public_data).get('k') or '').split() or None)
                parts=state_status(parts,interactive=json.loads(session.parts.public_data).get('i',1))
            quote=json.loads(session.parts.public_data).get('q','')
            if snapshot is not None and not self._quote_unavailable(snapshot):
                quote=snapshot.price.price_usdt
            parts=state_quote(parts,quote)
            caption=session.pending_caption
            if snapshot is not None:
                caption=build_widget_caption(snapshot.price,language=language,anomaly='price' in snapshot.anomalies)
                if self._quote_unavailable(snapshot) and not window.is_stale:
                    parts=state_status(parts,status='quote_unavailable')
            transition=self.sessions.prepare(where,session.revision,frame,next_revision,before,parts,caption,
                save_preference=action=='tf')
            await self._deliver(transition,session.revision)
        except (ChartSourceError, ValueError):
            if self.sessions.get(where) is not None:
                parts=state_status(session.parts,status='unavailable',revision=next_revision)
                a,b=json.loads(parts.public_data),json.loads(parts.chat_data)
                age=time.time()-a['a']
                if not (0<=age<=self.stale_max_age_seconds) or self.identity_valid() is not True:
                    a.update(e='expired',c='',q='')
                    b.update(c='',n=0,w=0,h=False)
                    parts=WidgetStateParts(compact(a),compact(b),parts.visible_start)
                try:
                    transition=self.sessions.prepare(where,session.revision,session.timeframe,next_revision,
                        session.before_open_time,parts,session.pending_caption)
                    await self._deliver(transition,session.revision)
                except WidgetApiError:
                    logging.warning('Widget source/status update failed')
        except WidgetApiError:
            # No send/replacement here: an uncertain edit never creates a copy.
            logging.warning('Widget state update failed; message retained')
        finally:
            self.sessions.release(where,session.revision)
