import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import managed_destinations
from telegram.error import TelegramError

from farming import FARMING_POOLS, PoolMetrics, RateMetric, SourceStatus
from keyboards import admin_keyboard
from managed_destinations import (
    ChannelPublicationCoordinator,
    ManagedDestinationStore,
    dispatch_managed_publications,
    farming_publication_card,
)
from price import PriceMetrics
from snapshot import DataSnapshot


NOW = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)
ADMIN_IDS = {111, 222}


def _snapshot(*, first_apr="12.50", anomalies=None, stale=False):
    farming = {}
    for index, pool in enumerate(FARMING_POOLS):
        kind = "APY" if pool.dex == "STON.fi" else "APR"
        value = Decimal(first_apr) if index == 0 else Decimal("7.25")
        rate = RateMetric(value, kind, "farming")
        farming[pool.address] = PoolMetrics(
            pool=pool,
            farm_rates=(rate,),
            pool_rates=(),
            total_rate=rate,
            tvl_usd=Decimal("12345.6"),
            fetched_at=NOW,
            status=SourceStatus.OK,
            identity_valid=True,
            is_stale=stale,
        )
    price = PriceMetrics(
        price_usdt=Decimal("0.02"),
        bid_usdt=None,
        ask_usdt=None,
        quote_volume_24h=None,
        symbol="JETTONUSDT",
        quote_currency="USDT",
        source="MEXC spot",
        fetched_at=NOW,
        source_timestamp=NOW,
        status=SourceStatus.OK,
    )
    return DataSnapshot(NOW, farming, price, {"farming": "ok", "price": "ok"}, anomalies or {})


def _register(store, chat_id=-100123, *, name="POOLMASTER Channel", kind="channel"):
    return store.register_destination(
        chat_id,
        name,
        kind,
        actor_id=111,
        admin_ids=ADMIN_IDS,
    )


def _enabled_store(tmp_path, *, interval=3):
    store = ManagedDestinationStore(tmp_path / "managed.json")
    _register(store)
    store.set_schedule(-100123, interval, actor_id=111, admin_ids=ADMIN_IDS)
    return store


def test_managed_publication_card_uses_public_gram_labels():
    card = farming_publication_card(_snapshot())

    assert card is not None
    assert "DeDust JETTON/GRAM" in card[0]
    assert "STON.fi JETTON/GRAM" in card[0]
    assert "JETTON/TON" not in card[0]


@pytest.mark.parametrize("pool_index", [0, 2])
def test_managed_publication_accepts_partial_primary_yield_without_auxiliary_fields(pool_index):
    snapshot = _snapshot()
    pool = FARMING_POOLS[pool_index]
    partial = replace(
        snapshot.farming[pool.address],
        status=SourceStatus.PARTIAL,
        pool_rates=(),
        total_rate=None,
        tvl_usd=None,
    )
    snapshot = replace(snapshot, farming={**snapshot.farming, pool.address: partial})

    card = farming_publication_card(snapshot)

    assert card is not None
    assert "Доходность: <b>12.50%</b>" in card[0]


def test_destination_store_starts_empty_and_registration_is_admin_only(tmp_path):
    path = tmp_path / "managed.json"
    store = ManagedDestinationStore(path)

    with pytest.raises(PermissionError):
        store.register_destination(-100123, "Channel", "channel", actor_id=333, admin_ids=ADMIN_IDS)

    assert store.list_destinations() == []
    assert not path.exists()


def test_corrupt_destination_state_is_backed_up_before_empty_allow_list_is_used(tmp_path):
    path = tmp_path / "managed.json"
    original = '{"schema_version": 1, "destinations": {"-100123": {"enabled": true}}}'
    path.write_text(original, encoding="utf-8")

    store = ManagedDestinationStore(path)

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("managed.json.corrupt-*"))
    assert store.list_destinations() == []


