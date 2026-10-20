from keyboards import (
    admin_keyboard,
    channel_post_keyboard,
    group_response_keyboard,
    inline_result_keyboard,
    private_alert_keyboard,
    farming_history_keyboard,
    private_user_keyboard,
)
from farming import FARMING_POOLS


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_private_home_includes_primary_actions_and_language_selection():
    buttons = _buttons(private_user_keyboard("home"))

    assert [button.text for button in buttons] == ["Цена", "Фарминг", "Уведомления", "Язык / Language"]
    assert {button.callback_data for button in buttons} == {"ui:price", "ui:farming", "ui:alerts", "ui:language"}


def test_farming_screen_keeps_pool_links_without_duplicate_alert_entry():
    buttons = _buttons(private_user_keyboard("farming"))

    assert not any(button.callback_data == "ui:alerts" for button in buttons)
    assert {
        button.url
        for button in buttons
        if button.url and "гайд" not in button.text.lower() and "guide" not in button.text.lower()
    } == {pool.url for pool in FARMING_POOLS}


def test_private_farming_keyboard_opens_history_and_group_does_not():
    private_buttons = _buttons(private_user_keyboard("farming"))
    public_buttons = _buttons(group_response_keyboard())

    history_button = next(button for button in private_buttons if button.callback_data == "ui:farming:history")
    assert history_button.text == "История"
    english_button = next(
        button
        for button in _buttons(private_user_keyboard("farming", language="en"))
        if button.callback_data == "ui:farming:history"
    )
    assert english_button.text == "History"
    assert all(button.callback_data != "ui:farming:history" for button in public_buttons)
    assert all(button.callback_data is None for button in _buttons(inline_result_keyboard()))


def test_history_keyboard_has_only_danger_back():
    buttons = _buttons(farming_history_keyboard("en"))

    back = next(button for button in buttons if button.callback_data == "ui:farming:history:back")
    assert len(buttons) == 1
    assert back.text == "Back"
    assert back.style == "danger"


def test_private_farming_keyboard_includes_official_ru_and_en_guides():
    buttons = _buttons(private_user_keyboard("farming"))

    guides = {
        button.text: button.url
        for button in buttons
        if button.url and ("гайд" in button.text.lower() or "guide" in button.text.lower())
    }
    assert guides == {
        "Гайд DeDust · Русский": "https://help.dedust.io/en/ru/liquidity/pools",
        "DeDust guide · English": "https://help.dedust.io/en/liquidity/pools",
        "Гайд STON.fi · Русский": "https://guide.ston.fi/ru/farming/kak-farmit-na-ston.fi",
        "STON.fi guide · English": "https://guide.ston.fi/providing-liquidity/how-to-provide-liquidity-on-ston.fi",
    }


def test_public_keyboards_have_no_personal_or_admin_callbacks():
    for markup in (group_response_keyboard(), channel_post_keyboard(), inline_result_keyboard()):
        buttons = _buttons(markup)
        assert all(button.url for button in buttons)
        assert all(button.callback_data is None for button in buttons)


def test_public_pool_links_use_gram_pair_for_both_exchanges():
    buttons = _buttons(group_response_keyboard())

    assert any(button.text == "DeDust JETTON/GRAM" for button in buttons)
    assert any(button.text == "STON.fi JETTON/GRAM" for button in buttons)


def test_admin_subscreen_back_button_uses_danger_style():
    buttons = _buttons(admin_keyboard("status"))

    back = next(button for button in buttons if button.callback_data == "admin:home")
    assert back.style == "danger"


def test_alerts_screen_exposes_switches_and_configuration_screens():
    buttons = _buttons(private_user_keyboard("alerts"))

    callbacks = {button.callback_data for button in buttons}
    assert {
        "ui:alert:yield:toggle",
        "ui:alert:yield",
        "ui:alert:price:toggle",
        "ui:alert:price",
    } <= callbacks
    assert not any(callback and callback.startswith("ui:set:") for callback in callbacks)
    labels = {button.text for button in buttons}
    assert "Доходность · ВЫКЛ" in labels
    assert "Цена · ВЫКЛ" in labels
    assert "Пауза 24 ч" in labels
    assert not any(term in label for label in labels for term in ("APR", "APY", "farming alerts", "ON/OFF"))

    assert next(button for button in buttons if button.callback_data == "ui:alert:yield:toggle").style == "danger"
    assert next(button for button in buttons if button.callback_data == "ui:alert:price:toggle").style == "danger"


def test_private_alert_controls_open_notifications_and_pause():
    buttons = _buttons(private_alert_keyboard())

    assert {button.callback_data for button in buttons} == {"ui:alerts", "ui:pause24"}
