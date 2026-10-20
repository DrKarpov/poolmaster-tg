from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

import httpx


@dataclass(frozen=True)
class FarmingPool:
    name: str
    dex: str
    address: str
    url: str


class SourceStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True)
class RateMetric:
    value_percent: Decimal
    kind: str
    source_field: str
    period: str | None = None
    farm_address: str | None = None
    reward_assets: tuple[str, ...] = ()


@dataclass(frozen=True)
class PoolMetrics:
    pool: FarmingPool
    farm_rates: tuple[RateMetric, ...]
    pool_rates: tuple[RateMetric, ...]
    total_rate: RateMetric | None
    tvl_usd: Decimal | None
    fetched_at: datetime
    status: SourceStatus
    identity_valid: bool
    status_detail: str | None = None
    is_stale: bool = False
    stale_farm_yield_fallback_allowed: bool = False

    @property
    def pool_id(self) -> str:
        return self.pool.address

    @property
    def dex(self) -> str:
        return self.pool.dex

    @property
    def pair(self) -> str:
        return self.pool.name

    @property
    def source_status(self) -> str:
        return self.status.value

    @property
    def primary_farm_yield(self) -> RateMetric | None:
        """Return the one active farming rate shared by cards and alerts.

        DeDust farm_rates contains only apr_rewards, while STON.fi farm_rates
        contains active farm APY values. If a source reports several active
        farms, there is no single primary value to present or compare.
        """
        if len(self.farm_rates) != 1:
            return None
        return self.farm_rates[0]


def is_primary_farming_usable(metrics: PoolMetrics, *, anomaly: bool = False) -> bool:
    """Whether this pool has a fresh, identity-verified primary farm yield."""
    rate = metrics.primary_farm_yield
    return bool(
        not anomaly
        and metrics.identity_valid is True
        and not metrics.is_stale
        and metrics.status in {SourceStatus.OK, SourceStatus.PARTIAL}
        and rate is not None
        and isinstance(rate.value_percent, Decimal)
        and rate.value_percent.is_finite()
        and rate.value_percent >= 0
    )


@dataclass(frozen=True)
class IdentityCheck:
    valid: bool | None
    detail: str


JETTON_MASTER_ADDRESS = "EQAQXlWJvGbbFfE8F3oS8s87lIgdovS455IsWFaRdmJetTon"
TON_MASTER_ADDRESS = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
USDT_MASTER_ADDRESS = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"

FARMING_POOLS = [
    FarmingPool(
        name="JETTON/TON",
        dex="DeDust",
        address="EQBOUKUYz_3gMLHhJLLajoDqU7-jEV4c7TRZE7EArYrKsb9o",
        url="https://app.dedust.io/pools/EQBOUKUYz_3gMLHhJLLajoDqU7-jEV4c7TRZE7EArYrKsb9o",
    ),
    FarmingPool(
        name="JETTON/USDT",
        dex="DeDust",
        address="EQA3AcjdKlCGerzwaI7ZIU_HsWW_OEH_1XoITHM1Q21Yq55z",
        url="https://app.dedust.io/pools/EQA3AcjdKlCGerzwaI7ZIU_HsWW_OEH_1XoITHM1Q21Yq55z",
    ),
    FarmingPool(
        name="JETTON/TON",
        dex="STON.fi",
        address="EQD_Xx-iQR0z3GJozQt0dE1ybgwBFFyfT5yehnwOJE3kkCM0",
        url="https://app.ston.fi/pools/EQD_Xx-iQR0z3GJozQt0dE1ybgwBFFyfT5yehnwOJE3kkCM0",
    ),
    FarmingPool(
        name="JETTON/USDT",
        dex="STON.fi",
        address="EQBG_nP6eUpp89ZyPRCxNtyIQGHfBuYCzLXVhBQwbv5TEJnE",
        url="https://app.ston.fi/pools/EQBG_nP6eUpp89ZyPRCxNtyIQGHfBuYCzLXVhBQwbv5TEJnE",
    ),
]

REQUEST_TIMEOUT_SECONDS = 8
POOL_FETCH_TIMEOUT_SECONDS = 15
DEDUST_BASE_URL = "https://mainnet.api.dedust.io/v4/api/pools"
STON_BASE_URL = "https://api.ston.fi/v1"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _invalid_metrics(
    pool: FarmingPool,
    fetched_at: datetime,
    detail: str,
    *,
    identity_valid: bool = False,
    status: SourceStatus = SourceStatus.INVALID_RESPONSE,
) -> PoolMetrics:
    return PoolMetrics(
        pool=pool,
        farm_rates=(),
        pool_rates=(),
        total_rate=None,
        tvl_usd=None,
        fetched_at=fetched_at,
        status=status,
        identity_valid=identity_valid,
        status_detail=detail,
    )