def test_corrupt_destination_state_fails_closed_if_backup_cannot_be_created(tmp_path, monkeypatch):
    path = tmp_path / "managed.json"
    original = '{"schema_version": 1, "destinations": []}'
    path.write_text(original, encoding="utf-8")

    def fail_copy(*_args, **_kwargs):
        raise OSError("backup disk unavailable")

    monkeypatch.setattr(managed_destinations, "secure_backup_file", fail_copy)

    with pytest.raises(OSError, match="backup disk unavailable"):
        ManagedDestinationStore(path)

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "chat_id,name,kind",
    [
        (123, "private chat", "group"),
        (0, "zero", "channel"),
        (-100123, "", "channel"),
        (-100123, "valid name", "private"),
        (-100123, "x" * 129, "group"),
    ],
)
def test_destination_registration_rejects_invalid_id_name_or_type(tmp_path, chat_id, name, kind):
    store = ManagedDestinationStore(tmp_path / "managed.json")

    with pytest.raises(ValueError):
        _register(store, chat_id, name=name, kind=kind)


def test_schedule_presets_persist_and_off_disables_destination(tmp_path):
    path = tmp_path / "managed.json"
    store = ManagedDestinationStore(path)
    destination = _register(store)
    assert destination["enabled"] is False
    assert destination["farming_interval_hours"] is None

    for interval in (3, 6, 12, 24):
        store.set_schedule(-100123, interval, actor_id=111, admin_ids=ADMIN_IDS)
        saved = ManagedDestinationStore(path).get_destination(-100123)
        assert saved["enabled"] is True
        assert saved["farming_interval_hours"] == interval

    store.set_schedule(-100123, None, actor_id=111, admin_ids=ADMIN_IDS)
    saved = ManagedDestinationStore(path).get_destination(-100123)
    assert saved["enabled"] is False
    assert saved["farming_interval_hours"] is None


def test_non_admin_cannot_change_schedule_and_unsupported_intervals_are_rejected(tmp_path):
    store = ManagedDestinationStore(tmp_path / "managed.json")
    _register(store)

    with pytest.raises(PermissionError):
        store.set_schedule(-100123, 6, actor_id=333, admin_ids=ADMIN_IDS)
    with pytest.raises(ValueError):
        store.set_schedule(-100123, 9, actor_id=111, admin_ids=ADMIN_IDS)

    assert store.get_destination(-100123)["enabled"] is False


def test_destination_registration_preserves_schedule_and_publication_state(tmp_path):
    store = _enabled_store(tmp_path)
    store.record_successful_publication(-100123, "fingerprint", NOW)

    updated = store.register_destination(
        -100123,
        "Renamed Channel",
        "channel",
        actor_id=222,
        admin_ids=ADMIN_IDS,
    )

    assert updated["name"] == "Renamed Channel"
    assert updated["farming_interval_hours"] == 3
    assert updated["last_fingerprint"] == "fingerprint"


