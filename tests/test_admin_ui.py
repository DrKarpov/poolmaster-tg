import pytest

import keyboards
from keyboards import admin_keyboard
from localization import translate


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_admin_home_contains_all_private_admin_sections():
    buttons = _buttons(admin_keyboard("home"))
    callbacks = {button.callback_data for button in buttons}

    assert callbacks == {
        "admin:push",
        "admin:destinations",
        "admin:users",
        "admin:status",
    }
    assert {button.text for button in buttons} == {"Рассылка", "Каналы и группы", "Пользователи", "Состояние"}


def test_admin_screens_have_back_and_no_duplicate_user_screens():
    for screen in ("push", "destinations", "users", "status"):
        buttons = _buttons(
            admin_keyboard(
                screen,
                bot_username="TESTPOOLMASTERBOT" if screen == "destinations" else None,
            )
        )
        assert any(button.text == "Назад" and button.callback_data == "admin:home" for button in buttons)

    with pytest.raises(ValueError):
        admin_keyboard("farming")
    with pytest.raises(ValueError):
        admin_keyboard("price")


def test_status_screen_owns_global_alert_control():
    paused = _buttons(admin_keyboard("status", global_paused=True))
    resumed = _buttons(admin_keyboard("status", global_paused=False))

    assert any(button.text == "Возобновить автоуведомления" and button.callback_data == "admin:global:resume" for button in paused)
    assert any(button.text == "Приостановить автоуведомления" and button.callback_data == "admin:global:pause" for button in resumed)


@pytest.mark.parametrize(
    ("language", "expected"),
    [("ru", "частично"), ("en", "partial")],
)
def test_admin_partial_source_status_has_ru_and_en_labels(language, expected):
    assert translate(language, "admin.status.source.partial") == expected


def test_users_screen_has_single_csv_export_action():
    buttons = _buttons(admin_keyboard("users"))

    assert [(button.text, button.callback_data) for button in buttons] == [
        ("Скачать список", "admin:users:export"),
        ("Назад", "admin:home"),
    ]


def test_channel_list_has_open_add_and_back_actions_only():
    destinations = [
        {
            "chat_id": -100123,
            "name": "Official Channel",
            "type": "channel",
            "enabled": True,
            "farming_interval_hours": 6,
        }
    ]

    buttons = _buttons(
        admin_keyboard("destinations", destinations=destinations, bot_username="TESTPOOLMASTERBOT")
    )

    assert {button.callback_data for button in buttons if button.callback_data} == {
        "admin:dest:open:-100123",
        "admin:home",
    }
    assert all("admin:dest:set:" not in (button.callback_data or "") for button in buttons)
    assert {
        button.text: button.url
        for button in buttons
        if button.url
    } == {
        "➕ Добавить канал": "https://t.me/TESTPOOLMASTERBOT?startchannel&admin=post_messages",
        "➕ Добавить группу": "https://t.me/TESTPOOLMASTERBOT?startgroup&admin=manage_chat",
    }


def test_channel_details_have_schedule_manual_publish_and_back():
    destination = {
        "chat_id": -100123,
        "name": "Official Channel",
        "type": "channel",
        "enabled": False,
        "farming_interval_hours": None,
    }
    destination.update({"bot_status": "administrator", "can_post_messages": True})
    buttons = _buttons(
        admin_keyboard("destination", destination=destination, bot_username="TESTPOOLMASTERBOT")
    )

    assert {button.callback_data for button in buttons} == {
        "admin:dest:set:-100123:3",
        "admin:dest:set:-100123:6",
        "admin:dest:set:-100123:12",
        "admin:dest:set:-100123:24",
        "admin:dest:set:-100123:off",
        "admin:dest:publish:-100123",
        "admin:destinations",
    }
    assert any(button.text == "Опубликовать сейчас" for button in buttons)
    off_button = next(button for button in buttons if button.callback_data == "admin:dest:set:-100123:off")
    assert off_button.text == "✓ Автопост выключен"
    assert off_button.style == "danger"
    assert next(button for button in buttons if button.callback_data == "admin:destinations").style == "danger"


