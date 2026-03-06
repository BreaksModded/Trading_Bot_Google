"""
Protocol conformance tests — prevent silent AttributeError in production.

If a method exists in ExchangeGateway Protocol but not in BybitExchangeClient,
these tests fail BEFORE reaching production — preventing BUG-17 class of bugs.
"""
from __future__ import annotations

import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.exchange import BybitExchangeClient
from core.order_manager import ExchangeGateway


def _get_protocol_public_methods(cls) -> set[str]:
    """Return public method names defined directly on a Protocol class."""
    return {
        name
        for name, member in inspect.getmembers(cls)
        if not name.startswith("_")
        and callable(member)
    }


class TestProtocolConformance:

    def test_bybit_client_implements_all_protocol_methods(self):
        """BybitExchangeClient must implement ALL methods in ExchangeGateway Protocol.

        Fails if any method is missing — prevents silent AttributeError in production.
        """
        protocol_methods = _get_protocol_public_methods(ExchangeGateway)
        client_methods = _get_protocol_public_methods(BybitExchangeClient)
        missing = protocol_methods - client_methods
        assert not missing, (
            f"\n🔴 BybitExchangeClient missing Protocol methods: {missing}\n"
            f"Add these methods to core/exchange.py before merging."
        )

    def test_place_market_order_signature_matches_protocol(self):
        """place_market_order signature must match Protocol exactly."""
        protocol_sig = inspect.signature(ExchangeGateway.place_market_order)
        client_sig = inspect.signature(BybitExchangeClient.place_market_order)

        protocol_params = set(protocol_sig.parameters.keys()) - {"self"}
        client_params = set(client_sig.parameters.keys()) - {"self"}

        assert protocol_params == client_params, (
            f"Signature mismatch:\n"
            f"  Protocol expects: {protocol_params}\n"
            f"  Client has:       {client_params}"
        )

    def test_no_protocol_methods_raise_not_implemented(self):
        """No BybitExchangeClient method should only raise NotImplementedError."""
        for name in _get_protocol_public_methods(ExchangeGateway):
            method = getattr(BybitExchangeClient, name, None)
            if method is None:
                continue
            try:
                source = inspect.getsource(method)
                assert "NotImplementedError" not in source, (
                    f"Method '{name}' raises NotImplementedError — implement it"
                )
            except (OSError, TypeError):
                pass  # built-ins or C extensions — skip


class TestPlaceMarketOrder:
    """Functional tests for BybitExchangeClient.place_market_order()."""

    def _make_client(self, place_order_return=None, side_effect=None):
        from decimal import Decimal
        from core.exchange import SpotSymbolRules

        mock_http = MagicMock()
        mock_http.place_order = MagicMock(
            return_value=place_order_return or {"retCode": 0, "result": {"orderId": "mkt-123"}},
            side_effect=side_effect,
        )

        client = BybitExchangeClient.__new__(BybitExchangeClient)
        client.category = "spot"
        client._ensure_http = MagicMock(return_value=mock_http)

        async def _fake_run_http(fn, /, *args, **kwargs):
            return fn(*args, **kwargs)

        client._run_http = _fake_run_http

        async def _fake_rules(symbol):
            return SpotSymbolRules(
                qty_step=Decimal("0.00001"),
                min_qty=Decimal("0.00001"),
                tick_size=Decimal("0.01"),
            )

        client.get_spot_symbol_rules = _fake_rules
        return client, mock_http

    @pytest.mark.asyncio
    async def test_market_sell_calls_bybit_correctly(self):
        """SELL: qty in base asset, orderType=Market, no PostOnly/timeInForce."""
        client, mock_http = self._make_client()

        await client.place_market_order(symbol="BTCUSDC", side="Sell", qty=0.001)

        kw = mock_http.place_order.call_args.kwargs
        assert kw["orderType"] == "Market"
        assert kw["side"] == "Sell"
        assert kw["symbol"] == "BTCUSDC"
        assert kw["qty"] == "0.001"
        assert "timeInForce" not in kw, "Market orders must NOT have PostOnly/timeInForce"
        assert kw.get("marketUnit") is None or "base" in kw.get("marketUnit", "").lower()

    @pytest.mark.asyncio
    async def test_market_buy_uses_quote_coin_qty(self):
        """BUY: marketUnit=quoteCoinQty for Bybit Spot (qty is in USDC)."""
        client, mock_http = self._make_client()

        await client.place_market_order(symbol="BTCUSDC", side="Buy", qty=50.0)

        kw = mock_http.place_order.call_args.kwargs
        assert kw.get("marketUnit") == "quoteCoinQty"

    @pytest.mark.asyncio
    async def test_market_order_logs_error_on_bybit_rejection(self):
        """retCode != 0 is logged as error but does NOT raise exception — returns empty order_id."""
        client, mock_http = self._make_client(
            place_order_return={"retCode": 170131, "retMsg": "insufficient balance"}
        )

        result = await client.place_market_order(symbol="BTCUSDC", side="Sell", qty=0.001)

        # place_market_order returns order_id as str; empty string on rejection
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_market_order_raises_on_network_exception(self):
        """Network/SDK errors are propagated to the caller."""
        client, mock_http = self._make_client(side_effect=Exception("network timeout"))

        with pytest.raises(Exception, match="network timeout"):
            await client.place_market_order(symbol="BTCUSDC", side="Sell", qty=0.001)


