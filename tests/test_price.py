import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

import price
from farming import SourceStatus


FETCHED_AT = datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)
SOURCE_TIME_MS = int(FETCHED_AT.timestamp() * 1000)


def _ticker(**updates):
    payload = {
        "symbol": "JETTONUSDT",
        "lastPrice": "0.01865",
        "bidPrice": "0.01861",
        "askPrice": "0.01866",
        "quoteVolume": "58986.419",
        "priceChangePercent": "-0.0035",
        "highPrice": "0.01920",
        "lowPrice": "0.01810",
        "closeTime": SOURCE_TIME_MS,
    }
    payload.update(updates)
    return payload


def test_mexc_ticker_parser_preserves_price_quote_source_and_timestamp():
    metrics = price.parse_mexc_ticker(_ticker(), fetched_at=FETCHED_AT)

    assert metrics.status == SourceStatus.OK
    assert metrics.source == "MEXC spot"
    assert metrics.symbol == "JETTONUSDT"
    assert metrics.quote_currency == "USDT"
    assert metrics.price_usdt == Decimal("0.01865")
    assert metrics.bid_usdt == Decimal("0.01861")
    assert metrics.ask_usdt == Decimal("0.01866")
    assert metrics.quote_volume_24h == Decimal("58986.419")
    assert metrics.price_change_24h_percent == Decimal("-0.3500")
    assert metrics.high_price_24h == Decimal("0.01920")
    assert metrics.low_price_24h == Decimal("0.01810")
    assert metrics.fetched_at == FETCHED_AT
    assert metrics.source_timestamp == FETCHED_AT


def test_mexc_ticker_parser_rejects_inconsistent_24h_range():
    metrics = price.parse_mexc_ticker(
        _ticker(highPrice="0.01800", lowPrice="0.01900"),
        fetched_at=FETCHED_AT,
    )

    assert metrics.status == SourceStatus.INVALID_RESPONSE
    assert metrics.price_usdt is None


def test_mexc_ticker_parser_rejects_wrong_market_or_nonpositive_prices():
    wrong_market = price.parse_mexc_ticker(_ticker(symbol="JETTONUSD"), fetched_at=FETCHED_AT)
    no_price = price.parse_mexc_ticker(_ticker(lastPrice="0"), fetched_at=FETCHED_AT)

    assert wrong_market.status == SourceStatus.INVALID_RESPONSE
    assert wrong_market.price_usdt is None
    assert no_price.status == SourceStatus.INVALID_RESPONSE
    assert no_price.price_usdt is None


@pytest.mark.parametrize(
    ("source_offset", "expected_status"),
    ((timedelta(minutes=-6), SourceStatus.UNAVAILABLE), (timedelta(seconds=31), SourceStatus.INVALID_RESPONSE)),
)
def test_mexc_ticker_parser_rejects_stale_or_far_future_source_time(source_offset, expected_status):
    source_time = FETCHED_AT + source_offset
    metrics = price.parse_mexc_ticker(
        _ticker(closeTime=int(source_time.timestamp() * 1000)),
        fetched_at=FETCHED_AT,
    )

    assert metrics.status == expected_status
    assert metrics.price_usdt is None


def test_mexc_market_identity_checks_contract_and_quote_asset():
    async def run():
        def handler(request):
            assert request.url.path == "/api/v3/exchangeInfo"
            assert request.url.params["symbol"] == "JETTONUSDT"
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "JETTONUSDT",
                            "status": "1",
                            "baseAsset": "JETTON",
                            "quoteAsset": "USDT",
                            "contractAddress": price.JETTON_CONTRACT_ADDRESS,
                            "isSpotTradingAllowed": False,
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await price.verify_mexc_market_identity(client)

    result = asyncio.run(run())

    assert result.valid is True


def test_mexc_market_identity_rejects_a_different_contract():
    async def run():
        async def handler(_request):
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "JETTONUSDT",
                            "status": "1",
                            "baseAsset": "JETTON",
                            "quoteAsset": "USDT",
                            "contractAddress": "0:" + "0" * 64,
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await price.verify_mexc_market_identity(client)

    result = asyncio.run(run())

    assert result.valid is False
    assert "contract" in result.detail


def test_mexc_price_timeout_returns_a_typed_source_status():
    async def run():
        def handler(_request):
            raise httpx.ReadTimeout("sample timeout")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await price.fetch_price_metrics(client, fetched_at=FETCHED_AT)

    metrics = asyncio.run(run())

    assert metrics.status == SourceStatus.TIMEOUT
    assert metrics.price_usdt is None
