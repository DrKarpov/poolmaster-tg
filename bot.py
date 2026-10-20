from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import re
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Coroutine, Mapping
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from config import load_settings
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ChatAdministratorRights,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from chart import (
    COMMAND_CHART_CACHE_MAX_AGE_SECONDS,
    PUBLIC_TIMEFRAMES,
    build_empty_chart,
    fetch_chart,
)
from alerts import DeliverySummary, dispatch_user_alerts
from admin_state import AdminStateStore
from admin_ui import render_admin_screen
from cooldowns import InMemoryCooldowns
from farming import (
    FARMING_POOLS,
    IdentityCheck,
    PoolMetrics,
    SourceStatus,
    fetch_all_farming_info,
    is_primary_farming_usable,
    verify_farming_pool_identities,
)
from farming_cache import FarmingStaleCache
from farming_history import FarmingHistoryStore
from keyboards import (
    admin_keyboard,
    farming_history_keyboard,
    group_response_keyboard,
    inline_price_refresh_keyboard,
    private_user_keyboard,
)
from farming_history_chart import build_farming_history_chart
from localization import normalize_language, translate
from managed_destinations import (
    ChannelPublicationCoordinator,
    ManagedDestinationStore,
    dispatch_managed_publications,
    destination_connection_state,
    farming_publication_card,
    membership_can_publish,
    verify_managed_destination,
)
from inline_mode import INLINE_CACHE_SECONDS, build_inline_price_text, build_inline_results
from manual_push import ManualPushCoordinator
from price import PriceIdentityCheck, fetch_price_metrics, verify_mexc_market_identity
from snapshot import DataSnapshot, SnapshotManager
from state_backup import secure_backup_file
from user_state import UserAlertStore
from user_ui import (
    USER_SCREENS,
    private_callback_is_owned,
    render_farming_text,
    render_language_prompt,
    render_language_screen,
    render_user_screen,
)

FAST_CALLBACK_COOLDOWN_SECONDS = 1
CHART_COMMAND_COOLDOWN_SECONDS = 10


def _alert_snapshot_ready(kind: str, snapshot: Any, invalid_pool_ids: set[str]) -> bool:
    if kind == "price":
        price = snapshot.price
        return (
            price.status == SourceStatus.OK
            and not price.is_stale
            and price.price_usdt is not None
            and "price" not in snapshot.anomalies
        )
    if kind == "yield":
        return any(
            is_primary_farming_usable(
                metrics,
                anomaly=f"farming:{pool_id}" in snapshot.anomalies,
            )
            and pool_id not in invalid_pool_ids
            for pool_id, metrics in snapshot.farming.items()
        )
    return False


def _alert_reference_signature(kind: str, snapshot: Any, invalid_pool_ids: set[str]) -> dict[str, Any]:
    if kind == "price":
        return {"price": str(snapshot.price.price_usdt)}
    if kind == "yield":
        return {
            pool_id: {
                "value": str(metrics.primary_farm_yield.value_percent),
                "kind": metrics.primary_farm_yield.kind,
                "source_field": metrics.primary_farm_yield.source_field,
            }
            for pool_id, metrics in snapshot.farming.items()
            if is_primary_farming_usable(
                metrics,
                anomaly=f"farming:{pool_id}" in snapshot.anomalies,
            )
            and pool_id not in invalid_pool_ids
        }
    return {}


def _alert_snapshot_is_recent(kind: str, snapshot: Any, max_age_seconds: int) -> bool:
    now = datetime.now(timezone.utc)
    if kind == "price":
        timestamps = [snapshot.price.fetched_at]
    elif kind == "yield":
        timestamps = [
            metrics.fetched_at
            for pool_id, metrics in snapshot.farming.items()
            if is_primary_farming_usable(
                metrics,
                anomaly=f"farming:{pool_id}" in snapshot.anomalies,
            )
        ]
    else:
        return False
    return bool(timestamps) and all(
        timestamp.tzinfo is not None
        and timestamp.utcoffset() is not None
        and 0 <= (now - timestamp.astimezone(timezone.utc)).total_seconds() <= max_age_seconds
        for timestamp in timestamps
    )


def _parse_alert_threshold(raw: str, kind: str) -> Decimal | None:
    candidate = raw.strip().replace(",", ".")
    try:
        value = Decimal(candidate)
    except (InvalidOperation, ValueError):
        return None
    maximum = Decimal("99.99") if kind == "price" else Decimal("10000")
    return value if value.is_finite() and Decimal("0.01") <= value <= maximum else None


class PoolmasterApplication(Application):
    """PTB application that owns POOLMASTER's long-running background loops."""

    def __init__(
        self,
        *,
        background_task_factory: Callable[
            [Application], Iterable[tuple[str, Coroutine[Any, Any, Any]]]
        ],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._background_task_factory = background_task_factory
        self._background_tasks: list[asyncio.Task[Any]] = []

    async def start(self) -> None:
        await super().start()
        for name, coroutine in self._background_task_factory(self):
            self._background_tasks.append(
                self.create_task(coroutine, name=f"poolmaster:{name}")
            )

    async def stop(self) -> None:
        tasks = tuple(self._background_tasks)
        self._background_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().stop()


async def telegram_call_with_retry(operation: Any, attempts: int = 2) -> Any:
    for attempt in range(attempts):
        try:
            return await operation()
        except RetryAfter as exc:
            if attempt + 1 >= attempts:
                raise
            retry_after = exc.retry_after
            delay = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
            delay += 0.5
            logging.warning("Telegram flood control exceeded. Retrying in %.1f seconds", delay)
            await asyncio.sleep(delay)
    return None


async def telegram_read_call_with_retry(operation: Any, attempts: int = 2) -> Any:
    """Retry bounded, idempotent Telegram reads after transient network errors."""
    for attempt in range(attempts):
        try:
            return await operation()
        except NetworkError as exc:
            if attempt + 1 >= attempts:
                raise
            logging.warning(
                "Telegram channel validation read failed with %s; retrying once",
                type(exc).__name__,
            )
            await asyncio.sleep(0.5)
    return None


def quarantine_corrupt_json(path: Path) -> Path | None:
    """Preserve malformed state privately before a store writes fresh JSON."""
    if not path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(f"{path.stem}.corrupt.{timestamp}{path.suffix}")
    try:
        secure_backup_file(path, backup)
        path.unlink()
    except OSError:
        logging.exception("Could not quarantine corrupt JSON state file %s", path.name)
        raise
    logging.error("Corrupt JSON state preserved as %s", backup.name)
    return backup


class CommandStatsStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"users": {}, "events": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("users", {}), dict)
                or not isinstance(payload.get("events", []), list)
            ):
                raise ValueError("command stats state schema is invalid")
            return payload
        except (json.JSONDecodeError, ValueError):
            quarantine_corrupt_json(self.path)
            logging.exception("Failed to read command stats store")
            return {"users": {}, "events": []}

    def save(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data)

    def track(self, update: Update, command: str) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if not user:
            return

        with self.lock:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            data = self.load()
            users = data.setdefault("users", {})
            user_key = str(user.id)
            user_data = users.setdefault(
                user_key,
                {
                    "id": user.id,
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "chat_id": chat.id if chat else None,
                    "chat_type": chat.type if chat else None,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "total_commands": 0,
                    "commands": {},
                },
            )
            user_data.update(
                {
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "chat_id": chat.id if chat else user_data.get("chat_id"),
                    "chat_type": chat.type if chat else user_data.get("chat_type"),
                    "last_seen_at": now,
                }
            )
            user_data["total_commands"] = int(user_data.get("total_commands", 0)) + 1

            commands = user_data.setdefault("commands", {})
            command_data = commands.setdefault(command, {"count": 0, "first_used_at": now, "last_used_at": now})
            command_data["count"] = int(command_data.get("count", 0)) + 1
            command_data["last_used_at"] = now

            events = data.setdefault("events", [])
            events.append(
                {
                    "at": now,
                    "command": command,
                    "user_id": user.id,
                    "username": user.username,
                    "chat_id": chat.id if chat else None,
                    "chat_type": chat.type if chat else None,
                }
            )
            data["events"] = events[-1000:]
            self.save(data)

    def summary(self) -> dict[str, Any]:
        with self.lock:
            data = self.load()
            users = list(data.get("users", {}).values())
            users.sort(key=lambda item: item.get("last_seen_at", ""), reverse=True)
            return {"users": users, "events": data.get("events", [])}


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


