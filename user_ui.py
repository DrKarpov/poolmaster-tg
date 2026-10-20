from __future__ import annotations

import html
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from display_names import display_pair_name
from farming import FARMING_POOLS, PoolMetrics, SourceStatus, is_primary_farming_usable
from localization import normalize_language, translate
from price import PriceMetrics
from snapshot import DataSnapshot


USER_SCREENS = frozenset({
    "home", "price", "farming", "alerts", "settings", "language",
    "alert_price", "alert_yield", "alert_input", "alert_preview",
})


def private_callback_is_owned(query: Any) -> bool:
    user = getattr(query, "from_user", None)
    message = getattr(query, "message", None)
    chat = getattr(message, "chat", None)
    return bool(
        user
        and chat
        and getattr(chat, "type", None) == "private"
        and getattr(chat, "id", None) == getattr(user, "id", None)
    )


def render_language_prompt() -> str:
    return translate("ru", "language.prompt")


def render_language_screen(language: Any = "ru") -> str:
    return f"<b>{html.escape(translate(language, 'language.title'))}</b>"


def _format_time(value: datetime | None, language: str = "ru") -> str:
    if value is None:
        return translate(language, "time.unknown_updated")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_age_since(value: datetime, now: datetime, language: str = "ru") -> str:
    if value.tzinfo is None or value.utcoffset() is None or now.tzinfo is None or now.utcoffset() is None:
        return translate(language, "time.unknown_age")
    seconds = int((now.astimezone(timezone.utc) - value.astimezone(timezone.utc)).total_seconds())
    if seconds < 0:
        return translate(language, "time.invalid")
    if seconds < 60:
        return translate(language, "time.seconds_ago", value=seconds)
    if seconds < 3600:
        return translate(language, "time.minutes_ago", value=seconds // 60)
    if seconds < 86400:
        return translate(language, "time.hours_ago", value=seconds // 3600)
    return translate(language, "time.days_ago", value=seconds // 86400)


def _format_usdt(value: Decimal | None, language: str = "ru") -> str:
    if value is None:
        return translate(language, "value.unavailable")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _format_decimal(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "—"
    rendered = f"{value:,.{places}f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _alert_direction(language: str, direction: str) -> str:
    key = {
        "up": "alerts.direction_up_value",
        "down": "alerts.direction_down_value",
        "both": "alerts.direction_both_value",
    }.get(direction, "alerts.direction_both_value")
    return translate(language, key)


def _format_points(value: Decimal, language: str) -> str:
    amount = _format_decimal(value)
    if language == "en":
        unit = "percentage point" if value == 1 else "percentage points"
    elif value != value.to_integral_value():
        unit = "пункта"
    else:
        integer = abs(int(value))
        last_two = integer % 100
        last = integer % 10
        if last == 1 and last_two != 11:
            unit = "пункт"
        elif 2 <= last <= 4 and not 12 <= last_two <= 14:
            unit = "пункта"
        else:
            unit = "пунктов"
    return f"{amount} {unit}"


def _alert_threshold(language: str, value: Any, direction: str, *, yield_alert: bool) -> str:
    if value is None:
        return translate(language, "alerts.unset")
    decimal_value = Decimal(str(value))
    magnitude = _format_points(decimal_value, language) if yield_alert else _format_decimal(decimal_value)
    if direction == "up":
        sign = "+"
    elif direction == "down":
        sign = "−"
    else:
        sign = "±"
    unit = "" if yield_alert else "%"
    return f"{sign}{magnitude}{unit}"


def _price_alert_text(
    snapshot: DataSnapshot,
    state: dict[str, Any],
    language: str,
    *,
    pending_threshold: Any | None = None,
) -> str:
    is_preview = pending_threshold is not None
    threshold = pending_threshold if is_preview else state.get("price_threshold_pct")
    direction = str(state.get("price_direction", "both"))
    price = snapshot.price
    current = price.price_usdt if _price_has_displayable_value(price) else None
    lines = [
        f"<b>{translate(language, 'alerts.price_detail_title')}</b>",
        "",
        translate(language, "alerts.direction_line", direction=_alert_direction(language, direction)),
        translate(language, "alerts.threshold_line", threshold=_alert_threshold(language, threshold, direction, yield_alert=False)),
        translate(language, "alerts.price_help"),
        "",
    ]
    if not is_preview:
        lines.append(translate(
            language,
            "alerts.status_line",
            status=translate(language, "alerts.enabled" if state.get("price_enabled") else "alerts.disabled"),
        ))
    if is_preview:
        lines.append(translate(language, "alerts.preview_heading"))
    if price.is_stale:
        lines.append(translate(language, "price.stale"))
    if current is None:
        lines.append(translate(language, "alerts.no_current"))
    else:
        current_label = "alerts.preview_reference_price" if is_preview else "alerts.current_price"
        lines.append(translate(language, current_label, value=_format_usdt(current, language)))

    reference = current if is_preview else (
        Decimal(str(state["price_baseline"]))
        if threshold is not None and state.get("price_baseline") is not None
        else None
    )
    if reference is None:
        if threshold is None:
            lines.append(translate(language, "alerts.no_threshold"))
        elif not is_preview:
            lines.append(translate(language, "alerts.no_reference"))
        elif current is None:
            lines.append(translate(language, "alerts.no_reference"))
    else:
        if not is_preview:
            lines.append(translate(language, "alerts.reference_price", value=_format_usdt(reference, language)))
        if threshold is not None:
            pct = Decimal(str(threshold))
            targets: list[tuple[str, Decimal]] = []
            if direction in {"up", "both"}:
                targets.append(("up", reference * (Decimal(1) + pct / Decimal(100))))
            if direction in {"down", "both"}:
                targets.append(("down", reference * (Decimal(1) - pct / Decimal(100))))
            for target_direction, target in targets:
                target_key = "alerts.trigger_up_price" if target_direction == "up" else "alerts.trigger_down_price"
                lines.append(translate(language, target_key, value=_format_usdt(target, language)))
                if not is_preview and current is not None and not price.is_stale:
                    gap = target - current if target_direction == "up" else current - target
                    if gap <= 0:
                        distance = translate(language, "alerts.level_reached")
                    else:
                        distance_pct = gap / current * Decimal(100) if current > 0 else Decimal(0)
                        distance = translate(
                            language,
                            "alerts.distance_up_price" if target_direction == "up" else "alerts.distance_down_price",
                            value=_format_usdt(gap, language),
                            percent=_format_decimal(distance_pct),
                        )
                    lines.append(distance)
    if is_preview:
        lines.extend(["", translate(language, "alerts.preview_note")])
    return "\n".join(lines)


def _yield_alert_text(
    snapshot: DataSnapshot,
    state: dict[str, Any],
    language: str,
    invalid_pool_ids: set[str] | frozenset[str],
    *,
    pending_threshold: Any | None = None,
) -> str:
    is_preview = pending_threshold is not None
    threshold = pending_threshold if is_preview else state.get("farming_change_threshold_pp")
    direction = str(state.get("farming_direction", "both"))
    baselines = state.get("last_notified_yield", {})
    if not isinstance(baselines, dict):
        baselines = {}
    lines = [
        f"<b>{translate(language, 'alerts.yield_detail_title')}</b>",
        "",
        translate(language, "alerts.direction_line", direction=_alert_direction(language, direction)),
        translate(language, "alerts.threshold_line", threshold=_alert_threshold(language, threshold, direction, yield_alert=True)),
        translate(language, "alerts.yield_help"),
    ]
    if not is_preview:
        lines.append(translate(
            language,
            "alerts.status_line",
            status=translate(language, "alerts.enabled" if state.get("farming_enabled") else "alerts.disabled"),
        ))
    if is_preview:
        lines.extend(["", translate(language, "alerts.preview_heading")])
    elif threshold is None:
        lines.extend(["", translate(language, "alerts.no_threshold")])

    available = False
    for pool_id, metrics in snapshot.farming.items():
        pair = display_pair_name(metrics.pool)
        lines.extend(["", f"• <b>{html.escape(metrics.pool.dex)} {html.escape(pair)}</b>"])
        metric = metrics.primary_farm_yield
        valid = (
            pool_id not in invalid_pool_ids
            and is_primary_farming_usable(
                metrics,
                anomaly=f"farming:{pool_id}" in snapshot.anomalies,
            )
        )
        displayable = (
            metric is not None
            and metrics.identity_valid is True
            and pool_id not in invalid_pool_ids
            and isinstance(metric.value_percent, Decimal)
            and metric.value_percent.is_finite()
            and metric.value_percent >= 0
        )
        if not displayable:
            lines.append(translate(language, "alerts.pool_unavailable"))
            continue
        available = available or valid
        baseline = baselines.get(pool_id)
        baseline_value = None
        if threshold is not None and isinstance(baseline, dict) and baseline.get("kind") == metric.kind and baseline.get("source_field") == metric.source_field:
            try:
                baseline_value = Decimal(str(baseline.get("value")))
            except (InvalidOperation, TypeError, ValueError):
                baseline_value = None
        if is_preview:
            baseline_value = metric.value_percent if valid else None
        if not valid:
            lines.append(translate(language, "alerts.no_current"))
        else:
            lines.append(translate(language, "alerts.pool_current", value=_format_decimal(metric.value_percent), kind=metric.kind))
        if threshold is None and not is_preview:
            continue
        if baseline_value is None:
            lines.append(translate(language, "alerts.no_reference"))
            continue
        reference_key = "alerts.pool_preview_reference" if is_preview else "alerts.pool_reference"
        lines.append(translate(language, reference_key, value=_format_decimal(baseline_value), kind=metric.kind))
        if threshold is None:
            continue
        change = Decimal(str(threshold))
        targets: list[tuple[str, Decimal]] = []
        if direction in {"up", "both"}:
            targets.append(("up", baseline_value + change))
        if direction in {"down", "both"}:
            if baseline_value - change < 0:
                lines.append(translate(language, "alerts.pool_trigger_unreachable"))
            else:
                targets.append(("down", baseline_value - change))
        for target_direction, target in targets:
            target_key = "alerts.pool_trigger_up" if target_direction == "up" else "alerts.pool_trigger_down"
            lines.append(translate(language, target_key, value=_format_decimal(target), kind=metric.kind))
            if not is_preview and valid:
                gap = target - metric.value_percent if target_direction == "up" else metric.value_percent - target
                distance = translate(language, "alerts.level_reached") if gap <= 0 else _format_points(gap, language)
                lines.append(translate(
                    language,
                    "alerts.pool_distance_up" if target_direction == "up" else "alerts.pool_distance_down",
                    value=distance,
                ))
    if not available:
        lines.extend(["", translate(language, "alerts.no_current")])
    if is_preview:
        lines.extend(["", translate(language, "alerts.preview_note")])
    return "\n".join(lines)


def _price_has_displayable_value(price: PriceMetrics) -> bool:
    if price.price_usdt is None or price.status == SourceStatus.INVALID_RESPONSE:
        return False
    if price.status in {SourceStatus.UNAVAILABLE, SourceStatus.TIMEOUT}:
        return price.is_stale
    return True


def render_price_text(price: PriceMetrics, *, language: str = "ru") -> str:
    language = normalize_language(language)
    source = "MEXC Spot" if "mexc" in price.source.lower() else html.escape(price.source)
    if not _price_has_displayable_value(price):
        return (
            f"<b>{translate(language, 'price.title')}</b>\n\n"
            f"{translate(language, 'price.unavailable')}\n"
            f"{translate(language, 'common.source')}: {source}"
        )

    lines = [
        f"<b>{translate(language, 'price.title')}</b>",
        "",
        f"<b>{_format_usdt(price.price_usdt, language)} USDT</b>",
        f"{translate(language, 'common.source')}: {source}",
        f"{translate(language, 'common.updated')}: {_format_time(price.source_timestamp, language)}",
    ]
    if price.is_stale:
        lines.insert(2, translate(language, "price.stale"))
    return "\n".join(lines)


def render_inline_price_text(price: PriceMetrics, *, language: str = "ru") -> str:
    """Render a compact shareable quote without chart or candle commentary."""
    language = normalize_language(language)
    lines = [render_price_text(price, language=language)]
    if not _price_has_displayable_value(price):
        return lines[0]

    market_lines: list[str] = []
    if price.price_change_24h_percent is not None:
        market_lines.append(translate(
            language,
            "inline.price.change_24h",
            value=f"{price.price_change_24h_percent:+.2f}",
        ))
    if price.low_price_24h is not None and price.high_price_24h is not None:
        market_lines.append(translate(
            language,
            "inline.price.range_24h",
            low=_format_usdt(price.low_price_24h, language),
            high=_format_usdt(price.high_price_24h, language),
        ))
    if price.quote_volume_24h is not None:
        volume = f"{price.quote_volume_24h:,.2f}".rstrip("0").rstrip(".")
        market_lines.append(translate(language, "inline.price.volume_24h", value=volume))
    if market_lines:
        lines.append("\n".join(market_lines))
    return "\n\n".join(lines)


def render_farming_text(
    pool_metrics: list[PoolMetrics],
    *,
    invalid_pool_ids: set[str] | frozenset[str] = frozenset(),
    anomalies: Mapping[str, str] | None = None,
    now: datetime | None = None,
    language: str = "ru",
) -> str:
    language = normalize_language(language)
    current_time = now or datetime.now(timezone.utc)
    farming_anomalies = anomalies or {}
    lines = [
        f"<b>{translate(language, 'farming.title')}</b>",
        "",
        translate(language, "farming.intro"),
    ]
    displayed_tvl: list[Decimal] = []
    for info in pool_metrics:
        pair = display_pair_name(info.pool)
        lines.extend(["", f"• <b>{html.escape(info.pool.dex)} {html.escape(pair)}</b>"])
        if info.pool.address in invalid_pool_ids or not info.identity_valid:
            lines.append(translate(language, "farming.invalid"))
            continue
        if not info.is_stale and info.status in {
            SourceStatus.UNAVAILABLE,
            SourceStatus.TIMEOUT,
            SourceStatus.INVALID_RESPONSE,
        }:
            lines.append(translate(language, "farming.unavailable"))
            continue
        if info.is_stale:
            age = _format_age_since(info.fetched_at, current_time, language)
            if info.status_detail and "not confirmed" in info.status_detail:
                lines.append(translate(language, "farming.not_confirmed", age=age))
            else:
                lines.append(translate(language, "farming.stale", age=age))
        primary_yield = info.primary_farm_yield
        anomaly = f"farming:{info.pool.address}" in farming_anomalies
        fresh_usable = is_primary_farming_usable(info, anomaly=anomaly)
        stale_displayable = (
            info.is_stale
            and not anomaly
            and info.identity_valid is True
            and primary_yield is not None
            and isinstance(primary_yield.value_percent, Decimal)
            and primary_yield.value_percent.is_finite()
            and primary_yield.value_percent >= 0
        )
        if fresh_usable or stale_displayable:
            lines.append(translate(language, "farming.yield", value=primary_yield.value_percent))
        else:
            lines.append(translate(language, "farming.yield_unavailable"))
        if info.tvl_usd is not None:
            lines.append(translate(language, "farming.tvl", value=info.tvl_usd))
            displayed_tvl.append(info.tvl_usd)

    lines.append("")
    if not displayed_tvl:
        lines.append(translate(language, "farming.total_unavailable"))
    else:
        total = sum(displayed_tvl, Decimal("0"))
        key = "farming.total_complete" if len(displayed_tvl) == len(FARMING_POOLS) else "farming.total_partial"
        lines.append(translate(language, key, value=total))
    return "\n".join(lines)


def render_user_screen(
    screen: str,
    snapshot: DataSnapshot,
    *,
    invalid_pool_ids: set[str] | frozenset[str] = frozenset(),
    global_paused: bool = True,
    user_state: dict[str, Any] | None = None,
    now: datetime | None = None,
    language: str = "ru",
    pending_threshold: Any | None = None,
) -> str:
    if screen not in USER_SCREENS:
        raise ValueError(f"unknown user screen: {screen}")
    language = normalize_language(language)
    if screen == "language":
        return render_language_screen(language)
    if screen == "settings":
        screen = "alerts"
    if screen == "price":
        return render_price_text(snapshot.price, language=language)
    if screen == "farming":
        return render_farming_text(
            list(snapshot.farming.values()),
            invalid_pool_ids=invalid_pool_ids,
            anomalies=snapshot.anomalies,
            now=now,
            language=language,
        )
    state = user_state or {}
    pending_kind = pending_threshold.get("kind") if isinstance(pending_threshold, dict) else None
    if screen in {"alert_price", "alert_preview"} and (screen == "alert_price" or pending_kind == "price"):
        preview_value = pending_threshold.get("value") if isinstance(pending_threshold, dict) else None
        text = _price_alert_text(snapshot, state, language, pending_threshold=preview_value)
        if isinstance(pending_threshold, dict) and pending_threshold.get("changed"):
            text = f"{translate(language, 'alerts.preview_changed')}\n\n{text}"
        return text
    if screen in {"alert_yield", "alert_preview"} and (screen == "alert_yield" or pending_kind == "yield"):
        preview_value = pending_threshold.get("value") if isinstance(pending_threshold, dict) else None
        text = _yield_alert_text(snapshot, state, language, invalid_pool_ids, pending_threshold=preview_value)
        if isinstance(pending_threshold, dict) and pending_threshold.get("changed"):
            text = f"{translate(language, 'alerts.preview_changed')}\n\n{text}"
        return text
    if screen in {"alert_input", "alert_preview"}:
        return translate(language, "alerts.no_threshold")
    current_time = now or datetime.now(timezone.utc)
    paused_until = None
    if state.get("paused_until"):
        try:
            paused_until = datetime.fromisoformat(str(state["paused_until"]))
        except ValueError:
            paused_until = None
    is_paused = bool(
        paused_until
        and paused_until.tzinfo
        and current_time.tzinfo
        and current_time.astimezone(timezone.utc) < paused_until.astimezone(timezone.utc)
    )
    if is_paused:
        pause_text = translate(language, "alerts.paused", time=_format_time(paused_until, language))
    else:
        pause_text = translate(language, "alerts.unpaused")
    if screen == "alerts":
        yield_status = translate(language, "alerts.enabled" if state.get("farming_enabled") else "alerts.disabled")
        yield_threshold = _alert_threshold(language, state.get("farming_change_threshold_pp"), str(state.get("farming_direction", "both")), yield_alert=True)
        yield_direction = _alert_direction(language, str(state.get("farming_direction", "both")))
        price_status_key = "alerts.enabled" if state.get("price_enabled") else "alerts.disabled"
        price_status = translate(language, price_status_key)
        price_threshold = _alert_threshold(language, state.get("price_threshold_pct"), str(state.get("price_direction", "both")), yield_alert=False)
        price_direction = _alert_direction(language, str(state.get("price_direction", "both")))
        return "\n".join([
            f"<b>{translate(language, 'alerts.title')}</b>",
            "",
            translate(language, "alerts.yield_status", status=yield_status, threshold=yield_threshold, direction=yield_direction),
            translate(language, "alerts.price_status", status=price_status, threshold=price_threshold, direction=price_direction),
            pause_text,
            "",
            translate(language, "alerts.switch_legend"),
            translate(language, "alerts.unit_legend"),
        ])
    price = snapshot.price
    value = _format_usdt(price.price_usdt, language) if _price_has_displayable_value(price) else translate(language, "value.unavailable")
    stale = translate(language, "home.stale") if price.is_stale else ""
    return (
        f"<b>{translate(language, 'home.title')}</b>\n\n"
        f"JETTON: <b>{value} USDT</b>{stale}\n"
        f"{translate(language, 'common.updated')}: {_format_time(price.source_timestamp or snapshot.fetched_at, language)}\n\n"
        f"{translate(language, 'home.choose')}"
    )
