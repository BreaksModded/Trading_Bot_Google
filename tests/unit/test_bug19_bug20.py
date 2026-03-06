"""Tests for BUG-19 (Decimal×float TypeError) and BUG-20 (KeyError cooldown).

BUG-19  — root cause
    SpotSymbolRules fields (min_qty, qty_step, tick_size) are decimal.Decimal.
    The Phase-G committed code at main.py:924 did:
        min_threshold = rules.min_qty * live_price * MIN_VIABLE_LEVELS
    where live_price is float.  Python raises:
        TypeError: unsupported operand type(s) for *: 'decimal.Decimal' and 'float'
    Fix: float(rules.min_qty) * live_price (main.py:924).

BUG-20  — root cause (unconfirmed; awaiting enhanced traceback log)
    KeyError('"category"') propagates from somewhere inside place_grid_orders
    (or its callees) up to the _place_new_grids except-block.
    The error is caught; the symbol receives a 120-second placement cooldown.
    The key embedded double-quotes mean "price limit" and "170193" patterns
    do NOT match, so the SHORT 120-second cooldown (not 900-second) is applied.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core.exchange import SpotSymbolRules


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _btc_rules() -> SpotSymbolRules:
    return SpotSymbolRules(
        qty_step=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
    )


def _ada_rules() -> SpotSymbolRules:
    return SpotSymbolRules(
        qty_step=Decimal("1"),
        min_qty=Decimal("1"),
        tick_size=Decimal("0.0001"),
    )


# ── BUG-19: Decimal × float ────────────────────────────────────────────────────


class TestBug19DecimalFloat:
    """
    BUG-19: SpotSymbolRules.min_qty is decimal.Decimal.
    Arithmetic with float raises TypeError; only comparison (≥3.3) is allowed.
    """

    def test_decimal_times_float_raises_typeerror(self):
        """
        Reproduce the original crash: rules.min_qty * live_price where
        live_price is float.  Expected behaviour: TypeError.
        536 crashes in production before fix.
        """
        rules = _btc_rules()
        live_price: float = 85_000.0

        with pytest.raises(TypeError, match="unsupported operand type"):
            _ = rules.min_qty * live_price  # the unfixed line

    def test_float_of_decimal_times_float_does_not_raise(self):
        """Fix: float(rules.min_qty) * live_price must NOT raise TypeError."""
        rules = _btc_rules()
        live_price: float = 85_000.0
        MIN_VIABLE_LEVELS = 3

        # Exact formula from main.py:924 after the fix
        min_threshold = float(rules.min_qty) * live_price * MIN_VIABLE_LEVELS

        assert isinstance(min_threshold, float)
        assert min_threshold == pytest.approx(0.00001 * 85_000.0 * 3)

    def test_min_threshold_formula_btc(self):
        """BTC rules: min_threshold = float(min_qty) × price × 3 levels."""
        rules = _btc_rules()
        live_price = 85_000.0
        MIN_VIABLE_LEVELS = 3

        result = float(rules.min_qty) * live_price * MIN_VIABLE_LEVELS

        # 0.00001 BTC × 85000 $/BTC × 3 = 2.55 USDT
        assert result == pytest.approx(2.55)

    def test_min_threshold_formula_ada(self):
        """ADA rules: min_threshold = float(min_qty) × price × 3 levels."""
        rules = _ada_rules()
        live_price = 0.75
        MIN_VIABLE_LEVELS = 3

        result = float(rules.min_qty) * live_price * MIN_VIABLE_LEVELS

        # 1 ADA × 0.75 $/ADA × 3 = 2.25 USDT
        assert result == pytest.approx(2.25)

    def test_decimal_comparison_with_float_works_py33(self):
        """
        float < Decimal IS supported in Python 3.3+.
        This confirms line 942 (lvl.qty < float(rules.min_qty)) was a
        defensive improvement, NOT a crash fix — the comparison itself
        would not have raised TypeError.
        """
        rules = _btc_rules()

        qty_below = 0.000005  # half of min_qty
        qty_above = 0.0001    # ten times min_qty

        # float < Decimal comparison works without TypeError
        assert qty_below < rules.min_qty
        assert not (qty_above < rules.min_qty)

    def test_all_decimal_fields_convertible_to_float(self):
        """
        All three Decimal fields in SpotSymbolRules convert to float without
        precision loss for the tick sizes used in practice.
        """
        rules = SpotSymbolRules(
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            tick_size=Decimal("0.01"),
        )
        assert float(rules.qty_step) == pytest.approx(0.001)
        assert float(rules.min_qty) == pytest.approx(0.001)
        assert float(rules.tick_size) == pytest.approx(0.01)


# ── BUG-20: KeyError cooldown behaviour ───────────────────────────────────────


class TestBug20KeyErrorCooldown:
    """
    BUG-20: KeyError('"category"') raised inside place_grid_orders and caught
    by _place_new_grids.  Verifies the cooldown-selection logic that the
    except-block applies.
    """

    # Mirrors TradingBot constants (main.py:110-111)
    _PLACEMENT_COOLDOWN_SECS = 120
    _PRICE_LIMIT_COOLDOWN_SECS = 900

    def _select_cooldown(self, exc: Exception) -> int:
        """Replicate the cooldown-selection logic from main.py:1061-1065."""
        exc_str = str(exc)
        if "170193" in exc_str or "price limit" in exc_str.lower():
            return self._PRICE_LIMIT_COOLDOWN_SECS
        return self._PLACEMENT_COOLDOWN_SECS

    def test_keyerror_repr_contains_embedded_quotes(self):
        """
        KeyError('"category"') str() representation preserves embedded
        double-quote chars.  This is why the log shows '"category"' (10 chars)
        rather than 'category' (8 chars).
        """
        exc = KeyError('"category"')
        exc_str = str(exc)
        assert '"category"' in exc_str   # embedded double-quotes present
        assert "category" in exc_str

    def test_keyerror_category_does_not_match_price_limit_pattern(self):
        """
        str(KeyError('"category"')) contains neither '170193' nor 'price limit'.
        Consequence: 120-second standard cooldown is applied, not 900-second.
        """
        exc = KeyError('"category"')
        exc_str = str(exc)
        assert "170193" not in exc_str
        assert "price limit" not in exc_str.lower()

    def test_generic_exceptions_receive_standard_120s_cooldown(self):
        """Generic errors → 120-second cooldown (not 900-second)."""
        generic_errors = [
            KeyError('"category"'),
            RuntimeError("connection reset"),
            ValueError("invalid qty"),
            TypeError("Decimal × float"),
        ]
        for exc in generic_errors:
            assert self._select_cooldown(exc) == 120, (
                f"Expected 120s for {exc!r}, got {self._select_cooldown(exc)}"
            )

    def test_price_limit_error_receives_900s_cooldown(self):
        """Errors containing '170193' → 900-second extended cooldown."""
        price_limit_errors = [
            Exception("ErrCode: 170193 Order price out of limit"),
            Exception("170193"),
        ]
        for exc in price_limit_errors:
            assert self._select_cooldown(exc) == 900, (
                f"Expected 900s for {exc!r}, got {self._select_cooldown(exc)}"
            )

    def test_price_limit_text_receives_900s_cooldown(self):
        """Errors containing 'price limit' (case-insensitive) → 900-second cooldown."""
        price_limit_text_errors = [
            Exception("price limit exceeded"),
            Exception("PRICE LIMIT VIOLATION"),
            Exception("Order rejected: price limit too low"),
        ]
        for exc in price_limit_text_errors:
            assert self._select_cooldown(exc) == 900, (
                f"Expected 900s for {exc!r}, got {self._select_cooldown(exc)}"
            )

    def test_trading_bot_default_cooldown_constants(self):
        """
        TradingBot._placement_cooldown_seconds == 120
        TradingBot._price_limit_cooldown_seconds == 900
        These constants are hardcoded — verifies they match expected behaviour.
        """
        # Import lazily to avoid triggering full bot initialisation
        import inspect
        import main as main_module

        source = inspect.getsource(main_module.TradingBot.__init__)
        assert "_placement_cooldown_seconds: int = 120" in source, (
            "Expected _placement_cooldown_seconds = 120 in TradingBot.__init__"
        )
        assert "_price_limit_cooldown_seconds: int = 900" in source, (
            "Expected _price_limit_cooldown_seconds = 900 in TradingBot.__init__"
        )
