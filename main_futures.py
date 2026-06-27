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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from loguru import logger

from config.settings import Settings
from core.exchange import BybitExchangeClient
from core.futures_risk import FuturesRiskManager
from core.grid import build_grid_plan, validate_grid
from core.indicators import compute_chandelier_exit, enrich_indicators
from core.position_manager import FuturesPositionManager
from core.regime import MarketRegime, classify_futures_regime
from core.trend import TrendStop, decide_trend
from data.database import Database
from data.models import BotStatus, CircuitBreakerEvent, EventLogRecord, TradeRecord
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
        self._entry_cooldown_until: datetime | None = None
        self._entry_cooldown_s = 120
        self._exchange_sl: float | None = None
        self._funding_rate: float = 0.0
        self._last_equity_ts: datetime | None = None
        self._last_cleanup_date: date | None = None
        # Dashboard read-layer (Fase 0): rolling regime timeline + the last regime,
        # stamped onto close trades. Neither feeds back into any trading decision.
        self._regime_history: list[str] = []
        self._last_regime: str = "transitional"

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def startup(self) -> None:
        try:
            await self.exchange.set_leverage(symbol=self.symbol, leverage=self.s.leverage)
        except Exception as exc:
            logger.warning("set_leverage failed (continuing, set it manually): {}", exc)

        # Restore risk state. Uses a FUTURES-specific peak key (never inherits the
        # deleted spot bot's peak_equity) + day baselines + HALTED (death-loop fix).
        peak = self.db.get_runtime_config("futures_peak_equity")
        halted = self.db.get_runtime_config("halted")
        day_state = self.db.get_runtime_config("futures_day_state") or {}
        cd: date | None = None
        if day_state.get("current_day"):
            try:
                cd = date.fromisoformat(day_state["current_day"])
            except (ValueError, TypeError):
                cd = None
        self.risk.restore(
            peak_equity=float(peak) if isinstance(peak, (int, float)) else None,
            halted=bool(halted),
            day_start_equity=float(day_state["day_start_equity"]) if day_state.get("day_start_equity") else None,
            current_day=cd,
        )

        # Recover an in-flight trend position's trailing stop (preserve the ratchet).
        try:
            pos0 = await self.pm.get_position()
            ts0 = self.db.get_runtime_config("futures_trend_stop")
            if not pos0.is_flat and isinstance(ts0, (int, float)) and ts0 > 0:
                self.trend_stop = TrendStop(
                    side="Buy" if pos0.side == "long" else "Sell", stop_price=float(ts0),
                )
                self.mode = "trend"
                logger.info("Recovered trend {} position, stop={:.4f}", pos0.side, ts0)
        except Exception as exc:
            logger.warning("trend recovery failed: {}", exc)

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
        self._maybe_cleanup(now.date())

        if decision.flatten_and_halt:
            pos = await self.pm.get_position()
            if self.mode != "flat" or not pos.is_flat or self.pm.has_open_orders():
                await self._flatten_all("kill_switch")
                pos = await self.pm.get_position()  # re-read post-flatten → reflects flat
            self._maybe_log_kill_switch(decision, equity)
            self.db.update_bot_state(status=BotStatus.PAUSED, message=decision.reason)
            self._persist_halted_state(equity, free, pos)
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
        price = ind["close"]                      # last CLOSED candle (stable signals)
        live_price = await self._live_price(price)  # live tick (responsive stops)
        try:
            self._funding_rate = await self.exchange.get_funding_rate(symbol=self.symbol)
        except Exception:
            pass
        position = await self.pm.get_position()

        logger.info(
            "[it {}] {} px={:.2f} ADX={:.1f} regime={} htf={} mode={} pos={} eq={:.2f}",
            it, self.symbol, price, ind["adx"], regime.value, regime_htf.value,
            self.mode, position.side, equity,
        )

        self._last_regime = regime.value
        if regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            await self._handle_trend(regime, regime_htf, ind, live_price, position, equity, free)
        elif regime == MarketRegime.RANGING:
            await self._handle_range(ind, price, live_price, position, free)
        else:
            await self._handle_transitional(position)

        self._persist_state(equity, free, regime, regime_htf, ind, position)
        await self._heartbeat()
        await self.health.ping_if_due()

    # ── Regime handlers ───────────────────────────────────────────────

    async def _handle_trend(self, regime, regime_htf, ind, live_price, position, equity, free) -> None:
        if position.is_flat:
            now = datetime.now(UTC)
            if self._entry_cooldown_until and now < self._entry_cooldown_until:
                return
            if self.pm.has_open_orders():  # clear any grid first
                await self.pm.cancel_all()
                self.pm.reset()

        prev_stop = self.trend_stop.stop_price if self.trend_stop else None
        decision = decide_trend(
            regime=regime, regime_htf=regime_htf, position_side=position.side,
            position_flat=position.is_flat, chandelier_long=ind["chandelier_long"],
            chandelier_short=ind["chandelier_short"], live_price=live_price,
            equity=equity, available_margin=free, trend_stop=self.trend_stop,
            risk_pct=self.s.risk_per_trade_pct, leverage=self.s.leverage,
            require_htf=self.s.require_higher_tf_confirmation,
            qty_step=self.rules.qty_step, min_qty=self.rules.min_qty,
        )

        if decision.action == "enter":
            now = datetime.now(UTC)
            try:
                await self.exchange.place_market_linear(
                    symbol=self.symbol, side=decision.side, qty=decision.qty, reduce_only=False,
                )
            except Exception as exc:
                self._entry_cooldown_until = now + timedelta(seconds=self._entry_cooldown_s)
                await self._log("ERROR", "trend",
                                f"entry failed (cooldown {self._entry_cooldown_s}s): {exc}")
                self.mode = "flat"
                return
            opened = await self.pm.get_position()  # confirm it actually opened
            if opened.is_flat:
                self._entry_cooldown_until = now + timedelta(seconds=self._entry_cooldown_s)
                await self._log("WARNING", "trend",
                                "entry order placed but no position detected — cooldown")
                self.mode = "flat"
                return
            self.trend_stop = decision.stop
            self.mode = "trend"
            await self._set_exchange_sl(decision.stop.stop_price)  # exchange-side backstop
            await self._log("INFO", "trend",
                            f"ENTER {decision.side} {opened.size} @ ~{live_price:.2f} "
                            f"stop={decision.stop.stop_price:.2f} risk={self.s.risk_per_trade_pct:.1%}")
            # Record the entry so the dashboard shows the open (read/record layer only,
            # reached only after `opened` is confirmed non-flat above). pnl=0 → excluded
            # from win-rate/PF/avg/total (they count only ABS(pnl)>1e-12); the close is
            # recorded separately in _close_position, so there is no double count.
            await asyncio.to_thread(self.db.insert_trade, TradeRecord(
                timestamp=datetime.now(UTC), side=decision.side,
                price=opened.entry_price or live_price, qty=opened.size,
                fee=0.0, pnl=0.0, status="filled", symbol=self.symbol,
                order_type="Market", exchange_order_id=None,
                metadata={"reason": "trend_entry", "regime": self._last_regime},
            ))
        elif decision.action == "close":
            await self._close_position(position, decision.reason)
        elif decision.action == "hold":
            self.trend_stop = decision.stop
            self.mode = "trend"
            # Ratchet the exchange-side stop only when it moves materially (>0.2%).
            if prev_stop is not None and abs(decision.stop.stop_price - prev_stop) / max(prev_stop, 1e-9) > 0.002:
                await self._set_exchange_sl(decision.stop.stop_price)
        else:  # "none"
            self.mode = "flat"

    async def _handle_range(self, ind, price, live_price, position, free) -> None:
        if not position.is_flat:  # close a leftover trend position before gridding
            await self._close_position(position, "range_entry")
            return
        if self.mode != "grid" or not self.pm.has_open_orders():
            plan = build_grid_plan(
                symbol=self.symbol, mid=price, atr_pct=ind["atr_pct"], settings=self.s,
                available_usdt=free, qty_step=self.rules.qty_step,
                min_qty=self.rules.min_qty, tick_size=self.rules.tick_size,
            )
            ok, reason = validate_grid(
                plan, min_order_usdt=self.s.min_order_usdt,
                capital=free, grid_risk_pct=self.s.grid_risk_pct,
            )
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
            # ATR safety stop on the LIVE price: bail if price leaves the SL band.
            if self.grid_plan and (
                live_price < self.grid_plan.stop_loss_lower
                or live_price > self.grid_plan.stop_loss_upper
            ):
                await self._flatten_all("grid_atr_stop")
            else:
                await self.pm.sync_orders()

    async def _handle_transitional(self, position) -> None:
        if self.mode != "flat" or not position.is_flat or self.pm.has_open_orders():
            await self._flatten_all("transitional")

    async def _live_price(self, fallback: float) -> float:
        """Latest traded price for responsive stop checks; falls back to the
        last closed-candle close on error."""
        try:
            return await self.exchange.get_last_price(symbol=self.symbol)
        except Exception:
            return fallback

    async def _set_exchange_sl(self, stop_price: float) -> None:
        """Attach/update an exchange-side stop-loss as a crash/disconnect backstop."""
        try:
            await self.exchange.set_position_stop_loss(symbol=self.symbol, stop_price=stop_price)
            self._exchange_sl = stop_price
        except Exception as exc:
            logger.warning("set exchange SL failed: {}", exc)

    def _maybe_log_kill_switch(self, decision, equity: float) -> None:
        """Persist a CircuitBreakerEvent the first cycle a kill-switch fires.

        After the trigger cycle the risk manager only returns
        'halted_awaiting_manual_resume' (no ':'), so the prefix check logs exactly
        once per halt episode. Read-layer/persistence only — it never alters the
        trading decision, which has already been taken by the risk manager.
        """
        breaker_type, sep, raw = decision.reason.partition(":")
        if not sep or breaker_type not in ("max_daily_loss", "max_total_drawdown"):
            return
        try:
            trigger_value = float(raw)
        except (TypeError, ValueError):
            trigger_value = 0.0
        threshold = (self.s.max_daily_loss_pct if breaker_type == "max_daily_loss"
                     else self.s.max_total_drawdown_pct)
        try:
            self.db.log_circuit_breaker(CircuitBreakerEvent(
                timestamp=datetime.now(UTC), breaker_type=breaker_type,
                trigger_value=round(trigger_value, 6), threshold=threshold,
                action_taken="flatten+halt",
                details=f"equity={equity:.2f} symbol={self.symbol}",
            ))
        except Exception as exc:
            logger.debug("log_circuit_breaker failed: {}", exc)

    def _maybe_cleanup(self, today: date) -> None:
        """Prune old event logs once a day so the DB does not grow unbounded."""
        if self._last_cleanup_date == today:
            return
        self._last_cleanup_date = today
        try:
            # 14d retention (matches the file-log retention): the loguru→DB sink in
            # run_bot() mirrors the per-cycle stream, so keep this table bounded.
            self.db.cleanup_old_events(days=14)
        except Exception as exc:
            logger.debug("cleanup failed: {}", exc)

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
        self._exchange_sl = None

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
                metadata={"reason": reason, "regime": self._last_regime},
            ))
        except Exception as exc:
            await self._log("ERROR", "trend", f"close failed: {exc}")
        self.mode = "flat"
        self.trend_stop = None
        self._exchange_sl = None

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
                self.db.set_runtime_config("futures_peak_equity", self.risk.peak_equity)
            self.db.set_runtime_config("halted", self.risk.halted)
            if self.risk.day_start_equity and self.risk.current_day:
                self.db.set_runtime_config("futures_day_state", {
                    "day_start_equity": self.risk.day_start_equity,
                    "current_day": self.risk.current_day.isoformat(),
                })
            self.db.set_runtime_config(
                "futures_trend_stop", self.trend_stop.stop_price if self.trend_stop else None,
            )
        except Exception as exc:
            logger.debug("persist risk failed: {}", exc)

    def _persist_state(self, equity, free, regime, regime_htf=None, ind=None, position=None) -> None:
        try:
            now = datetime.now(UTC)
            # Throttle equity-curve writes to ~1/min (loop runs every ~10s).
            if self._last_equity_ts is None or (now - self._last_equity_ts).total_seconds() >= 60:
                self.db.record_equity(capital=equity, drawdown_pct=self.risk._last_drawdown * 100)  # canonical: percentage (see record_equity)
                self._last_equity_ts = now
            # Rolling regime timeline for the dashboard (last 48 cycles).
            self._regime_history.append(regime.value)
            if len(self._regime_history) > 48:
                self._regime_history = self._regime_history[-48:]
            blob = {
                "mode": self.mode, "regime": regime.value,
                "regime_htf": regime_htf.value if regime_htf is not None else None,
                "regime_history": list(self._regime_history),
                "symbol": self.symbol,
                "leverage": self.s.leverage, "equity": equity, "free": free,
                "peak_equity": self.risk.peak_equity, "funding_rate": self._funding_rate,
                "trend_stop": self.trend_stop.stop_price if self.trend_stop else None,
                "updated_at": now.isoformat(),
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

    def _persist_halted_state(self, equity, free, position) -> None:
        """Keep futures_state fresh while HALTED.

        The cycle returns before _persist_state when halted, which would freeze the
        whole blob (stale updated_at -> "sin datos"; stale position -> phantom). Instead
        of overwriting with a minimal blob (which would blank indicators/regime/timeline),
        READ the last persisted blob and OVERLAY only what must stay coherent: a fresh
        updated_at, the real (now flat) position, and trend_stop=None. indicators / regime
        / regime_history / mode are left intact (last known). Equity-curve point is still
        recorded (throttled) so there is no gap. Read/persist layer only — the halt
        decision was already taken by the risk manager; nothing here changes it.
        """
        try:
            now = datetime.now(UTC)
            if self._last_equity_ts is None or (now - self._last_equity_ts).total_seconds() >= 60:
                self.db.record_equity(capital=equity, drawdown_pct=self.risk._last_drawdown * 100)  # canonical: percentage (see record_equity)
                self._last_equity_ts = now
            blob = self.db.get_runtime_config("futures_state")
            if not isinstance(blob, dict) or not blob:
                # Booted already halted (no prior state): write a minimal coherent blob.
                blob = {
                    "mode": self.mode, "regime": self._last_regime, "regime_htf": None,
                    "regime_history": list(self._regime_history), "symbol": self.symbol,
                    "leverage": self.s.leverage, "equity": equity, "free": free,
                    "peak_equity": self.risk.peak_equity, "funding_rate": self._funding_rate,
                }
            blob["updated_at"] = now.isoformat()
            blob["equity"] = equity   # refresh: equity can drift (funding/fees) even flat
            blob["free"] = free
            blob["trend_stop"] = None
            blob["position"] = {
                "side": position.side, "size": position.size,
                "entry": position.entry_price, "mark": position.mark_price,
                "liq": position.liq_price, "uPnL": position.unrealized_pnl,
                "leverage": position.leverage, "margin": position.margin,
            }
            self.db.set_runtime_config("futures_state", blob)
        except Exception as exc:
            logger.debug("persist halted state failed: {}", exc)

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
                    # Stamp the regime the grid fill happened under (read-only label).
                    trade.metadata = {**(trade.metadata or {}), "regime": self._last_regime}
                    await asyncio.to_thread(self.db.insert_trade, trade)

    async def _log(self, level: str, module: str, message: str) -> None:
        try:
            await asyncio.to_thread(self.db.log_event, EventLogRecord(
                timestamp=datetime.now(UTC), level=level, module=module,
                message=message, payload={},
            ))
        except Exception:
            pass
        # _skip_db: already persisted above with a clean module tag; tell the DB
        # log-sink (run_bot) not to write a duplicate row for this same event.
        logger.bind(_skip_db=True).log(
            level if level in ("INFO", "WARNING", "ERROR", "CRITICAL") else "INFO", message,
        )
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

    # Fase 0 (§8.6): mirror the live loguru stream into event_logs so the dashboard
    # Logs screen shows real per-cycle activity instead of only the few explicit
    # _log() events. Read-layer only. Records from _log() carry _skip_db=True (already
    # persisted with a module tag), and DB-writer records are skipped, so there is no
    # duplication and no feedback loop. _maybe_cleanup bounds the table to 14 days.
    def _db_log_sink(message) -> None:
        rec = message.record
        if rec["extra"].get("_skip_db"):
            return
        name = rec["name"] or "bot"
        if "database" in name:
            return
        mod = name.split(".")[-1]
        if mod in ("main_futures", "__main__"):
            mod = "bot"
        try:
            db.log_event(EventLogRecord(
                timestamp=rec["time"], level=rec["level"].name,
                module=mod, message=rec["message"], payload={},
            ))
        except Exception:
            pass

    logger.add(_db_log_sink, level="INFO", enqueue=False)

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
