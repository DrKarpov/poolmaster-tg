from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from time import monotonic

import httpx
from PIL import Image, ImageDraw, ImageFont

from chart_source import (
    ChartSourceEmpty,
    ChartSourceError,
    ChartSourceInvalidResponse,
    ChartSourceRateLimited,
    ChartSourceTimeout,
    fetch_mexc_spot_ohlcv,
)
from config import load_settings
from localization import normalize_language, translate

CHART_CACHE_TTL_SECONDS = 60
COMMAND_CHART_CACHE_MAX_AGE_SECONDS = 10
SETTINGS = load_settings()
CHART_CACHE_DIR = SETTINGS.chart_cache_dir
CHART_STALE_MAX_AGE_SECONDS = SETTINGS.chart_stale_max_age_seconds


@dataclass(frozen=True)
class ChartTimeframe:
    code: str
    label: str
    range_label: str
    unit: str
    aggregate: int
    limit: int
    combine: int = 1


@dataclass(frozen=True)
class ChartPool:
    code: str
    label: str
    dex: str
    address: str
    url: str


@dataclass(frozen=True)
class ChartMarket:
    code: str
    label: str
    base: str
    quote: str
    symbol: str


@dataclass(frozen=True)
class ChartResult:
    caption: str
    image: BytesIO


@dataclass(frozen=True)
class CachedChart:
    expires_monotonic: float
    cached_at_epoch: float
    caption: str
    image_bytes: bytes
    image_name: str


CHART_CACHE: dict[tuple[str, ...], CachedChart] = {}
CHART_FETCH_TASKS: dict[tuple[str, str, str], asyncio.Task[ChartResult]] = {}


def _normalize_cache_key(cache_key: tuple[str, ...]) -> tuple[str, str, str]:
    if len(cache_key) == 2:
        return cache_key[0], cache_key[1], "ru"
    if len(cache_key) == 3:
        return cache_key[0], cache_key[1], normalize_language(cache_key[2])
    raise ValueError("chart cache key must contain market, timeframe, and optional language")


def make_chart_result(caption: str, image_bytes: bytes, image_name: str) -> ChartResult:
    output = BytesIO(image_bytes)
    output.name = image_name
    output.seek(0)
    return ChartResult(caption=caption, image=output)


def normalize_chart_caption(caption: str) -> str:
    if not caption.startswith("<b>JETTON chart</b>") and not caption.startswith("<b>"):
        return caption

    lines = caption.splitlines()
    price_line = next((line for line in lines if line.startswith("Цена USD:")), None)
    pool_line = next((line for line in lines if line.startswith("Пул:")), None)
    candle_line = next((line for line in lines if line.startswith("Свеча:")), None)
    range_line = next((line for line in lines if line.startswith("Диапазон:")), None)
    if not price_line or not pool_line or not candle_line or not range_line:
        return caption

    price_line = price_line.replace("Цена USD:", "Цена JETTON в USD:", 1)
    pool_line = pool_line.replace("Пул: ", "", 1)
    pool_line = pool_line.replace("Пул:", "", 1)
    metrics = [
        line
        for line in lines
        if line
        and not line.startswith("<b>")
        and not line.startswith("Пул:")
        and not line.startswith("Свеча:")
        and not line.startswith("Диапазон:")
        and not line.startswith("Цена USD:")
    ]
    return "\n".join([price_line, "", pool_line, candle_line, range_line, "", *metrics])


def cache_chart_result(
    cache_key: tuple[str, ...],
    result: ChartResult,
    *,
    cached_at: datetime | None = None,
) -> ChartResult:
    image_bytes = result.image.getvalue()
    image_name = result.image.name
    saved_at = cached_at or datetime.now(timezone.utc)
    if saved_at.tzinfo is None:
        raise ValueError("chart cache timestamp must be timezone-aware")
    saved_at = saved_at.astimezone(timezone.utc)
    entry = CachedChart(
        expires_monotonic=monotonic() + CHART_CACHE_TTL_SECONDS,
        cached_at_epoch=saved_at.timestamp(),
        caption=result.caption,
        image_bytes=image_bytes,
        image_name=image_name,
    )
    normalized_key = _normalize_cache_key(cache_key)
    CHART_CACHE[normalized_key] = entry
    # Keep the two-part Russian alias for existing integrations and cache readers.
    if len(cache_key) == 2:
        CHART_CACHE[cache_key] = entry
    save_disk_chart_cache(normalized_key, result.caption, image_bytes, image_name, cached_at=saved_at)
    return make_chart_result(result.caption, image_bytes, image_name)


