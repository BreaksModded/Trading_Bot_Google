"""Integration tests for the FuturesBot orchestrator state machine.

These cover the glue (main_futures.py) that the pure-function tests don't: trend
entries/rejections/confirmations, live-price stop-outs, reversals, grid placement,
the grid ATR stop, and the transitional stand-aside. The exchange and position
manager are faked so the handlers can be driven deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from config.settings import Settings
from core.exchange import SpotSymbolRules
from core.futures_risk import FuturesRiskManager
from core.regime import MarketRegime
from data.models import FuturesPosition
from main_futures import FuturesBot


def _pos(side="flat", size=0.0, entry=0.0, mark=0.0, liq=0.0, upnl=0.0) -> FuturesPosition:
    return FuturesPosition(
        symbol="ETHUSDT", side=side, size=size, entry_price=entry, mark_price=mark,
        liq_price=liq, leverage=5, unrealized_pnl=upnl, position_value=0.0,
        margin=0.0, updated_at=datetime.now(UTC),
    )


def _ind(price=2000.0, adx=30.0, ema_fast=2010.0, ema_slow=1990.0, atr_pct=0.01,
         cl=1950.0, cs=2050.0) -> dict:
    return {"close": price, "atr": price * atr_pct, "atr_pct": atr_pct, "adx": adx,
            "ema_fast": ema_fast, "ema_slow": ema_slow,
            "chandelier_long": cl, "chandelier_short": cs}


def _bot():
    settings = Settings()
    settings.futures.symbol = "ETHUSDT"
    ex = AsyncMock()
    ex.place_market_linear = AsyncMock(return_value="oid1")
    ex.set_position_stop_loss = AsyncMock()
    ex.get_last_price = AsyncMock(return_value=2000.0)
    pm = AsyncMock()
    pm.has_open_orders = MagicMock(return_value=False)
    pm.reset = MagicMock()
    pm.cancel_all = AsyncMock()
    pm.place_grid = AsyncMock(return_value=8)
    pm.sync_orders = AsyncMock()
    pm.get_position = AsyncMock(return_value=_pos())
    bot = FuturesBot(
        settings=settings, db=MagicMock(), exchange=ex, position_manager=pm,
        risk_manager=FuturesRiskManager(max_daily_loss_pct=0.06, max_total_drawdown_pct=0.20),
        notifier=AsyncMock(), health_monitor=AsyncMock(),
        rules=SpotSymbolRules(qty_step=Decimal("0.001"), min_qty=Decimal("0.001"), tick_size=Decimal("0.01")),
    )
    return bot, ex, pm


# ── Trend entry ────────────────────────────────────────────────────────


async def test_trend_entry_opens_long_and_sets_exchange_sl():
    bot, ex, pm = _bot()
    pm.get_position = AsyncMock(return_value=_pos(side="long", size=0.045, entry=2000))
    await bot._handle_trend(MarketRegime.TRENDING_UP, MarketRegime.TRENDING_UP,
                            _ind(), 2000.0, _pos(), equity=150.0, free=150.0)
    assert bot.mode == "trend"
    args = ex.place_market_linear.call_args.kwargs
    assert args["side"] == "Buy" and args["reduce_only"] is False
    ex.set_position_stop_loss.assert_awaited()  # exchange-side backstop wired


async def test_downtrend_opens_short():
    bot, ex, pm = _bot()
    pm.get_position = AsyncMock(return_value=_pos(side="short", size=0.045, entry=2000))
    await bot._handle_trend(MarketRegime.TRENDING_DOWN, MarketRegime.TRENDING_DOWN,
                            _ind(), 2000.0, _pos(), equity=150.0, free=150.0)
    assert ex.place_market_linear.call_args.kwargs["side"] == "Sell"


async def test_htf_conflict_blocks_entry():
    bot, ex, _ = _bot()
    await bot._handle_trend(MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN,
                            _ind(), 2000.0, _pos(), equity=150.0, free=150.0)
    assert bot.mode == "flat"
    ex.place_market_linear.assert_not_awaited()


# ── Entry robustness (A2) ──────────────────────────────────────────────


async def test_rejected_entry_sets_cooldown_and_stays_flat():
    bot, ex, _ = _bot()
    ex.place_market_linear = AsyncMock(side_effect=RuntimeError("retCode=110007"))
    await bot._handle_trend(MarketRegime.TRENDING_UP, MarketRegime.TRENDING_UP,
                            _ind(), 2000.0, _pos(), equity=150.0, free=150.0)
    assert bot.mode == "flat"
    assert bot._entry_cooldown_until is not None and bot._entry_cooldown_until > datetime.now(UTC)


async def test_entry_with_no_resulting_position_sets_cooldown():
    bot, ex, pm = _bot()
    pm.get_position = AsyncMock(return_value=_pos())  # still flat after "entry"
    await bot._handle_trend(MarketRegime.TRENDING_UP, MarketRegime.TRENDING_UP,
                            _ind(), 2000.0, _pos(), equity=150.0, free=150.0)
    assert bot.mode == "flat"
    assert bot._entry_cooldown_until is not None


async def test_cooldown_suppresses_new_entry():
    bot, ex, _ = _bot()
    bot._entry_cooldown_until = datetime.now(UTC).replace(year=2099)
    await bot._handle_trend(MarketRegime.TRENDING_UP, MarketRegime.TRENDING_UP,
                            _ind(), 2000.0, _pos(), equity=150.0, free=150.0)
    ex.place_market_linear.assert_not_awaited()


# ── Stops & reversal ───────────────────────────────────────────────────


async def test_live_price_below_stop_closes_long():
    bot, ex, pm = _bot()
    pos = _pos(side="long", size=0.045, entry=2000, mark=1940)
    # chandelier_long 1950, live 1940 -> stop hit -> close (reduce-only).
    await bot._handle_trend(MarketRegime.TRENDING_UP, MarketRegime.TRENDING_UP,
                            _ind(cl=1950.0), live_price=1940.0, position=pos,
                            equity=150.0, free=150.0)
    assert any(c.kwargs.get("reduce_only") for c in ex.place_market_linear.call_args_list)
    assert bot.mode == "flat"


async def test_reversal_closes_position():
    bot, ex, _ = _bot()
    pos = _pos(side="long", size=0.045, entry=2000, mark=2000)
    await bot._handle_trend(MarketRegime.TRENDING_DOWN, MarketRegime.TRENDING_DOWN,
                            _ind(), live_price=2000.0, position=pos, equity=150.0, free=150.0)
    assert ex.place_market_linear.call_args.kwargs.get("reduce_only") is True
    assert bot.mode == "flat"


# ── Range / grid ───────────────────────────────────────────────────────


async def test_range_places_grid():
    bot, ex, pm = _bot()
    bot.s.grid_risk_pct = 0.0  # test placement logic, not the risk cap
    await bot._handle_range(_ind(atr_pct=0.01), price=2000.0, live_price=2000.0,
                            position=_pos(), free=150.0)
    pm.place_grid.assert_awaited()
    assert bot.mode == "grid"


async def test_grid_atr_band_breach_flattens():
    bot, ex, pm = _bot()
    bot.mode = "grid"
    pm.has_open_orders = MagicMock(return_value=True)
    from core.grid import build_grid_plan
    bot.grid_plan = build_grid_plan(
        symbol="ETHUSDT", mid=2000.0, atr_pct=0.01, settings=bot.s, available_usdt=150.0,
        qty_step=Decimal("0.001"), min_qty=Decimal("0.001"), tick_size=Decimal("0.01"),
    )
    breach = bot.grid_plan.stop_loss_lower * 0.99  # below the band
    pm.get_position = AsyncMock(return_value=_pos())
    await bot._handle_range(_ind(), price=2000.0, live_price=breach, position=_pos(), free=150.0)
    pm.cancel_all.assert_awaited()  # _flatten_all ran
    assert bot.mode == "flat"


async def test_transitional_flattens_when_holding_orders():
    bot, ex, pm = _bot()
    bot.mode = "grid"
    pm.has_open_orders = MagicMock(return_value=True)
    pm.get_position = AsyncMock(return_value=_pos())
    await bot._handle_transitional(_pos())
    pm.cancel_all.assert_awaited()
    assert bot.mode == "flat"
