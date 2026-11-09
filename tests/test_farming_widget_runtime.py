import json
from pathlib import Path
import re
import subprocess
from datetime import date, timedelta
from decimal import Decimal

import pytest
from farming_widget import encode_farming_view, load_farming_widget_html
from farming_widget_data import FarmingWidgetView, FarmingWidgetPoint
from test_farming_widget_data import NOW


def state(revision=1,status='ok'):
    points = tuple(FarmingWidgetPoint(date(2026,5,25)+timedelta(weeks=i),date(2026,5,31)+timedelta(weeks=i),
                   (Decimal(10+i),Decimal(20+i),None,Decimal(20+i))) for i in range(23))
    return encode_farming_view(FarmingWidgetView('7D',points,Decimal(25),Decimal(1000),NOW,status),revision)


def runtime(actions=(),width=300,deferred=False):
    content = load_farming_widget_html(language='ru',interactive=True)
    script = re.search(r'<script>(.*?)</script>',content,re.S).group(1)
    parts = state()
    result = subprocess.run(['node',str(Path(__file__).with_name('farming_widget_runtime.cjs'))],
        input=json.dumps(dict(script=script,publicData=parts.public_data,chatData=parts.chat_data,actions=actions,width=width,deferred=deferred)),
        text=True,capture_output=True,check=True,timeout=15)
    return json.loads(result.stdout)


def test_no_caption_or_timeframe_placeholders():
    out=runtime()
    assert out['buttons']==['↻']
    assert out['mean']=='25.00%' and out['tvl']=='$1.00K'
    assert out['statusText']=='●'


@pytest.mark.parametrize('width',[160,280,390,560])
def test_all_weekly_points_fit_without_pan_or_zoom(width):
    out=runtime([{'kind':'drag','dx':170}],width=width)
    assert out['states'][0]['rows']==out['states'][-1]['rows']
    assert len(out['states'][0]['rows'])==23 and out['states'][-1]['pick']==-1
    assert out['requests']==[]


@pytest.mark.parametrize('index',[0,11,22])
def test_week_tap_shows_four_rates(index):
    out=runtime([{'kind':'week','index':index}])
    assert out['states'][-1]['pick']==index
    assert out['popupHidden'] is False
    assert f'{10+index}.00%' in out['popup']
    assert out['popup'].count('%')==3 and '—' in out['popup']
    assert out['requests']==[]


@pytest.mark.parametrize('close',[{'kind':'week','index':0},{'kind':'popup'}])
def test_same_week_or_popup_tap_closes(close):
    out=runtime([{'kind':'week','index':0},close])
    assert out['popupHidden'] is True and out['states'][-1]['pick']==-1


def test_other_week_replaces_values():
    out=runtime([{'kind':'week','index':0},{'kind':'week','index':22}])
    assert '32.00%' in out['popup'] and '10.00%' not in out['popup']


def test_empty_values_are_not_zero():
    out=runtime([{'kind':'week','index':0}])
    assert '—' in out['popup'] and '0.00%' not in out['popup'].replace('10.00%','').replace('20.00%','')


def test_pending_success_error_keep_card_height():
    parts=state(2)
    out=runtime([{'kind':'refresh'},{'kind':'data','publicData':parts.public_data,'chatData':parts.chat_data},
                 {'kind':'refresh'},{'kind':'timer'}])
    assert out['requests']==['fw1:refresh:1','fw1:refresh:2']
    assert [s['status'] for s in out['states']]==['ok','pending','ok','pending','error']
    assert out['statusText']=='●'


def test_recovered_revision_clears_pending_dot_without_duplicate_data_event():
    parts=state(2)
    out=runtime([{'kind':'data','publicData':parts.public_data,'chatData':parts.chat_data},
                 {'kind':'refresh'},{'kind':'reply','index':0,'value':{'status':'pending','revision':2}}],deferred=True)
    assert out['states'][-1]['status']=='ok'


def test_late_reply_cannot_cancel_a_new_refresh():
    parts=state(2)
    out=runtime([{'kind':'refresh'},{'kind':'data','publicData':parts.public_data,'chatData':parts.chat_data},
                 {'kind':'refresh'},{'kind':'reply','index':0,'value':{'status':'unavailable'}}],deferred=True)
    assert out['states'][-1]['status']=='pending'


def test_boot_uses_resize_observer_without_blocking_layout_query():
    out=runtime()
    assert out['layoutQueries']==0
    assert len(out['labels'])>0
