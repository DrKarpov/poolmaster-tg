from __future__ import annotations

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from chart import CHART_POOLS
from display_names import display_pair_name
from farming import FARMING_POOLS
from localization import normalize_language, translate


def build_destination_add_link(bot_username: str, destination_type: str) -> str:
    """Build a Telegram-native add link with only the required suggested right."""
    if destination_type not in {"channel", "group"}:
        raise ValueError("destination type must be channel or group")
    if not isinstance(bot_username, str):
        raise ValueError("bot username is invalid")
    username = bot_username.strip()
    if username.startswith("@"):
        username = username[1:]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
        raise ValueError("bot username is invalid")
    if destination_type == "channel":
        return f"https://t.me/{username}?startchannel&admin=post_messages"
    return f"https://t.me/{username}?startgroup&admin=manage_chat"


def private_user_keyboard(
    screen: str,
    *,
    is_paused: bool = False,
    language: str = "ru",
    user_state: dict[str, object] | None = None,
    pending_alert_kind: str | None = None,
    enable_on_save: bool = True,
) -> InlineKeyboardMarkup:
    language = normalize_language(language)
    if screen == "settings":
        screen = "alerts"
    if screen == "home":
        rows = [
            [
                InlineKeyboardButton(translate(language, "button.price"), callback_data="ui:price"),
                InlineKeyboardButton(translate(language, "button.farming"), callback_data="ui:farming"),
            ],
            [InlineKeyboardButton(translate(language, "button.alerts"), callback_data="ui:alerts")],
            [InlineKeyboardButton(translate(language, "button.language"), callback_data="ui:language")],
        ]
        return InlineKeyboardMarkup(rows)

    if screen == "language":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Русский", callback_data="ui:language:set:ru"),
                    InlineKeyboardButton("English", callback_data="ui:language:set:en"),
                ],
                [InlineKeyboardButton(translate(language, "button.back"), callback_data="ui:home", style="danger")],
            ]
        )

    if screen == "farming":
        dedust_ru_label = "button.guide.dedust_ru_en" if language == "en" else "button.guide.dedust_ru"
        ston_ru_label = "button.guide.ston_ru_en" if language == "en" else "button.guide.ston_ru"
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(translate(language, "button.refresh"), callback_data="ui:refresh:farming")],
                [InlineKeyboardButton(translate(language, "button.history"), callback_data="ui:farming:history")],
                *_pool_link_rows(),
                [
                    InlineKeyboardButton(translate(language, dedust_ru_label), url="https://help.dedust.io/en/ru/liquidity/pools"),
                    InlineKeyboardButton(translate(language, "button.guide.dedust_en"), url="https://help.dedust.io/en/liquidity/pools"),
                ],
                [
                    InlineKeyboardButton(translate(language, ston_ru_label), url="https://guide.ston.fi/ru/farming/kak-farmit-na-ston.fi"),
                    InlineKeyboardButton(
                        translate(language, "button.guide.ston_en"),
                        url="https://guide.ston.fi/providing-liquidity/how-to-provide-liquidity-on-ston.fi",
                    ),
                ],
                [InlineKeyboardButton(translate(language, "button.back"), callback_data="ui:home", style="danger")],
            ]
        )

    if screen == "alerts":
        state = user_state or {}
        yield_enabled = bool(state.get("farming_enabled"))
        price_enabled = bool(state.get("price_enabled"))
        pause_button = InlineKeyboardButton(
            translate(language, "button.resume" if is_paused else "button.pause24"),
            callback_data="ui:resume" if is_paused else "ui:pause24",
        )
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        translate(language, "alerts.yield_switch_on" if yield_enabled else "alerts.yield_switch_off"),
                        callback_data="ui:alert:yield:toggle",
                        style="success" if yield_enabled else "danger",
                    ),
                ],
                [InlineKeyboardButton(translate(language, "button.configure_yield"), callback_data="ui:alert:yield")],
                [
                    InlineKeyboardButton(
                        translate(language, "alerts.price_switch_on" if price_enabled else "alerts.price_switch_off"),
                        callback_data="ui:alert:price:toggle",
                        style="success" if price_enabled else "danger",
                    ),
                ],
                [InlineKeyboardButton(translate(language, "button.configure_price"), callback_data="ui:alert:price")],
                [pause_button],
                [InlineKeyboardButton(translate(language, "button.back"), callback_data="ui:home", style="danger")],
            ]
        )

    if screen in {"alert_price", "alert_yield"}:
        kind = "price" if screen == "alert_price" else "yield"
        direction_field = "price_direction" if kind == "price" else "farming_direction"
        direction = str((user_state or {}).get(direction_field, "both"))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(translate(language, "button.refresh"), callback_data=f"ui:refresh:{screen}")],
                [
                    InlineKeyboardButton(
                        translate(language, "alerts.direction_up"),
                        callback_data=f"ui:alert:{kind}:direction:up",
                        style="primary" if direction == "up" else None,
                    ),
                    InlineKeyboardButton(
                        translate(language, "alerts.direction_down"),
                        callback_data=f"ui:alert:{kind}:direction:down",
                        style="primary" if direction == "down" else None,
                    ),
                    InlineKeyboardButton(
                        translate(language, "alerts.direction_both"),
                        callback_data=f"ui:alert:{kind}:direction:both",
                        style="primary" if direction == "both" else None,
                    ),
                ],
                [InlineKeyboardButton(translate(language, "button.set_threshold"), callback_data=f"ui:alert:{kind}:input")],
                [InlineKeyboardButton(translate(language, "button.back"), callback_data="ui:alerts", style="danger")],
            ]
        )

    if screen == "alert_input":
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(translate(language, "button.cancel"), callback_data="ui:alert:cancel", style="danger")]]
        )

    if screen == "alert_preview":
        action = f"ui:alert:save:{pending_alert_kind}" if pending_alert_kind in {"price", "yield"} else "ui:alert:cancel"
        button_key = "button.confirm_threshold" if enable_on_save else "button.save_threshold"
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(translate(language, button_key), callback_data=action, style="success")],
                [InlineKeyboardButton(translate(language, "button.cancel"), callback_data="ui:alert:cancel", style="danger")],
            ]
        )

    raise ValueError(f"unknown user screen: {screen}")


