from __future__ import annotations

import html
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from typing import Iterable, Iterator

from PIL import Image, ImageDraw

from chart import ChartResult, draw_brand_logo, load_font
from farming_history import FarmingHistorySummary, WeeklyPoint
from localization import normalize_language, translate


WIDTH = 1100
HEIGHT = 700
BACKGROUND = "#091818"
PANEL = "#1B2744"
GRID = "#35415B"
TEXT = "#F2F7F8"
MUTED = "#C1CDD2"
ACCENT = "#14A7E8"
LIME = "#B8E800"
SERIES_COLORS = {
    "dedust_jetton_ton": "#14A7E8",
    "dedust_jetton_usdt": "#B8E800",
    "ston_jetton_ton": "#93C9A8",
    "ston_jetton_usdt": "#D98E8E",
}
RU_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
EN_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _format_date(value: date, language: str) -> str:
    if language == "ru":
        return f"{value.day} {RU_MONTHS[value.month - 1]} {value.year}"
    return f"{EN_MONTHS[value.month - 1]} {value.day}, {value.year}"


def _format_percent(value: Decimal, language: str) -> str:
    formatted = f"{value.quantize(Decimal('0.01')):,.2f}"
    if language == "ru":
        return formatted.replace(",", " ").replace(".", ",")
    return formatted


def _week_unit(count: int, language: str) -> str:
    if language == "en":
        return translate(language, "farming_history.week_unit_one" if count == 1 else "farming_history.week_unit_many")
    last_two = count % 100
    last = count % 10
    if 11 <= last_two <= 14:
        key = "farming_history.week_unit_many"
    elif last == 1:
        key = "farming_history.week_unit_one"
    elif 2 <= last <= 4:
        key = "farming_history.week_unit_few"
    else:
        key = "farming_history.week_unit_many"
    return translate(language, key)


def _next_week_boundary(as_of: datetime | None = None) -> datetime:
    reference = as_of or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)
    days_until_monday = (7 - reference.weekday()) % 7
    next_monday = reference.date() + timedelta(days=days_until_monday)
    boundary = datetime.combine(next_monday, time.min, tzinfo=timezone.utc)
    if boundary <= reference:
        boundary += timedelta(days=7)
    return boundary


def _caption(
    summary: FarmingHistorySummary,
    language: str,
    *,
    as_of: datetime | None = None,
) -> str:
    week_boundary = _next_week_boundary(as_of)
    next_collection = week_boundary + timedelta(minutes=5)
    update_notice = translate(
        language,
        "farming_history.caption.next_update",
        week_end=f"{_format_date(week_boundary.date(), language)}, 00:00 UTC",
        collection=f"{_format_date(next_collection.date(), language)}, 00:05 UTC",
    )
    if summary.period_start is None or summary.period_end is None:
        return "\n".join(
            (
                f"<b>{translate(language, 'farming_history.caption.title')}</b>",
                translate(language, "farming_history.caption.empty"),
                "",
                update_notice,
            )
        )

    lines = [
        f"<b>{translate(language, 'farming_history.caption.title')}</b>",
        translate(
            language,
            "farming_history.caption.period",
            start=_format_date(summary.period_start, language),
            end=_format_date(summary.period_end, language),
        ),
        "",
    ]
    for series in summary.series:
        value = (
            _format_percent(series.mean_pct, language)
            if series.mean_pct is not None
            else translate(language, "farming_history.caption.no_mean")
        )
        lines.append(
            translate(
                language,
                "farming_history.caption.pool_mean",
                pool=html.escape(series.label),
                value=value,
                count=series.observed_weeks,
                unit=_week_unit(series.observed_weeks, language),
            )
        )

    lines.append("")
    overall = (
        _format_percent(summary.overall_mean_pct, language)
        if summary.overall_mean_pct is not None
        else translate(language, "farming_history.caption.no_mean")
    )
    lines.append(
        translate(language, "farming_history.caption.aggregate", value=overall)
    )
    lines.extend(("", update_notice))
    return "\n".join(lines)


def _adjacent_week_pairs(points: Iterable[WeeklyPoint]) -> Iterator[tuple[WeeklyPoint, WeeklyPoint]]:
    ordered = tuple(points)
    for left, right in zip(ordered, ordered[1:]):
        if right.week_start - left.week_start == timedelta(days=7):
            yield left, right


def _render_empty(language: str, next_collection: datetime) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, WIDTH - 24, HEIGHT - 24), radius=20, fill=PANEL, outline=ACCENT, width=2)
    draw_brand_logo(image, 1000, 46, size=(56, 42))
    draw.text((56, 56), translate(language, "farming_history.image.title"), fill=TEXT, font=load_font(31, bold=True))
    draw.text((56, 180), translate(language, "farming_history.image.empty"), fill=MUTED, font=load_font(23))
    draw.text((56, 232), translate(language, "farming_history.image.empty_hint"), fill=MUTED, font=load_font(18))
    draw.text((56, HEIGHT - 64), translate(language, "farming_history.image.footer"), fill=LIME, font=load_font(15))
    draw.text(
        (56, HEIGHT - 43),
        translate(
            language,
            "farming_history.image.next_update",
            collection=f"{_format_date(next_collection.date(), language)}, 00:05 UTC",
        ),
        fill=MUTED,
        font=load_font(14),
    )
    return image