def test_detected_candidates_start_off_and_membership_removal_preserves_history(tmp_path):
    store = ManagedDestinationStore(tmp_path / "managed.json")

    candidate = store.observe_bot_membership(
        chat_id=-100123,
        name="JETTON Test Channel",
        destination_type="channel",
        bot_status="administrator",
        can_post_messages=True,
        observed_at=NOW,
    )
    assert candidate["enabled"] is False
    assert candidate["farming_interval_hours"] is None
    assert candidate["bot_status"] == "administrator"
    assert candidate["can_post_messages"] is True

    store.set_schedule(-100123, 6, actor_id=111, admin_ids=ADMIN_IDS)
    store.record_successful_publication(-100123, "history", NOW)
    removed = store.observe_bot_membership(
        chat_id=-100123,
        name="JETTON Test Channel",
        destination_type="channel",
        bot_status="left",
        can_post_messages=False,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert removed["enabled"] is False
    assert removed["farming_interval_hours"] is None
    assert removed["bot_status"] == "left"
    assert removed["last_fingerprint"] == "history"


def test_rights_loss_turns_autopost_off_and_preserves_candidate(tmp_path):
    store = ManagedDestinationStore(tmp_path / "managed.json")
    store.observe_bot_membership(
        chat_id=-100123,
        name="JETTON Test Channel",
        destination_type="channel",
        bot_status="administrator",
        can_post_messages=True,
        observed_at=NOW,
    )
    store.set_schedule(-100123, 3, actor_id=111, admin_ids=ADMIN_IDS)

    updated = store.observe_bot_membership(
        chat_id=-100123,
        name="JETTON Test Channel",
        destination_type="channel",
        bot_status="administrator",
        can_post_messages=False,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert updated["enabled"] is False
    assert updated["can_post_messages"] is False


def test_manual_channel_preview_confirm_publishes_to_only_selected_channel(tmp_path):
    store = ManagedDestinationStore(tmp_path / "managed.json")
    _register(store, chat_id=-100123, name="Channel One")
    _register(store, chat_id=-100456, name="Channel Two")
    flow = ChannelPublicationCoordinator()
    snapshot = _snapshot()
    preview = flow.create_preview(
        actor_id=111,
        admin_ids=ADMIN_IDS,
        chat_id=-100123,
        snapshot=snapshot,
        destinations=store,
        now=NOW,
    )

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)

    bot = FakeBot()

    async def snapshot_provider():
        return snapshot

    result = asyncio.run(
        flow.confirm(
            preview.token,
            actor_id=111,
            admin_ids=ADMIN_IDS,
            snapshot_provider=snapshot_provider,
            bot=bot,
            destinations=store,
            now=NOW,
        )
    )

    assert result.status == "complete"
    assert [message["chat_id"] for message in bot.messages] == [-100123]
    assert "Доходность" in bot.messages[0]["text"]
    assert store.get_destination(-100123)["last_manual_fingerprint"] == preview.fingerprint
    assert store.get_destination(-100456)["last_manual_fingerprint"] is None


def test_channel_preview_cancel_and_double_confirm_are_one_use(tmp_path):
    store = ManagedDestinationStore(tmp_path / "managed.json")
    _register(store)
    flow = ChannelPublicationCoordinator()
    snapshot = _snapshot()
    cancelled = flow.create_preview(
        actor_id=111,
        admin_ids=ADMIN_IDS,
        chat_id=-100123,
        snapshot=snapshot,
        destinations=store,
        now=NOW,
    )
    assert flow.cancel(cancelled.token, actor_id=111, admin_ids=ADMIN_IDS, now=NOW)

    active = flow.create_preview(
        actor_id=111,
        admin_ids=ADMIN_IDS,
        chat_id=-100123,
        snapshot=snapshot,
        destinations=store,
        now=NOW,
    )
    attempts = []

    class FakeBot:
        async def send_message(self, **kwargs):
            attempts.append(kwargs)

    async def snapshot_provider():
        return snapshot

    async def confirm_twice():
        return await asyncio.gather(*(
            flow.confirm(
                active.token,
                actor_id=111,
                admin_ids=ADMIN_IDS,
                snapshot_provider=snapshot_provider,
                bot=FakeBot(),
                destinations=store,
                now=NOW,
            )
            for _ in range(2)
        ))

    first, second = asyncio.run(confirm_twice())
    assert sorted((first.status, second.status)) == ["complete", "invalid"]
    assert len(attempts) == 1


def test_removed_channel_cannot_be_manually_published(tmp_path):
    store = ManagedDestinationStore(tmp_path / "managed.json")
    _register(store)
    flow = ChannelPublicationCoordinator()
    snapshot = _snapshot()
    preview = flow.create_preview(
        actor_id=111,
        admin_ids=ADMIN_IDS,
        chat_id=-100123,
        snapshot=snapshot,
        destinations=store,
        now=NOW,
    )
    store.observe_bot_membership(
        chat_id=-100123,
        name="POOLMASTER Channel",
        destination_type="channel",
        bot_status="left",
        can_post_messages=False,
        observed_at=NOW,
    )
    sent = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    async def snapshot_provider():
        return snapshot

    result = asyncio.run(
        flow.confirm(
            preview.token,
            actor_id=111,
            admin_ids=ADMIN_IDS,
            snapshot_provider=snapshot_provider,
            bot=FakeBot(),
            destinations=store,
            now=NOW,
        )
    )

    assert result.status == "unavailable"
    assert sent == []


def test_removed_channel_is_skipped_by_scheduled_publications(tmp_path):
    store = _enabled_store(tmp_path, interval=3)
    store.observe_bot_membership(
        chat_id=-100123,
        name="POOLMASTER Channel",
        destination_type="channel",
        bot_status="left",
        can_post_messages=False,
        observed_at=NOW,
    )

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)

    telegram = FakeBot()
    result = asyncio.run(dispatch_managed_publications(store, telegram, _snapshot(), now=NOW))

    assert result.sent == 0
    assert telegram.messages == []


