from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from admin_ui import render_admin_screen
from farming import FARMING_POOLS, IdentityCheck, PoolMetrics, RateMetric, SourceStatus
from price import PriceMetrics
from snapshot import DataSnapshot


NOW = datetime(2026, 10, 17, 17, 30, tzinfo=timezone.utc)


def _snapshot():
    farming = {}
    for pool in FARMING_POOLS:
        kind = "APY" if pool.dex == "STON.fi" else "APR"
        rate = RateMetric(Decimal("12.5"), kind, "source")
        farming[pool.address] = PoolMetrics(
            pool,
            (rate,),
            (),
            rate,
            Decimal("1200"),
            NOW,
            SourceStatus.OK,
            True,
        )
    price = PriceMetrics(
        Decimal("0.02"),
        Decimal("0.019"),
        Decimal("0.021"),
        Decimal("12000"),
        "JETTONUSDT",
        "USDT",
        "MEXC spot",
        NOW,
        NOW,
        SourceStatus.OK,
    )
    statuses = {"farming": "ok", "price": "ok"}
    statuses.update({f"farming:{pool.address}": "ok" for pool in FARMING_POOLS})
    return DataSnapshot(NOW, farming, price, statuses, {})


def _state(**overrides):
    return {
        "snapshot": _snapshot(),
        "global_paused": True,
        "subscriber_count": 12,
        "destinations": [],
        "bot_started_at": NOW,
        "pool_identity_checks": {pool.address: IdentityCheck(True, "verified") for pool in FARMING_POOLS},
        "last_auto_alert_at": NOW,
        "last_auto_publication_at": NOW,
        "last_manual_push_at": NOW,
        "now": NOW,
        **overrides,
    }


def test_admin_home_contains_only_human_friendly_admin_overview():
    text = render_admin_screen("home", **_state())

    assert "POOLMASTER ADMIN" in text
    assert "Пользователей: <b>12</b>" in text
    assert "Каналов и групп: <b>0</b>" in text
    for internal in ("Auto push", "Subscribers", "Managed destinations", "Farming data", "Snapshot"):
        assert internal not in text


def test_admin_status_labels_destination_count_as_channels_and_groups():
    destinations = [{}, {}, {}, {}]
    text = render_admin_screen("status", **_state(destinations=destinations))

    assert "Каналов и групп: <b>4</b>" in text
    assert "Каналов: <b>4</b>" not in text

    english = render_admin_screen("status", **_state(destinations=destinations, language="en"))
    assert "Channels and groups: <b>4</b>" in english


def test_admin_destination_screen_escapes_names_and_shows_schedule_and_last_post():
    destination = {
        "chat_id": -100123,
        "name": "<Official & Test>",
        "type": "channel",
        "enabled": True,
        "farming_interval_hours": 12,
        "last_publication_at": NOW.isoformat(),
        "last_fingerprint": "hash",
        "bot_status": "administrator",
        "can_post_messages": True,
    }

    text = render_admin_screen("destinations", **_state(destinations=[destination]))

    assert "&lt;Official &amp; Test&gt;" in text
    assert "12 ч" in text
    assert "Канал · Подключён" in text
    assert NOW.strftime("%Y-%m-%d %H:%M UTC") in text


def test_admin_destination_list_uses_human_connection_states_without_telegram_details():
    destinations = [
        {
            "chat_id": -100123,
            "name": "News",
            "type": "channel",
            "enabled": True,
            "farming_interval_hours": 6,
            "bot_status": "administrator",
            "can_post_messages": True,
        },
        {
            "chat_id": -100456,
            "name": "Community",
            "type": "supergroup",
            "enabled": False,
            "farming_interval_hours": None,
            "bot_status": "administrator",
            "can_post_messages": False,
        },
        {
            "chat_id": -100789,
            "name": "Old group",
            "type": "group",
            "enabled": False,
            "farming_interval_hours": None,
            "bot_status": "kicked",
            "can_post_messages": False,
        },
    ]

    text = render_admin_screen("destinations", **_state(destinations=destinations))

    assert "Каналы и группы" in text
    assert "Канал · Подключён" in text
    assert "Группа · Недостаточно прав" in text
    assert "Группа · Отключён" in text
    assert "-100123" not in text
    assert "administrator" not in text
    assert "can_post_messages" not in text
    assert "Перешлите" not in text
    assert "chat ID" not in text
    assert "@username" not in text


