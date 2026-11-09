"""Fair, bounded handler execution; waiting actions do not consume work permits."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable
import logging
from typing import Any

from telegram.ext import BaseUpdateProcessor

from widget_updates import parse_widget_query


class KeyedUpdateProcessor(BaseUpdateProcessor):
    """Admit quickly, then drain FIFO lanes using application-owned tasks.

    PTB's admission semaphore never waits for a user's network operation. The
    separate work semaphore bounds actual handlers, after lane ordering. Thus a
    single user's queued actions cannot occupy every permit while waiting.
    PoolmasterApplication.stop waits for owned drain tasks before closing HTTP clients.
    """

    def __init__(self, max_concurrent_updates: int):
        super().__init__(max_concurrent_updates)
        self._work=asyncio.Semaphore(max_concurrent_updates)
        self._lanes: dict[tuple, deque] = {}
        self._workers: set[asyncio.Task] = set()
        self._application=None
        self._closing=False
        self._active=0

    def bind(self, application) -> None:
        if self._application is not None and self._application is not application:
            raise RuntimeError('update processor is already bound')
        self._application=application

    @property
    def current_concurrent_updates(self) -> int:
        return self._active

    @staticmethod
    def _key(update) -> tuple:
        widget=parse_widget_query(update)
        if widget is not None:
            return ('widget',widget.context.chat_id,widget.context.message_id,widget.context.inline_message_id)
        query=getattr(update,'callback_query',None)
        inline=getattr(query,'inline_message_id',None)
        if inline:
            return ('inline',inline)
        message=getattr(query,'message',None)
        if str(getattr(query,'data','')).startswith('wchart:') and message is not None:
            return ('widget',message.chat.id,message.message_id,None)
        actor=getattr(update,'effective_user',None)
        if actor is not None:
            return ('user',actor.id)
        chat=getattr(update,'effective_chat',None)
        return ('chat',chat.id) if chat is not None else ('other',)

    async def initialize(self) -> None:
        self._closing=False

    async def shutdown(self) -> None:
        self._closing=True
        await self.drain()

    async def drain(self) -> None:
        while self._workers:
            await asyncio.gather(*tuple(self._workers),return_exceptions=True)

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        if self._closing or self._application is None:
            self._close(coroutine)
            raise RuntimeError('update processor is not accepting work')
        key=self._key(update)
        lane=self._lanes.get(key)
        if lane is not None:
            lane.append((update,coroutine))
            return
        lane=self._lanes[key]=deque([(update,coroutine)])
        drain=self._drain(key,lane)
        try:
            task=self._application.create_work_task(drain,update=update,name='poolmaster:user-queue')
        except BaseException:
            drain.close()
            self._lanes.pop(key,None)
            self._close(coroutine)
            raise
        self._workers.add(task)
        def finished(done):
            self._workers.discard(done)
            # Cancellation before _drain's first step never enters its finally.
            if self._lanes.get(key) is lane:
                self._close_lane(key,lane)
        task.add_done_callback(finished)

    @staticmethod
    def _close(coroutine) -> None:
        close=getattr(coroutine,'close',None)
        if close is not None:
            close()

    def _close_lane(self, key, lane):
        self._lanes.pop(key,None)
        while lane:
            self._close(lane.popleft()[1])

    async def _drain(self, key, lane) -> None:
        try:
            while lane:
                update,coroutine=lane.popleft()
                started=False
                try:
                    async with self._work:
                        self._active+=1
                        try:
                            started=True
                            await coroutine
                        finally:
                            self._active-=1
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    try:
                        await self._application.process_error(update=update,error=error)
                    except Exception:
                        logging.exception('Queued update error handler failed')
                finally:
                    if not started:
                        self._close(coroutine)
        finally:
            self._close_lane(key,lane)


def callback_for_queue(query):
    """Preserve accepted actions after Telegram's short ACK window expires."""
    return _QueuedCallback(query) if query is not None else None


class _QueuedCallback:
    def __init__(self, query):
        self._query=query

    def __getattr__(self, name):
        return getattr(self._query,name)

    async def answer(self, *args, **kwargs):
        from telegram.error import BadRequest
        try:
            return await self._query.answer(*args,**kwargs)
        except BadRequest as error:
            detail=str(error).lower()
            if 'query is too old' not in detail and 'query id is invalid' not in detail:
                raise
            # ACK expiry is not an authorization check. The unchanged handler
            # still verifies ownership and validates the requested action.
            logging.info('Queued callback acknowledgement expired; accepted action continues')
            return False
