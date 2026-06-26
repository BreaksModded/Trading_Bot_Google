"""Futures bot entrypoint: regime-switching neutral-grid + trend-following.

Single symbol, linear perpetuals. Each cycle classifies the market regime and
dispatches:

  RANGING       -> neutral grid (harvest chop) with an ATR safety stop
  TRENDING_UP   -> ride long  with a Chandelier trailing stop
  TRENDING_DOWN -> ride short with a Chandelier trailing stop
  TRANSITIONAL  -> stand aside (flat)

Trend positions are sized fixed-fractional (risk a fixed % of equity per trade).
An account-level kill-switch flattens and halts on excess daily loss / drawdown,
with a persisted HALTED state so a restart never re-fires the unwind.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from config.settings import Settings
from core.exchange import BybitExchangeClient
from core.futures_risk import FuturesRiskManager
from core.grid import build_grid_plan, validate_grid
from core.indicators import compute_chandelier_exit, enrich_indicators
from core.position_manager import FuturesPositionManager
from core.regime import MarketRegime, classify_futures_regime
from core.trend import (
    TrendStop, compute_fixed_fractional_qty, evaluate_trend_entry, initial_trend_stop,
)
from data.database import Database
from data.models import BotStatus, EventLogRecord, TradeRecord
from services.health_monitor import HealthMonitor
from services.notifier import TelegramNotifier


class FuturesBot:
    """Regime-switching futures bot for a single linear-perpetual symbol."""

    def __init__(
        self, *, settings: Settings, db: Database, exchange: BybitExchangeClient,
        position_manager: FuturesPositionManager, risk_manager: FuturesRiskManager,
        notifier: TelegramNotifier, health_monitor: HealthMonitor, rules: Any,
    ) -> None:
        self.settings = settings
        self.s = settings.futures
        self.db = db
        self.exchange = exchange
        self.pm = position_manager
        self.risk = risk_manager
        self.notifier = notifier
        self.health = health_monitor
        self.rules = rules
        self.symbol = self.s.symbol
        self.quote = Settings.parse_quote_coin(self.symbol)

        self.mode = "flat"            # flat | grid | trend
        self.trend_stop: TrendStop | None = None
        self.grid_plan = None
        self.running = True
        self._shutdown = asyncio.Event()
        self._started_at: datetime | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def startup(self) -> None:
        try:
            await self.exchange.set_leverage(symbol=self.symbol, leverage=self.s.leverage)
        except Exception as exc:
            logger.warning("set_leverage failed (continuing, set it manually): {}", exc)

        # Restore risk state (peak + persisted HALTED flag -> death-loop fix).
        peak = self.db.get_runtime_config("peak_equity")
        halted = self.db.get_runtime_config("halted")
        self.risk.restore(
            peak_equity=float(peak) if isinstance(peak, (int, float)) else None,
            halted=bool(halted),
        )
        self._started_at = datetime.now(UTC)
        self.db.update_bot_state(status=BotStatus.RUNNING, started_at=self._started_at)

        if self.exchange.api_key and self.exchange.api_secret:
            await self.exchange.start_websockets(
                market_callback=self._on_market, private_callback=self._on_private,
                symbols=[self.symbol],
            )
        await self._log("INFO", "bot",
                        f"Futures bot started: {self.symbol} lev={self.s.leverage}x "
                        f"risk/trade={self.s.risk_per_trade_pct:.1%} halted={self.risk.halted}")

    async def run(self) -> None:
        await self.startup()
        interval = self.s.loop_interval_seconds
        it = 0
        while self.running and not self._shutdown.is_set():
            started = datetime.now(UTC)
            it += 1
            try:
                await self._process_commands()
                await self._cycle(it)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                await self._log("ERROR", "bot", f"cycle error: {exc}")
            finally:
                elapsed = (datetime.now(UTC) - started).total_seconds()
                await asyncio.sleep(max(0.0, interval - elapsed))
        await self.shutdown("loop_terminated")

    async def shutdown(self, reason: str = "manual") -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self.running = False
        try:
            await self.exchange.stop_websockets()
        except Exception as exc:
            logger.error("stop_websockets failed: {}", exc)
        self.db.update_bot_state(status=BotStatus.STOPPED, message=reason)
        await self._log("INFO", "bot", f"Futures bot stopped ({reason}).")

    # ── Main cycle ────────────────────────────────────────────────────

    async def _cycle(self, it: int) -> None:
        equity, free, _ = await self.exchange.get_portfolio_equity(
            symbols=[self.symbol], quote_coin=self.quote,
        )
        now = datetime.now(UTC)
        decision = self.risk.evaluate(equity=equity, now=now)
        self._persist_risk(equity)

        if decision.flatten_and_halt:
            pos = await self.pm.get_position()
            if self.mode != "flat" or not pos.is_flat or self.pm.has_open_orders():
                await self._flatten_all("kill_switch")
            self.db.update_bot_state(status=BotStatus.PAUSED, message=decision.reason)
            logger.warning("[it {}] HALTED — {} (manual resume required)", it, decision.reason)
            return

        kl = await self.exchange.get_klines(symbol=self.symbol, interval=self.s.timeframe, limit=300)
        if kl.empty or len(kl) < self.s.ema_slow:
            logger.warning("[it {}] insufficient klines ({} rows)", it, len(kl))
            return
        kl_htf = await self.exchange.get_klines(
            symbol=self.symbol, interval=self.s.higher_timeframe, limit=300,
        )

        ind = self._indicators(kl)
        regime = classify_futures_regime(
            ind["adx"], ind["ema_fast"], ind["ema_slow"],
            adx_trend=self.s.adx_trend_threshold, adx_range=self.s.adx_range_threshold,
        )
        regime_htf = self._htf_regime(kl_htf)
        price = ind["close"]
        position = await self.pm.get_position()

        logger.info(
            "[it {}] {} px={:.2f} ADX={:.1f} regime={} htf={} mode={} pos={} eq={:.2f}",
            it, self.symbol, price, ind["adx"], regime.value, regime_htf.value,
            self.mode, position.side, equity,
        )

        if regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            await self._handle_trend(regime, regime_htf, ind, price, position, equity, free)
        elif regime == MarketRegime.RANGING:
            await self._handle_range(ind, price, position, free)
        else:
            await self._handle_transitional(position)

        self._persist_state(equity, free, regime, ind, position)
        await self._heartbeat()
        await self.health.ping_if_due()

    # ── Regime handlers ───────────────────────────────────────────────

    async def _handle_trend(self, regime, regime_htf, ind, price, position, equity, free) -> None:
        desired_side = "Buy" if regime == MarketRegime.TRENDING_UP else "Sell"
        desired_pos = "long" if desired_side == "Buy" else "short"

        if position.is_flat:
            if self.pm.has_open_orders():  # clear any grid first
                await self.pm.cancel_all()
                self.pm.reset()
            entry = evaluate_trend_entry(
                regime=regime, higher_tf_regime=regime_htf,
                require_htf=self.s.require_higher_tf_confirmation,
            )
            if entry.side is None:
                self.mode = "flat"
                return
            stop = initial_trend_stop(
                entry.side, chandelier_long=ind["chandelier_long"],
                chandelier_short=ind["chandelier_short"],
            )
            qty = compute_fixed_fractional_qty(
                equity=equity, risk_pct=self.s.risk_per_trade_pct, entry_price=price,
                stop_price=stop.stop_price, qty_step=self.rules.qty_step,
                min_qty=self.rules.min_qty, available_margin=free, leverage=self.s.leverage,
            )
            if qty <= 0:
                logger.info("trend entry skipped — qty below viable minimum")
                self.mode = "flat"
                return
            try:
                await self.exchange.place_market_linear(
                    symbol=self.symbol, side=entry.side, qty=qty, reduce_only=False,
                )
                self.trend_stop = stop
                self.mode = "trend"
                await self._log("INFO", "trend",
                                f"ENTER {entry.side} {qty} @ ~{price:.2f} "
                                f"stop={stop.stop_price:.2f} risk={self.s.risk_per_trade_pct:.1%}")
            except Exception as exc:
                await self._log("ERROR", "trend", f"entry failed: {exc}")
            return

        # We hold a position.
        if position.side != desired_pos:
            await self._close_position(position, "trend_reversal")
            return
        if self.trend_stop is None or self.trend_stop.side != desired_side:
            self.trend_stop = initial_trend_stop(
                desired_side, chandelier_long=ind["chandelier_long"],
                chandelier_short=ind["chandelier_short"],
            )
        self.trend_stop.update(
            chandelier_long=ind["chandelier_long"], chandelier_short=ind["chandelier_short"],
        )
        self.mode = "trend"
        if self.trend_stop.is_hit(price):
            await self._close_position(position, "chandelier_stop")

    async def _handle_range(self, ind, price, position, free) -> None:
        if not position.is_flat:  # close a leftover trend position before gridding
            await self._close_position(position, "range_entry")
            return
        if self.mode != "grid" or not self.pm.has_open_orders():
            plan = build_grid_plan(
                symbol=self.symbol, mid=price, atr_pct=ind["atr_pct"], settings=self.s,
                available_usdt=free, qty_step=self.rules.qty_step,
                min_qty=self.rules.min_qty, tick_size=self.rules.tick_size,
            )
            ok, reason = validate_grid(plan, min_order_usdt=self.s.min_order_usdt)
            if not ok:
                logger.info("grid not viable: {}", reason)
                self.mode = "flat"
                return
            await self.pm.cancel_all()
            placed = await self.pm.place_grid(plan)
            self.grid_plan = plan
            self.mode = "grid" if placed else "flat"
            if placed:
                await self._log("INFO", "grid",
                                f"grid placed: {placed} orders, spacing {plan.spacing_pct:.2%}")
        else:
            # ATR safety stop: bail out of the grid if price leaves the SL band.
            if self.grid_plan and (price < self.grid_plan.stop_loss_lower or price > self.grid_plan.stop_loss_upper):
                await self._flatten_all("grid_atr_stop")
            else:
                await self.pm.sync_orders()

    async def _handle_transitional(self, position) -> None:
        if self.mode != "flat" or not position.is_flat or self.pm.has_open_orders():
            await self._flatten_all("transitional")

    # ── Position / order helpers ──────────────────────────────────────

    async def _flatten_all(self, reason: str) -> None:
        try:
            await self.pm.cancel_all()
            self.pm.reset()
        except Exception as exc:
            logger.error("cancel grid failed: {}", exc)
        pos = await self.pm.get_position()
        if not pos.is_flat:
            await self._close_position(pos, reason)
        self.mode = "flat"
        self.trend_stop = None
        self.grid_plan = None

    async def _close_position(self, position, reason: str) -> None:
        close_side = "Sell" if position.side == "long" else "Buy"
        try:
            await self.exchange.place_market_linear(
                symbol=self.symbol, side=close_side, qty=position.size, reduce_only=True,
            )
            await self._log("INFO", "trend",
                            f"CLOSE {position.side} {position.size} ({reason}) "
                            f"uPnL={position.unrealized_pnl:.2f}")
            # Record the close so the loss/gain is NEVER off-book (audit H1).
            # Realized PnL ~ uPnL at market close; the accounting phase refines it
            # via the closed-pnl endpoint.
            await asyncio.to_thread(self.db.insert_trade, TradeRecord(
                timestamp=datetime.now(UTC), side=close_side, price=position.mark_price,
                qty=position.size, fee=0.0, pnl=position.unrealized_pnl, status="filled",
                symbol=self.symbol, order_type="Market", exchange_order_id=None,
                metadata={"reason": reason},
            ))
        except Exception as exc:
            await self._log("ERROR", "trend", f"close failed: {exc}")
        self.mode = "flat"
        self.trend_stop = None

    # ── Indicators ────────────────────────────────────────────────────

    def _indicators(self, kl) -> dict[str, float]:
        clean = kl.iloc[:-1] if len(kl) > 1 else kl  # drop the forming candle
        enriched = enrich_indicators(
            clean, ema_fast=self.s.ema_fast, ema_slow=self.s.ema_slow,
            atr_period=self.s.atr_period, adx_period=self.s.adx_period,
        )
        ce = compute_chandelier_exit(
            clean, period=self.s.chandelier_period, atr_mult=self.s.chandelier_atr_mult,
            atr_period=self.s.atr_period,
        )
        last = enriched.iloc[-1]
        return {
            "close": float(last["close"]),
            "atr": float(last["atr"]),
            "atr_pct": float(last["atr_pct"]),
            "adx": float(last["adx"]),
            "ema_fast": float(last["ema_fast"]),
            "ema_slow": float(last["ema_slow"]),
            "chandelier_long": float(ce["chandelier_long"].iloc[-1]),
            "chandelier_short": float(ce["chandelier_short"].iloc[-1]),
        }

    def _htf_regime(self, kl_htf) -> MarketRegime:
        if kl_htf is None or kl_htf.empty or len(kl_htf) < self.s.ema_slow:
            return MarketRegime.TRANSITIONAL
        ind = self._indicators(kl_htf)
        return classify_futures_regime(
            ind["adx"], ind["ema_fast"], ind["ema_slow"],
            adx_trend=self.s.adx_trend_threshold, adx_range=self.s.adx_range_threshold,
        )

    # ── Persistence / commands ────────────────────────────────────────

    def _persist_risk(self, equity: float) -> None:
        try:
            self.db.set_runtime_config("futures_risk_status", self.risk.status())
            if self.risk.peak_equity:
                self.db.set_runtime_config("peak_equity", self.risk.peak_equity)
            self.db.set_runtime_config("halted", self.risk.halted)
        except Exception as exc:
            logger.debug("persist risk failed: {}", exc)

    def _persist_state(self, equity, free, regime, ind=None, position=None) -> None:
        try:
            self.db.record_equity(capital=equity, drawdown_pct=self.risk._last_drawdown)
            blob = {
                "mode": self.mode, "regime": regime.value, "symbol": self.symbol,
                "leverage": self.s.leverage, "equity": equity, "free": free,
                "peak_equity": self.risk.peak_equity,
                "trend_stop": self.trend_stop.stop_price if self.trend_stop else None,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            if ind is not None:
                blob["indicators"] = {
                    "price": ind["close"], "adx": ind["adx"],
                    "ema_fast": ind["ema_fast"], "ema_slow": ind["ema_slow"],
                    "atr_pct": ind["atr_pct"],
                    "chandelier_long": ind["chandelier_long"],
                    "chandelier_short": ind["chandelier_short"],
                }
            if position is not None:
                blob["position"] = {
                    "side": position.side, "size": position.size,
                    "entry": position.entry_price, "mark": position.mark_price,
                    "liq": position.liq_price, "uPnL": position.unrealized_pnl,
                    "leverage": position.leverage, "margin": position.margin,
                }
            self.db.set_runtime_config("futures_state", blob)
        except Exception as exc:
            logger.debug("persist state failed: {}", exc)

    async def _process_commands(self) -> None:
        try:
            commands = self.db.fetch_pending_commands(limit=10)
        except Exception:
            return
        for cmd in commands:
            cid = int(cmd["id"])
            action = str(cmd["command"]).lower()
            try:
                if action == "stop":
                    self.running = False
                elif action == "resume":
                    equity, _, _ = await self.exchange.get_portfolio_equity(
                        symbols=[self.symbol], quote_coin=self.quote,
                    )
                    self.risk.resume(equity)
                    await self._log("INFO", "risk", f"manual resume — peak rebased to {equity:.2f}")
                elif action in ("flatten", "emergency"):
                    await self._flatten_all("manual_flatten")
                self.db.mark_command_processed(cid, status="processed")
            except Exception as exc:
                self.db.mark_command_processed(cid, status="failed")
                logger.error("command {} failed: {}", action, exc)

    async def _heartbeat(self) -> None:
        try:
            path = self.settings.heartbeat_full_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        except Exception as exc:
            logger.debug("heartbeat failed: {}", exc)

    # ── WS handlers ───────────────────────────────────────────────────

    async def _on_market(self, message: dict[str, Any]) -> None:
        return  # timer-driven; market WS not required for signals

    async def _on_private(self, message: dict[str, Any]) -> None:
        topic = str(message.get("topic", ""))
        if not topic.startswith("order"):
            return
        data = message.get("data", [])
        for ev in (data if isinstance(data, list) else [data]):
            if str(ev.get("orderStatus")) not in ("Filled", "PartiallyFilled"):
                continue
            if self.mode == "grid":  # grid flips: place the partner on each fill
                trade = await self.pm.handle_fill(ev)
                if trade:
                    await asyncio.to_thread(self.db.insert_trade, trade)

    async def _log(self, level: str, module: str, message: str) -> None:
        try:
            await asyncio.to_thread(self.db.log_event, EventLogRecord(
                timestamp=datetime.now(UTC), level=level, module=module,
                message=message, payload={},
            ))
        except Exception:
            pass
        logger.log(level if level in ("INFO", "WARNING", "ERROR", "CRITICAL") else "INFO", message)
        try:
            if level in ("ERROR", "CRITICAL", "WARNING"):
                await self.notifier.send_alert(f"[{level}] {message}")
        except Exception:
            pass


# ── Entrypoint ────────────────────────────────────────────────────────


async def run_bot() -> None:
    settings = Settings()
    logger.remove()
    logger.add(str(settings.log_full_path), rotation="10 MB", retention="14 days",
               level=settings.log_level, enqueue=True)
    logger.add(lambda m: print(m, end=""), level=settings.log_level)

    db = Database(settings.db_full_path)
    db.init_schema()

    symbol = settings.futures.symbol
    exchange = BybitExchangeClient(
        api_key=settings.exchange.api_key, api_secret=settings.exchange.api_secret,
        testnet=settings.exchange.testnet, symbol=symbol, category="linear",
        timeframe=settings.futures.timeframe, domain=settings.exchange.domain,
        tld=settings.exchange.tld,
    )
    logger.info("BOOT: {} linear perp, testnet={}", symbol, settings.exchange.testnet)

    rules = await exchange.get_symbol_rules(symbol)
    pm = FuturesPositionManager(
        exchange=exchange, symbol=symbol, tick_size=rules.tick_size,
        qty_step=rules.qty_step, min_qty=rules.min_qty,
    )
    risk = FuturesRiskManager(
        max_daily_loss_pct=settings.futures.max_daily_loss_pct,
        max_total_drawdown_pct=settings.futures.max_total_drawdown_pct,
    )
    notifier = TelegramNotifier(
        enabled=settings.telegram.enabled, token=settings.telegram.bot_token,
        chat_id=settings.telegram.chat_id,
    )
    health = HealthMonitor(
        url=settings.healthcheck.ping_url, interval_seconds=settings.healthcheck.interval_seconds,
    )

    bot = FuturesBot(
        settings=settings, db=db, exchange=exchange, position_manager=pm,
        risk_manager=risk, notifier=notifier, health_monitor=health, rules=rules,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    async def _request_shutdown(reason: str) -> None:
        await bot.shutdown(reason=reason)
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(
                _request_shutdown(reason=f"signal:{s}")))
        except NotImplementedError:
            signal.signal(sig, lambda s, f: asyncio.create_task(
                _request_shutdown(reason=f"signal:{s}")))

    bot_task = asyncio.create_task(bot.run(), name="futures-bot")
    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-wait")
    await asyncio.wait([bot_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
    if not bot_task.done():
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)
    db.close()


if __name__ == "__main__":
    asyncio.run(run_bot())
