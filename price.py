from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from farming import JETTON_MASTER_ADDRESS, SourceStatus


MEXC_API_BASE_URL = "https://api.mexc.com/api/v3"
MEXC_SYMBOL = "JETTONUSDT"
MEXC_BASE_ASSET = "JETTON"
MEXC_QUOTE_ASSET = "USDT"
JETTON_CONTRACT_ADDRESS = JETTON_MASTER_ADDRESS
PRICE_REQUEST_TIMEOUT_SECONDS = 8
PRICE_SOURCE_MAX_FUTURE_SKEW_SECONDS = 30


@dataclass(frozen=True)
class PriceMetrics:
    price_usdt: Decimal | None
    bid_usdt: Decimal | None
    ask_usdt: Decimal | None
    quote_volume_24h: Decimal | None
    symbol: str
    quote_currency: str
    source: str
    fetched_at: datetime
    source_timestamp: datetime | None
    status: SourceStatus
    status_detail: str | None = None
    is_stale: bool = False
    price_change_24h_percent: Decimal | None = None
    high_price_24h: Decimal | None = None
    low_price_24h: Decimal | None = None


@dataclass(frozen=True)
class PriceIdentityCheck:
    valid: bool | None
    detail: str


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field} is not numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed == 0):
        raise ValueError(f"{field} is outside the accepted numeric domain")
    return parsed


def _signed_decimal(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field} is not numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} is not finite")
    return parsed


def _invalid_price(fetched_at: datetime, status: SourceStatus, detail: str) -> PriceMetrics:
    return PriceMetrics(
        price_usdt=None,
        bid_usdt=None,
        ask_usdt=None,
        quote_volume_24h=None,
        symbol=MEXC_SYMBOL,
        quote_currency=MEXC_QUOTE_ASSET,
        source="MEXC spot",
        fetched_at=fetched_at,
        source_timestamp=None,
        status=status,
        status_detail=detail,
    )


def parse_mexc_ticker(
    payload: Any,
    *,
    fetched_at: datetime | None = None,
    max_source_age_seconds: int = 300,
) -> PriceMetrics:
    timestamp = fetched_at or _now_utc()
    if max_source_age_seconds < 1:
        raise ValueError("max_source_age_seconds must be greater than zero")
    if not isinstance(payload, dict) or payload.get("symbol") != MEXC_SYMBOL:
        return _invalid_price(timestamp, SourceStatus.INVALID_RESPONSE, "unexpected ticker schema or symbol")

    try:
        price_usdt = _decimal(payload.get("lastPrice"), "lastPrice", positive=True)
        bid_usdt = _decimal(payload.get("bidPrice"), "bidPrice", positive=True)
        ask_usdt = _decimal(payload.get("askPrice"), "askPrice", positive=True)
        quote_volume_24h = _decimal(payload.get("quoteVolume"), "quoteVolume")
        # MEXC returns this field as a ratio (for example, 0.004 = 0.4%).
        raw_price_change_24h = _signed_decimal(payload.get("priceChangePercent"), "priceChangePercent")
        high_price_24h = _decimal(payload.get("highPrice"), "highPrice", positive=True)
        low_price_24h = _decimal(payload.get("lowPrice"), "lowPrice", positive=True)
        if high_price_24h is not None and low_price_24h is not None and high_price_24h < low_price_24h:
            raise ValueError("24-hour high is below the 24-hour low")
        close_time_ms = payload.get("closeTime")
        if isinstance(close_time_ms, bool) or not isinstance(close_time_ms, (int, float)):
            raise ValueError("closeTime is missing or invalid")
        if close_time_ms <= 0:
            raise ValueError("closeTime is outside the accepted timestamp domain")
        source_timestamp = datetime.fromtimestamp(float(close_time_ms) / 1000, timezone.utc)
    except (ValueError, OSError, OverflowError) as exc:
        return _invalid_price(timestamp, SourceStatus.INVALID_RESPONSE, str(exc))

    if price_usdt is None:
        return _invalid_price(timestamp, SourceStatus.INVALID_RESPONSE, "lastPrice is missing")
    source_age_seconds = (timestamp - source_timestamp).total_seconds()
    if source_age_seconds < -PRICE_SOURCE_MAX_FUTURE_SKEW_SECONDS:
        return _invalid_price(timestamp, SourceStatus.INVALID_RESPONSE, "ticker source timestamp is in the future")
    if source_age_seconds > max_source_age_seconds:
        return _invalid_price(timestamp, SourceStatus.UNAVAILABLE, "ticker source timestamp is stale")
    return PriceMetrics(
        price_usdt=price_usdt,
        bid_usdt=bid_usdt,
        ask_usdt=ask_usdt,
        quote_volume_24h=quote_volume_24h,
        symbol=MEXC_SYMBOL,
        quote_currency=MEXC_QUOTE_ASSET,
        source="MEXC spot",
        fetched_at=timestamp,
        source_timestamp=source_timestamp,
        status=SourceStatus.OK,
        price_change_24h_percent=(
            raw_price_change_24h * Decimal(100) if raw_price_change_24h is not None else None
        ),
        high_price_24h=high_price_24h,
        low_price_24h=low_price_24h,
    )


