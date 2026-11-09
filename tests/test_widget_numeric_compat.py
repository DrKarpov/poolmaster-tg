"""Exercise the actual pinned native engine when its isolated probe is supplied.

These tests catch wrong values crossing both JSON boundaries, including a
stringify reference captured by the sandbox before the widget starts.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def bootstrap():
    return (ROOT/'assets/widgets/numeric-compat.js').read_text()


@pytest.fixture
def native():
    binary = os.environ.get('POOLMASTER_QUICKJS_PROBE')
    preload = os.environ.get('POOLMASTER_QUICKJS_LOCALE_PRELOAD')
    locales = os.environ.get('POOLMASTER_QUICKJS_LOCALE_DIR')
    if not all((binary, preload, locales)):
        pytest.skip('Supply the pinned native QuickJS probe and isolated locale')

    def run(code, *, comma=True):
        env = dict(os.environ, LC_ALL='C')
        if comma:
            env.update(LD_PRELOAD=preload, LOCPATH=locales)
        else:
            env.pop('LD_PRELOAD', None)
        result = subprocess.run([binary, '-'], input=code, text=True,
                                capture_output=True, check=True, timeout=20, env=env)
        return json.loads(result.stdout)
    return run


def test_price_scale_and_fractional_json_are_restored(native):
    result = native(bootstrap() + '''
      const value=JSON.parse('{"s":[5,5],"fraction":0.01622}');
      JSON.stringify({scale:value.s[0],price:1622/10**value.s[0],fraction:value.fraction})
    ''')
    assert result == {'scale': 5, 'price': .01622, 'fraction': .01622}


@pytest.mark.parametrize('values', [
    [1, 0, 0, 1, 0, 0],
    [{'offset': .5, 'opacity': .2, 'timing': {'duration': 800, 'delay': .5}}],
    [1.65, .07228915662650602, .7, 6.283185307179586],
    ['c', 'arc', 0, 0, 46, 0, 6.283185307179586],
    [0, -0.0, -1.65, 1e-20, 1e20, 5e-324, 1.7976931348623157e308],
    [['c', 'scale', 1.65, .14], ['s', 'lineWidth', 1.5]],
])
def test_captured_serializer_preserves_canvas_numbers_for_separate_parser(native, values):
    wire = native('const captured=JSON.stringify;' + bootstrap()
                  + '\nJSON.stringify(captured(' + json.dumps(values) + '))')
    # The trusted validator has another context: do not apply our parse fix there.
    received = native('JSON.stringify(JSON.parse(' + json.dumps(wire) + '))')
    assert received == values


def test_strings_reviver_and_nonfinite_serialization_keep_standard_behavior(native):
    text = '{"v":0.01622,"s":"1.65,5\\\" 9","a":[5,5]}'
    result = native(bootstrap() + '\nconst text=' + json.dumps(text) + ''';
      const decoded=JSON.parse(text,function(k,v){return k==='v'?v*2:v});
      JSON.stringify({decoded,nonfinite:[NaN,Infinity,-Infinity,undefined]})
    ''')
    assert result == {'decoded': {'v': .03244, 's': '1.65,5" 9', 'a': [5, 5]},
                      'nonfinite': [None, None, None, None]}


def test_correct_native_engine_keeps_original_parser_and_array_prototype(native):
    result = native('const original=JSON.parse;' + bootstrap() + '''
      JSON.stringify({same:JSON.parse===original,hook:Object.hasOwn(Array.prototype,'toJSON')})
    ''', comma=False)
    assert result == {'same': True, 'hook': False}


def test_ordinary_browser_without_raw_json_keeps_original_behavior():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node needed for the ordinary-browser check')
    code = 'const original=JSON.parse;delete JSON.rawJSON;' + bootstrap() + '''
      console.log(JSON.stringify({same:JSON.parse===original,
        hook:Object.hasOwn(Array.prototype,'toJSON'),value:JSON.parse('[5,5]')}))
    '''
    result = subprocess.run([node, '-e', code], text=True, capture_output=True,
                            check=True, timeout=10)
    assert json.loads(result.stdout) == {'same': True, 'hook': False, 'value': [5, 5]}


@pytest.mark.parametrize('kind', ['price', 'farming', 'coin'])
@pytest.mark.parametrize('width,dpr', [(320, 2), (560, 1)])
def test_shipped_widgets_match_control_through_separate_native_validator(native, kind, width, dpr):
    from native_widget_probe import SDK, widget_program, validator_program
    if not (SDK/'sandbox-bundle.js').exists():
        pytest.skip('Pinned official native sandbox/validator bundles required')
    if kind == 'price':
        from chart_widget import ChartPreferences, encode_window, load_widget_html
        from test_chart_widget import window
        parts = encode_window(window(), ChartPreferences(), 1)
        html = load_widget_html(language='ru', interactive=True)
    elif kind == 'farming':
        from farming_widget import load_farming_widget_html
        from test_farming_widget_runtime import state
        parts = state()
        html = load_farming_widget_html(language='ru', interactive=True)
    else:
        from coin_widget import load_coin_widget_html
        parts = type('Empty', (), dict(public_data='{}', chat_data='{}'))()
        html = load_coin_widget_html('ru')
    program = widget_program(html, parts.public_data, parts.chat_data,
                             kind=kind, width=width, dpr=dpr)
    control = native(program, comma=False)
    affected = native(program)
    assert control['logs'] == affected['logs'] == []
    assert affected['facts'] == control['facts']
    if kind == 'coin':
        assert affected['facts'] == {'side': '1', 'busy': 'false'}
    elif kind == 'price':
        assert 'O 0.01622 H 0.01624' in affected['facts']['info']
    else:
        assert affected['facts']['mean'] == '25.00%'
        assert '%' in affected['facts']['popup']
    baseline = native(validator_program(control['batches'], control['anims']), comma=False)
    received = native(validator_program(affected['batches'], affected['anims']))
    assert baseline['diags'] == received['diags'] == []
    assert baseline['stopped'] == received['stopped'] == ''
    expected = [json.loads(batch) for batch in baseline['batches']]
    actual = [json.loads(batch) for batch in received['batches']]
    assert any(op.get('op') == 'canvas' for batch in actual for op in batch)
    assert actual == expected
    assert [json.loads(a) for a in received['anims']] == [json.loads(a) for a in baseline['anims']]
