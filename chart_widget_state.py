"""Bounded durable message sessions, kept separately from alert state."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time

from chart_widget import WidgetStateParts
from widget_transport import WidgetMessageContext


@dataclass(frozen=True)
class ChartWidgetSession:
    context: WidgetMessageContext
    timeframe: str
    revision: int
    before_open_time: int | None
    parts: WidgetStateParts
    pending_caption: str | None


@dataclass(frozen=True)
class ChartWidgetTransition(ChartWidgetSession):
    save_preference: bool


class ChartWidgetSessionStore:
    def __init__(self, path: str | Path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS widget_sessions (
                chat_id INTEGER, message_id INTEGER, owner_id INTEGER NOT NULL,
                timeframe TEXT NOT NULL, revision INTEGER NOT NULL, before_time INTEGER,
                public_data TEXT NOT NULL, chat_data TEXT NOT NULL, visible_start INTEGER NOT NULL,
                pending_caption TEXT, active INTEGER NOT NULL DEFAULT 1, busy INTEGER NOT NULL DEFAULT 0,
                updated REAL NOT NULL, PRIMARY KEY(chat_id,message_id))''')
            db.execute('UPDATE widget_sessions SET busy=0')
            db.execute('''CREATE TABLE IF NOT EXISTS widget_transitions (
                chat_id INTEGER, message_id INTEGER, owner_id INTEGER NOT NULL,
                timeframe TEXT NOT NULL, revision INTEGER NOT NULL, before_time INTEGER,
                public_data TEXT NOT NULL, chat_data TEXT NOT NULL, visible_start INTEGER NOT NULL,
                pending_caption TEXT, save_preference INTEGER NOT NULL,
                PRIMARY KEY(chat_id,message_id))''')

    @contextmanager
    def _connect(self):
        db=sqlite3.connect(self.path,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _key(context):
        context.coordinates()
        if context.inline_message_id: raise ValueError('inline sessions are disabled')
        return context.chat_id,context.message_id,context.owner_id

    @staticmethod
    def _session(row):
        return ChartWidgetSession(WidgetMessageContext(row['chat_id'],row['message_id'],row['owner_id']),
            row['timeframe'],row['revision'],row['before_time'],
            WidgetStateParts(row['public_data'],row['chat_data'],row['visible_start']),row['pending_caption'])

    def register(self, context, timeframe, parts, *, claimed=False):
        key=self._key(context)
        revision=json.loads(parts.public_data)['r']
        with self._connect() as db:
            db.execute('DELETE FROM widget_sessions WHERE updated < ?', (time.time()-86400,))
            db.execute('''INSERT INTO widget_sessions(chat_id,message_id,owner_id,timeframe,revision,
                public_data,chat_data,visible_start,updated,busy) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (*key,timeframe,revision,parts.public_data,parts.chat_data,parts.visible_start,time.time(),int(claimed)))
            db.execute('''DELETE FROM widget_sessions WHERE rowid NOT IN
                (SELECT rowid FROM widget_sessions ORDER BY updated DESC LIMIT 2048)''')
            db.execute('''DELETE FROM widget_transitions WHERE NOT EXISTS
                (SELECT 1 FROM widget_sessions s WHERE s.chat_id=widget_transitions.chat_id
                AND s.message_id=widget_transitions.message_id AND s.active=1)''')

    def get(self, context):
        try: key=self._key(context)
        except ValueError: return None
        with self._connect() as db:
            row=db.execute('''SELECT * FROM widget_sessions WHERE chat_id=? AND message_id=? AND owner_id=?
                AND active=1 AND updated>=?''',(*key,time.time()-86400)).fetchone()
        return self._session(row) if row else None

    def claim(self, context, revision):
        session=self.get(context)
        if session is None or session.revision!=revision: return None
        with self._connect() as db:
            changed=db.execute('''UPDATE widget_sessions SET busy=1 WHERE chat_id=? AND message_id=?
                AND owner_id=? AND revision=? AND active=1 AND busy=0''',(*self._key(context),revision)).rowcount
        return session if changed else None

    def pending(self, context):
        if self.get(context) is None:
            return None
        with self._connect() as db:
            row=db.execute('''SELECT * FROM widget_transitions
                WHERE chat_id=? AND message_id=? AND owner_id=?''', self._key(context)).fetchone()
        if row is None:
            return None
        return ChartWidgetTransition(**self._session(row).__dict__,save_preference=bool(row['save_preference']))

    def prepare(self, context, expected_revision, timeframe, revision, before, parts,
                pending_caption=None, *, save_preference=False):
        """Persist intent before any remote edit; retain it across uncertain delivery."""
        key=self._key(context)
        with self._connect() as db:
            current=db.execute('''SELECT revision FROM widget_sessions WHERE chat_id=? AND message_id=?
                AND owner_id=? AND active=1 AND busy=1 AND updated>=?''',(*key,time.time()-86400)).fetchone()
            if current is None or current['revision']!=expected_revision or revision!=expected_revision+1:
                return None
            db.execute('''INSERT OR REPLACE INTO widget_transitions
                (chat_id,message_id,owner_id,timeframe,revision,before_time,public_data,chat_data,
                visible_start,pending_caption,save_preference) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (*key,timeframe,revision,before,parts.public_data,parts.chat_data,parts.visible_start,
                 pending_caption,int(save_preference)))
        return self.pending(context)

    def finish(self, context, timeframe, revision, before, parts, pending_caption=None):
        with self._connect() as db:
            db.execute('''UPDATE widget_sessions SET timeframe=?,revision=?,before_time=?,public_data=?,
                chat_data=?,visible_start=?,pending_caption=?,busy=0,updated=?
                WHERE chat_id=? AND message_id=? AND owner_id=? AND active=1''',
                (timeframe,revision,before,parts.public_data,parts.chat_data,parts.visible_start,
                 pending_caption,time.time(),*self._key(context)))
            db.execute('DELETE FROM widget_transitions WHERE chat_id=? AND message_id=? AND owner_id=?',self._key(context))

    def release(self, context, revision=None):
        with self._connect() as db:
            condition=' AND revision=?' if revision is not None else ''
            parameters=(*self._key(context),revision) if revision is not None else self._key(context)
            db.execute('UPDATE widget_sessions SET busy=0 WHERE chat_id=? AND message_id=? AND owner_id=? AND busy=1'+condition,parameters)

    def close(self, context):
        try: key=self._key(context)
        except ValueError: return
        with self._connect() as db:
            db.execute('UPDATE widget_sessions SET active=0,busy=0 WHERE chat_id=? AND message_id=? AND owner_id=?',key)
            db.execute('DELETE FROM widget_transitions WHERE chat_id=? AND message_id=? AND owner_id=?',key)
