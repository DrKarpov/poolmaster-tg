"""Compact, versioned widget state. Prices and quote volumes round-trip exactly."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path
import zlib

from chart_data import ChartWindow, WIDGET_TIMEFRAMES, FRAME_LABELS
from chart_source import MexcSpotCandle

AXIS_MONTHS = {
    'ru': ('янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'),
    'en': ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'),
}

@dataclass(frozen=True)
class ChartPreferences:
    timeframe: str = '1h'

    def __post_init__(self):
        if self.timeframe not in WIDGET_TIMEFRAMES:
            raise ValueError('unsupported widget timeframe')


@dataclass(frozen=True)
class WidgetStateParts:
    public_data: str
    chat_data: str
    visible_start: int


@dataclass(frozen=True)
class DecodedWindow:
    timeframe: str
    revision: int
    candles: tuple[MexcSpotCandle, ...]
    warmup_count: int


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _base36(value: int) -> str:
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    result = ''
    while value:
        value, digit = divmod(value, 36)
        result = digits[digit] + result
    return result or '0'


def _scale(values) -> int:
    return max(0, max(-value.normalize().as_tuple().exponent for value in values))


def _pack(value: Decimal, scale: int) -> str:
    # scaleb uses the Decimal context for giant numbers; strings preserve those.
    if len(value.as_tuple().digits) + scale > 25:
        return '~' + str(value)
    integer = value.scaleb(scale)
    if integer != integer.to_integral_value() or abs(integer) > 2**53-1:
        return '~' + str(value)
    return _base36(int(integer))


def _unpack(token: str, scale: int) -> Decimal:
    if token.startswith('~'):
        return Decimal(token[1:])
    return Decimal(int(token, 36)).scaleb(-scale)


def encode_window(window: ChartWindow, preferences: ChartPreferences, revision: int,
                  *, status: str = 'ok', navigation: int = 1, latest: bool = True, labels=(*FRAME_LABELS['ru'],*AXIS_MONTHS['ru'])) -> WidgetStateParts:
    # Shared windows are immutable. Cache only market/encoding inputs, never
    # per-user state; normalize labels so initial and subsequent edits reuse it.
    return _encode_window(window, preferences, revision, status, navigation,
                          latest, tuple(labels) if labels is not None else None)


@lru_cache(maxsize=128)
def _encode_window(window, preferences, revision, status, navigation, latest, labels):
    if not window.candles or window.timeframe != preferences.timeframe or revision < 1:
        raise ValueError('cannot encode an empty or mismatched window')
    count = window.visible_count
    while count:
        candles = window.candles[-min(len(window.candles), count + 6):]
        price_scale = _scale(v for c in candles for v in (c.open, c.high, c.low, c.close))
        volume_scale = _scale(c.quote_volume for c in candles)
        start = candles[0].open_time
        rows = [','.join([_base36(c.open_time-start), *(_pack(v, price_scale)
                for v in (c.open, c.high, c.low, c.close)), _pack(c.quote_volume, volume_scale)]) for c in candles]
        for split in range(1, len(rows)+1):
            public = {'v': 1, 'r': revision, 'u': navigation, 'l': int(latest), 'f': window.timeframe, 't': start,
                      's': [price_scale, volume_scale], 'q': str(candles[-1].close),
                      'a': int(window.fetched_at.timestamp()), 'e': 'stale' if window.is_stale else status,
                      'c': ';'.join(rows[:split])}
            if labels is not None: public['k']=' '.join(labels)
            private = {'v': 1, 'r': revision, 'f': window.timeframe, 't': start,
                       'n': count, 'w': len(candles)-count,
                       'h': window.has_earlier or len(window.candles) > len(candles),
                       'c': ';'.join(rows[split:])}
            a, b = compact(public), compact(private)
            # Reserve public bytes for a localized status and interactive flag.
            if len(a.encode()) <= 804 and len(b.encode()) <= (1000 if labels is not None else 900):
                return WidgetStateParts(a, b, candles[-count].open_time)
        count -= 1
    raise ValueError('exact OHLCV cannot fit the widget state budget')


def encode_history_page(window: ChartWindow, revision: int, before: int) -> str:
    """Read-only candle reserve, sized for one query answer (not message state)."""
    result = {'status':'history','r':revision,'f':window.timeframe,'b':before,
              't':before,'s':[0,0],'c':'','h':False}
    if window.candles:
        if any(c.open_time >= before for c in window.candles):
            raise ValueError('history crossed its requested boundary')
        parts = encode_window(window,ChartPreferences(window.timeframe),revision,labels=None)
        public,private = json.loads(parts.public_data),json.loads(parts.chat_data)
        rows = (public['c']+';'+private['c']).strip(';').split(';')
        result.update(t=public['t'],s=public['s'],h=private['h'])
        while rows:
            result['c']=';'.join(rows)
            if len(compact(result).encode()) <= 1024:
                return compact(result)
            rows.pop(0)
            result['h']=True
        raise ValueError('one exact candle exceeds the query answer budget')
    return compact(result)


def decode_window(parts: WidgetStateParts) -> DecodedWindow:
    a, b = json.loads(parts.public_data), json.loads(parts.chat_data)
    if any(a.get(k) != b.get(k) for k in ('v', 'r', 'f', 't')) or a.get('v') != 1:
        raise ValueError('widget fragments do not share a version and page')
    candles = []
    for row in filter(None, (a['c']+';'+b['c']).split(';')):
        t,o,h,l,c,v = row.split(',')
        op,hi,lo,cl = (_unpack(x,a['s'][0]) for x in (o,h,l,c))
        candles.append(MexcSpotCandle(a['t']+int(t,36), 0, op,hi,lo,cl,
                                     Decimal(0), _unpack(v,a['s'][1])))
    return DecodedWindow(a['f'],a['r'],tuple(candles),b['w'])


@lru_cache(maxsize=4)
def load_widget_html(*, language: str, interactive: bool) -> str:
    root = Path(__file__).resolve().parent
    template = (root/'assets/widgets/price/widget.html').read_text()
    from widget_compat import with_numeric_compat
    html = with_numeric_compat(template.replace('__QUERY__', '1' if interactive else '0'))
    if len(html.encode()) > 32768 or len(zlib.compress(html.encode(),9)) > 4096:
        raise ValueError('widget HTML exceeds the platform size budget')
    return html