def chart_cache_base_path(cache_key: tuple[str, ...]) -> Path:
    pool_code, timeframe_code, language = _normalize_cache_key(cache_key)
    suffix = "" if language == "ru" else f"_{language}"
    return CHART_CACHE_DIR / f"{pool_code}_{timeframe_code}{suffix}"


def save_disk_chart_cache(
    cache_key: tuple[str, ...],
    caption: str,
    image_bytes: bytes,
    image_name: str,
    *,
    cached_at: datetime | None = None,
) -> None:
    try:
        CHART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        base_path = chart_cache_base_path(cache_key)
        base_path.with_suffix(".png").write_bytes(image_bytes)
        base_path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "caption": caption,
                    "image_name": image_name,
                    "cached_at": (cached_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        logging.exception("Failed to save chart cache for %s", cache_key)


def load_disk_chart_cache(
    cache_key: tuple[str, ...],
    *,
    now: datetime | None = None,
) -> CachedChart | None:
    base_path = chart_cache_base_path(cache_key)
    meta_path = base_path.with_suffix(".json")
    image_path = base_path.with_suffix(".png")
    if not meta_path.exists() or not image_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        image_bytes = image_path.read_bytes()
        cached_at = datetime.fromisoformat(str(meta["cached_at"]))
        if cached_at.tzinfo is None:
            raise ValueError("chart cache timestamp is missing a timezone")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        logging.exception("Failed to load chart cache for %s", cache_key)
        return None
    saved_at = cached_at.astimezone(timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("chart cache age check requires a timezone-aware timestamp")
    age = current.astimezone(timezone.utc).timestamp() - saved_at.timestamp()
    if age < 0 or age > CHART_STALE_MAX_AGE_SECONDS:
        return None
    return CachedChart(
        expires_monotonic=0,
        cached_at_epoch=saved_at.timestamp(),
        caption=(
            normalize_chart_caption(str(meta.get("caption") or "<b>JETTON chart</b>"))
            if _normalize_cache_key(cache_key)[2] == "ru"
            else str(meta.get("caption") or "<b>JETTON chart</b>")
        ),
        image_bytes=image_bytes,
        image_name=str(meta.get("image_name") or image_path.name),
    )


TIMEFRAMES = {
    "5m": ChartTimeframe("5m", "5 минут", "примерно 2 часа", "minute", 5, 24),
    "10m": ChartTimeframe("10m", "10 минут", "примерно 8 часов", "minute", 5, 96, combine=2),
    "15m": ChartTimeframe("15m", "15 минут", "примерно 6 часов", "minute", 15, 24),
    "30m": ChartTimeframe("30m", "30 минут", "примерно 12 часов", "minute", 15, 48, combine=2),
    "1h": ChartTimeframe("1h", "1 час", "примерно 24 часа", "hour", 1, 24),
    "12h": ChartTimeframe("12h", "12 часов", "примерно 12 дней", "hour", 4, 72, combine=3),
}
PUBLIC_TIMEFRAMES = ("1h", "12h")


CHART_POOLS = {
    "dedust": ChartPool(
        code="dedust",
        label="DeDust",
        dex="DeDust",
        address="EQBOUKUYz_3gMLHhJLLajoDqU7-jEV4c7TRZE7EArYrKsb9o",
        url="https://app.dedust.io/pools/EQBOUKUYz_3gMLHhJLLajoDqU7-jEV4c7TRZE7EArYrKsb9o",
    ),
    "ston": ChartPool(
        code="ston",
        label="STON.fi",
        dex="STON.fi",
        address="EQD_Xx-iQR0z3GJozQt0dE1ybgwBFFyfT5yehnwOJE3kkCM0",
        url="https://app.ston.fi/pools/EQD_Xx-iQR0z3GJozQt0dE1ybgwBFFyfT5yehnwOJE3kkCM0",
    ),
}

CHART_MARKETS = {
    "mexc": ChartMarket("mexc", "MEXC Spot", "JETTON", "USDT", "JETTONUSDT"),
}


def build_chart_caption_header(
    market: ChartMarket,
    timeframe: ChartTimeframe,
    *,
    language: str = "ru",
) -> str:
    language = normalize_language(language)
    timeframe_label = translate(language, f"chart.timeframe.{timeframe.code}")
    range_label = translate(language, f"chart.timeframe.{timeframe.code}_range")
    return (
        f"<b>{market.label} · {market.base}/{market.quote}</b>\n"
        f"{translate(language, 'chart.caption.candle', value=timeframe_label)}\n"
        f"{translate(language, 'chart.caption.range', value=range_label)}"
    )


def format_price(value: float) -> str:
    if value >= 1:
        return f"{value:,.4f}"
    if value >= 0.01:
        return f"{value:.5f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def format_volume(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:.2f}"


def build_empty_chart(caption: str, *, language: str = "ru") -> ChartResult:
    language = normalize_language(language)
    width, height = 1100, 700
    image = Image.new("RGB", (width, height), "#091818")
    draw = ImageDraw.Draw(image)
    title_font = load_font(34, bold=True)
    body_font = load_font(23)
    small_font = load_font(17)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=20, fill="#1B2744", outline="#14A7E8", width=2)
    draw.text((56, 50), "JETTON/USDT", fill="#F3F8FA", font=title_font)
    draw.text((56, 101), translate(language, "chart.empty_title"), fill="#D6E0E4", font=body_font)
    source_prefix = "Источник:" if language == "ru" else "Source:"
    chart_prefix = "График JETTON/USDT:" if language == "ru" else "JETTON/USDT chart"
    detail_lines = [
        line
        for line in caption.splitlines()
        if line and not line.startswith(source_prefix) and not line.startswith(chart_prefix)
    ]
    for index, line in enumerate(detail_lines[:3]):
        draw.text((56, 158 + index * 30), line, fill="#D98E8E" if index else "#C1CDD2", font=small_font)
    draw.text((56, height - 66), translate(language, "chart.image.source"), fill="#B8E800", font=small_font)
    draw_brand_logo(image, width - 120, 42)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return make_chart_result(caption, output.getvalue(), "jetton_chart_mexc_unavailable.png")


def mark_caption_stale(caption: str, *, language: str = "ru") -> str:
    language = normalize_language(language)
    stale_notice = translate(language, "chart.stale")
    if stale_notice in caption:
        return caption
    return f"{caption}\n\n{stale_notice}"


def combine_candles(candles: list[list[float]], size: int) -> list[list[float]]:
    if size <= 1:
        return candles
    combined = []
    for index in range(0, len(candles), size):
        group = candles[index:index + size]
        if len(group) < size:
            continue
        ts = group[0][0]
        open_price = float(group[0][1])
        high_price = max(float(candle[2]) for candle in group)
        low_price = min(float(candle[3]) for candle in group)
        close_price = float(group[-1][4])
        volume = sum(float(candle[5]) for candle in group)
        combined.append([ts, open_price, high_price, low_price, close_price, volume])
    return combined


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        "/usr/share/fonts/truetype/dejavu/" + font_name,
        "/usr/share/fonts/dejavu/" + font_name,
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


@lru_cache(maxsize=1)
def load_chart_logo() -> Image.Image | None:
    logo_path = Path(__file__).parent / "assets" / "jetton_logo.png"
    if not logo_path.is_file():
        return None
    with Image.open(logo_path) as source:
        return source.convert("RGBA")


def draw_brand_logo(image: Image.Image, x: int, y: int, *, size: tuple[int, int] = (72, 56)) -> None:
    logo = load_chart_logo()
    if logo is None:
        return
    logo = logo.copy()
    logo.thumbnail(size, Image.Resampling.LANCZOS)
    image.paste(logo, (x, y), logo)


def price_to_y(price: float, low: float, high: float, top: int, bottom: int) -> int:
    if high == low:
        return (top + bottom) // 2
    return int(bottom - ((price - low) / (high - low) * (bottom - top)))


def moving_average(values: list[float], window: int) -> list[float | None]:
    if window <= 1:
        return values
    output: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < window:
            output.append(None)
            continue
        chunk = values[index + 1 - window:index + 1]
        output.append(sum(chunk) / window)
    return output


def build_chart_image(
    candles: list[list[float]],
    base: str,
    quote: str,
    market: ChartMarket,
    timeframe: ChartTimeframe,
    last: float,
    change: float,
    high: float,
    low: float,
    *,
    language: str = "ru",
) -> BytesIO:
    language = normalize_language(language)
    width, height = 1100, 700
    margin_left, margin_right = 86, 154
    chart_top, chart_bottom = 160, 500
    volume_top, volume_bottom = 540, 610
    background = "#091818"
    panel = "#1B2744"
    grid = "#35415B"
    text = "#F2F7F8"
    muted = "#C1CDD2"
    up = "#93C9A8"
    down = "#D98E8E"
    accent = "#14A7E8"
    lime = "#B8E800"
    ma = "#E6C97A"

    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    title_font = load_font(31, bold=True)
    price_font = load_font(29, bold=True)
    label_font = load_font(20, bold=True)
    small_font = load_font(17)
    axis_font = load_font(15)

    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=20, fill=panel, outline=accent, width=2)
    draw.text((54, 47), f"{base}/{quote}", fill=text, font=title_font)
    timeframe_label = translate(language, f"chart.timeframe.{timeframe.code}")
    timeframe_range = translate(language, f"chart.timeframe.{timeframe.code}_range")
    draw.text(
        (56, 94),
        f"{market.label} · {market.symbol} · {timeframe_label} · {timeframe_range}",
        fill=muted,
        font=small_font,
    )
    draw.rounded_rectangle((54, 132, 236, 137), radius=2, fill=lime)
    draw.text((704, 44), f"{format_price(last)} {quote}", fill=text, font=price_font)
    draw.text((706, 92), f"{change:+.2f}%", fill=up if change >= 0 else down, font=label_font)
    draw_brand_logo(image, 1000, 46, size=(56, 42))

    for index in range(5):
        y = chart_top + int((chart_bottom - chart_top) * index / 4)
        draw.line((margin_left, y, width - margin_right, y), fill=grid, width=1)
        value = high - ((high - low) * index / 4)
        draw.text((width - margin_right + 10, y - 10), f"{format_price(value)} {quote}", fill=muted, font=axis_font)

    draw.line((margin_left, chart_bottom, width - margin_right, chart_bottom), fill=accent, width=1)
    draw.line((margin_left, chart_top, margin_left, chart_bottom), fill=accent, width=1)
    draw.line((margin_left, volume_bottom, width - margin_right, volume_bottom), fill=accent, width=1)

    candle_count = len(candles)
    chart_width = width - margin_left - margin_right
    step = chart_width / max(candle_count, 1)
    candle_width = max(8, int(step * 0.56))
    max_volume = max(float(candle[5]) for candle in candles) or 1
    closes = [float(candle[4]) for candle in candles]
    ma_values = moving_average(closes, 7)

    previous_ma_point: tuple[int, int] | None = None
    for index, candle in enumerate(candles):
        _, open_price, high_price, low_price, close_price, _volume = [float(value) for value in candle]
        x = int(margin_left + step * index + step / 2)
        open_y = price_to_y(open_price, low, high, chart_top, chart_bottom)
        close_y = price_to_y(close_price, low, high, chart_top, chart_bottom)
        high_y = price_to_y(high_price, low, high, chart_top, chart_bottom)
        low_y = price_to_y(low_price, low, high, chart_top, chart_bottom)
        color = up if close_price >= open_price else down
        volume_height = max(1, int((_volume / max_volume) * (volume_bottom - volume_top)))
        draw.rectangle(
            (
                x - candle_width // 2,
                volume_bottom - volume_height,
                x + candle_width // 2,
                volume_bottom,
            ),
            fill=color,
        )

        draw.line((x, high_y, x, low_y), fill=color, width=3)
        top = min(open_y, close_y)
        bottom = max(open_y, close_y)
        if bottom - top < 3:
            bottom = top + 3
        draw.rounded_rectangle(
            (x - candle_width // 2, top, x + candle_width // 2, bottom),
            radius=2,
            fill=color,
        )

        ma_value = ma_values[index]
        if ma_value is not None:
            ma_point = (x, price_to_y(ma_value, low, high, chart_top, chart_bottom))
            if previous_ma_point:
                draw.line((previous_ma_point[0], previous_ma_point[1], ma_point[0], ma_point[1]), fill=ma, width=3)
            previous_ma_point = ma_point

    last_candle_time = datetime.fromtimestamp(int(candles[-1][0]), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    draw.text((margin_left, 516), translate(language, "chart.image.volume"), fill=muted, font=axis_font)
    draw.text((54, 621), translate(language, "chart.image.high", value=format_price(high), quote=quote), fill=muted, font=small_font)
    draw.text((274, 621), translate(language, "chart.image.low", value=format_price(low), quote=quote), fill=muted, font=small_font)
    draw.text((482, 621), translate(language, "chart.image.legend"), fill=muted, font=small_font)
    draw.text(
        (54, 650),
        translate(language, "chart.image.source_last", source=market.label, symbol=market.symbol, value=last_candle_time),
        fill=lime,
        font=axis_font,
    )

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = f"jetton_chart_{market.code}_{timeframe.code}.png"
    return output


def chart_cache_is_usable(cached: CachedChart, *, now_epoch: float | None = None) -> bool:
    current_epoch = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    age = current_epoch - cached.cached_at_epoch
    return 0 <= age <= CHART_STALE_MAX_AGE_SECONDS


def stale_chart_from_cache(
    cached: CachedChart | None,
    *,
    now_epoch: float | None = None,
    language: str = "ru",
) -> ChartResult | None:
    if not cached:
        return None
    if not chart_cache_is_usable(cached, now_epoch=now_epoch):
        return None
    return make_chart_result(mark_caption_stale(cached.caption, language=language), cached.image_bytes, cached.image_name)


async def _fetch_chart_uncached(
    market_code: str,
    timeframe_code: str,
    cached: CachedChart | None,
    language: str = "ru",
) -> ChartResult:
    language = normalize_language(language)
    market = CHART_MARKETS.get(market_code, CHART_MARKETS["mexc"])
    timeframe = TIMEFRAMES.get(timeframe_code, TIMEFRAMES["1h"])
    cache_key = (market.code, timeframe.code, language)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12, connect=4),
            headers={"Accept": "application/json", "User-Agent": "POOLMASTER Telegram Bot/1.0"},
            follow_redirects=True,
        ) as client:
            candles = await fetch_mexc_spot_ohlcv(client, timeframe.code)
    except ChartSourceError as exc:
        stale = stale_chart_from_cache(cached, language=language)
        if stale:
            logging.warning(
                "Using stale chart for %s %s after MEXC source error_type=%s",
                market.code,
                timeframe.code,
                type(exc).__name__,
            )
            return stale
        if isinstance(exc, ChartSourceRateLimited):
            reason = translate(language, "chart.reason.rate_limited")
        elif isinstance(exc, ChartSourceTimeout):
            reason = translate(language, "chart.reason.timeout")
        elif isinstance(exc, ChartSourceInvalidResponse):
            reason = translate(language, "chart.reason.invalid")
        elif isinstance(exc, ChartSourceEmpty):
            reason = translate(language, "chart.reason.empty")
        else:
            reason = translate(language, "chart.reason.unavailable")
        return build_empty_chart(
            "\n".join([
                translate(language, "chart.caption.unavailable"),
                translate(language, "chart.caption.timeframe", value=translate(language, f"chart.timeframe.{timeframe.code}")),
                translate(language, "chart.caption.source", source=market.label, symbol=market.symbol),
                reason,
            ]),
            language=language,
        )

    if not candles:
        stale = stale_chart_from_cache(cached, language=language)
        if stale:
            logging.warning("Using stale chart for %s %s because MEXC returned no candles", market.code, timeframe.code)
            return stale
        return build_empty_chart(
            "\n".join([
                translate(language, "chart.caption.unavailable"),
                translate(language, "chart.caption.timeframe", value=translate(language, f"chart.timeframe.{timeframe.code}")),
                translate(language, "chart.caption.source", source=market.label, symbol=market.symbol),
                translate(language, "chart.reason.empty"),
            ]),
            language=language,
        )

    rows = [
        [
            float(candle.open_time),
            float(candle.open),
            float(candle.high),
            float(candle.low),
            float(candle.close),
            float(candle.quote_volume),
        ]
        for candle in candles
    ]
    closes = [float(candle.close) for candle in candles]
    volumes = [float(candle.quote_volume) for candle in candles]
    first = closes[0]
    last = closes[-1]
    high = max(float(candle.high) for candle in candles)
    low = min(float(candle.low) for candle in candles)
    change = ((last - first) / first * 100) if first else 0
    updated_at = datetime.now(timezone.utc)
    updated_time = updated_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    last_ts = candles[-1].open_time
    last_time = datetime.fromtimestamp(last_ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    caption = "\n".join([
        translate(language, "chart.caption.price", pair=f"{market.base}/{market.quote}", value=format_price(last), quote=market.quote),
        "",
        build_chart_caption_header(market, timeframe, language=language),
        "",
        translate(language, "chart.caption.updated", value=updated_time),
        translate(language, "chart.caption.change", value=change),
        translate(language, "chart.caption.high_low", high=format_price(high), low=format_price(low), quote=market.quote),
        translate(language, "chart.caption.volume", value=format_volume(sum(volumes)), quote=market.quote),
        translate(language, "chart.caption.last", value=last_time),
        f"{translate(language, 'common.source')}: <b>{market.label} · {market.symbol}</b>",
    ])
    image = build_chart_image(rows, market.base, market.quote, market, timeframe, last, change, high, low, language=language)
    return cache_chart_result(cache_key, ChartResult(caption=caption, image=image), cached_at=updated_at)


async def fetch_chart(
    market_code: str,
    timeframe_code: str,
    *,
    force_refresh: bool = False,
    max_cache_age_seconds: int | None = None,
    language: str = "ru",
) -> ChartResult:
    language = normalize_language(language)
    market = CHART_MARKETS.get(market_code, CHART_MARKETS["mexc"])
    timeframe = TIMEFRAMES.get(timeframe_code, TIMEFRAMES["1h"])
    cache_key = (market.code, timeframe.code, language)
    cached = None if force_refresh else CHART_CACHE.get(cache_key)
    if cached is None and language == "ru" and not force_refresh:
        cached = CHART_CACHE.get((market.code, timeframe.code))
    if force_refresh:
        CHART_CACHE.pop(cache_key, None)
        if language == "ru":
            CHART_CACHE.pop((market.code, timeframe.code), None)
    if cached and not chart_cache_is_usable(cached):
        CHART_CACHE.pop(cache_key, None)
        if language == "ru":
            CHART_CACHE.pop((market.code, timeframe.code), None)
        cached = None
    if cached is None and not force_refresh:
        cached = load_disk_chart_cache(cache_key)
    if cached and cached.expires_monotonic > monotonic():
        if max_cache_age_seconds is None:
            return make_chart_result(cached.caption, cached.image_bytes, cached.image_name)
        cache_age_seconds = datetime.now(timezone.utc).timestamp() - cached.cached_at_epoch
        if 0 <= cache_age_seconds <= max_cache_age_seconds:
            return make_chart_result(cached.caption, cached.image_bytes, cached.image_name)

    task = CHART_FETCH_TASKS.get(cache_key)
    if task is None or task.done():
        if language == "ru":
            task = asyncio.create_task(_fetch_chart_uncached(market.code, timeframe.code, cached))
        else:
            task = asyncio.create_task(_fetch_chart_uncached(market.code, timeframe.code, cached, language))
        CHART_FETCH_TASKS[cache_key] = task

        def remove_completed(done_task: asyncio.Task[ChartResult]) -> None:
            if CHART_FETCH_TASKS.get(cache_key) is done_task:
                CHART_FETCH_TASKS.pop(cache_key, None)

        task.add_done_callback(remove_completed)
    result = await asyncio.shield(task)
    return make_chart_result(result.caption, result.image.getvalue(), result.image.name)