def farming_history_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    language = normalize_language(language)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(translate(language, "button.back"), callback_data="ui:farming:history:back", style="danger")],
        ]
    )


def _pool_link_rows() -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    pools = list(FARMING_POOLS)
    for index in range(0, len(pools), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    f"{pool.dex} {display_pair_name(pool)}",
                    url=pool.url,
                )
                for pool in pools[index:index + 2]
            ]
        )
    return rows


def group_response_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_pool_link_rows())


def channel_post_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_pool_link_rows())


def inline_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_pool_link_rows())


def inline_price_refresh_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    language = normalize_language(language)
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                translate(language, "button.refresh"),
                callback_data="inline:price:refresh",
            )
        ]]
    )


def private_alert_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    language = normalize_language(language)
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(translate(language, "button.alerts"), callback_data="ui:alerts"),
            InlineKeyboardButton(translate(language, "button.pause24"), callback_data="ui:pause24"),
        ]]
    )


def admin_keyboard(
    screen: str,
    *,
    global_paused: bool = True,
    destinations: list[dict[str, object]] | None = None,
    destination: dict[str, object] | None = None,
    chat_id: int | None = None,
    token: str | None = None,
    confirm_enabled: bool = True,
    bot_username: str | None = None,
    language: str = "ru",
) -> InlineKeyboardMarkup:
    language = normalize_language(language)
    label = lambda key: translate(language, key)
    if screen == "home":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(label("admin.home.broadcast"), callback_data="admin:push")],
                [InlineKeyboardButton(label("admin.home.destinations"), callback_data="admin:destinations")],
                [InlineKeyboardButton(label("admin.home.users"), callback_data="admin:users")],
                [InlineKeyboardButton(label("admin.home.status"), callback_data="admin:status")],
            ]
        )
    if screen == "destinations":
        if not bot_username:
            raise ValueError("destination list requires the runtime bot username")
        from managed_destinations import destination_connection_state

        rows: list[list[InlineKeyboardButton]] = []
        for destination in destinations or []:
            chat_id = int(destination["chat_id"])
            name = str(destination["name"])
            state_key = f"admin.destination.state.{destination_connection_state(destination)}"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{name[:24]} · {label(state_key)}",
                        callback_data=f"admin:dest:open:{chat_id}",
                    )
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        label("admin.destination.add_channel"),
                        url=build_destination_add_link(bot_username, "channel"),
                    )
                ],
                [
                    InlineKeyboardButton(
                        label("admin.destination.add_group"),
                        url=build_destination_add_link(bot_username, "group"),
                    )
                ],
                [InlineKeyboardButton(label("admin.destination.back"), callback_data="admin:home", style="danger")],
            ]
        )
        return InlineKeyboardMarkup(rows)
    if screen == "destination":
        if not destination:
            raise ValueError("destination detail screen requires a destination")
        from managed_destinations import destination_connection_state

        target_id = int(destination["chat_id"])
        active_interval = destination.get("farming_interval_hours")
        rows = []
        if destination_connection_state(destination) == "connected":
            rows.extend([
                [
                    InlineKeyboardButton(
                        f"{'✓ ' if active_interval == 3 else ''}{label('admin.destination.schedule.3')}",
                        callback_data=f"admin:dest:set:{target_id}:3",
                        style="success" if active_interval == 3 else None,
                    ),
                    InlineKeyboardButton(
                        f"{'✓ ' if active_interval == 6 else ''}{label('admin.destination.schedule.6')}",
                        callback_data=f"admin:dest:set:{target_id}:6",
                        style="success" if active_interval == 6 else None,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        f"{'✓ ' if active_interval == 12 else ''}{label('admin.destination.schedule.12')}",
                        callback_data=f"admin:dest:set:{target_id}:12",
                        style="success" if active_interval == 12 else None,
                    ),
                    InlineKeyboardButton(
                        f"{'✓ ' if active_interval == 24 else ''}{label('admin.destination.schedule.24')}",
                        callback_data=f"admin:dest:set:{target_id}:24",
                        style="success" if active_interval == 24 else None,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        label(
                            "admin.destination.schedule.off_selected"
                            if active_interval is None
                            else "admin.destination.schedule.turn_off"
                        ),
                        callback_data=f"admin:dest:set:{target_id}:off",
                        style="danger",
                    )
                ],
                [InlineKeyboardButton(label("admin.destination.publish_now"), callback_data=f"admin:dest:publish:{target_id}")],
            ])
        else:
            if not bot_username:
                raise ValueError("reconnect action requires the runtime bot username")
            destination_type = "channel" if destination.get("type") == "channel" else "group"
            rows.append(
                [
                    InlineKeyboardButton(
                        label("admin.destination.reconnect"),
                        url=build_destination_add_link(bot_username, destination_type),
                    )
                ]
            )
        rows.append(
            [InlineKeyboardButton(label("admin.destination.back"), callback_data="admin:destinations", style="danger")]
        )
        return InlineKeyboardMarkup(rows)
    if screen == "destination_confirmation":
        if not destination:
            raise ValueError("destination confirmation requires a destination")
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    label("admin.destination.open_settings"),
                    callback_data=f"admin:dest:open:{int(destination['chat_id'])}",
                )
            ]]
        )
    if screen == "destination_confirm":
        if not token or chat_id is None:
            raise ValueError("publication preview requires a destination and token")
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(label("admin.destination.confirm_publish"), callback_data=f"admin:dest:confirm:{chat_id}:{token}", style="success"),
                InlineKeyboardButton(label("admin.destination.cancel"), callback_data=f"admin:dest:cancel:{chat_id}:{token}", style="danger"),
            ]]
        )
    if screen == "push":
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Назад", callback_data="admin:home", style="danger")]]
        )
    if screen == "push_confirm":
        if not token:
            raise ValueError("push confirmation screen requires a token")
        confirm_data = f"admin:push:confirm:{token}" if confirm_enabled else "admin:push:disabled"
        confirm_text = "Отправить всем" if confirm_enabled else "Нет получателей для отправки"
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    confirm_text,
                    callback_data=confirm_data,
                    style="success",
                ),
                InlineKeyboardButton("Отмена", callback_data=f"admin:push:cancel:{token}", style="danger"),
            ]]
        )
    if screen in {"push_done", "destination_preview"}:
        back_screen = "home" if screen == "push_done" else "destinations"
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Назад", callback_data=f"admin:{back_screen}", style="danger")]]
        )
    if screen == "users":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Скачать список", callback_data="admin:users:export")],
                [InlineKeyboardButton("Назад", callback_data="admin:home", style="danger")],
            ]
        )
    if screen == "status":
        pause_label = "Возобновить автоуведомления" if global_paused else "Приостановить автоуведомления"
        pause_action = "resume" if global_paused else "pause"
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Обновить", callback_data="admin:refresh:status")],
                [InlineKeyboardButton(pause_label, callback_data=f"admin:global:{pause_action}")],
                [InlineKeyboardButton("Назад", callback_data="admin:home", style="danger")],
            ]
        )
    if screen not in {"destinations", "push", "users", "status"}:
        raise ValueError(f"unknown admin screen: {screen}")
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Назад", callback_data="admin:home", style="danger")]]
    )


def chart_pool_links() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(f"DeDust {CHART_POOLS['dedust'].label}", url=CHART_POOLS["dedust"].url),
            InlineKeyboardButton(f"STON.fi {CHART_POOLS['ston'].label}", url=CHART_POOLS["ston"].url),
        ]]
    )
