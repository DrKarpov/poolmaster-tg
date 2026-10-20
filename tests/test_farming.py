import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
import farming
from bot import PoolmasterBot


JETTON = "EQAQXlWJvGbbFfE8F3oS8s87lIgdovS455IsWFaRdmJetTon"
TON = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
USDT = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
JETTON_ASSET_ID = "jetton:0:105e5589bc66db15f13c177a12f2cf3b94881da2f4b8e7922c58569176625eb5"
USDT_ASSET_ID = "jetton:0:b113a994b5024a16719f69139328eb759596c38a25f59028b146fecdc3621dfe"
FIXED_TIME = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

DEDUST_TON_POOL = farming.FARMING_POOLS[0]
DEDUST_USDT_POOL = farming.FARMING_POOLS[1]
STON_TON_POOL = farming.FARMING_POOLS[2]
STON_USDT_POOL = farming.FARMING_POOLS[3]


def _parse(name, *args):
    parser = getattr(farming, name, None)
    assert callable(parser), f"strict parser {name} is not available"
    return parser(*args, fetched_at=FIXED_TIME)


def _dedust_ton_payload():
    return {
        "assets_metadata": {
            JETTON_ASSET_ID: {"ticker": "JETTON", "decimals": 9},
            "native": {"ticker": "TON", "decimals": 9},
        },
        "pool": {
            "address": "0:4e50a518cffde030b1e124b2da8e80ea53bfa3115e1ced345913b100ad8acab1",
            "apr": "88.02887578281931",
            "apr_fees": "0.13878090544169",
            "apr_rewards": "87.89009487737762",
            "assets": [JETTON_ASSET_ID, "native"],
            "tvl_usd": "77052.055829832767351543779047318",
        },
    }


def _ston_ton_pool_payload():
    return {
        "pool": {
            "address": STON_TON_POOL.address,
            "token0_address": TON,
            "token1_address": JETTON,
            "lp_total_supply_usd": "132092.244853663",
            "apy_1d": "0.003730136",
            "apy_7d": "0.013475494",
            "apy_30d": "0.02479065",
        }
    }


def _ston_farm(apy, *, status="operational", reward_status="active", distributed=False, name="JETTON farm"):
    return {
        "minter_address": f"farm-{name}",
        "pool_address": STON_TON_POOL.address,
        "status": status,
        "apy": apy,
        "locked_total_lp_usd": "125510.051434972",
        "all_rewards_distributed": distributed,
        "rewards": [
            {
                "address": JETTON,
                "status": reward_status,
                "rewards_distributed": distributed,
                "reward_rate_24h": "11290322580645",
            }
        ],
    }


def test_dedust_parser_keeps_farm_fee_and_total_apr_separate():
    metrics = _parse("parse_dedust_pool", _dedust_ton_payload(), DEDUST_TON_POOL)

    assert metrics.status == farming.SourceStatus.OK
    assert metrics.identity_valid is True
    assert metrics.farm_rates[0].kind == "APR"
    assert metrics.farm_rates[0].value_percent == Decimal("87.89009487737762")
    assert metrics.pool_rates[0].kind == "APR"
    assert metrics.pool_rates[0].value_percent == Decimal("0.13878090544169")
    assert metrics.total_rate.value_percent == Decimal("88.02887578281931")
    assert metrics.tvl_usd == Decimal("77052.055829832767351543779047318")
    assert metrics.fetched_at == FIXED_TIME


def test_primary_farm_yield_uses_dedust_reward_apr_not_fee_or_total_apr():
    metrics = _parse("parse_dedust_pool", _dedust_ton_payload(), DEDUST_TON_POOL)

    assert metrics.primary_farm_yield == metrics.farm_rates[0]
    assert metrics.primary_farm_yield.value_percent == Decimal("87.89009487737762")
    assert metrics.primary_farm_yield.source_field == "apr_rewards"