def test_admin_destination_details_use_chat_neutral_connection_state_copy():
    text = render_admin_screen(
        "destination",
        **_state(),
        destination={
            "chat_id": -100123,
            "name": "Community",
            "type": "supergroup",
            "enabled": True,
            "farming_interval_hours": 6,
            "bot_status": "administrator",
            "can_post_messages": False,
            "last_publication_at": NOW.isoformat(),
        },
    )

    assert "Группа · Недостаточно прав" in text
    assert "Автопост: <b>выключен</b>" in text
    assert "administrator" not in text
    assert "can_post_messages" not in text


@pytest.mark.parametrize(
    ("language", "expected"),
    [("ru", "Канал · Подключён"), ("en", "Channel · Connected")],
)
def test_admin_destination_status_follows_admin_language(language, expected):
    text = render_admin_screen(
        "destination",
        **_state(),
        destination={
            "chat_id": -100123,
            "name": "News",
            "type": "channel",
            "enabled": False,
            "farming_interval_hours": None,
            "bot_status": "creator",
            "can_post_messages": True,
        },
        language=language,
    )

    assert expected in text


def test_admin_status_screen_shows_sources_identity_snapshot_and_uptime():
    text = render_admin_screen("status", **_state())

    assert "DeDust" in text
    assert "STON.fi" in text
    assert "MEXC" in text
    assert "Автоуведомления пользователям" in text
    assert "Время работы" in text


def test_admin_status_reports_canonical_chart_market():
    text = render_admin_screen("status", **_state())

    assert "MEXC: <b>ОК</b>" in text


@pytest.mark.parametrize(
    ("language", "expected"),
    [("ru", "DeDust: <b>ОК</b>"), ("en", "DeDust: <b>OK</b>")],
)
def test_admin_status_localizes_healthy_sources(language, expected):
    text = render_admin_screen("status", **_state(), language=language)

    assert expected in text


@pytest.mark.parametrize(
    ("language", "expected"),
    [("ru", "STON.fi: <b>частично</b>"), ("en", "STON.fi: <b>partial</b>")],
)
def test_admin_status_reports_partial_source_when_primary_yield_is_usable(language, expected):
    snapshot = _snapshot()
    pool = next(pool for pool in FARMING_POOLS if pool.dex == "STON.fi")
    metrics = replace(snapshot.farming[pool.address], status=SourceStatus.PARTIAL, tvl_usd=None)
    snapshot = replace(
        snapshot,
        farming={**snapshot.farming, pool.address: metrics},
        source_statuses={**snapshot.source_statuses, f"farming:{pool.address}": "partial"},
    )

    text = render_admin_screen("status", **_state(snapshot=snapshot), language=language)

    assert expected in text
    assert "STON.fi: <b>ошибка</b>" not in text
    assert "STON.fi: <b>error</b>" not in text


@pytest.mark.parametrize(
    ("language", "expected"),
    [("ru", "DeDust: <b>ошибка</b>"), ("en", "DeDust: <b>error</b>")],
)
def test_admin_status_reports_error_when_primary_source_is_unavailable(language, expected):
    snapshot = _snapshot()
    pool = next(pool for pool in FARMING_POOLS if pool.dex == "DeDust")
    metrics = replace(
        snapshot.farming[pool.address],
        farm_rates=(),
        total_rate=None,
        status=SourceStatus.TIMEOUT,
    )
    snapshot = replace(
        snapshot,
        farming={**snapshot.farming, pool.address: metrics},
        source_statuses={**snapshot.source_statuses, f"farming:{pool.address}": "timeout"},
    )

    text = render_admin_screen("status", **_state(snapshot=snapshot), language=language)

    assert expected in text


def test_admin_users_screen_is_a_simple_private_registry_count():
    text = render_admin_screen("users", **_state())

    assert "<b>Пользователи</b>" in text
    assert "Всего: <b>12</b>" in text


def test_unknown_admin_screen_is_rejected():
    with pytest.raises(ValueError):
        render_admin_screen("unknown", **_state())


@pytest.mark.parametrize("screen", ["farming", "price"])
def test_admin_has_no_duplicate_user_screens(screen):
    with pytest.raises(ValueError):
        render_admin_screen(screen, **_state())