def unavailable_metrics(
    pool: FarmingPool,
    status: SourceStatus,
    detail: str,
    fetched_at: datetime | None = None,
) -> PoolMetrics:
    return _invalid_metrics(
        pool,
        fetched_at or _now_utc(),
        detail,
        status=status,
    )


def _parse_decimal(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field} is not a numeric value")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not a numeric value") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} is outside the accepted numeric domain")
    return number


def _parse_auxiliary_decimal(data: dict[str, Any], field: str) -> tuple[Decimal | None, bool]:
    """Parse a non-primary source metric without discarding usable primary data."""
    try:
        return _parse_decimal(data.get(field), field), False
    except ValueError:
        return None, True


def _ratio_as_percent(value: Decimal | None) -> Decimal | None:
    return value * Decimal(100) if value is not None else None


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _address_id(address: Any) -> str | None:
    if not isinstance(address, str):
        return None
    if ":" in address:
        workchain_text, hash_text = address.split(":", 1)
        try:
            workchain = int(workchain_text)
            hash_bytes = bytes.fromhex(hash_text)
        except ValueError:
            return None
        if len(hash_bytes) != 32:
            return None
        return f"{workchain}:{hash_bytes.hex()}"

    try:
        encoded = base64.b64decode(address + "=" * (-len(address) % 4), altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if len(encoded) != 36 or encoded[0] not in {0x11, 0x51, 0x91, 0xD1}:
        return None
    if _crc16_xmodem(encoded[:34]) != int.from_bytes(encoded[34:], "big"):
        return None
    workchain = encoded[1] if encoded[1] < 128 else encoded[1] - 256
    return f"{workchain}:{encoded[2:34].hex()}"


def _asset_id_address(asset_id: Any) -> str | None:
    if asset_id == "native":
        return _address_id(TON_MASTER_ADDRESS)
    if not isinstance(asset_id, str) or not asset_id.startswith("jetton:"):
        return None
    parts = asset_id.split(":")
    if len(parts) != 3:
        return None
    return _address_id(f"{parts[1]}:{parts[2]}")


def _expected_pair_addresses(pool: FarmingPool) -> set[str] | None:
    address_by_symbol = {
        "JETTON": JETTON_MASTER_ADDRESS,
        "TON": TON_MASTER_ADDRESS,
        "USDT": USDT_MASTER_ADDRESS,
    }
    symbols = pool.name.split("/")
    if len(symbols) != 2 or any(symbol not in address_by_symbol for symbol in symbols):
        return None
    normalized = {_address_id(address_by_symbol[symbol]) for symbol in symbols}
    if None in normalized:
        return None
    return normalized  # type: ignore[return-value]


def _validate_dedust_identity(payload: dict[str, Any], pool: FarmingPool) -> tuple[bool, str | None]:
    pool_data = payload.get("pool")
    if not isinstance(pool_data, dict):
        return False, "pool object missing"
    if _address_id(pool_data.get("address")) != _address_id(pool.address):
        return False, "pool address mismatch"

    expected_addresses = _expected_pair_addresses(pool)
    assets = pool_data.get("assets")
    metadata = payload.get("assets_metadata")
    if expected_addresses is None or not isinstance(assets, list) or len(assets) != 2 or not isinstance(metadata, dict):
        return False, "pool asset metadata missing"

    actual_addresses: set[str] = set()
    expected_symbols = {symbol for symbol in pool.name.split("/")}
    actual_symbols: set[str] = set()
    for asset_id in assets:
        address = _asset_id_address(asset_id)
        asset_metadata = metadata.get(asset_id)
        if address is None or not isinstance(asset_metadata, dict):
            return False, "unrecognized pool asset"
        actual_addresses.add(address)
        ticker = asset_metadata.get("ticker")
        if not isinstance(ticker, str):
            return False, "pool asset ticker missing"
        actual_symbols.add(ticker)

    if actual_addresses != expected_addresses or actual_symbols != expected_symbols:
        return False, "pool asset identity mismatch"
    return True, None


def _ston_pool_identity(pool_data: dict[str, Any], pool: FarmingPool) -> tuple[bool, str | None]:
    if _address_id(pool_data.get("address")) != _address_id(pool.address):
        return False, "pool address mismatch"
    expected = _expected_pair_addresses(pool)
    actual = {
        _address_id(pool_data.get("token0_address")),
        _address_id(pool_data.get("token1_address")),
    }
    if expected is None or None in actual or actual != expected:
        return False, "pool token identity mismatch"
    return True, None


def _validate_ston_asset_identity(payload: Any, address: str, symbol: str) -> tuple[bool, str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("asset"), dict):
        return False, "asset metadata schema is invalid"
    asset = payload["asset"]
    if _address_id(asset.get("contract_address")) != _address_id(address):
        return False, "asset contract address mismatch"

    if symbol == "TON":
        if asset.get("kind") != "Ton":
            return False, "TON asset kind mismatch"
        # STON.fi currently names its native TON asset GRAM; address and kind are authoritative.
        return True, None

    if asset.get("kind") != "Jetton":
        return False, f"{symbol} asset kind mismatch"
    expected_symbols = {"USDT", "USD₮"} if symbol == "USDT" else {symbol}
    if asset.get("symbol") not in expected_symbols:
        return False, f"{symbol} asset symbol mismatch"
    return True, None


async def _verify_pool_identity(client: httpx.AsyncClient, pool: FarmingPool) -> IdentityCheck:
    try:
        if pool.dex == "DeDust":
            payload = await _request_json(client, f"{DEDUST_BASE_URL}/{pool.address}")
            if not isinstance(payload, dict):
                return IdentityCheck(False, "DeDust pool response schema is invalid")
            valid, detail = _validate_dedust_identity(payload, pool)
            return IdentityCheck(valid, detail or "pool and asset metadata match")

        if pool.dex == "STON.fi":
            payload = await _request_json(client, f"{STON_BASE_URL}/pools/{pool.address}")
            if not isinstance(payload, dict) or not isinstance(payload.get("pool"), dict):
                return IdentityCheck(False, "STON.fi pool response schema is invalid")
            valid, detail = _ston_pool_identity(payload["pool"], pool)
            if not valid:
                return IdentityCheck(False, detail or "STON.fi pool identity mismatch")

            addresses = {
                "JETTON": JETTON_MASTER_ADDRESS,
                "TON": TON_MASTER_ADDRESS,
                "USDT": USDT_MASTER_ADDRESS,
            }
            for symbol in pool.name.split("/"):
                address = addresses[symbol]
                asset_payload = await _request_json(client, f"{STON_BASE_URL}/assets/{address}")
                valid, detail = _validate_ston_asset_identity(asset_payload, address, symbol)
                if not valid:
                    return IdentityCheck(False, detail or f"{symbol} asset identity mismatch")
            return IdentityCheck(True, "pool and asset metadata match")

        return IdentityCheck(False, "unknown DEX")
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return IdentityCheck(None, "identity metadata request timed out")
    except httpx.HTTPError:
        return IdentityCheck(None, "identity metadata source unavailable")
    except (ValueError, TypeError, KeyError):
        return IdentityCheck(False, "identity metadata schema is invalid")


async def verify_farming_pool_identities(
    client: httpx.AsyncClient | None = None,
) -> dict[str, IdentityCheck]:
    if client is not None:
        checks = await asyncio.gather(*(_verify_pool_identity(client, pool) for pool in FARMING_POOLS))
    else:
        timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=4)
        headers = {"Accept": "application/json", "User-Agent": "POOLMASTER Telegram Bot/1.0"}
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as owned_client:
            checks = await asyncio.gather(*(_verify_pool_identity(owned_client, pool) for pool in FARMING_POOLS))
    return {pool.address: check for pool, check in zip(FARMING_POOLS, checks, strict=True)}