def _save_result(image: Image.Image, caption: str, filename: str) -> ChartResult:
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = filename
    return ChartResult(caption=caption, image=output)


def build_farming_history_chart(
    summary: FarmingHistorySummary,
    language: str = "ru",
    *,
    as_of: datetime | None = None,
) -> ChartResult:
    language = normalize_language(language)
    as_of = as_of or datetime.now(timezone.utc)
    week_boundary = _next_week_boundary(as_of)
    next_collection = week_boundary + timedelta(minutes=5)
    caption = _caption(summary, language, as_of=as_of)
    all_points = [point for series in summary.series for point in series.points]
    if not all_points:
        return _save_result(
            _render_empty(language, next_collection),
            caption,
            "jetton_farming_history_empty.png",
        )

    first_week = min(point.week_start for point in all_points)
    last_week = max(point.week_start for point in all_points)
    week_slots = max(1, (last_week - first_week).days // 7 + 1)
    low_value = min(point.value_pct for point in all_points)
    high_value = max(point.value_pct for point in all_points)
    span = high_value - low_value
    padding = max(span * Decimal("0.08"), Decimal("1"))
    y_min = low_value - padding
    y_max = high_value + padding

    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, WIDTH - 24, HEIGHT - 24), radius=20, fill=PANEL, outline=ACCENT, width=2)
    draw.text((56, 42), translate(language, "farming_history.image.title"), fill=TEXT, font=load_font(30, bold=True))
    period = translate(
        language,
        "farming_history.image.period",
        start=_format_date(first_week, language),
        end=_format_date(last_week + timedelta(days=6), language),
    )
    draw.text((58, 91), period, fill=MUTED, font=load_font(17))
    draw_brand_logo(image, 1000, 46, size=(56, 42))
    draw.rounded_rectangle((56, 126, 238, 131), radius=2, fill=LIME)

    plot_left, plot_right = 100, 1036
    plot_top, plot_bottom = 170, 472
    plot_height = plot_bottom - plot_top
    plot_width = plot_right - plot_left
    axis_font = load_font(15)
    label_font = load_font(16)
    small_font = load_font(15)

    draw.text((plot_left, 143), translate(language, "farming_history.image.y_axis"), fill=MUTED, font=small_font)
    for index in range(5):
        ratio = Decimal(index) / Decimal(4)
        y = plot_top + round(plot_height * index / 4)
        value = y_max - (y_max - y_min) * ratio
        draw.line((plot_left, y, plot_right, y), fill=GRID, width=1)
        draw.text((42, y - 9), f"{_format_percent(value, language)}%", fill=MUTED, font=axis_font)
    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=ACCENT, width=1)
    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=ACCENT, width=1)

    def x_position(week_start: date) -> int:
        slot = (week_start - first_week).days // 7
        return plot_left + round(plot_width * slot / max(week_slots - 1, 1))

    def y_position(value: Decimal) -> int:
        ratio = float((value - y_min) / (y_max - y_min))
        return int(plot_bottom - ratio * plot_height)

    for series in summary.series:
        color = SERIES_COLORS.get(series.series_key, ACCENT)
        for left, right in _adjacent_week_pairs(series.points):
            draw.line(
                (x_position(left.week_start), y_position(left.value_pct), x_position(right.week_start), y_position(right.value_pct)),
                fill=color,
                width=4,
            )
        for point in series.points:
            x = x_position(point.week_start)
            y = y_position(point.value_pct)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color, outline=PANEL, width=1)

    tick_count = min(6, week_slots)
    tick_indices = sorted({round(index * (week_slots - 1) / max(tick_count - 1, 1)) for index in range(tick_count)})
    for slot in tick_indices:
        tick_week = first_week + timedelta(weeks=slot)
        x = x_position(tick_week)
        date_label = (
            f"{tick_week.day} {RU_MONTHS[tick_week.month - 1]}"
            if language == "ru"
            else f"{EN_MONTHS[tick_week.month - 1][:3]} {tick_week.day}"
        )
        left = min(max(x - 32, plot_left), plot_right - 66)
        draw.text((left, plot_bottom + 10), date_label, fill=MUTED, font=axis_font)
    draw.text((plot_left, 508), translate(language, "farming_history.image.x_axis"), fill=MUTED, font=small_font)

    for index, series in enumerate(summary.series):
        column = index % 2
        row = index // 2
        x = 58 + column * 514
        y = 548 + row * 32
        color = SERIES_COLORS.get(series.series_key, ACCENT)
        draw.rounded_rectangle((x, y + 7, x + 28, y + 14), radius=3, fill=color)
        draw.text((x + 40, y), series.label, fill=TEXT, font=label_font)

    draw.text((56, HEIGHT - 64), translate(language, "farming_history.image.footer"), fill=LIME, font=load_font(15))
    draw.text(
        (56, HEIGHT - 43),
        translate(
            language,
            "farming_history.image.next_update",
            collection=f"{_format_date(next_collection.date(), language)}, 00:05 UTC",
        ),
        fill=MUTED,
        font=load_font(14),
    )
    return _save_result(image, caption, "jetton_farming_history.png")
