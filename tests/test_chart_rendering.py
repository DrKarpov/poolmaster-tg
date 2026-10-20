from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

from PIL import Image, ImageColor
import pytest

import chart
from chart import TIMEFRAMES, build_chart_image, build_empty_chart


START = int(datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp())


def _candles():
    candles = []
    for index in range(10):
        open_price = 0.020 + index * 0.0001
        close_price = open_price + (0.0003 if index % 2 == 0 else -0.0002)
        candles.append(
            [
                START + index * 300,
                open_price,
                max(open_price, close_price) + 0.0002,
                min(open_price, close_price) - 0.0002,
                close_price,
                100 + 10 * index,
            ]
        )
    return candles


def _render(candles=None):
    data = candles or _candles()
    return build_chart_image(
        data,
        "JETTON",
        "USDT",
        SimpleNamespace(code="mexc", label="MEXC Spot", symbol="JETTONUSDT"),
        TIMEFRAMES["5m"],
        last=data[-1][4],
        change=1.2,
        high=max(candle[2] for candle in data),
        low=min(candle[3] for candle in data),
    )


def _record_drawn_text(monkeypatch):
    strings = []
    real_draw = chart.ImageDraw.Draw

    class RecordingDraw:
        def __init__(self, draw):
            self.draw = draw

        def text(self, xy, text, *args, **kwargs):
            strings.append(str(text))
            return self.draw.text(xy, text, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.draw, name)

    monkeypatch.setattr(chart.ImageDraw, "Draw", lambda image, *args, **kwargs: RecordingDraw(real_draw(image, *args, **kwargs)))
    return strings


@pytest.fixture
def chart_without_logo(monkeypatch, tmp_path):
    chart.load_chart_logo.cache_clear()
    monkeypatch.setattr(chart, "__file__", str(tmp_path / "chart.py"))
    yield
    chart.load_chart_logo.cache_clear()


def test_chart_renderer_uses_approved_palette_and_not_a_duplicate_close_line(chart_without_logo):
    rendered = _render()
    rendered.seek(0)
    with Image.open(rendered) as image:
        image.load()
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (1100, 700)
        colors = set(image.get_flattened_data())
        expected = {
            ImageColor.getrgb("#091818"),
            ImageColor.getrgb("#1B2744"),
            ImageColor.getrgb("#14A7E8"),
            ImageColor.getrgb("#B8E800"),
            ImageColor.getrgb("#D98E8E"),
            ImageColor.getrgb("#93C9A8"),
            ImageColor.getrgb("#E6C97A"),
        }
        assert expected <= colors
        assert ImageColor.getrgb("#28A8FF") not in colors


def test_chart_renderer_composites_the_supplied_logo(monkeypatch):
    logo = Image.new("RGBA", (16, 16), (230, 0, 230, 255))
    monkeypatch.setattr(chart, "load_chart_logo", lambda: logo, raising=False)

    rendered = _render()

    with Image.open(rendered) as image:
        assert (230, 0, 230) in set(image.get_flattened_data())


def test_chart_source_and_last_candle_time_are_drawn_on_the_image(monkeypatch, chart_without_logo):
    drawn_text = _record_drawn_text(monkeypatch)

    _render()

    assert any("MEXC Spot" in text for text in drawn_text)
    assert any("2026-10-18 12:45 UTC" in text for text in drawn_text)
    assert any("USDT" in text for text in drawn_text)


def test_chart_logo_loader_returns_none_when_asset_is_missing(chart_without_logo):
    assert chart.load_chart_logo() is None


def test_empty_chart_uses_the_approved_canvas_and_identifies_its_source(monkeypatch, chart_without_logo):
    drawn_text = _record_drawn_text(monkeypatch)
    rendered = build_empty_chart("График временно недоступен")

    with Image.open(BytesIO(rendered.image.getvalue())) as image:
        assert image.format == "PNG"
        assert image.size == (1100, 700)
        assert ImageColor.getrgb("#091818") in set(image.get_flattened_data())
    assert any("MEXC Spot" in text for text in drawn_text)
    assert any("временно недоступен" in text.lower() for text in drawn_text)
