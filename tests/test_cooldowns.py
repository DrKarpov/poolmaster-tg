from cooldowns import InMemoryCooldowns


def test_cooldown_blocks_same_key_until_interval_expires():
    cooldowns = InMemoryCooldowns()

    assert cooldowns.allow(("chart", 123), 10, now=100)
    assert not cooldowns.allow(("chart", 123), 10, now=109.9)
    assert cooldowns.allow(("chart", 123), 10, now=110)


def test_cooldown_keys_are_independent_and_zero_interval_is_unlimited():
    cooldowns = InMemoryCooldowns()

    assert cooldowns.allow(("group", -100), 10, now=100)
    assert cooldowns.allow(("group", -200), 10, now=100)
    assert cooldowns.allow(("callback", 123), 0, now=100)
    assert cooldowns.allow(("callback", 123), 0, now=100)


def test_cooldown_bookkeeping_is_bounded():
    cooldowns = InMemoryCooldowns(max_entries=2)

    cooldowns.allow(("user", 1), 10, now=100)
    cooldowns.allow(("user", 2), 10, now=101)
    cooldowns.allow(("user", 3), 10, now=102)

    assert len(cooldowns._last_used) <= 2
