from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from farming import FARMING_POOLS, IdentityCheck, is_primary_farming_usable
from localization import normalize_language, translate
from managed_destinations import destination_connection_state
from snapshot import DataSnapshot
from user_ui import _format_time


ADMIN_SCREENS = frozenset({"home", "destinations", "destination", "push", "users", "status"})


def _time(value: datetime | None) -> str:
    return _format_time(value)


def _latest_destination_publication(destinations: list[dict[str, Any]]) -> datetime | None:
    latest: datetime | None = None
    for destination in destinations:
        raw = destination.get("last_publication_at")
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def _age_text(now: datetime, then: datetime | None) -> str:
    if then is None or now.tzinfo is None or then.tzinfo is None:
        return "неизвестно"
    seconds = max(0, int((now.astimezone(timezone.utc) - then.astimezone(timezone.utc)).total_seconds()))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


def _uptime(now: datetime, started_at: datetime | None) -> str:
    if started_at is None or now.tzinfo is None or started_at.tzinfo is None:
        return "неизвестно"
    total_minutes = max(0, int((now - started_at).total_seconds() // 60))
    return f"{total_minutes // 1440} д {total_minutes % 1440 // 60} ч {total_minutes % 60} мин"


def _identity_text(check: IdentityCheck | None) -> str:
    if check is None or check.valid is None:
        return "не подтверждена"
    return "OK" if check.valid else "ОШИБКА"


def _farming_source_state(snapshot: DataSnapshot, dex: str) -> str:
    pools = [pool for pool in FARMING_POOLS if pool.dex == dex]
    if not pools:
        return "error"

    statuses: list[str] = []
    for pool in pools:
        status = snapshot.source_statuses.get(f"farming:{pool.address}", "unknown")
        metrics = snapshot.farming.get(pool.address)
        usable = bool(
            metrics
            and is_primary_farming_usable(
                metrics,
                anomaly=bool(snapshot.anomalies.get(f"farming:{pool.address}")),
            )
        )
        if status not in {"ok", "partial"} or not usable:
            return "error"
        statuses.append(status)
    return "partial" if "partial" in statuses else "ok"


def render_admin_screen(
    screen: str,
    *,
    snapshot: DataSnapshot,
    global_paused: bool,
    subscriber_count: int,
    destinations: list[dict[str, Any]],
    destination: dict[str, Any] | None = None,
    bot_started_at: datetime | None,
    pool_identity_checks: dict[str, IdentityCheck],
    last_auto_alert_at: datetime | None,
    last_auto_publication_at: datetime | None,
    last_manual_push_at: datetime | None,
    now: datetime | None = None,
    chart_status: str = "MEXC Spot JETTON/USDT · spot OHLCV",
    language: str = "ru",
) -> str:
    if screen not in ADMIN_SCREENS:
        raise ValueError(f"unknown admin screen: {screen}")
    current = now or datetime.now(timezone.utc)
    language = normalize_language(language)

    def status_label(status: str) -> str:
        return translate(language, f"admin.status.source.{status}")

    destination_count = len(destinations)
    dedust_status = _farming_source_state(snapshot, "DeDust")
    ston_status = _farming_source_state(snapshot, "STON.fi")
    mexc_status = "ok" if snapshot.source_statuses.get("price") == "ok" else "error"

    if screen == "home":
        return (
            "<b>POOLMASTER ADMIN</b>\n\n"
            f"Пользователей: <b>{subscriber_count}</b>\n"
            f"{translate(language, 'admin.destinations.count')}: <b>{destination_count}</b>"
        )

    if screen == "users":
        return f"<b>Пользователи</b>\n\nВсего: <b>{subscriber_count}</b>"

    if screen == "destinations":
        lines = [f"<b>{translate(language, 'admin.destinations.title')}</b>", ""]
        if not destinations:
            lines.append(translate(language, "admin.destinations.empty"))
        for destination in destinations:
            name = html.escape(str(destination["name"]))
            connection_state = destination_connection_state(destination)
            connection_label = translate(language, f"admin.destination.state.{connection_state}")
            kind_key = "channel" if destination.get("type") == "channel" else "group"
            kind_label = translate(language, f"admin.destination.type.{kind_key}")
            interval = destination["farming_interval_hours"]
            schedule = (
                translate(language, "admin.destination.autopost_interval", hours=interval)
                if connection_state == "connected" and destination.get("enabled") and interval is not None
                else translate(language, "admin.destination.autopost_off")
            )
            last = destination.get("last_publication_at")
            try:
                last_time = datetime.fromisoformat(last) if last else None
            except ValueError:
                last_time = None
            lines.extend(
                [
                    f"<b>{name}</b>",
                    f"{kind_label} · {connection_label}",
                    f"{translate(language, 'admin.destination.autopost')}: {schedule}",
                    translate(language, "admin.destination.last_publication", time=_time(last_time)),
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    if screen == "destination":
        if destination is None:
            raise ValueError("destination detail screen requires a destination")
        name = html.escape(str(destination["name"]))
        connection_state = destination_connection_state(destination)
        connection_label = translate(language, f"admin.destination.state.{connection_state}")
        kind_key = "channel" if destination.get("type") == "channel" else "group"
        kind_label = translate(language, f"admin.destination.type.{kind_key}")
        schedule = destination.get("farming_interval_hours")
        schedule_text = (
            translate(language, "admin.destination.autopost_interval", hours=schedule)
            if connection_state == "connected" and destination.get("enabled") and schedule is not None
            else translate(language, "admin.destination.autopost_off")
        )
        last = destination.get("last_publication_at")
        try:
            last_time = datetime.fromisoformat(last) if last else None
        except ValueError:
            last_time = None
        return (
            f"<b>{name}</b>\n"
            f"{kind_label} · {connection_label}\n"
            f"{translate(language, 'admin.destination.autopost')}: <b>{schedule_text}</b>\n"
            f"{translate(language, 'admin.destination.last_publication', time=_time(last_time))}"
        )

    if screen == "push":
        return (
            "<b>Рассылка</b>\n\n"
            "Отправьте сообщение для рассылки.\n"
            "Можно отправить текст или фото с подписью."
        )

    pause = translate(language, "admin.status.paused" if global_paused else "admin.status.enabled")
    return (
        f"<b>{translate(language, 'admin.status.title')}</b>\n\n"
        f"{translate(language, 'admin.status.bot')}: <b>{translate(language, 'admin.status.running')}</b>\n"
        f"{translate(language, 'admin.status.users')}: <b>{subscriber_count}</b>\n"
        f"{translate(language, 'admin.status.destinations')}: <b>{destination_count}</b>\n"
        f"DeDust: <b>{status_label(dedust_status)}</b>\n"
        f"STON.fi: <b>{status_label(ston_status)}</b>\n"
        f"MEXC: <b>{status_label(mexc_status)}</b>\n"
        f"{translate(language, 'admin.status.alerts')}: <b>{pause}</b>\n"
        f"{translate(language, 'admin.status.uptime')}: <b>{_uptime(current, bot_started_at)}</b>"
    )