def test_primary_yield_is_usable_when_only_auxiliary_fields_are_missing():
    dedust_payload = _dedust_ton_payload()
    dedust_payload["pool"].pop("apr_fees")
    dedust_payload["pool"].pop("tvl_usd")
    dedust = _parse("parse_dedust_pool", dedust_payload, DEDUST_TON_POOL)

    ston_pool = _ston_ton_pool_payload()
    ston_pool["pool"].pop("apy_1d")
    ston_pool["pool"].pop("apy_7d")
    ston_pool["pool"].pop("apy_30d")
    ston = _parse("parse_ston_pool", ston_pool, {"farm_list": [_ston_farm("0.61")]}, STON_TON_POOL)

    assert dedust.status == farming.SourceStatus.PARTIAL
    assert dedust.primary_farm_yield.value_percent == Decimal("87.89009487737762")
    assert farming.is_primary_farming_usable(dedust) is True
    assert ston.status == farming.SourceStatus.PARTIAL
    assert ston.primary_farm_yield.value_percent == Decimal("61.00")
    assert farming.is_primary_farming_usable(ston) is True


def test_primary_yield_survives_malformed_auxiliary_source_fields():
    dedust_payload = _dedust_ton_payload()
    dedust_payload["pool"].update({"apr": "bad", "apr_fees": "bad", "tvl_usd": "bad"})
    dedust = _parse("parse_dedust_pool", dedust_payload, DEDUST_TON_POOL)

    ston_pool = _ston_ton_pool_payload()
    ston_pool["pool"].update({"lp_total_supply_usd": "bad", "apy_7d": "bad"})
    ston = _parse("parse_ston_pool", ston_pool, {"farm_list": [_ston_farm("0.61")]}, STON_TON_POOL)

    assert dedust.status == farming.SourceStatus.PARTIAL
    assert dedust.total_rate is None
    assert dedust.pool_rates == ()
    assert dedust.tvl_usd is None
    assert dedust.primary_farm_yield.value_percent == Decimal("87.89009487737762")
    assert farming.is_primary_farming_usable(dedust) is True
    assert ston.status == farming.SourceStatus.PARTIAL
    assert ston.tvl_usd is None
    assert [rate.period for rate in ston.pool_rates] == ["1d", "30d"]
    assert ston.primary_farm_yield.value_percent == Decimal("61.00")
    assert farming.is_primary_farming_usable(ston) is True


def test_dedust_primary_yield_survives_missing_total_apr():
    payload = _dedust_ton_payload()
    payload["pool"].pop("apr")

    metrics = _parse("parse_dedust_pool", payload, DEDUST_TON_POOL)

    assert metrics.status == farming.SourceStatus.PARTIAL
    assert metrics.total_rate is None
    assert metrics.primary_farm_yield.value_percent == Decimal("87.89009487737762")
    assert farming.is_primary_farming_usable(metrics) is True


def test_primary_yield_usability_rejects_unsafe_metrics_and_unconfirmed_anomalies():
    metrics = _parse("parse_dedust_pool", _dedust_ton_payload(), DEDUST_TON_POOL)

    assert farming.is_primary_farming_usable(replace(metrics, identity_valid=False)) is False
    assert farming.is_primary_farming_usable(replace(metrics, is_stale=True)) is False
    assert farming.is_primary_farming_usable(replace(metrics, status=farming.SourceStatus.TIMEOUT)) is False
    assert farming.is_primary_farming_usable(metrics, anomaly=True) is False
    invalid_rate = replace(metrics, farm_rates=(farming.RateMetric(Decimal("-1"), "APR", "apr_rewards"),))
    assert farming.is_primary_farming_usable(invalid_rate) is False


def test_dedust_parser_does_not_guess_ratio_units_for_apr_fields():
    payload = _dedust_ton_payload()
    payload["pool"].update({"apr": "0.5", "apr_rewards": "0.25", "apr_fees": "0.25"})

    metrics = _parse("parse_dedust_pool", payload, DEDUST_TON_POOL)

    assert metrics.total_rate.value_percent == Decimal("0.5")
    assert metrics.farm_rates[0].value_percent == Decimal("0.25")


