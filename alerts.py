from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram.error import Forbidden, RetryAfter, TelegramError

from display_names import display_pair_name
from farming import PoolMetrics, SourceStatus, is_primary_farming_usable
from keyboards import private_alert_keyboard
from localization import normalize_language, translate
from snapshot import DataSnapshot
from user_state import UserAlertStore


@dataclass(frozen=True)
class AlertCandidate:
    user_id: int
    kind: str
    text: str
    fingerprint: str
    value: Decimal
    delta: Decimal
    pool_id: str | None = None
    metric_kind: str | None = None
    source_field: str | None = None
    language: str = "ru"
    configuration_revision: int = 0


@dataclass(frozen=True)
class DeliverySummary:
    sent: int = 0
    failed: int = 0


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _is_user_paused(user: dict[str, Any], now: datetime) -> bool:
    raw = user.get("paused_until")
    if not raw:
        return False
    try:
        paused_until = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    return (
        paused_until.tzinfo is not None
        and now.tzinfo is not None
        and now.astimezone(timezone.utc) < paused_until.astimezone(timezone.utc)
    )


def _yield_is_eligible(metrics: PoolMetrics, snapshot: DataSnapshot) -> bool:
    return is_primary_farming_usable(
        metrics,
        anomaly=f"farming:{metrics.pool.address}" in snapshot.anomalies,
    )


def _price_is_eligible(snapshot: DataSnapshot) -> bool:
    price = snapshot.price
    return (
        price.status == SourceStatus.OK
        and not price.is_stale
        and price.price_usdt is not None
        and "price" not in snapshot.anomalies
    )


def _normal_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _observation_identity(observed_at: datetime) -> str:
    """Identify one accepted market observation across alert retries."""
    return observed_at.astimezone(timezone.utc).isoformat()


def _price_cooldown_active(user: dict[str, Any], now: datetime, cooldown_seconds: int) -> bool:
    raw = user.get("price_last_alert_at")
    if not raw:
        return False
    try:
        sent_at = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if sent_at.tzinfo is None or now.tzinfo is None:
        return True
    return now.astimezone(timezone.utc) - sent_at.astimezone(timezone.utc) < timedelta(seconds=cooldown_seconds)


def evaluate_user_alerts(
    user_id: int,
    user: dict[str, Any],
    snapshot: DataSnapshot,
    *,
    now: datetime,
    price_cooldown_seconds: int,
    global_paused: bool = False,
) -> list[AlertCandidate]:
    if global_paused or now.tzinfo is None or _is_user_paused(user, now):
        return []

    candidates: list[AlertCandidate] = []
    language = normalize_language(user.get("language"))
    if user.get("farming_enabled"):
        threshold = _decimal(user.get("farming_change_threshold_pp"))
        last_notified = user.get("last_notified_yield", {})
        if threshold is not None and threshold > 0 and isinstance(last_notified, dict):
            for pool_id, metrics in snapshot.farming.items():
                if not _yield_is_eligible(metrics, snapshot) or pool_id not in last_notified:
                    continue
                metric = metrics.primary_farm_yield
                baseline = last_notified.get(pool_id)
                if (
                    metric is None
                    or not isinstance(baseline, dict)
                    or baseline.get("kind") != metric.kind
                    or baseline.get("source_field") != metric.source_field
                ):
                    continue
                previous = _decimal(baseline.get("value"))
                current = metric.value_percent
                if previous is None:
                    continue
                difference = current - previous
                direction = user.get("farming_direction", "both")
                if direction == "up" and difference <= 0:
                    continue
                if direction == "down" and difference >= 0:
                    continue
                delta = abs(difference)
                if delta < threshold:
                    continue
                sign = "+" if difference >= 0 else "−"
                fingerprint = (
                    f"yield:{pool_id}:{metric.kind}:{metric.source_field}:{_normal_decimal(current)}:"
                    f"{_observation_identity(metrics.fetched_at)}"
                )
                candidates.append(
                    AlertCandidate(
                        user_id=user_id,
                        kind="yield",
                        text=translate(
                            language,
                            "alert.yield",
                            dex=metrics.pool.dex,
                            pair=display_pair_name(metrics.pool),
                            previous=previous,
                            current=current,
                            sign=sign,
                            delta=delta,
                        ),
                        fingerprint=fingerprint,
                        value=current,
                        delta=difference,
                        pool_id=pool_id,
                        metric_kind=metric.kind,
                        source_field=metric.source_field,
                        language=language,
                        configuration_revision=int(user.get("yield_alert_revision", 0)),
                    )
                )

    if user.get("price_enabled") and _price_is_eligible(snapshot):
        threshold = _decimal(user.get("price_threshold_pct"))
        previous = _decimal(user.get("price_baseline"))
        current = snapshot.price.price_usdt
        if (
            threshold is not None
            and threshold > 0
            and previous is not None
            and previous > 0
            and current is not None
            and not _price_cooldown_active(user, now, price_cooldown_seconds)
        ):
            difference = current - previous
            relative_change = difference / previous * Decimal(100)
            direction = user.get("price_direction", "both")
            direction_matches = (
                direction == "both"
                or direction == "up" and relative_change > 0
                or direction == "down" and relative_change < 0
            )
            if direction_matches and abs(relative_change) >= threshold:
                sign = "+" if relative_change >= 0 else "−"
                fingerprint = (
                    f"price:{_normal_decimal(current)}:"
                    f"{_observation_identity(snapshot.price.fetched_at)}"
                )
                candidates.append(
                    AlertCandidate(
                        user_id=user_id,
                        kind="price",
                        text=translate(
                            language,
                            "alert.price",
                            previous=previous,
                            current=current,
                            sign=sign,
                            change=abs(relative_change),
                            source=snapshot.price.source,
                        ),
                        fingerprint=fingerprint,
                        value=current,
                        delta=relative_change,
                        language=language,
                        configuration_revision=int(user.get("price_alert_revision", 0)),
                    )
                )
    return candidates


