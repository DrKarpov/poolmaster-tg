"""Public snapshot must work with TST and without private branding files."""
from pathlib import Path
import xml.etree.ElementTree as ET

from farming_widget import load_farming_widget_html
from coin_widget import load_coin_widget_html


ROOT = Path(__file__).resolve().parents[1]


def test_farming_loads_public_vector_without_private_logo_files():
    assert not (ROOT/'assets/jetton_logo.png').exists()
    assert not (ROOT/'assets/jetton_logo.svg').exists()
    html = load_farming_widget_html(language='ru', interactive=True)
    vector = ET.fromstring((ROOT/'assets/widgets/price/logo.svg').read_text())
    assert vector.attrib['viewBox'] == '0 0 120 56'
    outline = vector.find('{http://www.w3.org/2000/svg}path').attrib['d']
    assert outline in html and '__LOGO__' not in html


def test_coin_draws_same_tst_outline_as_public_svg():
    vector = ET.fromstring((ROOT/'assets/widgets/price/logo.svg').read_text())
    outline = vector.find('{http://www.w3.org/2000/svg}path').attrib['d']
    html = load_coin_widget_html('en')
    assert "new Path2D('" + outline + "')" in html
    assert 'TST' in html and 'JETTON logo.' not in html