def test_dedust_parser_rejects_apr_hidden_in_unrelated_nested_fields():
    payload = {
        "pool": {
            "address": "0:4e50a518cffde030b1e124b2da8e80ea53bfa3115e1ced345913b100ad8acab1",
            "assets": [JETTON_ASSET_ID, "native"],
            "rewards": [{"apy": "999"}],
        },
        "assets_metadata": {
            JETTON_ASSET_ID: {"ticker": "JETTON"},
            "native": {"ticker": "TON"},
        },
    }

    metrics = _parse("parse_dedust_pool", payload, DEDUST_TON_POOL)

    assert metrics.status == farming.SourceStatus.INVALID_RESPONSE
    assert metrics.farm_rates == ()
    assert metrics.total_rate is None


def test_dedust_parser_rejects_wrong_pool_address_or_asset_identity():
    payload = _dedust_ton_payload()
    payload["pool"]["address"] = "0:" + "0" * 64

    metrics = _parse("parse_dedust_pool", payload, DEDUST_TON_POOL)

    assert metrics.identity_valid is False
    assert metrics.status == farming.SourceStatus.INVALID_RESPONSE
    assert metrics.total_rate is None


def test_ston_parser_selects_active_farm_and_keeps_apy_distinct_from_pool_apy():
    pool_payload = _ston_ton_pool_payload()
    farms_payload = {
        "farm_list": [
            _ston_farm("0.99", status="finished", reward_status="finished", distributed=True, name="old"),
            _ston_farm("0.610858536", name="active"),
        ]
    }

    metrics = _parse("parse_ston_pool", pool_payload, farms_payload, STON_TON_POOL)

    assert metrics.status == farming.SourceStatus.OK
    assert metrics.identity_valid is True
    assert len(metrics.farm_rates) == 1
    assert metrics.farm_rates[0].kind == "APY"
    assert metrics.farm_rates[0].value_percent == Decimal("61.0858536")
    assert metrics.farm_rates[0].farm_address == "farm-active"
    assert metrics.pool_rates[1].kind == "APY"
    assert metrics.pool_rates[1].period == "7d"
    assert metrics.pool_rates[1].value_percent == Decimal("1.3475494")
    assert metrics.total_rate is None
    assert metrics.primary_farm_yield == metrics.farm_rates[0]


def test_ston_parser_does_not_choose_or_sum_multiple_active_farms():
    metrics = _parse(
        "parse_ston_pool",
        _ston_ton_pool_payload(),
        {"farm_list": [_ston_farm("0.4", name="one"), _ston_farm("0.6", name="two")]},
        STON_TON_POOL,
    )

    assert metrics.status == farming.SourceStatus.PARTIAL
    assert [rate.value_percent for rate in metrics.farm_rates] == [Decimal("40"), Decimal("60")]
    assert metrics.total_rate is None
    assert metrics.primary_farm_yield is None


def test_ston_parser_does_not_promote_valid_farm_when_another_active_farm_is_malformed():
    metrics = _parse(
        "parse_ston_pool",
        _ston_ton_pool_payload(),
        {"farm_list": [_ston_farm("0.12", name="valid"), _ston_farm("bad", name="malformed")]},
        STON_TON_POOL,
    )

    assert metrics.status == farming.SourceStatus.PARTIAL
    assert metrics.farm_rates == ()
    assert metrics.primary_farm_yield is None
    assert farming.is_primary_farming_usable(metrics) is False


def test_ston_parser_never_falls_back_to_pool_apy_as_farming_apy():
    metrics = _parse(
        "parse_ston_pool",
        _ston_ton_pool_payload(),
        {"farm_list": [_ston_farm("0.99", status="finished", reward_status="finished", distributed=True)]},
        STON_TON_POOL,
    )

    assert metrics.farm_rates == ()
    assert metrics.pool_rates
    assert metrics.status == farming.SourceStatus.PARTIAL


def test_malformed_ston_farm_payload_keeps_valid_pool_apy_as_partial():
    metrics = _parse("parse_ston_pool", _ston_ton_pool_payload(), {"unexpected": []}, STON_TON_POOL)

    assert metrics.status == farming.SourceStatus.PARTIAL
    assert metrics.identity_valid is True
    assert metrics.farm_rates == ()
    assert len(metrics.pool_rates) == 3


