"""Unit tests for the OKX X-Perps client — pure logic only (no network / no auth).

Covers the parts that translate between the bot's coin-denominated world and OKX's
contract-denominated one, plus the fill-event translation that feeds handle_fill.
The live read/trade paths are validated against OKX demo (see docs/OKX_MIGRATION.md).
"""
import pytest

from core.exchange_okx import OKXExchangeClient


@pytest.fixture
def client() -> OKXExchangeClient:
    # ccxt.pro.okx() constructs without a running loop (session is created lazily),
    # so this is safe in a sync test. No network is touched by the methods under test.
    c = OKXExchangeClient(
        api_key="", api_secret="", passphrase="",
        demo=True, symbol="ETHUSDT", timeframe="60",
    )
    c._contract_size = 0.001   # ETH X-Perp: 1 contract = 0.001 ETH (confirmed via ccxt)
    return c


def test_base_of_strips_quote():
    assert OKXExchangeClient._base_of("ETHUSDT") == "ETH"
    assert OKXExchangeClient._base_of("BTC/USD:USD-310404") == "BTC"
    assert OKXExchangeClient._base_of("SOLUSDC") == "SOL"


def test_coin_to_contracts_floors(client):
    assert client._to_contracts(0.5) == 500
    assert client._to_contracts(0.0249) == 24      # floors partial contracts
    assert client._to_contracts(0.0009) == 0       # below 1 contract
    assert client._to_coin(500) == pytest.approx(0.5)


def test_clean_cl_ord_id(client):
    # OKX clOrdId must be alphanumeric, <=32 chars; the bot's ids carry hyphens.
    assert client._clean_cl_ord_id("grid-S2-1782589243224") == "gridS21782589243224"
    assert client._clean_cl_ord_id(None) is None
    assert client._clean_cl_ord_id("a" * 40) == "a" * 32


def test_translate_filled_order_to_bybit_shape(client):
    okx_order = {
        "id": "abc", "clientOrderId": "x", "status": "closed", "filled": 500.0,
        "average": 1577.0, "price": 1577.0, "side": "buy",
        "fee": {"cost": 0.12}, "info": {"tradeId": "t1", "pnl": "3.4"},
    }
    ev = client._translate_order(okx_order)
    assert ev["orderStatus"] == "Filled"
    assert ev["cumExecQty"] == pytest.approx(0.5)   # 500 contracts -> 0.5 ETH
    assert ev["execQty"] == pytest.approx(0.5)
    assert ev["side"] == "Buy"                       # buy -> Buy (Bybit shape)
    assert ev["avgPrice"] == 1577.0
    assert ev["execFee"] == 0.12
    assert ev["closedPnl"] == 3.4
    assert ev["execId"] == "t1"


def test_translate_partial_fill(client):
    okx_order = {
        "id": "abc", "status": "open", "filled": 100.0, "average": 1577.0,
        "side": "sell", "fee": {}, "info": {},
    }
    ev = client._translate_order(okx_order)
    assert ev["orderStatus"] == "PartiallyFilled"
    assert ev["cumExecQty"] == pytest.approx(0.1)    # 100 contracts -> 0.1 ETH
    assert ev["side"] == "Sell"


def test_translate_unfilled_order_is_skipped(client):
    assert client._translate_order({"id": "abc", "status": "open", "filled": 0.0, "info": {}}) is None