async def _send_with_retry(bot: Any, candidate: AlertCandidate) -> None:
    for attempt in range(2):
        try:
            await bot.send_message(
                chat_id=candidate.user_id,
                text=candidate.text,
                reply_markup=private_alert_keyboard(candidate.language),
                disable_web_page_preview=True,
            )
            return
        except RetryAfter as exc:
            if attempt == 1:
                raise
            delay = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
            await asyncio.sleep(max(0.0, delay))


async def dispatch_user_alerts(
    store: UserAlertStore,
    bot: Any,
    snapshot: DataSnapshot,
    *,
    now: datetime,
    price_cooldown_seconds: int,
    global_paused: bool = False,
) -> DeliverySummary:
    if global_paused:
        return DeliverySummary()
    sent = 0
    failed = 0
    for user_id in store.list_active_user_ids():
        user = store.seed_missing_baselines(user_id, snapshot, now=now)
        candidates = evaluate_user_alerts(
            user_id,
            user,
            snapshot,
            now=now,
            price_cooldown_seconds=price_cooldown_seconds,
            global_paused=global_paused,
        )
        for candidate in candidates:
            if not store.alert_revision_matches(
                user_id, candidate.kind, candidate.configuration_revision
            ):
                continue
            if store.has_fingerprint(user_id, candidate.fingerprint):
                continue
            try:
                await _send_with_retry(bot, candidate)
            except Forbidden:
                logging.info("Private alert destination blocked the bot. user_id=%s", user_id)
                store.mark_blocked(user_id, observed_at=now)
                failed += 1
                break
            except (RetryAfter, TelegramError):
                logging.exception("Telegram rejected a private alert. user_id=%s kind=%s", user_id, candidate.kind)
                failed += 1
                continue
            except Exception:
                logging.exception("Private alert delivery failed. user_id=%s kind=%s", user_id, candidate.kind)
                failed += 1
                continue

            if candidate.kind == "yield" and candidate.pool_id is not None:
                store.record_yield_delivery(
                    user_id,
                    candidate.pool_id,
                    candidate.value,
                    candidate.metric_kind or "",
                    candidate.source_field or "",
                    candidate.fingerprint,
                    sent_at=now,
                    expected_revision=candidate.configuration_revision,
                )
            elif candidate.kind == "price":
                store.record_price_delivery(
                    user_id,
                    candidate.value,
                    candidate.fingerprint,
                    now,
                    expected_revision=candidate.configuration_revision,
                )
            sent += 1
    return DeliverySummary(sent=sent, failed=failed)