def test_ston_parser_rejects_wrong_pool_or_paired_asset_identity():
    wrong_pool = _ston_ton_pool_payload()
    wrong_pool["pool"]["token0_address"] = USDT

    metrics = _parse(
        "parse_ston_pool",
        wrong_pool,
        {"farm_list": [_ston_farm("0.61")]},
        STON_TON_POOL,
    )

    assert metrics.identity_valid is False
    assert metrics.status == farming.SourceStatus.INVALID_RESPONSE
    assert metrics.farm_rates == ()
    assert metrics.total_rate is None


def test_farming_card_shows_one_neutral_ston_farm_yield_and_tvl():
    metrics = _parse(
        "parse_ston_pool",
        _ston_ton_pool_payload(),
        {"farm_list": [_ston_farm("0.610858536")]},
        STON_TON_POOL,
    )

    text = PoolmasterBot.__new__(PoolmasterBot).build_farming_text([metrics])

    assert "Доходность: <b>61.09%</b>" in text
    assert "TVL: $132,092" in text
    assert "APY" not in text
    assert "Pool" not in text


def test_farming_card_shows_only_dedust_reward_yield_and_tvl():
    metrics = _parse("parse_dedust_pool", _dedust_ton_payload(), DEDUST_TON_POOL)

    text = PoolmasterBot.__new__(PoolmasterBot).build_farming_text([metrics])

    assert "Доходность: <b>87.89%</b>" in text
    assert "TVL: $77,052" in text
    assert "APR" not in text
    assert "Pool-fee" not in text


def test_group_farming_card_hides_unconfirmed_yield_without_hiding_tvl():
    metrics = _parse("parse_dedust_pool", _dedust_ton_payload(), DEDUST_TON_POOL)
    bot = PoolmasterBot.__new__(PoolmasterBot)

    text = bot.build_farming_text(
        [metrics],
        anomalies={f"farming:{DEDUST_TON_POOL.address}": "unconfirmed"},
    )

    assert "87.89" not in text
    assert "Доходность временно недоступна" in text
    assert "TVL: $77,052" in text


def test_farming_summary_does_not_call_mixed_yields_apr():
    text = PoolmasterBot.__new__(PoolmasterBot).build_farming_text([])

    assert "APR обновлен прямо сейчас:" not in text


def test_startup_invalid_pool_is_hidden_from_farming_output():
    metrics = _parse("parse_dedust_pool", _dedust_ton_payload(), DEDUST_TON_POOL)
    bot = PoolmasterBot.__new__(PoolmasterBot)
    bot.invalid_pool_ids = {DEDUST_TON_POOL.address}

    text = bot.build_farming_text([metrics])

    assert "данные этого пула временно недоступны" in text.lower()
    assert "87.89" not in text


def test_ston_farm_endpoint_failure_keeps_pool_apy_as_partial_data():
    async def run():
        def handler(request):
            if request.url.path == f"/v1/pools/{STON_TON_POOL.address}":
                return httpx.Response(200, json=_ston_ton_pool_payload())
            if request.url.path == f"/v1/farms/by_pool/{STON_TON_POOL.address}":
                return httpx.Response(503)
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await farming.fetch_pool_info(client, STON_TON_POOL)

    metrics = asyncio.run(run())

    assert metrics.status == farming.SourceStatus.PARTIAL
    assert metrics.identity_valid is True
    assert metrics.farm_rates == ()
    assert metrics.pool_rates
    assert metrics.stale_farm_yield_fallback_allowed is False


def test_ston_farm_transport_timeout_allows_stale_fallback():
    async def run():
        def handler(request):
            if request.url.path == f"/v1/pools/{STON_TON_POOL.address}":
                return httpx.Response(200, json=_ston_ton_pool_payload())
            if request.url.path == f"/v1/farms/by_pool/{STON_TON_POOL.address}":
                raise httpx.ReadTimeout("farms endpoint timeout")
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await farming.fetch_pool_info(client, STON_TON_POOL)

    metrics = asyncio.run(run())

    assert metrics.status == farming.SourceStatus.PARTIAL
    assert metrics.identity_valid is True
    assert metrics.farm_rates == ()
    assert metrics.pool_rates
    assert metrics.stale_farm_yield_fallback_allowed is True


