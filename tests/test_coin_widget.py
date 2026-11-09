import asyncio
import json
from pathlib import Path
import zlib

import httpx
import pytest

from config import load_settings
from widget_transport import WidgetTransport


def test_coin_flag_is_independent_and_opt_in():
    assert load_settings({}).coin_widgets_enabled is False
    assert load_settings({'COIN_WIDGETS_ENABLED':'true'}).coin_widgets_enabled is True
    assert load_settings({'COIN_WIDGETS_ENABLED':'false','PRICE_WIDGETS_ENABLED':'true'}).coin_widgets_enabled is False
    with pytest.raises(ValueError,match='COIN_WIDGETS_ENABLED'):
        load_settings({'COIN_WIDGETS_ENABLED':'invalid'})


@pytest.mark.parametrize('language,labels',[
    ('ru',['Цена','Фарминг','Уведомления','Язык / Language']),
    ('en',['Price','Farming','Notifications','Language / Язык']),
])
def test_coin_delivery_has_empty_caption_and_existing_localized_menu(language,labels):
    from coin_widget import send_coin_menu
    calls=[]
    def handler(request):
        assert request.url.path=='/beta/bot123:synthetic-secret/sendWidget'
        body=json.loads(request.content);calls.append(body)
        return httpx.Response(200,json={'ok':True,'result':{
            'chat':{'id':7,'type':'private'},'message_id':51,'widget':{'can_send_queries':False}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await send_coin_menu(WidgetTransport('123:synthetic-secret',client=client),7,language)
    receipt=asyncio.run(run())
    assert receipt.chat_id==7 and receipt.message_id==51
    assert len(calls)==1
    body=calls[0]
    assert body['caption']=='' and body['public_data']=='{}' and body['chat_data']=='{}'
    assert body['allow_queries'] is False and body['is_shared'] is False
    assert body['width']==body['height']==320
    buttons=[b for row in body['reply_markup']['inline_keyboard'] for b in row]
    assert [b['callback_data'] for b in buttons]==['ui:price','ui:farming','ui:alerts','ui:language']
    assert [b['text'] for b in buttons]==labels
    assert len(body['html'].encode())<=32768 and len(zlib.compress(body['html'].encode(),9))<=4096


def test_public_coin_localizes_accessible_labels_and_stays_within_budget():
    from coin_widget import load_coin_widget_html
    english=load_coin_widget_html('en')
    assert 'aria-label="Toss the TST coin"' in english
    assert 'Подбросить монетку' not in english and 'Гладкая сторона' not in english
    assert len(english.encode())<=32768 and len(zlib.compress(english.encode(),9))<=4096


@pytest.mark.parametrize('side',[0,1])
def test_coin_haptics_follow_toss_and_first_landing_only(side):
    result=coin_runtime(['tap','tap',939,940,960,1030,1120],side=side)
    assert result['haptics']==[{'style':'light','at':0},{'style':'medium','at':940}]
    assert result['samples']==[1,1,1,2,2,2,2]
    assert result['attrs']['data-side']==str(side) and result['attrs']['aria-busy']=='false'
    assert result['frames']==0


def test_hidden_coin_does_not_play_a_landing_haptic():
    result=coin_runtime(['tap',300,'hide',1120])
    assert result['haptics']==[{'style':'light','at':0}]
    assert result['attrs']['aria-busy']=='false'


@pytest.mark.parametrize('support',['missing','throws'])
def test_unavailable_haptics_do_not_interrupt_the_coin(support):
    result=coin_runtime(['tap',940,1120],support=support)
    assert result['haptics']==[] and result['attrs']['aria-busy']=='false'


def test_reduced_motion_coin_has_one_haptic_without_a_double_impact():
    result=coin_runtime(['tap'],reduced=True)
    assert result['haptics']==[{'style':'light','at':0}]
    assert result['attrs']['aria-busy']=='false' and result['frames']==0


def coin_runtime(actions,**options):
    import re
    import shutil
    import subprocess
    from coin_widget import load_coin_widget_html
    node=shutil.which('node')
    if node is None: pytest.skip('Node is needed for the animation boundary test')
    script=re.search(r'<script>(.*?)</script>',load_coin_widget_html('ru'),re.S).group(1)
    p=subprocess.run([node,str(Path(__file__).with_name('coin_widget_runtime.cjs'))],
        input=json.dumps({'script':script,'actions':actions,**options}),text=True,
        capture_output=True,check=True,timeout=15)
    return json.loads(p.stdout)


def test_each_new_toss_gets_its_own_landing_haptic():
    result=coin_runtime(['tap',940,1120,'tap',2060,2240])
    assert result['haptics']==[
        {'style':'light','at':0},{'style':'medium','at':940},
        {'style':'light','at':1120},{'style':'medium','at':2060}]
    assert result['attrs']['aria-busy']=='false'