def parse_dedust_pool(
    payload: Any,
    pool: FarmingPool,
    *,
    fetched_at: datetime | None = None,
) -> PoolMetrics:
    timestamp = fetched_at or _now_utc()
    if not isinstance(payload, dict):
        return _invalid_metrics(pool, timestamp, "response is not an object")

    identity_valid, identity_error = _validate_dedust_identity(payload, pool)
    if not identity_valid:
        return _invalid_metrics(pool, timestamp, identity_error or "pool identity mismatch")

    pool_data = payload["pool"]
    try:
        farm_apr = _parse_decimal(pool_data.get("apr_rewards"), "pool.apr_rewards")
    except ValueError as exc:
        return _invalid_metrics(pool, timestamp, str(exc), identity_valid=True)

    total_apr, invalid_total_apr = _parse_auxiliary_decimal(pool_data, "apr")
    pool_apr, invalid_pool_apr = _parse_auxiliary_decimal(pool_data, "apr_fees")
    tvl_usd, invalid_tvl = _parse_auxiliary_decimal(pool_data, "tvl_usd")

    if total_apr is None and farm_apr is None:
        return _invalid_metrics(
            pool,
            timestamp,
            "pool.apr and pool.apr_rewards are missing from response",
            identity_valid=True,
        )

    farm_rates = (
        (RateMetric(farm_apr, "APR", "apr_rewards"),)
        if farm_apr is not None
        else ()
    )
    pool_rates = (
        (RateMetric(pool_apr, "APR", "apr_fees"),)
        if pool_apr is not None
        else ()
    )
    status = (
        SourceStatus.OK
        if farm_apr is not None
        and total_apr is not None
        and pool_apr is not None
        and tvl_usd is not None
        else SourceStatus.PARTIAL
    )
    has_malformed_auxiliary = invalid_total_apr or invalid_pool_apr or invalid_tvl
    return PoolMetrics(
        pool=pool,
        farm_rates=farm_rates,
        pool_rates=pool_rates,
        total_rate=RateMetric(total_apr, "APR", "apr") if total_apr is not None else None,
        tvl_usd=tvl_usd,
        fetched_at=timestamp,
        status=status,
        identity_valid=True,
        status_detail=(
            None
            if status == SourceStatus.OK
            else "one or more APR/TVL fields are malformed"
            if has_malformed_auxiliary
            else "one or more APR/TVL fields are unavailable"
        ),
    )