def test_manual_publication_fingerprint_is_separate_from_scheduled_fingerprint(tmp_path):
    store = _enabled_store(tmp_path)
    store.record_successful_publication(-100123, "scheduled-hash", NOW)

    store.record_successful_manual_publication(-100123, "manual-hash", NOW + timedelta(minutes=1))

    saved = store.get_destination(-100123)
    assert saved["last_fingerprint"] == "scheduled-hash"
    assert saved["last_manual_fingerprint"] == "manual-hash"
    assert saved["last_manual_publication_at"] == (NOW + timedelta(minutes=1)).isoformat()


def test_recent_manual_card_suppresses_identical_scheduled_card(tmp_path):
    store = _enabled_store(tmp_path, interval=3)
    snapshot = _snapshot()
    from managed_destinations import farming_publication_card

    fingerprint = farming_publication_card(snapshot)[1]
    store.record_successful_manual_publication(-100123, fingerprint, NOW)

    async def run():
        bot = _RecordingBot()
        result = await dispatch_managed_publications(
            store,
            bot,
            snapshot,
            now=NOW + timedelta(hours=4),
        )
        return bot, result

    bot, result = asyncio.run(run())

    assert result.sent == 0
    assert result.skipped == 1
    assert bot.messages == []


def test_destination_connection_state_distinguishes_permission_and_membership_status():
    state = getattr(managed_destinations, "destination_connection_state", None)
    assert callable(state), "destination connection-state helper is missing"

    assert state({"bot_status": "administrator", "can_post_messages": True}) == "connected"
    assert state({"bot_status": "member", "can_post_messages": True}) == "connected"
    assert state({"bot_status": "administrator", "can_post_messages": False}) == "insufficient_rights"
    assert state({"bot_status": "unknown", "can_post_messages": False}) == "insufficient_rights"
    assert state({"bot_status": "left", "can_post_messages": False}) == "disconnected"
    assert state({"bot_status": "kicked", "can_post_messages": False}) == "disconnected"