class PoolmasterBot:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.started_at = datetime.now(timezone.utc)
        self.token = self.settings.telegram_bot_token
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

        self.admin_ids = set(self.settings.admin_user_ids)
        self.cooldowns = InMemoryCooldowns()
        self.admin_state_store = AdminStateStore(
            self.settings.admin_state_path,
            initial_paused=self.settings.global_auto_push_paused,
        )
        self.user_alert_store = UserAlertStore(self.settings.user_state_path)
        self.user_alert_store.migrate_legacy_private_users(
            self.settings.subscribers_path,
            self.settings.stats_path,
        )
        self.managed_destination_store = ManagedDestinationStore(self.settings.managed_destinations_path)
        self.channel_publication_coordinator = ChannelPublicationCoordinator()
        self.manual_push_coordinator = ManualPushCoordinator()
        self.stats_store = CommandStatsStore(self.settings.stats_path)
        self.bot_username = self.settings.telegram_bot_username
        self.bot_id: int | None = None
        self.delete_command_messages = self.settings.delete_command_messages
        self.pool_identity_checks: dict[str, IdentityCheck] = {}
        self.invalid_pool_ids: set[str] = set()
        self.farming_cache = FarmingStaleCache(
            self.settings.farming_stale_max_age_seconds,
            self.settings.farming_cache_path,
        )
        self.farming_history_store: FarmingHistoryStore | None = None
        try:
            self.farming_history_store = FarmingHistoryStore(self.settings.farming_history_path)
        except Exception:
            logging.exception("Farming history storage is unavailable; continuing without history collection")
        else:
            history_seed_path = Path(__file__).resolve().parent / "resources" / "farming_history_seed.csv"
            try:
                seeded_history_rows = self.farming_history_store.seed_csv(history_seed_path)
                logging.info("Farming history seed imported rows=%s", seeded_history_rows)
            except Exception:
                logging.exception("Farming history seed import failed; continuing with live collection")
        self.price_identity_check: PriceIdentityCheck | None = None
        self.snapshot_manager = SnapshotManager(
            self.fetch_farming_data,
            fetch_price=partial(
                fetch_price_metrics,
                max_source_age_seconds=self.settings.price_source_max_age_seconds,
            ),
            snapshot_path=self.settings.snapshot_cache_path,
            farming_cache=self.farming_cache,
            farming_history_store=self.farming_history_store,
            farming_stale_max_age_seconds=self.settings.farming_stale_max_age_seconds,
            price_stale_max_age_seconds=self.settings.price_stale_max_age_seconds,
            freshness_seconds=self.settings.price_poll_interval_seconds,
            farming_freshness_seconds=self.settings.farming_poll_interval_seconds,
            price_freshness_seconds=self.settings.price_poll_interval_seconds,
            apr_anomaly_confirm_threshold=self.settings.apr_anomaly_confirm_threshold,
            price_anomaly_confirm_threshold=self.settings.price_anomaly_confirm_threshold,
            price_identity_valid=None,
            verify_price_identity=self.check_price_identity,
        )

    async def initialize_pool_identity_checks(self) -> None:
        self.pool_identity_checks = await verify_farming_pool_identities()
        self.invalid_pool_ids = set()
        for pool in FARMING_POOLS:
            check = self.pool_identity_checks.get(
                pool.address,
                IdentityCheck(None, "identity check returned no result"),
            )
            if check.valid is False:
                self.invalid_pool_ids.add(pool.address)
                self.snapshot_manager.set_farming_identity(pool.address, False)
                logging.critical(
                    "Farming pool identity mismatch. dex=%s pool=%s detail=%s",
                    pool.dex,
                    pool.address,
                    check.detail,
                )
            elif check.valid is None:
                logging.error(
                    "Farming pool identity could not be verified. dex=%s pool=%s detail=%s",
                    pool.dex,
                    pool.address,
                    check.detail,
                )
            else:
                logging.info("Farming pool identity verified. dex=%s pool=%s", pool.dex, pool.address)
        self.snapshot_manager.invalidate_freshness()

    def apply_pool_identity_gate(self, pool_infos: list[PoolMetrics]) -> list[PoolMetrics]:
        invalid_pool_ids = getattr(self, "invalid_pool_ids", set())
        return [
            replace(
                info,
                status=SourceStatus.INVALID_RESPONSE,
                identity_valid=False,
                status_detail="startup pool identity check failed",
            )
            if info.pool.address in invalid_pool_ids
            else info
            for info in pool_infos
        ]

    async def fetch_farming_data(self) -> list[PoolMetrics]:
        return self.apply_pool_identity_gate(await fetch_all_farming_info())

    async def check_price_identity(self) -> bool | None:
        self.price_identity_check = await verify_mexc_market_identity()
        if self.price_identity_check.valid is False:
            logging.critical("Canonical price market identity mismatch: %s", self.price_identity_check.detail)
        elif self.price_identity_check.valid is None:
            logging.error("Canonical price market identity could not be verified: %s", self.price_identity_check.detail)
        else:
            logging.info("Canonical price market identity verified: %s", self.price_identity_check.detail)
        return self.price_identity_check.valid

    async def fetch_verified_mexc_chart(
        self,
        timeframe: str,
        *,
        force_refresh: bool = False,
        max_cache_age_seconds: int | None = None,
        language: str = "ru",
    ) -> Any:
        language = normalize_language(language)
        identity_check = self.price_identity_check
        if identity_check is None or identity_check.valid is not True:
            verification = "unavailable" if identity_check is None else identity_check.valid
            logging.error("Withholding MEXC chart because market identity is not verified: %s", verification)
            return build_empty_chart(
                translate(language, "chart.identity_failed"),
                language=language,
            )
        if force_refresh:
            if language == "ru":
                return await fetch_chart("mexc", timeframe, force_refresh=True)
            return await fetch_chart("mexc", timeframe, force_refresh=True, language=language)
        cache_options = (
            {"max_cache_age_seconds": max_cache_age_seconds}
            if max_cache_age_seconds is not None
            else {}
        )
        if language == "ru":
            return await fetch_chart("mexc", timeframe, **cache_options)
        return await fetch_chart("mexc", timeframe, language=language, **cache_options)

    def farming_keyboard(self) -> InlineKeyboardMarkup:
        return group_response_keyboard()

    def _allow_fast_callback(self, query: Any) -> bool:
        user = getattr(query, "from_user", None)
        data = getattr(query, "data", None)
        if user is None or data is None:
            return False
        return self.cooldowns.allow(
            ("callback", user.id, data),
            FAST_CALLBACK_COOLDOWN_SECONDS,
        )

    def _allow_group_refresh(self, chat: Any) -> bool:
        if getattr(chat, "type", None) not in {"group", "supergroup"}:
            return True
        return self.cooldowns.allow(
            ("group_refresh", chat.id),
            self.settings.ui_refresh_cooldown_seconds,
        )

    def chart_keyboard(
        self,
        active_market: str,
        active_timeframe: str,
        *,
        language: str = "ru",
    ) -> InlineKeyboardMarkup:
        language = normalize_language(language)
        timeframe_buttons = [
            InlineKeyboardButton(
                f"• {translate(language, f'chart.button.{code}')}" if code == active_timeframe else translate(language, f"chart.button.{code}"),
                callback_data=f"chart:{active_market}:{code}",
            )
            for code in PUBLIC_TIMEFRAMES
        ]
        return InlineKeyboardMarkup(
            [
                timeframe_buttons,
                [
                    InlineKeyboardButton(
                        translate(language, "chart.button.refresh"),
                        callback_data=f"chart:{active_market}:refresh:{active_timeframe}",
                    )
                ],
                [InlineKeyboardButton(translate(language, "chart.button.back"), callback_data="chart:back", style="danger")],
            ]
        )

    async def delete_command_message(self, update: Update) -> None:
        chat = update.effective_chat
        message = update.effective_message
        if not self.delete_command_messages or not chat or not message:
            return
        if chat.type not in {"group", "supergroup"}:
            return
        try:
            await telegram_call_with_retry(message.delete)
        except BadRequest as exc:
            logging.info("Could not delete command message in chat %s: %s", chat.id, exc)
        except TelegramError:
            logging.exception("Failed to delete command message in chat %s", chat.id)

    def build_farming_text(
        self,
        pool_infos: list[PoolMetrics],
        *,
        anomalies: Mapping[str, str] | None = None,
    ) -> str:
        return render_farming_text(
            pool_infos,
            invalid_pool_ids=getattr(self, "invalid_pool_ids", set()),
            anomalies=anomalies,
        )

    async def send_language_prompt(self, message: Any) -> None:
        await telegram_call_with_retry(
            lambda: message.reply_text(
                render_language_prompt(),
                reply_markup=private_user_keyboard("language"),
                disable_web_page_preview=True,
            )
        )

    async def send_user_screen(self, update: Update, screen: str) -> None:
        message = update.effective_message
        if not message:
            return
        user = getattr(update, "effective_user", None)
        user_state = self.user_alert_store.ensure_user(user.id) if user and user.id > 0 else None
        if user_state is None:
            return
        language = user_state.get("language")
        if language is None:
            await self.send_language_prompt(message)
            return
        language = normalize_language(language)
        snapshot = await self.snapshot_manager.get_snapshot()
        if user_state is not None:
            user_state = self.user_alert_store.seed_missing_baselines(user.id, snapshot)
        is_paused = self.user_alert_store.is_user_paused(user_state or {})
        await telegram_call_with_retry(
            lambda: message.reply_text(
                render_user_screen(
                    screen,
                    snapshot,
                    invalid_pool_ids=self.invalid_pool_ids,
                    global_paused=self.admin_state_store.is_global_auto_push_paused(),
                    user_state=user_state,
                    language=language,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=private_user_keyboard(
                    screen, is_paused=is_paused, language=language, user_state=user_state
                ),
                disable_web_page_preview=True,
            )
        )

    async def edit_private_alert_screen(
        self,
        query: Any,
        screen: str,
        snapshot: Any,
        user_state: dict[str, Any],
        language: str,
        *,
        pending_threshold: dict[str, Any] | None = None,
    ) -> None:
        user_id = query.from_user.id
        user_state = self.user_alert_store.seed_missing_baselines(user_id, snapshot)
        is_paused = self.user_alert_store.is_user_paused(user_state)
        await query.edit_message_text(
            render_user_screen(
                screen,
                snapshot,
                invalid_pool_ids=self.invalid_pool_ids,
                global_paused=self.admin_state_store.is_global_auto_push_paused(),
                user_state=user_state,
                language=language,
                pending_threshold=pending_threshold,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=private_user_keyboard(
                screen,
                is_paused=is_paused,
                language=language,
                user_state=user_state,
                pending_alert_kind=(pending_threshold or {}).get("kind"),
                enable_on_save=bool((pending_threshold or {}).get("enable_on_save", True)),
            ),
            disable_web_page_preview=True,
        )

    async def send_private_farming_history_photo(
        self,
        query: Any,
        context: ContextTypes.DEFAULT_TYPE,
        language: str,
    ) -> None:
        message = query.message
        if message is None:
            return
        if self.farming_history_store is None:
            raise RuntimeError("Farming history storage is unavailable")
        await self.snapshot_manager.get_snapshot()
        if getattr(self.snapshot_manager, "last_history_write_error", None) is not None:
            await telegram_call_with_retry(
                lambda: context.bot.send_message(
                    chat_id=message.chat.id,
                    text=translate(language, "farming_history.error.persistence"),
                    reply_markup=farming_history_keyboard(language),
                )
            )
            try:
                await telegram_call_with_retry(message.delete)
            except TelegramError:
                logging.exception("Could not remove the previous Farming text after history write failure")
            return
        as_of = datetime.now(timezone.utc)
        chart_result = build_farming_history_chart(
            self.farming_history_store.get_summary(as_of=as_of),
            language=language,
            as_of=as_of,
        )
        await telegram_call_with_retry(
            lambda: context.bot.send_photo(
                chat_id=message.chat.id,
                photo=chart_result.image,
                caption=chart_result.caption,
                parse_mode=ParseMode.HTML,
                reply_markup=farming_history_keyboard(language),
            )
        )
        try:
            await telegram_call_with_retry(message.delete)
        except TelegramError:
            logging.exception("Could not remove the previous Farming text after opening history")

    async def restore_farming_after_history(
        self,
        query: Any,
        context: ContextTypes.DEFAULT_TYPE,
        language: str,
    ) -> None:
        message = query.message
        if message is None:
            return
        user_id = query.from_user.id
        snapshot = await self.snapshot_manager.get_snapshot()
        user_state = self.user_alert_store.seed_missing_baselines(user_id, snapshot)
        text = render_user_screen(
            "farming",
            snapshot,
            invalid_pool_ids=self.invalid_pool_ids,
            global_paused=self.admin_state_store.is_global_auto_push_paused(),
            user_state=user_state,
            language=language,
        )
        markup = private_user_keyboard("farming", language=language, user_state=user_state)
        await telegram_call_with_retry(message.delete)
        await telegram_call_with_retry(
            lambda: context.bot.send_message(
                chat_id=message.chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        )

    def register_private_interaction(self, update: Update) -> None:
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        if user and chat and chat.type == "private" and chat.id == user.id:
            self.user_alert_store.reactivate_user(user)

    async def send_chart_reply(
        self,
        message: Any,
        timeframe: str = "1h",
        *,
        language: str = "ru",
        include_keyboard: bool = True,
    ) -> None:
        language = normalize_language(language)
        chart_options: dict[str, Any] = {
            "max_cache_age_seconds": COMMAND_CHART_CACHE_MAX_AGE_SECONDS,
        }
        if language != "ru":
            chart_options["language"] = language
        chart = await self.fetch_verified_mexc_chart(timeframe, **chart_options)
        await telegram_call_with_retry(
            lambda: message.reply_photo(
                photo=chart.image,
                caption=chart.caption,
                parse_mode=ParseMode.HTML,
                reply_markup=(
                    self.chart_keyboard("mexc", timeframe, language=language)
                    if include_keyboard
                    else None
                ),
            )
        )

    async def price(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return
        await self.delete_command_message(update)
        if not self._allow_group_refresh(chat):
            return
        self.stats_store.track(update, "price")
        if chat.type == "private":
            self.register_private_interaction(update)
            user_state = self.user_alert_store.ensure_user(update.effective_user.id)
            if user_state.get("language") is None:
                await self.send_language_prompt(message)
                return
            await self.send_chart_reply(message, "1h", language=user_state.get("language", "ru"))
            return
        await self.send_chart_reply(message, "1h", include_keyboard=False)

    async def settings(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return
        await self.delete_command_message(update)
        self.stats_store.track(update, "settings")
        if chat.type != "private":
            await telegram_call_with_retry(
                lambda: message.reply_text("Личные настройки доступны только в личном чате с POOLMASTER.")
            )
            return
        self.register_private_interaction(update)
        await self.send_user_screen(update, "alerts")

    async def user_ui_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        user_id = query.from_user.id
        current_state = self.user_alert_store.get_user(user_id)
        language = normalize_language(current_state.get("language"))
        if not private_callback_is_owned(query):
            await query.answer(translate(language, "callback.owner"), show_alert=True)
            return
        user_state = self.user_alert_store.reactivate_user(query.from_user)
        data = query.data or ""

        if data == "ui:language":
            await query.answer()
            if query.message:
                await query.edit_message_text(
                    render_language_screen(language),
                    parse_mode=ParseMode.HTML,
                    reply_markup=private_user_keyboard("language", language=language),
                )
            return
        if data.startswith("ui:language:set:"):
            selected_language = data.removeprefix("ui:language:set:")
            if selected_language not in {"ru", "en"}:
                await query.answer(translate(language, "callback.unknown_setting"), show_alert=True)
                return
            user_state = self.user_alert_store.set_language(user_id, selected_language)
            language = normalize_language(selected_language)
            if user_id not in self.admin_ids:
                await self.set_user_command_scope(context.bot, user_id, language)
            await query.answer(translate(language, "language.saved"))
            if query.message:
                snapshot = await self.snapshot_manager.get_snapshot()
                user_state = self.user_alert_store.seed_missing_baselines(user_id, snapshot)
                await query.edit_message_text(
                    render_user_screen(
                        "home",
                        snapshot,
                        invalid_pool_ids=self.invalid_pool_ids,
                        global_paused=self.admin_state_store.is_global_auto_push_paused(),
                        user_state=user_state,
                        language=language,
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=private_user_keyboard("home", language=language),
                    disable_web_page_preview=True,
                )
            return
        if user_state.get("language") is None:
            await query.answer(render_language_prompt())
            if query.message:
                await query.edit_message_text(
                    render_language_prompt(),
                    reply_markup=private_user_keyboard("language"),
                    disable_web_page_preview=True,
                )
            return
        language = normalize_language(user_state.get("language"))

        force = data.startswith("ui:refresh:")
        is_chart_refresh = data == "ui:refresh:price"
        if force or is_chart_refresh:
            refresh_scope = "price" if is_chart_refresh else data.removeprefix("ui:refresh:")
            if refresh_scope not in {"farming", "price", "alert_price", "alert_yield"}:
                await query.answer(translate(language, "callback.unknown_refresh"), show_alert=True)
                return
            if not self.cooldowns.allow(
                ("user_refresh", user_id, refresh_scope),
                self.settings.ui_refresh_cooldown_seconds,
            ):
                await query.answer(translate(language, "callback.cooldown"), show_alert=False)
                return
        elif not self._allow_fast_callback(query):
            await query.answer(translate(language, "callback.busy"), show_alert=False)
            return

        if data == "ui:farming:history":
            await query.answer()
            try:
                await self.send_private_farming_history_photo(query, context, language)
            except TelegramError:
                logging.exception("Telegram rejected the private farming history photo")
                if query.message:
                    await telegram_call_with_retry(
                        lambda: context.bot.send_message(
                            chat_id=query.message.chat.id,
                            text=translate(language, "farming_history.error.telegram"),
                            reply_markup=private_user_keyboard("farming", language=language),
                        )
                    )
            except Exception:
                logging.exception("Could not open private farming history")
                if query.message:
                    await telegram_call_with_retry(
                        lambda: context.bot.send_message(
                            chat_id=query.message.chat.id,
                            text=translate(language, "farming_history.error.open"),
                            reply_markup=private_user_keyboard("farming", language=language),
                        )
                    )
            return

        if data == "ui:farming:history:back":
            await query.answer()
            try:
                await self.restore_farming_after_history(query, context, language)
            except TelegramError:
                logging.exception("Telegram rejected the private Farming screen after history")
                if query.message:
                    await telegram_call_with_retry(
                        lambda: context.bot.send_message(
                            chat_id=query.message.chat.id,
                            text=translate(language, "farming_history.error.telegram"),
                            reply_markup=private_user_keyboard("farming", language=language),
                        )
                    )
            except Exception:
                logging.exception("Could not restore the private Farming screen after history")
                if query.message:
                    await telegram_call_with_retry(
                        lambda: context.bot.send_message(
                            chat_id=query.message.chat.id,
                            text=translate(language, "farming_history.error.open"),
                            reply_markup=private_user_keyboard("farming", language=language),
                        )
                    )
            return

        if data in {"ui:price", "ui:chart", "ui:refresh:price"}:
            if is_chart_refresh:
                await query.answer(translate(language, "callback.chart_refreshing"))
            else:
                await query.answer()
            try:
                if is_chart_refresh:
                    if language == "ru":
                        chart = await self.fetch_verified_mexc_chart("1h", force_refresh=True)
                    else:
                        chart = await self.fetch_verified_mexc_chart("1h", force_refresh=True, language=language)
                else:
                    chart = await self.fetch_verified_mexc_chart("1h") if language == "ru" else await self.fetch_verified_mexc_chart("1h", language=language)
                if not query.message:
                    return
                await telegram_call_with_retry(query.message.delete)
                await telegram_call_with_retry(
                    lambda: context.bot.send_photo(
                        chat_id=query.message.chat.id,
                        photo=chart.image,
                        caption=chart.caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=self.chart_keyboard("mexc", "1h", language=language),
                    )
                )
            except TelegramError:
                logging.exception("Failed to replace private price screen with chart")
                if query.message and context and context.bot:
                    await telegram_call_with_retry(
                        lambda: context.bot.send_message(
                            chat_id=query.message.chat.id,
                            text=translate(language, "callback.chart_failed"),
                            reply_markup=private_user_keyboard("home", language=language),
                        )
                    )
            return
        if data == "ui:alert:cancel":
            if context.user_data is not None:
                context.user_data.pop("poolmaster_alert_pending", None)
            await query.answer(translate(language, "alerts.input_cancelled"))
            snapshot = await self.snapshot_manager.get_snapshot()
            user_state = self.user_alert_store.seed_missing_baselines(user_id, snapshot)
            await self.edit_private_alert_screen(query, "alerts", snapshot, user_state, language)
            return

        if data.startswith("ui:alert:"):
            parts = data.split(":")
            if len(parts) >= 3 and parts[2] in {"price", "yield"}:
                kind = parts[2]
                detail_screen = "alert_price" if kind == "price" else "alert_yield"

                if len(parts) == 3:
                    await query.answer(translate(language, "callback.updating"))
                    snapshot = (
                        await self.snapshot_manager.refresh_price()
                        if kind == "price"
                        else await self.snapshot_manager.refresh_farming()
                    )
                    user_state = self.user_alert_store.seed_missing_baselines(user_id, snapshot)
                    await self.edit_private_alert_screen(query, detail_screen, snapshot, user_state, language)
                    return

                if len(parts) == 4 and parts[3] == "input":
                    await query.answer()
                    prompt_key = "alerts.input_price" if kind == "price" else "alerts.input_yield"
                    if context.user_data is not None:
                        context.user_data["poolmaster_alert_pending"] = {
                            "kind": kind,
                            "phase": "input",
                            "enable_on_save": bool(
                                user_state.get("price_enabled" if kind == "price" else "farming_enabled")
                                or user_state.get("price_threshold_pct" if kind == "price" else "farming_change_threshold_pp") is None
                            ),
                            "chat_id": query.message.chat.id if query.message else user_id,
                            "message_id": query.message.message_id if query.message else None,
                        }
                    if query.message:
                        await query.edit_message_text(
                            translate(language, prompt_key),
                            reply_markup=private_user_keyboard("alert_input", language=language),
                            disable_web_page_preview=True,
                        )
                    return

                if len(parts) == 4 and parts[3] == "toggle":
                    enabled_field = "price_enabled" if kind == "price" else "farming_enabled"
                    threshold_field = "price_threshold_pct" if kind == "price" else "farming_change_threshold_pp"
                    currently_enabled = bool(user_state.get(enabled_field))
                    if currently_enabled:
                        self.user_alert_store.set_alert_enabled(user_id, kind, False)
                        user_state = self.user_alert_store.get_user(user_id)
                        await query.answer()
                        snapshot = await self.snapshot_manager.get_snapshot()
                        await self.edit_private_alert_screen(query, "alerts", snapshot, user_state, language)
                        return
                    if user_state.get(threshold_field) is None:
                        await query.answer(translate(language, "callback.updating"))
                        snapshot = (
                            await self.snapshot_manager.refresh_price()
                            if kind == "price"
                            else await self.snapshot_manager.refresh_farming()
                        )
                        user_state = self.user_alert_store.seed_missing_baselines(user_id, snapshot)
                        await self.edit_private_alert_screen(query, detail_screen, snapshot, user_state, language)
                        return
                    await query.answer(translate(language, "callback.updating"))
                    snapshot = (
                        await self.snapshot_manager.refresh_price()
                        if kind == "price"
                        else await self.snapshot_manager.refresh_farming()
                    )
                    if not _alert_snapshot_ready(kind, snapshot, self.invalid_pool_ids):
                        key = "alerts.price_data_required" if kind == "price" else "alerts.yield_data_required"
                        if query.message:
                            await query.message.reply_text(translate(language, key))
                        return
                    user_state = self.user_alert_store.set_alert_enabled(user_id, kind, True, snapshot)
                    await self.edit_private_alert_screen(query, "alerts", snapshot, user_state, language)
                    return

                if len(parts) == 5 and parts[3] == "direction" and parts[4] in {"up", "down", "both"}:
                    current_direction = user_state.get("price_direction" if kind == "price" else "farming_direction", "both")
                    await query.answer(translate(language, "callback.updating"))
                    snapshot = (
                        await self.snapshot_manager.refresh_price()
                        if kind == "price"
                        else await self.snapshot_manager.refresh_farming()
                    )
                    if current_direction != parts[4]:
                        if not _alert_snapshot_ready(kind, snapshot, self.invalid_pool_ids):
                            key = "alerts.price_data_required" if kind == "price" else "alerts.yield_data_required"
                            if query.message:
                                await query.message.reply_text(translate(language, key))
                            return
                        user_state = self.user_alert_store.set_alert_direction(user_id, kind, parts[4], snapshot)
                    await self.edit_private_alert_screen(query, detail_screen, snapshot, user_state, language)
                    return

            if len(parts) == 4 and parts[2] == "save" and parts[3] in {"price", "yield"}:
                pending = (context.user_data or {}).get("poolmaster_alert_pending")
                kind = parts[3]
                if not isinstance(pending, dict) or pending.get("kind") != kind or pending.get("phase") != "preview":
                    await query.answer(translate(language, "callback.unknown_setting"), show_alert=True)
                    return
                threshold = _parse_alert_threshold(str(pending.get("value", "")), kind)
                if threshold is None:
                    await query.answer(translate(language, "alerts.input_invalid"), show_alert=True)
                    return
                await query.answer(translate(language, "callback.updating"))
                preview_snapshot = pending.get("preview_snapshot")
                max_preview_age = max(
                    30,
                    self.settings.price_poll_interval_seconds if kind == "price" else self.settings.farming_poll_interval_seconds,
                )
                if (
                    isinstance(preview_snapshot, DataSnapshot)
                    and _alert_snapshot_ready(kind, preview_snapshot, self.invalid_pool_ids)
                    and _alert_snapshot_is_recent(kind, preview_snapshot, max_preview_age)
                ):
                    snapshot = preview_snapshot
                else:
                    snapshot = (
                        await self.snapshot_manager.refresh_price()
                        if kind == "price"
                        else await self.snapshot_manager.refresh_farming()
                    )
                if not _alert_snapshot_ready(kind, snapshot, self.invalid_pool_ids):
                    key = "alerts.price_data_required" if kind == "price" else "alerts.yield_data_required"
                    if query.message:
                        await query.message.reply_text(translate(language, key))
                    return
                signature = _alert_reference_signature(kind, snapshot, self.invalid_pool_ids)
                if pending.get("reference_signature") != signature:
                    pending["reference_signature"] = signature
                    pending["preview_snapshot"] = snapshot
                    pending["changed"] = True
                    if context.user_data is not None:
                        context.user_data["poolmaster_alert_pending"] = pending
                    user_state = self.user_alert_store.get_user(user_id)
                    await self.edit_private_alert_screen(
                        query,
                        "alert_preview",
                        snapshot,
                        user_state,
                        language,
                        pending_threshold=pending,
                    )
                    return
                user_state = (
                    self.user_alert_store.set_price_threshold(
                        user_id, threshold, snapshot, enabled=bool(pending.get("enable_on_save", True))
                    )
                    if kind == "price"
                    else self.user_alert_store.set_farming_threshold(
                        user_id, threshold, snapshot, enabled=bool(pending.get("enable_on_save", True))
                    )
                )
                if context.user_data is not None:
                    context.user_data.pop("poolmaster_alert_pending", None)
                await self.edit_private_alert_screen(
                    query, "alert_price" if kind == "price" else "alert_yield", snapshot, user_state, language
                )
                return

            await query.answer(translate(language, "callback.unknown_setting"), show_alert=True)
            return

        action = data.split(":")
        screen = action[-1] if force else data.removeprefix("ui:")
        if data.startswith("ui:set:"):
            if len(action) != 4 or action[2] not in {"apr", "price"}:
                await query.answer(translate(language, "callback.unknown_setting"), show_alert=True)
                return
            allowed_values = {"1", "3", "5", "off"} if action[2] == "apr" else {"1", "2", "5", "10", "off"}
            if action[3] not in allowed_values:
                await query.answer(translate(language, "callback.unknown_threshold"), show_alert=True)
                return
            value = None if action[3] == "off" else Decimal(action[3])
            snapshot = await self.snapshot_manager.get_snapshot()
            if action[2] == "apr":
                user_state = self.user_alert_store.set_farming_threshold(user_id, value, snapshot)
            else:
                user_state = self.user_alert_store.set_price_threshold(user_id, value, snapshot)
            screen = "alerts"
        elif data == "ui:pause24":
            self.user_alert_store.pause_user(user_id)
            user_state = self.user_alert_store.get_user(user_id)
            snapshot = await self.snapshot_manager.get_snapshot()
            screen = "alerts"
        elif data == "ui:resume":
            user_state = self.user_alert_store.resume_user(user_id)
            snapshot = await self.snapshot_manager.get_snapshot()
            screen = "alerts"
        else:
            screen = data.removeprefix("ui:refresh:") if force else data.removeprefix("ui:")
            if screen == "settings":
                screen = "alerts"
            if screen not in USER_SCREENS:
                await query.answer(translate(language, "callback.unknown_screen"), show_alert=True)
                return
            user_state = self.user_alert_store.ensure_user(user_id)
            snapshot = None
        if force:
            await query.answer(translate(language, "callback.updating"), show_alert=False)
        else:
            await query.answer()
        try:
            if snapshot is None:
                if force and screen in {"farming", "alert_yield"}:
                    snapshot = await self.snapshot_manager.refresh_farming()
                elif force and screen in {"price", "alert_price"}:
                    snapshot = await self.snapshot_manager.refresh_price()
                else:
                    snapshot = await self.snapshot_manager.get_snapshot()
            user_state = self.user_alert_store.seed_missing_baselines(user_id, snapshot)
            is_paused = self.user_alert_store.is_user_paused(user_state)
            await query.edit_message_text(
                render_user_screen(
                    screen,
                    snapshot,
                    invalid_pool_ids=self.invalid_pool_ids,
                    global_paused=self.admin_state_store.is_global_auto_push_paused(),
                    user_state=user_state,
                    language=language,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=private_user_keyboard(
                    screen, is_paused=is_paused, language=language, user_state=user_state
                ),
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logging.exception("Could not update private screen")
            if query.message:
                await telegram_call_with_retry(
                    lambda: query.message.reply_text(
                        translate(language, "callback.update_failed"),
                        reply_markup=private_user_keyboard(screen, language=language),
                    )
                )
        except TelegramError:
            logging.exception("Could not update private screen")
            if query.message:
                await telegram_call_with_retry(
                    lambda: query.message.reply_text(
                        translate(language, "callback.telegram_failed"),
                        reply_markup=private_user_keyboard(screen, language=language),
                    )
                )

    async def send_farming_info(self, update: Update) -> None:
        chat = update.effective_chat
        message = update.effective_message
        if not message:
            return
        if chat and chat.type == "private":
            await self.send_user_screen(update, "farming")
            return
        language = "ru"
        if self.farming_history_store is None:
            await telegram_call_with_retry(
                lambda: message.reply_text(translate(language, "farming_history.error.open"))
            )
            return
        await self.snapshot_manager.get_snapshot()
        if getattr(self.snapshot_manager, "last_history_write_error", None) is not None:
            await telegram_call_with_retry(
                lambda: message.reply_text(translate(language, "farming_history.error.persistence"))
            )
            return
        as_of = datetime.now(timezone.utc)
        chart_result = build_farming_history_chart(
            self.farming_history_store.get_summary(as_of=as_of),
            language=language,
            as_of=as_of,
        )
        await telegram_call_with_retry(
            lambda: message.reply_photo(
                photo=chart_result.image,
                caption=chart_result.caption,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        await self.delete_command_message(update)
        if update.effective_chat.type == "private":
            self.stats_store.track(update, "start")
            self.register_private_interaction(update)
            if self.is_private_admin(update):
                await self.set_admin_command_scope(context.bot, update.effective_user.id)
            else:
                user_state = self.user_alert_store.ensure_user(update.effective_user.id)
                if user_state.get("language") is not None:
                    await self.set_user_command_scope(
                        context.bot,
                        update.effective_user.id,
                        normalize_language(user_state.get("language")),
                    )
            await self.send_user_screen(update, "home")

    async def farming(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat:
            return
        await self.delete_command_message(update)
        if not self._allow_group_refresh(chat):
            return
        self.stats_store.track(update, "farming")
        if chat.type == "private":
            self.register_private_interaction(update)
        await self.send_farming_info(update)

    async def price_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.price(update, context)

    async def inline_query(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.inline_query
        if not query:
            return
        try:
            snapshot = await self.snapshot_manager.get_snapshot()
        except Exception:
            logging.exception("Could not read accepted snapshot for inline query")
            snapshot = None
        results = build_inline_results(
            snapshot,
            invalid_pool_ids=self.invalid_pool_ids,
        )
        await telegram_call_with_retry(
            lambda: query.answer(
                results,
                cache_time=INLINE_CACHE_SECONDS,
                is_personal=False,
            )
        )

    async def inline_price_callback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or query.data != "inline:price:refresh":
            return
        if not query.inline_message_id:
            await telegram_call_with_retry(
                lambda: query.answer(translate("ru", "inline.price.refresh_unavailable"), show_alert=False)
            )
            return

        if not self.cooldowns.allow(
            ("inline_price_refresh", query.from_user.id),
            self.settings.ui_refresh_cooldown_seconds,
        ):
            await telegram_call_with_retry(
                lambda: query.answer(translate("ru", "callback.cooldown"), show_alert=False)
            )
            return

        await telegram_call_with_retry(
            lambda: query.answer(translate("ru", "callback.updating"), show_alert=False)
        )
        try:
            snapshot_result = await self.snapshot_manager.refresh_price()
        except Exception as exc:
            snapshot_result = exc
        if isinstance(snapshot_result, Exception):
            logging.error("Could not refresh inline spot price: %s", type(snapshot_result).__name__)
            try:
                snapshot = await self.snapshot_manager.get_snapshot()
            except Exception:
                logging.exception("Could not read fallback inline spot price")
                snapshot = None
        else:
            snapshot = snapshot_result
        price_text = build_inline_price_text(snapshot)
        try:
            await telegram_call_with_retry(
                lambda: query.edit_message_text(
                    text=price_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=inline_price_refresh_keyboard(),
                )
            )
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logging.exception("Could not update inline price message")
        except TelegramError:
            logging.exception("Telegram rejected the inline price update")

    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.settings(update, context)

    async def chart(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat:
            return
        if chat.type != "private":
            await self.price(update, _)
            return
        await self.delete_command_message(update)
        if not self.cooldowns.allow(("chart_command", chat.id), CHART_COMMAND_COOLDOWN_SECONDS):
            return
        self.stats_store.track(update, "chart")
        self.register_private_interaction(update)
        user_state = self.user_alert_store.ensure_user(update.effective_user.id)
        if user_state.get("language") is None:
            await self.send_language_prompt(update.effective_message)
            return
        language = normalize_language(user_state.get("language"))
        try:
            await self.send_chart_reply(update.effective_message, "1h", language=language)
        except TelegramError:
            logging.exception("Failed to send chart photo")
            await telegram_call_with_retry(
                lambda: update.effective_message.reply_text(
                    translate(language, "chart.command_failed")
                )
            )

    async def chart_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        user_id = query.from_user.id
        current_state = self.user_alert_store.get_user(user_id)
        language = normalize_language(current_state.get("language"))
        if not private_callback_is_owned(query):
            await query.answer(translate(language, "private.chart_only"), show_alert=True)
            return
        user_state = self.user_alert_store.reactivate_user(query.from_user)
        if user_state.get("language") is None:
            await query.answer(render_language_prompt())
            if query.message:
                message = query.message
                try:
                    await telegram_call_with_retry(
                        lambda: context.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
                    )
                except TelegramError:
                    logging.exception("Could not replace stale chart with language chooser")
                await telegram_call_with_retry(
                    lambda: context.bot.send_message(
                        chat_id=message.chat.id,
                        text=render_language_prompt(),
                        reply_markup=private_user_keyboard("language"),
                        disable_web_page_preview=True,
                    )
                )
            return
        language = normalize_language(user_state.get("language"))
        is_legacy_refresh = query.data == "chart:mexc:refresh"
        is_refresh = is_legacy_refresh or query.data.startswith("chart:mexc:refresh:")
        cooldown_key = ("chart_refresh", user_id) if is_refresh else ("chart_callback", user_id)
        cooldown_seconds = self.settings.ui_refresh_cooldown_seconds if is_refresh else FAST_CALLBACK_COOLDOWN_SECONDS
        if not self.cooldowns.allow(cooldown_key, cooldown_seconds):
            retry_message = translate(language, "callback.cooldown") if is_refresh else translate(language, "callback.chart_wait")
            await query.answer(retry_message, show_alert=False)
            return

        if query.data == "chart:back":
            await query.answer()
            try:
                message = query.message
                if message is None:
                    return
                await telegram_call_with_retry(
                    lambda: context.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
                )
                snapshot = await self.snapshot_manager.get_snapshot()
                user_state = self.user_alert_store.seed_missing_baselines(user_id, snapshot)
                await telegram_call_with_retry(
                    lambda: context.bot.send_message(
                        chat_id=message.chat.id,
                        text=render_user_screen(
                            "home",
                            snapshot,
                            invalid_pool_ids=self.invalid_pool_ids,
                            global_paused=self.admin_state_store.is_global_auto_push_paused(),
                            user_state=user_state,
                            language=language,
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_markup=private_user_keyboard("home", language=language),
                        disable_web_page_preview=True,
                    )
                )
            except TelegramError:
                logging.exception("Could not return from chart to home screen")
            return

        parts = query.data.split(":")
        if is_legacy_refresh:
            timeframe = "1h"
        elif is_refresh:
            if len(parts) != 4 or parts[:3] != ["chart", "mexc", "refresh"] or parts[3] not in PUBLIC_TIMEFRAMES:
                await query.answer(translate(language, "callback.chart_intervals"), show_alert=False)
                return
            timeframe = parts[3]
        elif len(parts) == 3 and parts[0] == "chart" and parts[1] == "mexc" and parts[2] in PUBLIC_TIMEFRAMES:
            timeframe = parts[2]
        else:
            await query.answer(translate(language, "callback.chart_unavailable"), show_alert=False)
            return

        await query.answer(translate(language, "callback.chart_refreshing"), show_alert=False)
        pool_code = "mexc"
        try:
            if language == "ru":
                chart = await self.fetch_verified_mexc_chart(timeframe, force_refresh=is_refresh)
            else:
                chart = await self.fetch_verified_mexc_chart(timeframe, force_refresh=is_refresh, language=language)
        except Exception:
            logging.exception("Could not refresh MEXC chart")
            if query.message:
                await telegram_call_with_retry(
                    lambda: query.message.reply_text(
                        translate(language, "callback.chart_update_failed"),
                        reply_markup=self.chart_keyboard("mexc", timeframe, language=language),
                    )
                )
            return
        try:
            await telegram_call_with_retry(
                lambda: query.edit_message_media(
                    media=InputMediaPhoto(
                        media=chart.image,
                        caption=chart.caption,
                        parse_mode=ParseMode.HTML,
                    ),
                    reply_markup=self.chart_keyboard(pool_code, timeframe, language=language),
                )
            )
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logging.exception("Failed to edit chart message")
            if query.message:
                await telegram_call_with_retry(
                    lambda: query.message.reply_text(translate(language, "callback.chart_reopen"))
                )
        except TelegramError:
            logging.exception("Failed to edit chart message")
            if query.message:
                await telegram_call_with_retry(
                    lambda: query.message.reply_text(translate(language, "callback.chart_telegram_failed"))
                )

    def parse_text_command(self, text: str) -> tuple[str, str | None] | None:
        match = re.match(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:\s|$)", text.strip())
        if not match:
            return None
        return match.group(1).lower(), match.group(2)

    def is_command_for_this_bot(self, mention: str | None) -> bool:
        return not mention or mention.lower() == self.bot_username.lower()

    async def command_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if not message or not message.text or not chat:
            return

        parsed = self.parse_text_command(message.text)
        if not parsed:
            return
        command, mention = parsed
        if not self.is_command_for_this_bot(mention):
            return

        logging.info(
            "Fallback command handler matched /%s in chat_id=%s chat_type=%s mention=%s",
            command,
            chat.id,
            chat.type,
            mention,
        )

        if command == "farming":
            await self.farming(update, context)
        elif command == "chart":
            await self.chart(update, context)
        elif command == "start":
            await self.start(update, context)

    async def alert_loop(self, application: Application) -> None:
        logging.info("Personal alert loop started")
        interval = min(
            self.settings.price_poll_interval_seconds,
            self.settings.farming_poll_interval_seconds,
        )
        while True:
            try:
                if not self.admin_state_store.is_global_auto_push_paused():
                    snapshot = await self.snapshot_manager.get_snapshot()
                    summary: DeliverySummary = await dispatch_user_alerts(
                        self.user_alert_store,
                        application.bot,
                        snapshot,
                        now=datetime.now(timezone.utc),
                        price_cooldown_seconds=self.settings.price_alert_cooldown_seconds,
                    )
                    if summary.sent or summary.failed:
                        logging.info(
                            "Personal alert cycle completed. sent=%s failed=%s",
                            summary.sent,
                            summary.failed,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Personal alert cycle failed")
            await asyncio.sleep(interval)

    async def farming_history_loop(self, application: Application) -> None:
        if self.farming_history_store is None:
            logging.error("Daily farming history collection is disabled because storage is unavailable")
            return
        logging.info("Daily farming history collection loop started")
        while True:
            if getattr(self.snapshot_manager, "last_history_write_error", None) is not None:
                await asyncio.sleep(300)
                if not self.snapshot_manager.retry_pending_farming_history_write():
                    continue
            else:
                try:
                    await self.snapshot_manager.refresh_farming()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception("Daily farming history collection failed")
                if getattr(self.snapshot_manager, "last_history_write_error", None) is not None:
                    continue

            now = datetime.now(timezone.utc)
            next_run = datetime.combine(
                now.date() + timedelta(days=1),
                time(hour=0, minute=5),
                tzinfo=timezone.utc,
            )
            await asyncio.sleep(max(0.0, (next_run - now).total_seconds()))

    async def managed_publication_loop(self, application: Application) -> None:
        logging.info("Managed destination publication loop started")
        interval = max(60, self.settings.farming_poll_interval_seconds)
        while True:
            try:
                destinations = self.managed_destination_store.list_destinations()
                if any(destination["enabled"] for destination in destinations):
                    snapshot = await self.snapshot_manager.get_snapshot()
                    summary = await dispatch_managed_publications(
                        self.managed_destination_store,
                        application.bot,
                        snapshot,
                        now=datetime.now(timezone.utc),
                    )
                    if summary.sent or summary.failed:
                        logging.info(
                            "Managed publication cycle completed. sent=%s failed=%s skipped=%s",
                            summary.sent,
                            summary.failed,
                            summary.skipped,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Managed publication cycle failed")
            await asyncio.sleep(interval)

    async def stats(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            await telegram_call_with_retry(lambda: update.effective_message.reply_text("Команда доступна только админу."))
            return

        if not self.is_private_admin(update):
            await telegram_call_with_retry(
                lambda: update.effective_message.reply_text("Откройте личный чат с POOLMASTER для просмотра статистики.")
            )
            return
        await self.delete_command_message(update)
        summary = self.stats_store.summary()
        users = summary["users"]
        events = summary["events"]
        lines = [
            "<b>Статистика POOLMASTER</b>",
            f"Пользователей: <b>{len(users)}</b>",
            f"Событий команд: <b>{len(events)}</b>",
            "",
        ]

        if not users:
            lines.append("Пока команд никто не вызывал.")
        else:
            for user_data in users[:20]:
                username = user_data.get("username")
                name = " ".join(filter(None, [user_data.get("first_name"), user_data.get("last_name")])) or "Без имени"
                username_text = f"@{html.escape(username)}" if username else "username нет"
                name_text = html.escape(name)
                commands = user_data.get("commands", {})
                command_parts = [
                    f"/{html.escape(command)}: {details.get('count', 0)}"
                    for command, details in sorted(commands.items())
                ]
                lines.append(
                    f"ID <code>{user_data.get('id')}</code> | {username_text} | {name_text}\n"
                    f"Всего: <b>{user_data.get('total_commands', 0)}</b> | "
                    f"Последний раз: <code>{user_data.get('last_seen_at')}</code>\n"
                    f"{', '.join(command_parts)}"
                )

        await telegram_call_with_retry(
            lambda: update.effective_message.reply_text(
                "\n\n".join(lines),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        )

    async def stats_full(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update):
            await telegram_call_with_retry(lambda: update.effective_message.reply_text("Команда доступна только админу."))
            return

        if not self.is_private_admin(update):
            await telegram_call_with_retry(
                lambda: update.effective_message.reply_text("Откройте личный чат с POOLMASTER для экспорта статистики.")
            )
            return
        await self.delete_command_message(update)
        document, count = self._registered_users_csv()
        await telegram_call_with_retry(
            lambda: update.effective_message.reply_document(
                document=document,
                filename="poolmaster_users.csv",
                caption=f"Пользователей: {count}",
            )
        )

    def _registered_users_csv(self) -> tuple[io.BytesIO, int]:
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        writer.writerow(("telegram_id", "username", "first_name", "last_name", "first_seen", "last_seen"))
        user_ids = self.user_alert_store.list_registered_user_ids()
        for user_id in user_ids:
            record = self.user_alert_store.get_user(user_id)
            if record.get("registered") is not True:
                continue
            writer.writerow((
                user_id,
                record.get("username") or "",
                record.get("first_name") or "",
                record.get("last_name") or "",
                record.get("first_seen_at") or "",
                record.get("last_seen_at") or "",
            ))
        payload = io.BytesIO(("\ufeff" + text.getvalue()).encode("utf-8"))
        payload.name = "poolmaster_users.csv"
        return payload, len(user_ids)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        update_id = getattr(update, "update_id", None)
        error = context.error
        logging.error(
            "Unhandled Telegram update error. update_id=%s error_type=%s",
            update_id,
            type(error).__name__ if error is not None else "unknown",
        )

    def is_admin(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id in self.admin_ids)

    def is_private_admin(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        return bool(
            self.is_admin(update)
            and user
            and chat
            and chat.type == "private"
            and chat.id == user.id
        )

    @staticmethod
    def user_commands(language: str = "ru") -> list[BotCommand]:
        return [
            BotCommand("start", translate(language, "command.start")),
            BotCommand("farming", translate(language, "command.farming")),
            BotCommand("price", translate(language, "command.price")),
        ]

    @classmethod
    def admin_commands(cls) -> list[BotCommand]:
        return [*cls.user_commands(), BotCommand("admin", "Админ-панель")]

    @staticmethod
    def group_commands() -> list[BotCommand]:
        return [
            BotCommand("farming", translate("ru", "command.farming")),
            BotCommand("price", translate("ru", "command.price")),
        ]

    async def set_admin_command_scope(self, telegram_bot: Any, admin_id: int) -> None:
        try:
            await telegram_call_with_retry(
                lambda: telegram_bot.set_my_commands(
                    self.admin_commands(),
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
            )
        except TelegramError:
            logging.warning("Could not set private command scope for configured admin")

    async def set_user_command_scope(self, telegram_bot: Any, user_id: int, language: str) -> None:
        try:
            await telegram_call_with_retry(
                lambda: telegram_bot.set_my_commands(
                    self.user_commands(language),
                    scope=BotCommandScopeChat(chat_id=user_id),
                )
            )
        except TelegramError:
            logging.warning("Could not set private user command scope")

    @staticmethod
    def _minimal_default_administrator_rights(*, for_channels: bool) -> ChatAdministratorRights:
        return ChatAdministratorRights(
            is_anonymous=False,
            can_manage_chat=not for_channels,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
            can_post_messages=for_channels,
            can_edit_messages=False,
            can_pin_messages=False,
            can_manage_topics=False,
            can_manage_direct_messages=False,
            can_manage_tags=False,
            api_kwargs={"can_send_welcome_messages": False},
        )

    @staticmethod
    def _default_administrator_rights_match(
        current: Any,
        desired: ChatAdministratorRights,
        *,
        for_channels: bool,
    ) -> bool:
        if not isinstance(current, ChatAdministratorRights):
            return False
        fields = (
            "is_anonymous",
            "can_manage_chat",
            "can_delete_messages",
            "can_manage_video_chats",
            "can_restrict_members",
            "can_promote_members",
            "can_change_info",
            "can_invite_users",
            "can_post_stories",
            "can_edit_stories",
            "can_delete_stories",
            "can_post_messages",
            "can_edit_messages",
            "can_pin_messages",
            "can_manage_topics",
            "can_manage_direct_messages",
            "can_manage_tags",
        )
        optional_fields = {
            "can_post_messages",
            "can_edit_messages",
            "can_pin_messages",
            "can_manage_topics",
            "can_manage_direct_messages",
            "can_manage_tags",
        }
        for field in fields:
            expected = getattr(desired, field)
            if field == "can_manage_chat" and for_channels and desired.can_post_messages is True:
                # Bot API may report this as implied by channel posting; it is not a separate grant.
                continue
            actual = getattr(current, field)
            if field in optional_fields and actual is None and expected is False:
                continue
            if actual != expected:
                return False
        current_welcome_messages = current.to_dict().get("can_send_welcome_messages", False)
        desired_welcome_messages = desired.to_dict().get("can_send_welcome_messages", False)
        if current_welcome_messages is None:
            current_welcome_messages = False
        if current_welcome_messages != desired_welcome_messages:
            return False
        return True

    async def configure_default_administrator_rights(self, telegram_bot: Any) -> None:
        for for_channels, scope in ((True, "channel"), (False, "group")):
            desired = self._minimal_default_administrator_rights(for_channels=for_channels)
            try:
                current = await telegram_read_call_with_retry(
                    lambda: telegram_bot.get_my_default_administrator_rights(
                        for_channels=for_channels
                    )
                )
                if self._default_administrator_rights_match(
                    current,
                    desired,
                    for_channels=for_channels,
                ):
                    continue
                await telegram_call_with_retry(
                    lambda: telegram_bot.set_my_default_administrator_rights(
                        rights=desired,
                        for_channels=for_channels,
                    )
                )
            except TelegramError as exc:
                logging.warning(
                    "Could not configure default Telegram administrator rights; scope=%s error_type=%s",
                    scope,
                    type(exc).__name__,
                )

    async def admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat:
            return
        if not user or user.id not in self.admin_ids:
            await telegram_call_with_retry(lambda: message.reply_text("Команда доступна только администратору POOLMASTER."))
            return
        if chat.type != "private" or chat.id != user.id:
            await telegram_call_with_retry(lambda: message.reply_text("Админ-панель доступна только в личном чате с ботом."))
            return
        args = list(context.args or [])
        if args:
            if len(args) == 2 and args[0].lower() == "bind":
                await self.bind_managed_destination(message, context, user.id, args[1])
                return
            await telegram_call_with_retry(lambda: message.reply_text("Неизвестная команда. Откройте /admin."))
            return
        snapshot = await self.snapshot_manager.get_snapshot()
        await telegram_call_with_retry(
            lambda: message.reply_text(
                self.render_admin("home", snapshot, admin_id=user.id),
                parse_mode=ParseMode.HTML,
                reply_markup=admin_keyboard("home", language=self._admin_language(user.id)),
                disable_web_page_preview=True,
            )
        )

    async def my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        membership_update = update.my_chat_member
        if not membership_update:
            return
        chat = membership_update.chat
        member = membership_update.new_chat_member
        chat_id = getattr(chat, "id", None)
        chat_type = getattr(chat, "type", None)
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id >= 0
            or chat_type not in {"group", "supergroup", "channel"}
        ):
            return

        existing_destination = self.managed_destination_store.get_destination(chat_id)
        existing = existing_destination is not None
        previously_verified_can_post = bool(
            existing_destination and existing_destination.get("can_post_messages") is True
        )
        actor = getattr(membership_update, "from_user", None)
        actor_id = getattr(actor, "id", None)
        if not existing and actor_id not in self.admin_ids:
            return

        event_status = getattr(member, "status", "")
        event_can_post = membership_can_publish(
            chat_type,
            member,
            existing=existing,
            chat_permissions=getattr(chat, "permissions", None),
            previously_verified_can_post=previously_verified_can_post,
        )
        event_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat_id)
        observed_at = getattr(membership_update, "date", None)
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            observed_at = datetime.now(timezone.utc)

        try:
            if self.bot_id is None:
                raise ValueError("runtime bot identity is not initialized")
            verified = await verify_managed_destination(
                context.bot,
                chat_id,
                self.bot_id,
                existing=existing,
                previously_verified_can_post=previously_verified_can_post,
            )
        except Exception as exc:
            logging.warning(
                "Managed destination membership verification failed; error_type=%s",
                type(exc).__name__,
            )
            if existing:
                fallback_status = event_status if event_status in {
                    "creator", "administrator", "member", "restricted", "left", "kicked"
                } else "unknown"
                verified_name = str(event_name).strip() or str(chat_id)
                can_post = membership_can_publish(
                    chat_type,
                    member,
                    existing=True,
                    chat_permissions=getattr(chat, "permissions", None),
                    previously_verified_can_post=previously_verified_can_post,
                )
            else:
                fallback_status = "unknown"
                verified_name = str(event_name).strip() or str(chat_id)
                can_post = False
            destination_type = chat_type
        else:
            verified_name = verified.name
            destination_type = verified.destination_type
            fallback_status = verified.bot_status
            can_post = verified.can_post_messages

        candidate = self.managed_destination_store.observe_bot_membership(
            chat_id=chat_id,
            name=verified_name,
            destination_type=destination_type,
            bot_status=fallback_status,
            can_post_messages=can_post,
            observed_at=observed_at,
            force_disable_schedule=existing and not event_can_post,
        )
        logging.info(
            "Bot managed-destination membership observed. chat_type=%s bot_status=%s can_post=%s scheduled=%s",
            destination_type,
            candidate["bot_status"],
            candidate["can_post_messages"],
            candidate["enabled"],
        )
        if not existing:
            language = self._admin_language(actor_id)
            if destination_connection_state(candidate) == "connected":
                confirmation_key = (
                    "admin.destination.confirm.channel"
                    if destination_type == "channel"
                    else "admin.destination.confirm.group"
                )
            else:
                confirmation_key = "admin.destination.confirm.check"
            confirmation = translate(
                language,
                confirmation_key,
                name=html.escape(candidate["name"]),
            )
            try:
                await context.bot.send_message(
                    chat_id=actor_id,
                    text=confirmation,
                    parse_mode=ParseMode.HTML,
                    reply_markup=admin_keyboard(
                        "destination_confirmation",
                        destination=candidate,
                        language=language,
                    ),
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                logging.warning(
                    "Could not send managed-destination private confirmation; error_type=%s",
                    type(exc).__name__,
                )

    async def bind_managed_destination(
        self,
        message: Any,
        context: ContextTypes.DEFAULT_TYPE,
        admin_id: int,
        raw_chat_id: str,
    ) -> None:
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            await telegram_call_with_retry(lambda: message.reply_text("Для аварийной привязки укажите числовой ID чата."))
            return
        await self._bind_managed_chat_reference(message, context, admin_id, chat_id)

    async def _bind_managed_chat_reference(
        self,
        message: Any,
        context: ContextTypes.DEFAULT_TYPE,
        admin_id: int,
        chat_reference: int | str,
    ) -> bool:
        try:
            target = await telegram_read_call_with_retry(
                lambda: context.bot.get_chat(chat_reference)
            )
            if target.type not in {"group", "supergroup", "channel"}:
                raise ValueError("Telegram вернул другой ID или неподдерживаемый тип чата.")
            chat_id = target.id
            if (
                not isinstance(chat_id, int)
                or chat_id >= 0
                or (isinstance(chat_reference, int) and chat_reference < 0 and chat_id != chat_reference)
            ):
                raise ValueError("Telegram вернул другой ID или неподдерживаемый тип чата.")
            existing_destination = self.managed_destination_store.get_destination(chat_id)
            previously_verified_can_post = bool(
                existing_destination and existing_destination.get("can_post_messages") is True
            )
            bot_user = await telegram_read_call_with_retry(context.bot.get_me)
            membership = await telegram_read_call_with_retry(
                lambda: context.bot.get_chat_member(chat_id, bot_user.id)
            )
            status = getattr(membership, "status", "")
            can_post_messages = False
            if target.type == "channel":
                can_post = status == "creator" or (
                    status == "administrator" and getattr(membership, "can_post_messages", False) is True
                )
                if not can_post:
                    raise ValueError("Для канала боту нужны права администратора с can_post_messages.")
                can_post_messages = True
            elif status in {"left", "kicked"}:
                raise ValueError("Сначала добавьте бота в целевую группу.")
            elif status == "restricted" and (
                not getattr(membership, "is_member", False)
                or not getattr(membership, "can_send_messages", False)
            ):
                raise ValueError("В целевой группе боту запрещена отправка сообщений.")
            elif status in {"creator", "administrator"}:
                can_post_messages = True
            elif status == "member":
                permissions = getattr(target, "permissions", None)
                can_post_messages = membership_can_publish(
                    target.type,
                    membership,
                    existing=existing_destination is not None,
                    chat_permissions=permissions,
                    previously_verified_can_post=previously_verified_can_post,
                )
                if not can_post_messages and existing_destination is None:
                    raise ValueError("Для подключения новой группы боту нужны права администратора.")
                if not can_post_messages and getattr(permissions, "can_send_messages", None) is False:
                    raise ValueError("В целевой группе участникам запрещена отправка сообщений.")
                if not can_post_messages:
                    raise ValueError("Telegram не подтвердил право бота отправлять сообщения в этой группе.")
            elif status == "restricted" and membership_can_publish(
                target.type,
                membership,
                existing=existing_destination is not None,
                chat_permissions=getattr(target, "permissions", None),
                previously_verified_can_post=previously_verified_can_post,
            ):
                can_post_messages = True
            elif status == "restricted":
                raise ValueError("В целевой группе боту запрещена отправка сообщений.")
            else:
                raise ValueError("Бот не состоит в целевой группе.")
            title = getattr(target, "title", None) or getattr(target, "username", None) or str(chat_id)
            destination = self.managed_destination_store.register_destination(
                chat_id,
                title,
                target.type,
                actor_id=admin_id,
                admin_ids=self.admin_ids,
                bot_status=status,
                can_post_messages=can_post_messages,
            )
        except ValueError as exc:
            await telegram_call_with_retry(lambda: message.reply_text(str(exc)))
            return False
        except NetworkError as exc:
            logging.warning(
                "Could not validate managed destination via emergency bind; error_type=%s",
                type(exc).__name__,
            )
            await telegram_call_with_retry(
                lambda: message.reply_text(
                    "Telegram временно недоступен. Чат не добавлен — попробуйте ещё раз через минуту."
                )
            )
            return False
        except TelegramError as exc:
            logging.warning(
                "Could not validate managed destination via emergency bind; error_type=%s",
                type(exc).__name__,
            )
            await telegram_call_with_retry(
                lambda: message.reply_text("Не удалось проверить чат и право бота на публикацию. Проверьте членство бота.")
            )
            return False
        await telegram_call_with_retry(
            lambda: message.reply_text(
                f"Чат добавлен: <b>{html.escape(destination['name'])}</b>. Автопост фарминга выключен.",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_keyboard(
                    "destinations",
                    destinations=self.managed_destination_store.list_destinations(),
                    bot_username=self.bot_username,
                ),
            )
        )
        logging.info(
            "Managed destination registered via emergency bind. type=%s schedule=OFF",
            destination["type"],
        )
        return True

    def _last_personal_alert_at(self) -> datetime | None:
        values = []
        for state in self.user_alert_store.load_users().values():
            raw = state.get("last_alert_at")
            if not raw:
                continue
            try:
                value = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                continue
            if value.tzinfo is not None:
                values.append(value)
        return max(values) if values else None

    def _admin_language(self, admin_id: int) -> str:
        record = self.user_alert_store.load_users().get(str(admin_id), {})
        return normalize_language(record.get("language"))

    def render_admin(
        self,
        screen: str,
        snapshot: Any,
        *,
        destination: dict[str, Any] | None = None,
        admin_id: int | None = None,
    ) -> str:
        destinations = self.managed_destination_store.list_destinations()
        scheduled_times = []
        for destination in destinations:
            raw = destination.get("last_publication_at")
            if not raw:
                continue
            try:
                published_at = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                continue
            if published_at.tzinfo is not None:
                scheduled_times.append(published_at)
        language = self._admin_language(admin_id) if admin_id is not None else "ru"
        return render_admin_screen(
            screen,
            snapshot=snapshot,
            global_paused=self.admin_state_store.is_global_auto_push_paused(),
            subscriber_count=len(self.user_alert_store.list_user_ids()),
            destinations=destinations,
            destination=destination,
            bot_started_at=self.started_at,
            pool_identity_checks=self.pool_identity_checks,
            last_auto_alert_at=self._last_personal_alert_at(),
            last_auto_publication_at=max(scheduled_times) if scheduled_times else None,
            last_manual_push_at=self.admin_state_store.last_manual_push_at(),
            language=language,
        )

    def _admin_callback_is_owned(self, query: Any) -> bool:
        return bool(private_callback_is_owned(query) and query.from_user.id in self.admin_ids)

    async def _edit_admin_screen(
        self,
        query: Any,
        screen: str,
        *,
        destination: dict[str, Any] | None = None,
        snapshot: Any | None = None,
    ) -> None:
        if snapshot is None:
            snapshot = await self.snapshot_manager.get_snapshot()
        destinations = self.managed_destination_store.list_destinations()
        language = self._admin_language(query.from_user.id)
        await query.edit_message_text(
            self.render_admin(screen, snapshot, destination=destination, admin_id=query.from_user.id),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(
                screen,
                global_paused=self.admin_state_store.is_global_auto_push_paused(),
                destinations=destinations,
                destination=destination,
                bot_username=self.bot_username,
                language=language,
            ),
            disable_web_page_preview=True,
        )

    async def manual_broadcast_content(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        pending_alert = (context.user_data or {}).get("poolmaster_alert_pending")
        if (
            isinstance(pending_alert, dict)
            and pending_alert.get("phase") == "input"
            and message
            and user
            and chat
            and chat.type == "private"
            and chat.id == user.id
        ):
            language = normalize_language(self.user_alert_store.get_user(user.id).get("language"))
            kind = pending_alert.get("kind")
            raw = getattr(message, "text", None)
            threshold = _parse_alert_threshold(raw, kind) if isinstance(raw, str) and kind in {"price", "yield"} else None
            if threshold is None:
                prompt_key = "alerts.input_price" if kind == "price" else "alerts.input_yield"
                await telegram_call_with_retry(
                    lambda: message.reply_text(
                        f"{translate(language, 'alerts.input_invalid')}\n\n{translate(language, prompt_key)}",
                        reply_markup=private_user_keyboard("alert_input", language=language),
                    )
                )
                return
            snapshot = (
                await self.snapshot_manager.refresh_price()
                if kind == "price"
                else await self.snapshot_manager.refresh_farming()
            )
            if not _alert_snapshot_ready(kind, snapshot, self.invalid_pool_ids):
                key = "alerts.price_data_required" if kind == "price" else "alerts.yield_data_required"
                await telegram_call_with_retry(lambda: message.reply_text(translate(language, key)))
                return
            pending_alert.update({"phase": "preview", "value": str(threshold)})
            pending_alert["reference_signature"] = _alert_reference_signature(
                kind, snapshot, self.invalid_pool_ids
            )
            pending_alert["preview_snapshot"] = snapshot
            if context.user_data is not None:
                context.user_data["poolmaster_alert_pending"] = pending_alert
            user_state = self.user_alert_store.seed_missing_baselines(user.id, snapshot)
            preview_text = render_user_screen(
                "alert_preview",
                snapshot,
                invalid_pool_ids=self.invalid_pool_ids,
                global_paused=self.admin_state_store.is_global_auto_push_paused(),
                user_state=user_state,
                language=language,
                pending_threshold=pending_alert,
            )
            reply_markup = private_user_keyboard(
                "alert_preview",
                language=language,
                user_state=user_state,
                pending_alert_kind=kind,
                enable_on_save=bool(pending_alert.get("enable_on_save", True)),
            )
            prompt_chat_id = pending_alert.get("chat_id", chat.id)
            prompt_message_id = pending_alert.get("message_id")
            edited = False
            if isinstance(prompt_message_id, int):
                try:
                    await telegram_call_with_retry(
                        lambda: context.bot.edit_message_text(
                            chat_id=prompt_chat_id,
                            message_id=prompt_message_id,
                            text=preview_text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup,
                            disable_web_page_preview=True,
                        )
                    )
                    edited = True
                except TelegramError:
                    logging.info("Could not replace alert threshold input prompt with preview")
            if not edited:
                await telegram_call_with_retry(
                    lambda: message.reply_text(
                        preview_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                        disable_web_page_preview=True,
                    )
                )
            return

        if (
            not message
            or not user
            or not chat
            or chat.type != "private"
            or chat.id != user.id
            or user.id not in self.admin_ids
        ):
            return

        if not self.manual_push_coordinator.is_waiting_for_content(user.id):
            return

        if getattr(message, "photo", None):
            kind = "photo"
            text = None
            caption = getattr(message, "caption", None)
            photo_file_id = message.photo[-1].file_id
            text_entities = None
            caption_entities = getattr(message, "caption_entities", None)
        elif getattr(message, "text", None):
            kind = "text"
            text = message.text
            caption = None
            photo_file_id = None
            text_entities = getattr(message, "entities", None)
            caption_entities = None
        else:
            await telegram_call_with_retry(
                lambda: message.reply_text(
                    "Поддерживаются только текст и фото с подписью. Попробуйте ещё раз.",
                    reply_markup=admin_keyboard("push"),
                )
            )
            return

        try:
            preview = self.manual_push_coordinator.create_preview(
                actor_id=user.id,
                admin_ids=self.admin_ids,
                source_chat_id=chat.id,
                source_message_id=message.message_id,
                kind=kind,
                text=text,
                caption=caption,
                photo_file_id=photo_file_id,
                text_entities=text_entities,
                caption_entities=caption_entities,
                recipient_count=len(
                    self.manual_push_coordinator.recipient_ids(
                        self.user_alert_store,
                        self.admin_ids,
                    )
                ),
                now=datetime.now(timezone.utc),
            )
        except ValueError as exc:
            await telegram_call_with_retry(
                lambda: message.reply_text(str(exc), reply_markup=admin_keyboard("push"))
            )
            return

        try:
            copied = await telegram_call_with_retry(
                lambda: context.bot.copy_message(
                    chat_id=chat.id,
                    from_chat_id=chat.id,
                    message_id=message.message_id,
                )
            )
            self.manual_push_coordinator.replace_preview_source(
                preview.token,
                actor_id=user.id,
                admin_ids=self.admin_ids,
                source_message_id=copied.message_id,
            )
            await telegram_call_with_retry(
                lambda: context.bot.edit_message_reply_markup(
                    chat_id=chat.id,
                    message_id=copied.message_id,
                    reply_markup=admin_keyboard("push_confirm", token=preview.token),
                )
            )
        except (TelegramError, AttributeError):
            self.manual_push_coordinator.cancel(
                preview.token,
                actor_id=user.id,
                admin_ids=self.admin_ids,
                now=datetime.now(timezone.utc),
            )
            self.manual_push_coordinator.begin_input(actor_id=user.id, admin_ids=self.admin_ids)
            await telegram_call_with_retry(
                lambda: message.reply_text(
                    "Не удалось создать предпросмотр. Отправьте текст или фото ещё раз.",
                    reply_markup=admin_keyboard("push"),
                )
            )
            return

        try:
            await telegram_call_with_retry(lambda: message.delete())
        except TelegramError:
            logging.debug("Could not remove the original broadcast input. admin_id=%s", user.id)

    async def admin_ui_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        if not self._admin_callback_is_owned(query):
            await query.answer("Админ-панель доступна только настроенным администраторам в личном чате.", show_alert=True)
            return
        data = query.data or ""
        parts = data.split(":")
        admin_id = query.from_user.id
        is_refresh = len(parts) == 3 and parts[1] == "refresh" and parts[2] == "status"
        if is_refresh and not self.cooldowns.allow(
            ("admin_refresh", admin_id),
            self.settings.ui_refresh_cooldown_seconds,
        ):
            await query.answer("Обновить можно раз в 10 секунд.", show_alert=False)
            return
        if not self._allow_fast_callback(query):
            await query.answer("Действие уже обрабатывается. Попробуйте ещё раз через секунду.", show_alert=False)
            return
        answered = False
        try:
            await query.answer("Обновляю…" if is_refresh else None)
            answered = True
            if data == "admin:push":
                self.manual_push_coordinator.begin_input(actor_id=admin_id, admin_ids=self.admin_ids)
                await self._edit_admin_screen(query, "push")
                return
            if data == "admin:home":
                self.manual_push_coordinator.cancel_input(admin_id, admin_ids=self.admin_ids)
                await self._edit_admin_screen(query, "home")
                return
            if data in {"admin:destinations", "admin:users", "admin:status"}:
                self.manual_push_coordinator.cancel_input(admin_id, admin_ids=self.admin_ids)
                await self._edit_admin_screen(query, parts[1])
                return
            if len(parts) == 4 and parts[:3] == ["admin", "dest", "publish"]:
                chat_id = int(parts[3])
                destination = self.managed_destination_store.get_destination(chat_id)
                snapshot = await self.snapshot_manager.get_snapshot()
                try:
                    preview = self.channel_publication_coordinator.create_preview(
                        actor_id=admin_id,
                        admin_ids=self.admin_ids,
                        chat_id=chat_id,
                        snapshot=snapshot,
                        destinations=self.managed_destination_store,
                        now=datetime.now(timezone.utc),
                    )
                    card = farming_publication_card(snapshot)
                    if destination is None or card is None:
                        raise ValueError("Карточка сейчас недоступна для публикации.")
                except ValueError as exc:
                    language = self._admin_language(admin_id)
                    await query.edit_message_text(
                        str(exc),
                        reply_markup=admin_keyboard(
                            "destination",
                            destination=destination,
                            bot_username=self.bot_username,
                            language=language,
                        ) if destination else admin_keyboard(
                            "destinations",
                            destinations=self.managed_destination_store.list_destinations(),
                            bot_username=self.bot_username,
                            language=language,
                        ),
                    )
                    return
                language = self._admin_language(admin_id)
                await query.edit_message_text(
                    f"<b>{translate(language, 'admin.destination.preview_title', name=html.escape(destination['name']))}</b>\n\n{card[0]}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=admin_keyboard(
                        "destination_confirm",
                        token=preview.token,
                        chat_id=chat_id,
                        language=language,
                    ),
                    disable_web_page_preview=True,
                )
                return
            if len(parts) == 4 and parts[:3] == ["admin", "dest", "open"]:
                destination = self.managed_destination_store.get_destination(int(parts[3]))
                if destination is None:
                    await self._edit_admin_screen(query, "destinations")
                    return
                await self._edit_admin_screen(query, "destination", destination=destination)
                return
            if data == "admin:users:export":
                if query.message:
                    document, count = self._registered_users_csv()
                    await telegram_call_with_retry(
                        lambda: query.message.reply_document(
                            document=document,
                            filename="poolmaster_users.csv",
                            caption=f"Пользователей: {count}",
                        )
                    )
                return
            if is_refresh:
                snapshot = await self.snapshot_manager.refresh()
                await self._edit_admin_screen(query, "status", snapshot=snapshot)
                return
            if data in {"admin:global:pause", "admin:global:resume"}:
                self.admin_state_store.set_global_auto_push_paused(
                    data.endswith(":pause"),
                    actor_id=admin_id,
                    admin_ids=self.admin_ids,
                )
                logging.info(
                    "Global automatic push state changed. admin_id=%s paused=%s",
                    admin_id,
                    data.endswith(":pause"),
                )
                await self._edit_admin_screen(query, "status")
                return
            if len(parts) == 5 and parts[:3] == ["admin", "dest", "set"]:
                chat_id = int(parts[3])
                interval = None if parts[4] == "off" else int(parts[4])
                self.managed_destination_store.set_schedule(
                    chat_id,
                    interval,
                    actor_id=admin_id,
                    admin_ids=self.admin_ids,
                )
                logging.info(
                    "Managed destination schedule changed. admin_id=%s chat_id=%s interval_hours=%s",
                    admin_id,
                    chat_id,
                    interval,
                )
                destination = self.managed_destination_store.get_destination(chat_id)
                await self._edit_admin_screen(query, "destination", destination=destination)
                return
            if len(parts) == 4 and parts[:3] == ["admin", "dest", "disable"]:
                chat_id = int(parts[3])
                self.managed_destination_store.disable_destination(
                    chat_id,
                    actor_id=admin_id,
                    admin_ids=self.admin_ids,
                )
                destination = self.managed_destination_store.get_destination(chat_id)
                if destination is None:
                    await self._edit_admin_screen(query, "destinations")
                    return
                await self._edit_admin_screen(query, "destination", destination=destination)
                return
            if len(parts) == 5 and parts[:3] in (["admin", "dest", "confirm"], ["admin", "dest", "cancel"]):
                chat_id, token = int(parts[3]), parts[4]
                if parts[2] == "cancel":
                    cancelled = self.channel_publication_coordinator.cancel(
                        token,
                        actor_id=admin_id,
                        admin_ids=self.admin_ids,
                        now=datetime.now(timezone.utc),
                    )
                    summary = "Публикация отменена." if cancelled else "Предпросмотр истёк или уже использован."
                else:
                    result = await self.channel_publication_coordinator.confirm(
                        token,
                        actor_id=admin_id,
                        admin_ids=self.admin_ids,
                        snapshot_provider=lambda: self.snapshot_manager.get_snapshot(),
                        bot=context.bot,
                        destinations=self.managed_destination_store,
                        now=datetime.now(timezone.utc),
                    )
                    summary = {
                        "complete": "admin.destination.publish_success",
                        "unavailable": "admin.destination.publish_unavailable",
                        "stale": "admin.destination.publish_stale",
                        "failed": "admin.destination.publish_failed",
                        "invalid": "admin.destination.publish_failed",
                    }
                    language = self._admin_language(admin_id)
                    summary = translate(
                        language,
                        summary.get(result.status, "admin.destination.publish_failed"),
                    )
                destination = self.managed_destination_store.get_destination(chat_id)
                if destination:
                    await query.edit_message_text(
                        summary + "\n\n" + self.render_admin(
                            "destination",
                            await self.snapshot_manager.get_snapshot(),
                            destination=destination,
                            admin_id=admin_id,
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_markup=admin_keyboard(
                            "destination",
                            destination=destination,
                            bot_username=self.bot_username,
                            language=language,
                        ),
                    )
                else:
                    await query.edit_message_text(
                        summary,
                        reply_markup=admin_keyboard(
                            "destinations",
                            destinations=self.managed_destination_store.list_destinations(),
                            bot_username=self.bot_username,
                            language=self._admin_language(admin_id),
                        ),
                    )
                return
            if len(parts) == 4 and parts[:2] == ["admin", "push"] and parts[2] == "cancel":
                cancelled = self.manual_push_coordinator.cancel(
                    parts[3],
                    actor_id=admin_id,
                    admin_ids=self.admin_ids,
                    now=datetime.now(timezone.utc),
                )
                if cancelled:
                    logging.info("Private-user broadcast cancelled. admin_id=%s", admin_id)
                    self.manual_push_coordinator.cancel_input(admin_id, admin_ids=self.admin_ids)
                    if query.message:
                        await telegram_call_with_retry(lambda: query.message.delete())
                    await telegram_call_with_retry(
                        lambda: context.bot.send_message(
                            chat_id=admin_id,
                            text="Рассылка отменена.",
                            reply_markup=admin_keyboard("push_done"),
                        )
                    )
                else:
                    logging.info("Broadcast cancel ignored for expired/used preview. admin_id=%s", admin_id)
                    if query.message:
                        await telegram_call_with_retry(lambda: query.message.delete())
                    await telegram_call_with_retry(
                        lambda: context.bot.send_message(
                            chat_id=admin_id,
                            text="Предпросмотр истёк или уже использован.",
                            reply_markup=admin_keyboard("push_done"),
                        )
                    )
                return
            if len(parts) == 4 and parts[:2] == ["admin", "push"] and parts[2] == "confirm":
                result = await self.manual_push_coordinator.confirm(
                    parts[3],
                    actor_id=admin_id,
                    admin_ids=self.admin_ids,
                    bot=context.bot,
                    users=self.user_alert_store,
                    admin_state=self.admin_state_store,
                    now=datetime.now(timezone.utc),
                )
                if result.status != "complete":
                    summary = "Предпросмотр истёк или уже был использован."
                else:
                    summary = (
                        f"Отправлено: {result.delivered}\n"
                        f"Заблокировали бота: {result.blocked}\n"
                        f"Ошибки: {result.failed + result.state_failures}"
                    )
                if query.message:
                    await telegram_call_with_retry(lambda: query.message.delete())
                await telegram_call_with_retry(
                    lambda: context.bot.send_message(
                        chat_id=admin_id,
                        text=summary,
                        reply_markup=admin_keyboard("push_done"),
                    )
                )
                return
            await query.edit_message_text(
                "Неизвестное действие админ-панели.",
                reply_markup=admin_keyboard("home"),
            )
        except (ValueError, KeyError, PermissionError) as exc:
            logging.info("Admin callback rejected. admin_id=%s reason=%s", admin_id, exc)
            if answered:
                await query.edit_message_text(
                    "Операция не выполнена. Проверьте target и права.",
                    reply_markup=admin_keyboard("home"),
                )
            else:
                await query.answer("Операция не выполнена. Проверьте target и права.", show_alert=True)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logging.exception("Could not update admin frame")
                if not answered:
                    await query.answer("Telegram не принял обновление админ-панели.", show_alert=True)
        except TelegramError:
            logging.exception("Admin callback Telegram operation failed. admin_id=%s", admin_id)
            if not answered:
                await query.answer("Telegram не принял операцию.", show_alert=True)

    async def post_init(self, application: Application) -> None:
        await self.initialize_pool_identity_checks()
        self.snapshot_manager.set_price_identity(await self.check_price_identity())
        if self.user_alert_store.list_user_ids():
            accepted_snapshot = await self.snapshot_manager.get_snapshot()
            self.user_alert_store.seed_all_baselines(accepted_snapshot)
        bot_user = await application.bot.get_me()
        if isinstance(getattr(bot_user, "id", None), int):
            self.bot_id = bot_user.id
        username = getattr(bot_user, "username", None)
        if isinstance(username, str) and username.strip():
            self.bot_username = username.strip().removeprefix("@").strip()
        logging.info("Telegram bot username resolved as @%s", self.bot_username)
        await self.configure_default_administrator_rights(application.bot)
        user_commands = self.user_commands()
        await telegram_call_with_retry(
            lambda: application.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
        )
        group_commands = self.group_commands()
        await telegram_call_with_retry(
            lambda: application.bot.set_my_commands(
                group_commands,
                scope=BotCommandScopeAllGroupChats(),
            )
        )
        for admin_id in sorted(self.admin_ids):
            await self.set_admin_command_scope(application.bot, admin_id)
    def build_application(self) -> Application:
        def background_task_factory(application: Application):
            return (
                ("personal-alerts", self.alert_loop(application)),
                ("managed-publications", self.managed_publication_loop(application)),
                ("farming-history", self.farming_history_loop(application)),
            )

        application = (
            ApplicationBuilder()
            .token(self.token)
            .application_class(
                PoolmasterApplication,
                kwargs={"background_task_factory": background_task_factory},
            )
            .post_init(self.post_init)
            .build()
        )
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("price", self.price_command))
        application.add_handler(CommandHandler("farming", self.farming))
        application.add_handler(CommandHandler("settings", self.settings_command))
        application.add_handler(CommandHandler("chart", self.chart))
        application.add_handler(CommandHandler("stats", self.stats))
        application.add_handler(CommandHandler("stats_full", self.stats_full))
        application.add_handler(CommandHandler("admin", self.admin))
        application.add_handler(InlineQueryHandler(self.inline_query))
        application.add_handler(CallbackQueryHandler(self.inline_price_callback, pattern=r"^inline:"))
        application.add_handler(CallbackQueryHandler(self.user_ui_callback, pattern=r"^ui:"))
        application.add_handler(CallbackQueryHandler(self.admin_ui_callback, pattern=r"^admin:"))
        application.add_handler(CallbackQueryHandler(self.chart_callback, pattern=r"^chart:"))
        application.add_handler(ChatMemberHandler(self.my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        application.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & ~filters.COMMAND,
                self.manual_broadcast_content,
            )
        )
        application.add_handler(MessageHandler(filters.COMMAND, self.command_fallback))
        application.add_error_handler(self.error_handler)
        return application

    def run(self) -> None:
        application = self.build_application()
        logging.info("POOLMASTER bot is running. Open Telegram and send /farming to @%s.", self.bot_username)
        application.run_polling()


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    # HTTPX logs the full request URL at INFO; Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    PoolmasterBot().run()


if __name__ == "__main__":
    main()