def _ston_farm_is_active(farm: dict[str, Any], pool: FarmingPool) -> bool:
    if farm.get("status") != "operational" or farm.get("all_rewards_distributed") is True:
        return False
    if _address_id(farm.get("pool_address")) != _address_id(pool.address):
        return False
    rewards = farm.get("rewards")
    if not isinstance(rewards, list):
        return False
    return any(
        isinstance(reward, dict)
        and reward.get("status") == "active"
        and reward.get("rewards_distributed") is False
        for reward in rewards
    )


def parse_ston_pool(
    pool_payload: Any,
    farms_payload: Any,
    pool: FarmingPool,
    *,
    fetched_at: datetime | None = None,
) -> PoolMetrics:
    timestamp = fetched_at or _now_utc()
    if not isinstance(pool_payload, dict) or not isinstance(pool_payload.get("pool"), dict):
        return _invalid_metrics(pool, timestamp, "pool response has an unknown schema")

    pool_data = pool_payload["pool"]
    identity_valid, identity_error = _ston_pool_identity(pool_data, pool)
    if not identity_valid:
        return _invalid_metrics(pool, timestamp, identity_error or "pool identity mismatch")

    tvl_usd, invalid_tvl = _parse_auxiliary_decimal(pool_data, "lp_total_supply_usd")
    pool_rates_list: list[RateMetric] = []
    malformed_pool_metric = invalid_tvl
    for field, period in (("apy_1d", "1d"), ("apy_7d", "7d"), ("apy_30d", "30d")):
        ratio, invalid_metric = _parse_auxiliary_decimal(pool_data, field)
        malformed_pool_metric = malformed_pool_metric or invalid_metric
        value_percent = _ratio_as_percent(ratio)
        if value_percent is not None:
            pool_rates_list.append(RateMetric(value_percent, "APY", field, period))
    pool_rates = tuple(pool_rates_list)

    if farms_payload is None:
        return PoolMetrics(
            pool=pool,
            farm_rates=(),
            pool_rates=pool_rates,
            total_rate=None,
            tvl_usd=tvl_usd,
            fetched_at=timestamp,
            status=SourceStatus.PARTIAL,
            identity_valid=True,
            status_detail="farm endpoint unavailable",
        )
    if not isinstance(farms_payload, dict) or not isinstance(farms_payload.get("farm_list"), list):
        return PoolMetrics(
            pool=pool,
            farm_rates=(),
            pool_rates=pool_rates,
            total_rate=None,
            tvl_usd=tvl_usd,
            fetched_at=timestamp,
            status=SourceStatus.PARTIAL,
            identity_valid=True,
            status_detail="farm response has an unknown schema",
        )

    active_farms = [
        farm for farm in farms_payload["farm_list"]
        if isinstance(farm, dict) and _ston_farm_is_active(farm, pool)
    ]
    farm_rates: list[RateMetric] = []
    malformed_active_farm = False
    for farm in active_farms:
        try:
            farm_apy_ratio = _parse_decimal(farm.get("apy"), "farm.apy")
        except ValueError:
            malformed_active_farm = True
            continue
        if farm_apy_ratio is None:
            malformed_active_farm = True
            continue
        reward_assets = tuple(
            sorted(
                reward["address"]
                for reward in farm.get("rewards", [])
                if isinstance(reward, dict)
                and reward.get("status") == "active"
                and reward.get("rewards_distributed") is False
                and isinstance(reward.get("address"), str)
            )
        )
        farm_rates.append(
            RateMetric(
                value_percent=farm_apy_ratio * Decimal(100),
                kind="APY",
                source_field="apy",
                farm_address=farm.get("minter_address"),
                reward_assets=reward_assets,
            )
        )

    status = SourceStatus.OK
    detail = None
    if malformed_active_farm:
        # A valid rate from one farm cannot be treated as the pool's unique
        # primary yield when another active farm's yield could not be parsed.
        farm_rates = []
    if (
        len(active_farms) != 1
        or malformed_active_farm
        or not pool_rates
        or tvl_usd is None
        or malformed_pool_metric
    ):
        status = SourceStatus.PARTIAL
        if malformed_active_farm:
            detail = "active farm APY missing or malformed"
        elif len(active_farms) > 1:
            detail = "multiple active farms; rates kept separate"
        elif not active_farms:
            detail = "no active farm with undistributed rewards"
        elif malformed_pool_metric:
            detail = "one or more pool APY/TVL fields are malformed"
        else:
            detail = "one or more pool APY/TVL fields are unavailable"

    return PoolMetrics(
        pool=pool,
        farm_rates=tuple(farm_rates),
        pool_rates=pool_rates,
        total_rate=None,
        tvl_usd=tvl_usd,
        fetched_at=timestamp,
        status=status,
        identity_valid=True,
        status_detail=detail,
    )