def test_existing_restricted_group_keeps_schedule_and_can_be_published(tmp_path):
    store = ManagedDestinationStore(tmp_path / "managed.json")
    _register(store, kind="group")
    store.set_schedule(-100123, 12, actor_id=111, admin_ids=ADMIN_IDS)

    destination = store.observe_bot_membership(
        chat_id=-100123,
        name="POOLMASTER Group",
        destination_type="group",
        bot_status="restricted",
        can_post_messages=True,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert destination["enabled"] is True
    assert destination["farming_interval_hours"] == 12
    assert managed_destinations.destination_connection_state(destination) == "connected"
    assert store.set_schedule(-100123, 6, actor_id=111, admin_ids=ADMIN_IDS)["enabled"] is True

    flow = ChannelPublicationCoordinator()
    snapshot = _snapshot()
    preview = flow.create_preview(
        actor_id=111,
        admin_ids=ADMIN_IDS,
        chat_id=-100123,
        snapshot=snapshot,
        destinations=store,
        now=NOW,
    )
    sent = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    async def snapshot_provider():
        return snapshot

    manual_result = asyncio.run(
        flow.confirm(
            preview.token,
            actor_id=111,
            admin_ids=ADMIN_IDS,
            snapshot_provider=snapshot_provider,
            bot=FakeBot(),
            destinations=store,
            now=NOW,
        )
    )

    assert manual_result.status == "complete"
    assert [message["chat_id"] for message in sent] == [-100123]


@pytest.mark.parametrize(
    "permissions",
    [None, SimpleNamespace(can_send_messages=None)],
    ids=["missing", "unset"],
)
def test_legacy_member_requires_explicit_or_previously_verified_send_permission(permissions):
    membership = SimpleNamespace(status="member")

    assert managed_destinations.membership_can_publish(
        "supergroup",
        membership,
        existing=True,
        chat_permissions=permissions,
    ) is False
    assert managed_destinations.membership_can_publish(
        "supergroup",
        membership,
        existing=True,
        chat_permissions=permissions,
        previously_verified_can_post=True,
    ) is True


@pytest.mark.parametrize(
    "permissions",
    [None, SimpleNamespace(can_send_messages=None)],
    ids=["missing", "unset"],
)
def test_new_group_member_with_unknown_send_permission_is_not_publishable(permissions):
    assert managed_destinations.membership_can_publish(
        "supergroup",
        SimpleNamespace(status="member"),
        existing=False,
        chat_permissions=permissions,
    ) is False


def _membership(status, **fields):
    user = fields.pop("user", SimpleNamespace(id=8674322726))
    return SimpleNamespace(status=status, user=user, **fields)


class _VerificationBot:
    def __init__(self, target, membership):
        self.target = target
        self.membership = membership
        self.calls = []

    async def get_chat(self, chat_id):
        self.calls.append(("get_chat", chat_id))
        if isinstance(self.target, Exception):
            raise self.target
        return self.target

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append(("get_chat_member", chat_id, user_id))
        if isinstance(self.membership, Exception):
            raise self.membership
        return self.membership


def _target(chat_id=-100123, chat_type="channel", **fields):
    defaults = {"id": chat_id, "type": chat_type, "title": "Verified destination", "username": None}
    defaults.update(fields)
    return SimpleNamespace(**defaults)


@pytest.mark.parametrize(
    ("chat_type", "status", "member_fields", "existing", "can_post"),
    [
        ("channel", "creator", {}, False, True),
        ("channel", "administrator", {"can_post_messages": True}, False, True),
        ("channel", "administrator", {"can_post_messages": False}, False, False),
        ("channel", "left", {}, True, False),
        ("channel", "kicked", {}, True, False),
        ("group", "creator", {}, False, True),
        ("supergroup", "administrator", {}, False, True),
        (
            "group",
            "member",
            {},
            False,
            False,
        ),
        (
            "supergroup",
            "member",
            {},
            True,
            True,
        ),
        (
            "supergroup",
            "member",
            {},
            True,
            False,
        ),
        (
            "group",
            "restricted",
            {"is_member": True, "can_send_messages": True},
            True,
            True,
        ),
        (
            "group",
            "restricted",
            {"is_member": True, "can_send_messages": True},
            False,
            False,
        ),
    ],
)
def test_verify_managed_destination_uses_chat_type_and_effective_bot_permissions(
    chat_type, status, member_fields, existing, can_post
):
    verify = getattr(managed_destinations, "verify_managed_destination", None)
    assert callable(verify), "managed-destination verification helper is missing"
    permissions = SimpleNamespace(can_send_messages=can_post) if status == "member" else None
    bot = _VerificationBot(
        _target(chat_type=chat_type, permissions=permissions),
        _membership(status, **member_fields),
    )

    result = asyncio.run(verify(bot, -100123, 8674322726, existing=existing))

    assert result.chat_id == -100123
    assert result.name == "Verified destination"
    assert result.destination_type == chat_type
    assert result.bot_status == status
    assert result.can_post_messages is can_post
    assert bot.calls == [("get_chat", -100123), ("get_chat_member", -100123, 8674322726)]


@pytest.mark.parametrize(
    ("permissions", "previously_verified_can_post", "expected"),
    [
        (None, False, False),
        (SimpleNamespace(can_send_messages=None), False, False),
        (None, True, True),
        (SimpleNamespace(can_send_messages=None), True, True),
        (SimpleNamespace(can_send_messages=False), True, False),
    ],
    ids=[
        "missing-unverified",
        "unset-unverified",
        "missing-legacy",
        "unset-legacy",
        "explicit-denial-overrides-legacy",
    ],
)
def test_verify_managed_destination_fails_closed_on_unknown_member_permissions(
    permissions, previously_verified_can_post, expected
):
    verify = getattr(managed_destinations, "verify_managed_destination", None)
    assert callable(verify)
    bot = _VerificationBot(
        _target(chat_type="supergroup", permissions=permissions),
        _membership("member"),
    )

    result = asyncio.run(
        verify(
            bot,
            -100123,
            8674322726,
            existing=True,
            previously_verified_can_post=previously_verified_can_post,
        )
    )

    assert result.can_post_messages is expected


@pytest.mark.parametrize(
    ("target", "membership", "chat_id", "message"),
    [
        (_target(chat_id=-100456), _membership("administrator"), -100123, "ID"),
        (_target(chat_type="private"), _membership("administrator"), -100123, "type"),
        (_target(), _membership("administrator", user=SimpleNamespace(id=7)), -100123, "bot"),
    ],
)
def test_verify_managed_destination_rejects_mismatched_telegram_identity(
    target, membership, chat_id, message
):
    verify = getattr(managed_destinations, "verify_managed_destination", None)
    assert callable(verify), "managed-destination verification helper is missing"
    bot = _VerificationBot(target, membership)

    with pytest.raises(ValueError, match=message):
        asyncio.run(verify(bot, chat_id, 8674322726, existing=False))


@pytest.mark.parametrize("failure_method", ["get_chat", "get_chat_member"])
def test_verify_managed_destination_propagates_telegram_api_errors(failure_method):
    verify = getattr(managed_destinations, "verify_managed_destination", None)
    assert callable(verify), "managed-destination verification helper is missing"
    failure = TelegramError("simulated Telegram API failure")
    bot = _VerificationBot(
        failure if failure_method == "get_chat" else _target(),
        failure if failure_method == "get_chat_member" else _membership("administrator"),
    )

    with pytest.raises(TelegramError, match="simulated Telegram API failure"):
        asyncio.run(verify(bot, -100123, 8674322726, existing=False))


def test_scheduled_publication_sends_once_with_public_keyboard_and_persists_state(tmp_path):
    store = _enabled_store(tmp_path)

    async def run():
        bot = _RecordingBot()
        first = await dispatch_managed_publications(store, bot, _snapshot(), now=NOW)
        second = await dispatch_managed_publications(store, bot, _snapshot(), now=NOW + timedelta(hours=4))
        return bot, first, second

    bot, first, second = asyncio.run(run())

    assert first.sent == 1
    assert second.sent == 0
    assert second.skipped == 1
    assert len(bot.messages) == 1
    message = bot.messages[0]
    assert message["chat_id"] == -100123
    assert "Фарминг JETTON" in message["text"]
    assert message["parse_mode"] == "HTML"
    assert all(button.url for row in message["reply_markup"].inline_keyboard for button in row)
    assert all(
        button.callback_data is None
        for row in message["reply_markup"].inline_keyboard
        for button in row
    )
    state = store.get_destination(-100123)
    assert state["last_publication_at"] == NOW.isoformat()
    assert state["last_fingerprint"]


def test_changed_values_wait_for_interval_then_publish_new_fingerprint(tmp_path):
    store = _enabled_store(tmp_path, interval=3)
    first = _snapshot()
    changed = _snapshot(first_apr="14.25")

    async def run():
        bot = _RecordingBot()
        initial = await dispatch_managed_publications(store, bot, first, now=NOW)
        early = await dispatch_managed_publications(store, bot, changed, now=NOW + timedelta(hours=2))
        due = await dispatch_managed_publications(store, bot, changed, now=NOW + timedelta(hours=3))
        return bot, initial, early, due

    bot, initial, early, due = asyncio.run(run())

    assert initial.sent == 1
    assert early.sent == 0
    assert due.sent == 1
    assert len(bot.messages) == 2
    assert "14.25%" in bot.messages[-1]["text"]


@pytest.mark.parametrize("snapshot", [_snapshot(stale=True), _snapshot(anomalies={f"farming:{FARMING_POOLS[0].address}": "unconfirmed"})])
def test_stale_or_anomalous_farming_snapshot_is_not_published(tmp_path, snapshot):
    store = _enabled_store(tmp_path)

    async def run():
        bot = _RecordingBot()
        result = await dispatch_managed_publications(store, bot, snapshot, now=NOW)
        return bot, result

    bot, result = asyncio.run(run())

    assert result.sent == 0
    assert bot.messages == []
    assert store.get_destination(-100123)["last_fingerprint"] is None


def test_forbidden_scheduled_publication_does_not_advance_history_and_turns_schedule_off(tmp_path):
    store = _enabled_store(tmp_path)

    async def run():
        bot = _RecordingBot(fail=True)
        result = await dispatch_managed_publications(store, bot, _snapshot(), now=NOW)
        return bot, result

    bot, result = asyncio.run(run())

    assert result.failed == 1
    assert store.get_destination(-100123)["last_fingerprint"] is None
    assert store.get_destination(-100123)["last_publication_at"] is None
    destination = store.get_destination(-100123)
    assert destination["can_post_messages"] is False
    assert destination["enabled"] is False
    assert destination["farming_interval_hours"] is None
    assert managed_destinations.destination_connection_state(destination) == "insufficient_rights"
    assert bot.attempts == 1
    with pytest.raises(ValueError, match="Бот не может публиковать"):
        store.set_schedule(-100123, 3, actor_id=111, admin_ids=ADMIN_IDS)


def test_forbidden_manual_publication_marks_destination_unavailable_and_turns_schedule_off(tmp_path):
    store = _enabled_store(tmp_path)
    flow = ChannelPublicationCoordinator()
    snapshot = _snapshot()
    preview = flow.create_preview(
        actor_id=111,
        admin_ids=ADMIN_IDS,
        chat_id=-100123,
        snapshot=snapshot,
        destinations=store,
        now=NOW,
    )

    class ForbiddenBot:
        async def send_message(self, **_kwargs):
            from telegram.error import Forbidden

            raise Forbidden("synthetic forbidden")

    async def snapshot_provider():
        return snapshot

    result = asyncio.run(
        flow.confirm(
            preview.token,
            actor_id=111,
            admin_ids=ADMIN_IDS,
            snapshot_provider=snapshot_provider,
            bot=ForbiddenBot(),
            destinations=store,
            now=NOW,
        )
    )
    destination = store.get_destination(-100123)

    assert result.status == "unavailable"
    assert destination["can_post_messages"] is False
    assert destination["enabled"] is False
    assert destination["farming_interval_hours"] is None
    assert managed_destinations.destination_connection_state(destination) == "insufficient_rights"
    with pytest.raises(ValueError, match="Бот не может публиковать"):
        store.set_schedule(-100123, 3, actor_id=111, admin_ids=ADMIN_IDS)
    buttons = [
        button
        for row in admin_keyboard(
            "destination",
            destination=destination,
            bot_username="TESTPOOLMASTERBOT",
        ).inline_keyboard
        for button in row
    ]
    assert any(button.url for button in buttons)
    assert not any(
        button.callback_data
        and button.callback_data.startswith(("admin:dest:set:", "admin:dest:publish:"))
        for button in buttons
    )


def test_retry_after_retries_scheduled_publication_before_recording_success(tmp_path, monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    store = _enabled_store(tmp_path)

    async def run():
        bot = _RecordingBot(retry_once=True)
        result = await dispatch_managed_publications(store, bot, _snapshot(), now=NOW)
        return bot, result

    bot, result = asyncio.run(run())

    assert result.sent == 1
    assert bot.attempts == 2
    assert store.get_destination(-100123)["last_fingerprint"] is not None


def test_failed_state_commit_after_send_is_reported_and_keeps_old_state(tmp_path, monkeypatch):
    store = _enabled_store(tmp_path)

    def fail_save():
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(store, "_save", fail_save)

    async def run():
        bot = _RecordingBot()
        result = await dispatch_managed_publications(store, bot, _snapshot(), now=NOW)
        return bot, result

    bot, result = asyncio.run(run())

    assert result.sent == 0
    assert result.failed == 1
    assert len(bot.messages) == 1
    assert store.get_destination(-100123)["last_fingerprint"] is None


def test_global_pause_blocks_all_managed_publications(tmp_path):
    store = _enabled_store(tmp_path)

    async def run():
        bot = _RecordingBot()
        result = await dispatch_managed_publications(
            store, bot, _snapshot(), now=NOW, global_paused=True
        )
        return bot, result

    bot, result = asyncio.run(run())

    assert result.sent == 0
    assert bot.messages == []


class _RecordingBot:
    def __init__(self, *, fail=False, retry_once=False):
        self.fail = fail
        self.retry_once = retry_once
        self.attempts = 0
        self.messages = []

    async def send_message(self, **kwargs):
        from telegram.error import Forbidden, RetryAfter

        self.attempts += 1
        if self.retry_once and self.attempts == 1:
            raise RetryAfter(timedelta(seconds=0))
        if self.fail:
            raise Forbidden("synthetic publication failure")
        self.messages.append(kwargs)