def test_active_schedule_is_green_and_turn_off_action_is_red():
    destination = {
        "chat_id": -100123,
        "name": "Official Channel",
        "type": "channel",
        "enabled": True,
        "farming_interval_hours": 6,
        "bot_status": "administrator",
        "can_post_messages": True,
    }

    buttons = _buttons(
        admin_keyboard("destination", destination=destination, bot_username="TESTPOOLMASTERBOT")
    )
    by_callback = {button.callback_data: button for button in buttons}

    assert by_callback["admin:dest:set:-100123:6"].text == "✓ 6 ч"
    assert by_callback["admin:dest:set:-100123:6"].style == "success"
    assert by_callback["admin:dest:set:-100123:3"].style is None
    assert by_callback["admin:dest:set:-100123:off"].text == "Выключить автопост"
    assert by_callback["admin:dest:set:-100123:off"].style == "danger"


@pytest.mark.parametrize(
    ("destination", "expected_url"),
    [
        (
            {"chat_id": -100123, "name": "Channel", "type": "channel", "bot_status": "administrator", "can_post_messages": False},
            "https://t.me/TESTPOOLMASTERBOT?startchannel&admin=post_messages",
        ),
        (
            {"chat_id": -100456, "name": "Group", "type": "supergroup", "bot_status": "kicked", "can_post_messages": False},
            "https://t.me/TESTPOOLMASTERBOT?startgroup&admin=manage_chat",
        ),
    ],
)
def test_unconnected_destination_has_reconnect_but_no_schedule_or_publish_actions(destination, expected_url):
    buttons = _buttons(
        admin_keyboard("destination", destination=destination, bot_username="TESTPOOLMASTERBOT")
    )

    assert any(button.text == "Переподключить" and button.url == expected_url for button in buttons)
    assert all(button.callback_data is None or not button.callback_data.startswith("admin:dest:set:") for button in buttons)
    assert all(button.callback_data is None or not button.callback_data.startswith("admin:dest:publish:") for button in buttons)


def test_manual_push_preview_buttons_are_single_frame_and_styled():
    buttons = _buttons(admin_keyboard("push_confirm", token="abcdef123456", confirm_enabled=True))

    confirm = next(button for button in buttons if button.callback_data == "admin:push:confirm:abcdef123456")
    cancel = next(button for button in buttons if button.callback_data == "admin:push:cancel:abcdef123456")
    assert confirm.style == "success"
    assert cancel.style == "danger"


def test_empty_destination_screen_offers_human_friendly_add_channel_action():
    buttons = _buttons(admin_keyboard("destinations", bot_username="TESTPOOLMASTERBOT"))

    assert {button.text for button in buttons if button.url} == {"➕ Добавить канал", "➕ Добавить группу"}


def test_manual_publication_preview_has_cancel_path():
    preview_buttons = _buttons(admin_keyboard("destination_confirm", token="token123", chat_id=-100123))

    assert {button.text for button in preview_buttons} == {"Опубликовать", "Отмена"}


@pytest.mark.parametrize(
    ("username", "destination_type", "expected"),
    [
        ("TESTPOOLMASTERBOT", "channel", "https://t.me/TESTPOOLMASTERBOT?startchannel&admin=post_messages"),
        ("@JETTONPOOL_BOT", "group", "https://t.me/JETTONPOOL_BOT?startgroup&admin=manage_chat"),
    ],
)
def test_destination_add_links_use_runtime_username_and_only_required_rights(
    username, destination_type, expected
):
    builder = getattr(keyboards, "build_destination_add_link", None)
    assert callable(builder), "native destination link builder is missing"

    assert builder(username, destination_type) == expected


@pytest.mark.parametrize(
    ("username", "destination_type"),
    [
        ("TESTPOOLMASTERBOT", "private"),
        ("", "channel"),
        ("@", "group"),
        ("https://t.me/OtherBot", "channel"),
    ],
)
def test_destination_add_links_reject_invalid_type_or_username(username, destination_type):
    builder = getattr(keyboards, "build_destination_add_link", None)
    assert callable(builder), "native destination link builder is missing"

    with pytest.raises(ValueError):
        builder(username, destination_type)