def test_ston_malformed_farm_schema_does_not_enable_stale_fallback():
    async def run():
        def handler(request):
            if request.url.path == f"/v1/pools/{STON_TON_POOL.address}":
                return httpx.Response(200, json=_ston_ton_pool_payload())
            if request.url.path == f"/v1/farms/by_pool/{STON_TON_POOL.address}":
                return httpx.Response(200, json={"farm_list": "invalid"})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await farming.fetch_pool_info(client, STON_TON_POOL)

    metrics = asyncio.run(run())

    assert metrics.status == farming.SourceStatus.PARTIAL
    assert metrics.status_detail == "farm response has an unknown schema"
    assert metrics.stale_farm_yield_fallback_allowed is False


def test_source_timeout_is_reported_without_failing_the_other_pools():
    async def run():
        def handler(_request):
            raise httpx.ReadTimeout("sample timeout")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await farming.fetch_pool_info(client, DEDUST_TON_POOL)

    metrics = asyncio.run(run())

    assert metrics.status == farming.SourceStatus.TIMEOUT
    assert metrics.farm_rates == ()


def _identity_transport(*, wrong_usdt_kind=False):
    dedust_ton = _dedust_ton_payload()
    dedust_usdt = {
        "assets_metadata": {
            JETTON_ASSET_ID: {"ticker": "JETTON", "decimals": 9},
            USDT_ASSET_ID: {"ticker": "USDT", "decimals": 6},
        },
        "pool": {
            "address": farming._address_id(DEDUST_USDT_POOL.address),
            "apr": "71.65",
            "apr_fees": "0.18",
            "apr_rewards": "71.47",
            "assets": [JETTON_ASSET_ID, USDT_ASSET_ID],
            "tvl_usd": "100000",
        },
    }
    ston_ton = _ston_ton_pool_payload()["pool"]
    ston_usdt = {
        "address": STON_USDT_POOL.address,
        "token0_address": USDT,
        "token1_address": JETTON,
    }
    assets = {
        JETTON: {"kind": "Jetton", "symbol": "JETTON", "contract_address": JETTON},
        TON: {"kind": "Ton", "symbol": "GRAM", "contract_address": TON},
        USDT: {
            "kind": "Jetton" if not wrong_usdt_kind else "Ton",
            "symbol": "USD₮",
            "contract_address": USDT,
        },
    }

    def handler(request):
        path = request.url.path
        if path.startswith("/v4/api/pools/"):
            payload = dedust_ton if DEDUST_TON_POOL.address in path else dedust_usdt
        elif path == f"/v1/pools/{STON_TON_POOL.address}":
            payload = {"pool": ston_ton}
        elif path == f"/v1/pools/{STON_USDT_POOL.address}":
            payload = {"pool": ston_usdt}
        elif path.startswith("/v1/assets/"):
            address = path.rsplit("/", 1)[-1]
            payload = {"asset": assets[address]}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_startup_identity_check_validates_all_four_pools_from_source_metadata():
    async def run():
        async with httpx.AsyncClient(transport=_identity_transport()) as client:
            return await farming.verify_farming_pool_identities(client)

    identities = asyncio.run(run())

    assert set(identities) == {pool.address for pool in farming.FARMING_POOLS}
    assert all(check.valid is True for check in identities.values())


def test_startup_identity_mismatch_invalidates_only_the_affected_pool():
    async def run():
        async with httpx.AsyncClient(transport=_identity_transport(wrong_usdt_kind=True)) as client:
            return await farming.verify_farming_pool_identities(client)

    identities = asyncio.run(run())

    assert identities[STON_USDT_POOL.address].valid is False
    assert "asset" in identities[STON_USDT_POOL.address].detail
    assert all(
        identities[pool.address].valid is True
        for pool in (DEDUST_TON_POOL, DEDUST_USDT_POOL, STON_TON_POOL)
    )
