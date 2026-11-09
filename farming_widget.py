"""Versioned farming state; display rounding never changes stored observations."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json

from chart_widget import WidgetStateParts, compact, _base36
from farming_widget_data import FarmingWidgetPoint, FarmingWidgetView, SUPPORTED_PERIODS


def _display(value):
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError('invalid farming value')
    return format(value.quantize(Decimal('.01'),rounding=ROUND_HALF_UP),'f')


def encode_farming_view(view, revision):
    if view.period not in SUPPORTED_PERIODS or revision < 1:
        raise ValueError('unsupported farming period or revision')
    start = view.points[0].start.toordinal() if view.points else 0
    rows = []
    prior = None
    for point in view.points:
        if len(point.values)!=4 or point.end!=point.start+timedelta(days=6) or (prior and point.start<=prior):
            raise ValueError('invalid farming points')
        prior = point.start
        values = ['-' if v is None else _base36(int(Decimal(_display(v))*100)) for v in point.values]
        rows.append(','.join([_base36(point.start.toordinal()-start),*values]))
    for split in range(len(rows)+1):
        a = dict(v=1,k='farm',r=revision,f=view.period,t=start,a=int(view.observed_at.timestamp()),
                 e=view.status,q=_display(view.current_mean_pct),l=_display(view.total_tvl_usd),c=';'.join(rows[:split]))
        b = dict(v=1,k='farm',r=revision,f=view.period,t=start,c=';'.join(rows[split:]))
        public, private = compact(a), compact(b)
        # Leave room for interaction flag and stored locale.
        if len(public.encode())<=870 and len(private.encode())<=900:
            return WidgetStateParts(public,private,start)
    raise ValueError('complete farming history exceeds widget state budget')


def decode_farming_view(parts):
    a,b = json.loads(parts.public_data),json.loads(parts.chat_data)
    if any(a.get(k)!=b.get(k) for k in ('v','k','r','f','t')) or a.get('k')!='farm' or a.get('v')!=1 or a.get('f') not in SUPPORTED_PERIODS:
        raise ValueError('mismatched farming state')
    points = []
    for row in filter(None,(a['c']+';'+b['c']).split(';')):
        offset,*tokens = row.split(',')
        start = date.fromordinal(a['t']+int(offset,36))
        if len(tokens)!=4:
            raise ValueError('invalid farming row')
        points.append(FarmingWidgetPoint(start,start+timedelta(days=6),tuple(None if v=='-' else Decimal(int(v,36))/100 for v in tokens)))
    return FarmingWidgetView(a['f'],tuple(points),Decimal(a['q']) if a['q'] is not None else None,
                             Decimal(a['l']) if a['l'] is not None else None,datetime.fromtimestamp(a['a'],timezone.utc),a['e'])


def load_farming_widget_html(*, language: str, interactive: bool) -> str:
    return _load_html(language, interactive)


from functools import lru_cache

@lru_cache(maxsize=4)
def _load_html(language, interactive):
    from pathlib import Path
    import zlib
    from chart_widget import AXIS_MONTHS
    from localization import normalize_language
    root = Path(__file__).resolve().parent
    language = normalize_language(language)
    template = (root/'assets/widgets/farming/widget.html').read_text()
    logo = (root/'assets/widgets/price/logo.svg').read_text().strip()
    logo = logo.replace(' xmlns="http://www.w3.org/2000/svg"','').replace(' fill="none"','')
    content = template.replace('__LOGO__',logo).replace('__QUERY__','1' if interactive else '0')
    content = content.replace('__MONTHS__',compact(AXIS_MONTHS[language]))
    content = content.replace('__MEAN__','Среднее' if language=='ru' else 'Average')
    from widget_compat import with_numeric_compat
    content = with_numeric_compat(content)
    if len(content.encode())>32768 or len(zlib.compress(content.encode(),9))>4096:
        raise ValueError('farming widget HTML exceeds platform budget')
    return content