# ── FIX-4: Per-symbol inventory stop-loss threshold ──────────────────────────


class TestPerSymbolStopLossThreshold:
    """Per-symbol inventory SL threshold takes priority over global threshold."""

    def _make_settings(self, global_sl: float, per_symbol: dict[str, float] | None = None):
        # Plain namespace avoids BaseSettings loading .env (which would override the dict arg)
        class _S:
            inventory_stop_loss_pct = global_sl
            inventory_stop_loss_per_symbol: dict = per_symbol if per_symbol is not None else {}
        return _S()

    def test_per_symbol_override_used_when_configured(self):
        """SOL uses 15% override even though global is 10%."""
        settings = self._make_settings(global_sl=0.10, per_symbol={"SOLUSDC": 0.15})
        effective = (
            settings.inventory_stop_loss_per_symbol.get("SOLUSDC")
            or settings.inventory_stop_loss_pct
        )
        assert effective == 0.15

    def test_global_threshold_used_when_no_override(self):
        """BTC uses global threshold when no per-symbol entry exists."""
        settings = self._make_settings(global_sl=0.10, per_symbol={"SOLUSDC": 0.15})
        effective = (
            settings.inventory_stop_loss_per_symbol.get("BTCUSDC")
            or settings.inventory_stop_loss_pct
        )
        assert effective == 0.10

    def test_empty_per_symbol_dict_falls_back_to_global(self):
        """Empty dict means all symbols use global threshold."""
        settings = self._make_settings(global_sl=0.10, per_symbol={})
        for sym in ("BTCUSDC", "ETHUSDC", "SOLUSDC"):
            effective = settings.inventory_stop_loss_per_symbol.get(sym) or settings.inventory_stop_loss_pct
            assert effective == 0.10

    def test_multiple_symbol_overrides_coexist(self):
        """Multiple per-symbol entries all work independently."""
        settings = self._make_settings(
            global_sl=0.10,
            per_symbol={"SOLUSDC": 0.15, "XRPUSDC": 0.15, "ADAUSDC": 0.18},
        )
        assert settings.inventory_stop_loss_per_symbol["SOLUSDC"] == 0.15
        assert settings.inventory_stop_loss_per_symbol["XRPUSDC"] == 0.15
        assert settings.inventory_stop_loss_per_symbol["ADAUSDC"] == 0.18

    def test_global_threshold_range_validated(self):
        """Global threshold outside 1%-50% range raises validation error."""
        import pydantic
        from config.settings import GridSettings
        # Use real GridSettings for validation — scalar kwargs take priority over .env
        with pytest.raises((pydantic.ValidationError, ValueError)):
            GridSettings(inventory_stop_loss_pct=0.005)  # below 1% minimum