async def _request_json(client: httpx.AsyncClient, url: str) -> Any:
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def _fetch_dedust_pool(client: httpx.AsyncClient, pool: FarmingPool) -> PoolMetrics:
    payload = await _request_json(client, f"{DEDUST_BASE_URL}/{pool.address}")
    return parse_dedust_pool(payload, pool)


async def _fetch_ston_pool(client: httpx.AsyncClient, pool: FarmingPool) -> PoolMetrics:
    pool_payload = await _request_json(client, f"{STON_BASE_URL}/pools/{pool.address}")
    farms_payload: Any = None
    farm_transport_failed = False
    try:
        farms_payload = await _request_json(client, f"{STON_BASE_URL}/farms/by_pool/{pool.address}")
    except httpx.TimeoutException as exc:
        logging.warning("STON.fi farms request timed out for %s", pool.address)
        farm_transport_failed = True
    except httpx.HTTPError as exc:
        logging.warning("STON.fi farms request failed for %s: %s", pool.address, exc)
        farm_transport_failed = isinstance(exc, httpx.RequestError)
    metrics = parse_ston_pool(pool_payload, farms_payload, pool)
    if farm_transport_failed and metrics.status == SourceStatus.PARTIAL and metrics.identity_valid:
        return replace(metrics, stale_farm_yield_fallback_allowed=True)
    return metrics


async def fetch_pool_info(client: httpx.AsyncClient, pool: FarmingPool) -> PoolMetrics:
    try:
        if pool.dex == "DeDust":
            operation = _fetch_dedust_pool(client, pool)
        elif pool.dex == "STON.fi":
            operation = _fetch_ston_pool(client, pool)
        else:
            return unavailable_metrics(pool, SourceStatus.INVALID_RESPONSE, "unknown DEX")
        return await asyncio.wait_for(operation, timeout=POOL_FETCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logging.warning("Farming request timed out for %s %s", pool.dex, pool.address)
        return unavailable_metrics(pool, SourceStatus.TIMEOUT, "request timed out")
    except httpx.TimeoutException:
        logging.warning("Farming request timed out for %s %s", pool.dex, pool.address)
        return unavailable_metrics(pool, SourceStatus.TIMEOUT, "request timed out")
    except httpx.HTTPError as exc:
        logging.warning("Farming request unavailable for %s %s: %s", pool.dex, pool.address, exc)
        return unavailable_metrics(pool, SourceStatus.UNAVAILABLE, "source request failed")
    except (ValueError, TypeError) as exc:
        logging.warning("Farming response invalid for %s %s: %s", pool.dex, pool.address, exc)
        return unavailable_metrics(pool, SourceStatus.INVALID_RESPONSE, "response could not be parsed")
    except Exception:
        logging.exception("Failed to fetch farming pool info for %s %s", pool.dex, pool.address)
        return unavailable_metrics(pool, SourceStatus.UNAVAILABLE, "unexpected source error")


async def fetch_all_farming_info() -> list[PoolMetrics]:
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=4)
    headers = {
        "Accept": "application/json",
        "User-Agent": "POOLMASTER Telegram Bot/1.0",
    }
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        return list(await asyncio.gather(*(fetch_pool_info(client, pool) for pool in FARMING_POOLS)))