async def _request_json(client: httpx.AsyncClient, path: str, **params: str) -> Any:
    response = await client.get(f"{MEXC_API_BASE_URL}{path}", params=params)
    response.raise_for_status()
    return response.json()


async def _verify_market_identity(client: httpx.AsyncClient) -> PriceIdentityCheck:
    try:
        payload = await _request_json(client, "/exchangeInfo", symbol=MEXC_SYMBOL)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return PriceIdentityCheck(None, "MEXC market identity request timed out")
    except httpx.HTTPError:
        return PriceIdentityCheck(None, "MEXC market identity source unavailable")
    except (ValueError, TypeError):
        return PriceIdentityCheck(False, "MEXC market identity response is invalid")

    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        return PriceIdentityCheck(False, "MEXC market identity response schema is invalid")
    market = next(
        (item for item in payload["symbols"] if isinstance(item, dict) and item.get("symbol") == MEXC_SYMBOL),
        None,
    )
    if not market:
        return PriceIdentityCheck(False, "MEXC JETTONUSDT market is missing")
    if market.get("baseAsset") != MEXC_BASE_ASSET or market.get("quoteAsset") != MEXC_QUOTE_ASSET:
        return PriceIdentityCheck(False, "MEXC market asset or quote mismatch")
    if market.get("contractAddress") != JETTON_CONTRACT_ADDRESS:
        return PriceIdentityCheck(False, "MEXC JETTON contract mismatch")
    if str(market.get("status")) != "1":
        return PriceIdentityCheck(False, "MEXC market is not online")
    return PriceIdentityCheck(True, "MEXC JETTONUSDT market metadata match")


async def verify_mexc_market_identity(
    client: httpx.AsyncClient | None = None,
) -> PriceIdentityCheck:
    if client is not None:
        return await _verify_market_identity(client)
    timeout = httpx.Timeout(PRICE_REQUEST_TIMEOUT_SECONDS, connect=4)
    headers = {"Accept": "application/json", "User-Agent": "POOLMASTER Telegram Bot/1.0"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as owned_client:
        return await _verify_market_identity(owned_client)


async def _fetch_price_metrics(
    client: httpx.AsyncClient,
    fetched_at: datetime | None = None,
    *,
    max_source_age_seconds: int = 300,
) -> PriceMetrics:
    timestamp = fetched_at or _now_utc()
    try:
        payload = await _request_json(client, "/ticker/24hr", symbol=MEXC_SYMBOL)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return _invalid_price(timestamp, SourceStatus.TIMEOUT, "MEXC ticker request timed out")
    except httpx.HTTPError as exc:
        logging.warning("MEXC ticker source unavailable: %s", exc)
        return _invalid_price(timestamp, SourceStatus.UNAVAILABLE, "MEXC ticker request failed")
    except (ValueError, TypeError):
        return _invalid_price(timestamp, SourceStatus.INVALID_RESPONSE, "MEXC ticker response is invalid")
    return parse_mexc_ticker(
        payload,
        fetched_at=timestamp,
        max_source_age_seconds=max_source_age_seconds,
    )


async def fetch_price_metrics(
    client: httpx.AsyncClient | None = None,
    *,
    fetched_at: datetime | None = None,
    max_source_age_seconds: int = 300,
) -> PriceMetrics:
    if client is not None:
        return await _fetch_price_metrics(
            client,
            fetched_at,
            max_source_age_seconds=max_source_age_seconds,
        )
    timeout = httpx.Timeout(PRICE_REQUEST_TIMEOUT_SECONDS, connect=4)
    headers = {"Accept": "application/json", "User-Agent": "POOLMASTER Telegram Bot/1.0"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as owned_client:
        return await _fetch_price_metrics(
            owned_client,
            fetched_at,
            max_source_age_seconds=max_source_age_seconds,
        )
