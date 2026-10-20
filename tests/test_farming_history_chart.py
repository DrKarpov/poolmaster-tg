from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO

from PIL import Image

import chart
import farming_history_chart as history_chart
from display_names import display_pair_name
from farming import FARMING_POOLS
from farming_history import FarmingHistorySummary, SERIES_KEY_BY_POOL_ADDRESS, WeeklyPoint, WeeklyPoolSeries


PALETTE = {"#14A7E8", "#B8E800", "#93C9A8", "#D98E8E"}


def _summary(*, missing_week: bool = False) -> FarmingHistorySummary:
    series = []
    start = date(2026, 5, 25)
    for index, pool in enumerate(FARMING_POOLS):
        key = SERIES_KEY_BY_POOL_ADDRESS[pool.address]
        weeks = [0, 2] if missing_week and index == 0 else [0, 1, 2]
        points = tuple(
            WeeklyPoint(
                week_start=start + timedelta(weeks=week),
                value_pct=Decimal(80 + index * 10 + week * 2),
                observation_count=2 if week == 0 and index == 0 else 1,
                metric_kinds=("APR",) if index < 2 else ("APY",),
            )
            for week in weeks
        )
        mean = sum((point.value_pct for point in points), Decimal(0)) / len(points)
        series.append(
            WeeklyPoolSeries(
                series_key=key,
                label=f"{pool.dex} {display_pair_name(pool)}",
                points=points,
                mean_pct=mean,
                observed_weeks=len(points),
            )
        )
    means = [item.mean_pct for item in series]
    return FarmingHistorySummary(
        series=tuple(series),
        period_start=start,
        period_end=start + timedelta(days=20),
        overall_mean_pct=sum(means, Decimal(0)) / Decimal(4),
    )


def _image(result):
    image = Image.open(BytesIO(result.image.getvalue()))
    image.load()
    return image


def test_history_chart_is_1100x700_and_uses_approved_palette():
    result = history_chart.build_farming_history_chart(_summary())

    image = _image(result)
    colors = {
        f"#{color[0]:02X}{color[1]:02X}{color[2]:02X}"
        for _count, color in image.getcolors(1100 * 700)
    }
    assert image.size == (1100, 700)
    assert PALETTE <= colors
    assert image.getpixel((0, 0)) == (9, 24, 24)
    assert image.getpixel((40, 60)) == (27, 39, 68)


def test_history_chart_preserves_logo_proportions(monkeypatch):
    calls = []
    logo = Image.new("RGBA", (40, 30), (255, 255, 255, 255))
    monkeypatch.setattr(chart, "load_chart_logo", lambda: logo, raising=False)
    draw_logo = history_chart.draw_brand_logo
    logo_width, logo_height = logo.size

    def capture_draw(image, x, y, *, size):
        calls.append((x, y, size))
        draw_logo(image, x, y, size=size)

    monkeypatch.setattr(history_chart, "draw_brand_logo", capture_draw)
    result = history_chart.build_farming_history_chart(_summary())

    assert _image(result).size == (1100, 700)
    assert calls
    assert calls == [(1000, 46, (56, 42))]
    assert all(width * logo_height == height * logo_width for _x, _y, (width, height) in calls)


def test_history_chart_breaks_lines_at_missing_weeks():
    points = _summary(missing_week=True).series[0].points

    segments = list(history_chart._adjacent_week_pairs(points))

    assert [(left.week_start, right.week_start) for left, right in segments] == []


def test_history_caption_contains_period_means_and_counts_without_technical_caveat_or_tvl():
    summary = _summary()

    russian = history_chart.build_farming_history_chart(summary, language="ru").caption
    english = history_chart.build_farming_history_chart(summary, language="en").caption

    assert "25 мая 2026" in russian
    assert "14 июня 2026" in russian
    assert "DeDust JETTON/GRAM" in russian
    assert "STON.fi JETTON/GRAM" in russian
    assert "JETTON/TON" not in russian
    assert "нед." in russian
    assert "Среднее по 4 пулам" in russian
    assert "APR/APY" not in russian
    assert "портфеля" not in russian
    assert "TVL" not in russian.upper()
    assert "May 25, 2026" in english
    assert "June 14, 2026" in english
    assert "DeDust JETTON/GRAM" in english
    assert "STON.fi JETTON/GRAM" in english
    assert "JETTON/TON" not in english
    assert "Average across 4 pools" in english
    assert "APR/APY" not in english
    assert "portfolio" not in english
    assert "TVL" not in english.upper()
    assert len(russian) <= 1024
    assert len(english) <= 1024


def test_history_caption_shows_next_completed_week_boundary_in_both_languages():
    as_of = datetime(2026, 10, 21, 15, 30, tzinfo=timezone.utc)

    russian = history_chart.build_farming_history_chart(_summary(), language="ru", as_of=as_of).caption
    english = history_chart.build_farming_history_chart(_summary(), language="en", as_of=as_of).caption

    assert "26 октября 2026, 00:00 UTC" in russian
    assert "00:05 UTC" in russian
    assert "October 26, 2026, 00:00 UTC" in english
    assert "00:05 UTC" in english


def test_empty_history_returns_localized_placeholder_and_uses_price_logo_position(monkeypatch):
    empty = FarmingHistorySummary(
        series=tuple(
            WeeklyPoolSeries(
                series_key=SERIES_KEY_BY_POOL_ADDRESS[pool.address],
                label=f"{pool.dex} {pool.name}",
                points=(),
                mean_pct=None,
                observed_weeks=0,
            )
            for pool in FARMING_POOLS
        ),
        period_start=None,
        period_end=None,
        overall_mean_pct=None,
    )

    as_of = datetime(2026, 10, 21, 15, 30, tzinfo=timezone.utc)
    calls = []
    draw_logo = history_chart.draw_brand_logo

    def capture_draw(image, x, y, *, size):
        calls.append((x, y, size))
        draw_logo(image, x, y, size=size)

    monkeypatch.setattr(history_chart, "draw_brand_logo", capture_draw)
    russian = history_chart.build_farming_history_chart(empty, language="ru", as_of=as_of)
    english = history_chart.build_farming_history_chart(empty, language="en", as_of=as_of)

    assert "Пока нет" in russian.caption
    assert "No data is saved" in english.caption
    assert "26 октября 2026, 00:00 UTC" in russian.caption
    assert "October 26, 2026, 00:00 UTC" in english.caption
    assert calls == [(1000, 46, (56, 42)), (1000, 46, (56, 42))]
    assert _image(russian).size == (1100, 700)
    assert _image(english).size == (1100, 700)
    assert len(russian.caption) <= 1024
    assert len(english.caption) <= 1024
