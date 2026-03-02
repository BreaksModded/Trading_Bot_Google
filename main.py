"""Trading bot process entrypoint with graceful shutdown support."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from config.settings import Settings
from core.exchange import BybitExchangeClient, SpotSymbolRules
from core.grid_refresh import RefreshConfig, RefreshState, is_grid_stale, should_refresh
from core.order_manager import OrderManager
from core.risk_manager import RiskDecision, RiskManager
from core.strategy import GridStrategy, StrategyConfig, StrategySignal
from data.database import Database
from data.models import BotStatus, ConfigSnapshot, EventLogRecord, OrderSide, PerformanceMetrics
from services.health_monitor import HealthMonitor
from services.notifier import TelegramNotifier


@dataclass(slots=True)
class BotRuntimeState:
    """Mutable state for lifecycle control."""

    running: bool = True
    manual_paused: bool = False
    started_at: datetime | None = None
    last_price: float = 0.0


class TradingBot:
    """Coordinates exchange, strategies, risk, persistence, and notifications."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        exchange: BybitExchangeClient,
        strategies: dict[str, GridStrategy],
        risk_manager: RiskManager,
        order_managers: dict[str, OrderManager],
        notifier: TelegramNotifier,
        health_monitor: HealthMonitor,
    ) -> None:
        self.settings = settings
        self.db = db
        self.exchange = exchange
        self.strategies = {s.upper(): strat for s, strat in strategies.items()}
        self.risk_manager = risk_manager
        self.order_managers = {s.upper(): mgr for s, mgr in order_managers.items()}
        self.notifier = notifier
        self.health_monitor = health_monitor
        self.state = BotRuntimeState()
        self._last_cleanup_date: date | None = None

        self.symbols = list(self.strategies.keys())
        quote_coins = {settings.parse_quote_coin(s) for s in self.symbols}
        if len(quote_coins) != 1:
            raise ValueError("Multi-pair mode requires same quote coin for all symbols.")
        self.quote_coin = next(iter(quote_coins))

        self.exchange.set_failure_callback(self._handle_ws_failure)
        self._shutdown_event = asyncio.Event()
        self._signal_eval_event = asyncio.Event()  # WKS-1: Triggered by WS candle close
        self._placement_cooldown_seconds: int = 120
        self._price_limit_cooldown_seconds: int = 900
        self._max_signal_price_deviation_pct: float = 0.25

        # SG5: Grid Refresh configuration
        self.refresh_cfg = RefreshConfig(
            price_distance_pct=settings.grid.grid_refresh_price_distance_pct,
            atr_multiplier=settings.grid.grid_refresh_atr_multiplier,
            max_grid_age_hours=settings.grid.grid_refresh_max_age_hours,
            cooldown_minutes=settings.grid.grid_refresh_cooldown_minutes,
            stale_confirm_cycles=settings.grid.grid_refresh_stale_confirm_cycles,
            max_refreshes_per_day=settings.grid.grid_refresh_max_per_day,
        )
        self._refresh_states: dict[str, RefreshState] = {s: RefreshState() for s in self.symbols}
        self._placement_cooldown_until: dict[str, datetime] = {}
    # ── Lifecycle ─────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Initialize runtime state and recover orphan exchange data."""
        from core.reinvestment import ReinvestmentEngine
        
        # Load account info
        free_usdt = await self.exchange.get_balance(coin=self.quote_coin)
        
        self.reinvestment_engine = ReinvestmentEngine(
            initial_baseline=free_usdt,
            config=self.settings.grid
        )
        self.state.running = True
        self.state.started_at = datetime.now(UTC)
        self.db.update_bot_state(
            status=BotStatus.RUNNING, started_at=self.state.started_at,
        )
        self.db.save_config_snapshot(
            ConfigSnapshot(
                timestamp=datetime.now(UTC),
                values=self.settings.public_dict(),
            )
        )

        await self._log_and_notify("INFO", "bot", "Bot startup completed.")
        await self._log_and_notify(
            "INFO", "bot",
            f"Trading symbols={self.symbols}, quote_coin={self.quote_coin}, "
            f"max_active_pairs={self.settings.grid.max_active_pairs}",
        )

        # Restore spacing from persisted grid states before crash recovery
        latest_grid_states = {
            row.get("symbol", "").upper(): row
            for row in self.db.get_latest_grid_states()
        }
        for symbol, manager in self.order_managers.items():
            latest = latest_grid_states.get(symbol)
            if latest:
                manager.set_spacing(
                    float(latest.get("buy_spacing_pct", latest.get("spacing_pct", 0.0))),
                    float(latest.get("sell_spacing_pct", 0.0))
                )
                
                # D4 FIX: Restore grid anchors so they aren't overwritten by the first loop save
                if latest.get("grid_anchor_price"):
                    manager.grid_anchor_price = float(latest["grid_anchor_price"])
                if latest.get("grid_created_at"):
                    dt_val = latest["grid_created_at"]
                    if isinstance(dt_val, str):
                        manager.grid_created_at = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
                    else:
                        manager.grid_created_at = dt_val
                        
                # D8 FIX: Restore pending inverse retries to prevent unhedged grids
                if latest.get("pending_retries"):
                    pending = latest["pending_retries"]
                    if isinstance(pending, list):
                        manager._pending_inverse_retries.extend(pending)
                        logger.info("Restored {} pending inverse retries for {}", len(pending), symbol)

        # Crash recovery
        recovered_total = 0
        for symbol, manager in self.order_managers.items():
            recovered = await manager.recover_from_crash()
            recovered_total += len(recovered)
            
            # SG6: Crash recovery inventory safety check
            self._refresh_states[symbol].refresh_in_progress = False
            if manager.has_unhedged_inventory() and not manager.open_orders:
                await self._log_and_notify(
                    "CRITICAL", "bot",
                    f"{symbol} recovered with unhedged inventory but NO entry orders. Triggering immediate grid placement."
                )
                self._placement_cooldown_until[symbol] = datetime.now(UTC) - timedelta(days=1)
                
        if recovered_total:
            await self._log_and_notify(
                "WARNING", "bot",
                f"recover_from_crash detected {recovered_total} orphan open orders.",
            )

        # SG7: Crash recovery for daily loss circuit breaker
        today = datetime.now(UTC).date()
        day_start_equity = self.db.get_day_start_equity(today)
        if day_start_equity is not None:
            self.risk_manager.day_start_equity = day_start_equity
            self.risk_manager.current_day = today
            logger.info("Restored day_start_equity={:.2f} for {}", day_start_equity, today)

        # Start WebSockets
        if self.settings.exchange.api_key and self.settings.exchange.api_secret:
            await self.exchange.start_websockets(
                market_callback=self._on_market_message,
                private_callback=self._on_private_message,
                symbols=self.symbols,
            )

        # M19: Start Telegram command bot in background
        self._command_bot_task: asyncio.Task | None = None
        if self.notifier.is_enabled:
            self._command_bot_task = asyncio.create_task(
                self.notifier.run_command_bot(self.db),
                name="telegram-command-bot",
            )
            logger.info("Telegram command bot task created")

        # M20: Schedule daily summary at 23:59 UTC
        try:
            from services.scheduler import TaskScheduler
            self._scheduler = TaskScheduler()

            # M21: AsyncIOScheduler supports async functions natively
            self._scheduler.add_daily_job(
                func=self._send_daily_summary,
                hour=23, minute=59,
                job_id="daily_summary",
                name="Daily Performance Summary",
            )
            self._scheduler.start()
        except Exception as sched_exc:
            logger.warning("Scheduler setup failed (non-fatal): {}", sched_exc)

    async def run(self) -> None:
        """Execute main control loop until stop is requested."""
        await self.startup()
        loop_interval = self.settings.grid.loop_interval_seconds

        iteration = 0
        while self.state.running and not self._shutdown_event.is_set():
            started = datetime.now(UTC)
            iteration += 1
            try:
                await self._process_control_commands()
                if self.state.manual_paused:
                    self.db.update_bot_state(
                        status=BotStatus.PAUSED, message="manual_pause_active",
                    )
                    await self._update_heartbeat()
                    await self.health_monitor.ping_if_due()
                    logger.info("[iter {}] PAUSED -- Manual pause active", iteration)
                    continue

                signals = await self._compute_signals()
                if not signals:
                    logger.warning("[iter {}] WARN -- No signals computed, skipping", iteration)
                    continue

                # Price registration for risk — ALL symbols (A1 fix)
                for symbol, signal in signals.items():
                    self.risk_manager.register_price(
                        now=datetime.now(UTC),
                        price=signal.current_price,
                        symbol=symbol,
                    )
                # Use first available price for state tracking
                first_signal = next(iter(signals.values()), None)
                if first_signal:
                    self.state.last_price = first_signal.current_price
                    # Log per-iteration summary
                    logger.info(
                        "[iter {}] SIGNAL {} price={:.2f} trend={} ADX={:.1f} ATR%={:.3f} pause={}",
                        iteration,
                        list(signals.keys()),
                        first_signal.current_price,
                        first_signal.trend_bias,
                        first_signal.adx_value,
                        first_signal.atr_pct * 100,
                        first_signal.pause_new_grid,
                    )

                # Save latest indicators to runtime_config for the dashboard
                indicators_dump = {
                    sym: {
                        "atr": sig.atr_pct * sig.current_price,
                        "atr_pct": sig.atr_pct * 100.0,
                        "adx": sig.adx_value,
                        "ema_fast": sig.ema_fast_val,
                        "ema_slow": sig.ema_slow_val,
                        "trend": sig.trend_bias.value,
                        "current_price": sig.current_price
                    } for sym, sig in signals.items()
                }
                await asyncio.to_thread(self.db.set_runtime_config, "latest_indicators", indicators_dump)

                # Risk evaluation — mark-to-market equity (R1 fix)
                equity, free_balance = await self.exchange.get_portfolio_equity(
                    symbols=self.symbols, quote_coin=self.quote_coin,
                )
                
                # Phase G: Evaluate Dynamic Reinvestment
                if hasattr(self, 'reinvestment_engine'):
                    status_emergency = False # Fallback check safely
                    if hasattr(self.state, 'status'):
                        status_emergency = self.state.status == BotStatus.EMERGENCY
                        
                    if status_emergency:
                        self.reinvestment_engine.reinitialize_after_stop(free_balance)
                    else:
                        dynamic_baseline = self.reinvestment_engine.maybe_recalculate(
                            current_free_equity=free_balance,
                            is_bot_stopped=(not self.state.running or self.state.manual_paused)
                        )
                        # Push scaled configuration down to active strategies
                        initial = getattr(self.reinvestment_engine, '_initial_baseline', 1.0)
                        if initial > 0:
                            multiplier = dynamic_baseline / initial
                            scaled_base_size = self.settings.grid.order_size_usdt * multiplier
                            for strat in self.strategies.values():
                                strat.config.order_size_usdt = scaled_base_size
                
                risk_decision = self.risk_manager.evaluate(
                    equity=equity, now=datetime.now(UTC),
                )
                await self._handle_risk_decision(risk_decision)
                if not risk_decision.allow_trading:
                    logger.info(
                        "[iter {}] RISK-BLOCK -- {}", iteration, risk_decision.reason,
                    )
                    await self._persist_metrics(equity=equity)
                    await self._update_heartbeat()
                    await self.health_monitor.ping_if_due()
                    
                    # WKS-2: Wait for WS trigger or 60s max heartbeat timeout
                    self._signal_eval_event.clear()
                    try:
                        await asyncio.wait_for(self._signal_eval_event.wait(), timeout=60.0)
                    except TimeoutError:
                        pass
                    continue


                # Sync tracked orders with exchange
                sync_results = await asyncio.gather(
                    *(mgr.sync_with_exchange() for mgr in self.order_managers.values()),
                    return_exceptions=True,
                )
                for sym, result in zip(self.order_managers.keys(), sync_results):
                    if isinstance(result, Exception):
                        await self._log_and_notify(
                            "ERROR", "sync", f"{sym} sync failed: {result}",
                        )

                # Place new grids where needed
                await self._place_new_grids(signals=signals, balance=free_balance)
                await self._persist_grid_states(signals)

                # Metrics and heartbeat
                await self._persist_metrics(equity=equity)
                await self._update_heartbeat()
                await self.health_monitor.ping_if_due()
                active_orders = sum(
                    len(mgr.open_orders) for mgr in self.order_managers.values()
                )
                logger.info(
                    "[iter {}] OK -- equity={:.2f} active_orders={}",
                    iteration, equity, active_orders,
                )
                await asyncio.to_thread(
                    self.db.update_bot_state,
                    status=BotStatus.RUNNING,
                    message=f"active_orders={active_orders}",
                )
                # E4: daily DB cleanup
                today = datetime.now(UTC).date()
                if self._last_cleanup_date != today:
                    await asyncio.to_thread(self.db.cleanup_old_events, days=90)
                    self._last_cleanup_date = today

                # WKS-2: Wait for WS trigger or 60s max heartbeat timeout
                self._signal_eval_event.clear()
                try:
                    await asyncio.wait_for(self._signal_eval_event.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                await self._log_and_notify("ERROR", "bot", f"Main loop error: {exc}")
            finally:
                elapsed = (datetime.now(UTC) - started).total_seconds()
                sleep_for = max(0.0, loop_interval - elapsed)
                await asyncio.sleep(sleep_for)

        await self.shutdown(reason="loop_terminated")

    async def shutdown(self, reason: str = "manual_shutdown") -> None:
        """Stop execution, cancel orders, persist final state and notify."""
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        self.state.running = False

        try:
            await asyncio.to_thread(self.db.stop_writer)
        except Exception as exc:
            logger.error("Stopping DB writer failed: {}", exc)

        try:
            await asyncio.gather(
                *(mgr.cancel_all() for mgr in self.order_managers.values()),
                return_exceptions=True,
            )
        except Exception as exc:
            logger.error("Cancel all during shutdown failed: {}", exc)
        try:
            await self.exchange.stop_websockets()
        except Exception as exc:
            logger.error("Stopping websockets failed: {}", exc)

        self.db.update_bot_state(status=BotStatus.STOPPED, message=reason)
        await self._log_and_notify("INFO", "bot", f"Bot stopped safely ({reason}).")

    async def emergency_stop(self, reason: str) -> None:
        """Execute emergency shutdown flow and status update."""
        self.db.update_bot_state(status=BotStatus.EMERGENCY, message=reason)
        await self._log_and_notify("CRITICAL", "risk", f"Emergency stop: {reason}")
        await self.shutdown(reason=reason)

    # ── Strategy ──────────────────────────────────────────────────────

    async def _compute_signals(self) -> dict[str, StrategySignal]:
        tasks = [
            self.exchange.get_klines(symbol=symbol, limit=500)
            for symbol in self.symbols
        ]
        klines_results = await asyncio.gather(*tasks, return_exceptions=True)

        signals: dict[str, StrategySignal] = {}
        for symbol, result in zip(self.symbols, klines_results, strict=True):
            if isinstance(result, Exception):
                await self._log_and_notify(
                    "ERROR", "strategy", f"{symbol} kline fetch failed: {result}",
                )
                continue
            try:
                # Drop currently forming incomplete candle to prevent indicator contamination
                if not result.empty and len(result) > 1:
                    clean_klines = result.iloc[:-1]
                else:
                    clean_klines = result
                signals[symbol] = self.strategies[symbol].compute_signal(clean_klines)
            except Exception as exc:
                await self._log_and_notify(
                    "ERROR", "strategy",
                    f"{symbol} signal computation failed: {exc}",
                )
        return signals

    async def _place_new_grids(
        self, *, signals: dict[str, StrategySignal], balance: float,
    ) -> None:
        active_symbols = [s for s, m in self.order_managers.items() if m.open_orders]
        candidates: list[tuple[float, str, float, StrategySignal]] = []
        
        for symbol, signal in signals.items():
            manager = self.order_managers[symbol]
            entry_orders = [o for o in manager.open_orders if not str(o.level_id).startswith("inverse-")]
            
            if entry_orders:
                # SG4: Integrate Advanced Grid Refresh (Phase B)
                grid_state = self.db.get_latest_grid_state()
                # Use current state specifically for the symbol (or fetch it if multiple)
                latest_states = {row.get("symbol", "").upper(): row for row in self.db.get_latest_grid_states()}
                sym_state = latest_states.get(symbol)
                
                if sym_state and sym_state.get("grid_anchor_price") and sym_state.get("grid_created_at"):
                    anchor_price = sym_state["grid_anchor_price"]
                    created_at = sym_state["grid_created_at"]
                    
                    stale = is_grid_stale(
                        current_price=signal.current_price,
                        anchor_price=anchor_price,
                        grid_created_at=created_at,
                        atr_pct=signal.atr_pct,
                        cfg=self.refresh_cfg,
                    )
                    
                    refresh_state = self._refresh_states[symbol]
                    go, reason = should_refresh(refresh_state, stale, self.refresh_cfg)
                    
                    if go:
                        logger.info(
                            "[REFRESH] {} | {} | dev={:.2%} | age={:.1f}h", 
                            symbol, reason, stale.price_deviation_pct, stale.grid_age_hours
                        )
                        await self._execute_grid_refresh(symbol, manager, refresh_state)
                    else:
                        if stale.is_stale:
                            logger.debug("[REFRESH SKIP] {} | {}", symbol, reason)
                
                continue

            # M1: placement cooldown check
            now = datetime.now(UTC)
            cooldown_until = self._placement_cooldown_until.get(symbol)
            if cooldown_until and now < cooldown_until:
                continue
            if signal.pause_new_grid:
                # M10: ADX fallback — allow if bot is completely idle (no grids open)
                if active_symbols:
                    logger.debug("{}: skipped -- pause_new_grid=True (ADX too high)", symbol)
                    continue
                else:
                    logger.info(
                        "{}: ADX pause overridden — bot is idle, allowing fallback grid",
                        symbol,
                    )
            # S5 fix: spot mode — only LONG bias grids (no naked sells)
            if str(signal.trend_bias).lower() != "long":
                logger.info(
                    "SKIP {}: trend_bias={} (spot only allows LONG)",
                    symbol, signal.trend_bias,
                )
                continue

            # S2: correlation filter
            if signal.close_history is not None and active_symbols:
                is_correlated = False
                for active_sym in active_symbols:
                    active_sig = signals.get(active_sym)
                    if active_sig and active_sig.close_history is not None:
                        corr = signal.close_history.corr(active_sig.close_history)
                        if corr > 0.8:
                            is_correlated = True
                            await self._log_and_notify(
                                "INFO", "strategy",
                                f"{symbol} rejected (corr {corr:.2f} with {active_sym})"
                            )
                            break
                if is_correlated:
                    continue

            required = self.strategies[symbol].estimate_required_capital(signal)
            score = self._score_signal(signal, signals)
            candidates.append((score, symbol, required, signal))

        # M2: batch-fetch live tickers and symbol rules for all candidates
        candidate_symbols = [sym for (_, sym, _, _) in candidates]
        ticker_results = await asyncio.gather(
            *(self.exchange.get_last_price(symbol=sym) for sym in candidate_symbols),
            return_exceptions=True,
        )
        rules_results = await asyncio.gather(
            *(self.exchange.get_spot_symbol_rules(sym) for sym in candidate_symbols),
            return_exceptions=True,
        )
        live_prices: dict[str, float] = {}
        symbol_rules: dict[str, SpotSymbolRules] = {}
        for sym, result in zip(candidate_symbols, ticker_results, strict=True):
            if isinstance(result, Exception):
                await self._log_event(
                    "WARNING", "strategy",
                    f"{sym} ticker fetch failed; skipping candidate",
                    payload={"error": str(result)},
                )
                continue
            live_prices[sym] = float(result)
        for sym, result in zip(candidate_symbols, rules_results, strict=True):
            if isinstance(result, Exception):
                await self._log_event(
                    "WARNING", "strategy",
                    f"{sym} rules fetch failed; skipping candidate",
                    payload={"error": str(result)},
                )
                continue
            symbol_rules[sym] = result

        # M2+M3: validate candidates against exchange constraints
        now = datetime.now(UTC)
        validated: list[tuple[float, str, float, StrategySignal]] = []
        for score, symbol, required, signal in candidates:
            live_price = live_prices.get(symbol)
            if not live_price or live_price <= 0:
                continue
            rules = symbol_rules.get(symbol)
            if rules is None:
                continue
            # APS-2: Use dynamic target notional
            target_notional = signal.target_notional
            raw_qty = Decimal(str(target_notional / live_price))
            normalized_qty = self._round_down_qty(raw_qty, rules.qty_step)
            current_notional = float(normalized_qty) * live_price
            
            if normalized_qty < rules.min_qty or current_notional < 5.0:
                self._placement_cooldown_until[symbol] = now + timedelta(seconds=1800)
                await self._log_event(
                    "WARNING", "strategy",
                    f"{symbol} skipped due to min_qty or notional constraint",
                    payload={
                        "target_notional": round(target_notional, 8),
                        "actual_notional": round(current_notional, 8),
                        "normalized_qty": float(normalized_qty),
                        "min_qty": float(rules.min_qty),
                        "atr_pct_used": round(signal.atr_pct, 4)
                    },
                )
                continue
            # M3: signal/ticker price deviation guard
            deviation = abs(signal.current_price - live_price) / live_price
            if deviation > self._max_signal_price_deviation_pct:
                self._placement_cooldown_until[symbol] = now + timedelta(
                    seconds=self._price_limit_cooldown_seconds,
                )
                await self._log_event(
                    "WARNING", "strategy",
                    f"{symbol} skipped due kline/ticker divergence",
                    payload={
                        "signal_price": round(signal.current_price, 8),
                        "ticker_price": round(live_price, 8),
                        "deviation_pct": round(deviation * 100, 4),
                    },
                )
                continue
            validated.append((score, symbol, required, signal))

        candidates = validated

        candidates.sort(key=lambda item: item[0], reverse=True)

        available_balance = balance
        selected: list[tuple[str, StrategySignal, float]] = []
        active_count = len(active_symbols)
        new_selections = 0

        for score, symbol, required, signal in candidates:
            # FIX #2: Cap accounts for already-running grids (new grids only)
            is_new = symbol not in active_symbols
            if is_new and (active_count + new_selections) >= self.settings.grid.max_active_pairs:
                continue
                
            # FIX #3 & #4: Graceful capital scaling logic over binary skip
            live_price = live_prices.get(symbol, signal.current_price)
            rules = symbol_rules.get(symbol)
            if not rules:
                continue

            # Assuming 3 is the minimum viable levels
            MIN_VIABLE_LEVELS = 3
            MINIMUM_PROFIT_BUFFER = 1.002
            TAKER_FEE_RATE = 0.001 # Approximation if fee rate is not in rules

            if required > available_balance:
                # Stage 1: Absolute minimum viability
                min_threshold = rules.min_qty * live_price * MIN_VIABLE_LEVELS
                if available_balance < min_threshold:
                    logger.debug(
                        f"[Capital] Skipping {symbol}: available={available_balance:.2f}, "
                        f"below minimum viable threshold {min_threshold:.2f} USDT."
                    )
                    continue
                
                # Stage 2: Calculate scaled signal
                scale_factor = available_balance / required
                scaled_signal = signal.scale(scale_factor)
                
                # Stage 3: Validate scaled grid viability
                viable = True
                if scaled_signal.level_count < MIN_VIABLE_LEVELS:
                    viable = False
                for lvl in scaled_signal.levels:
                    # check actual required notional & qty min rules
                    if lvl.qty * live_price < rules.min_notional or lvl.qty < rules.min_qty:
                        viable = False
                        break
                if scaled_signal.grid_spread_pct < (2 * TAKER_FEE_RATE * MINIMUM_PROFIT_BUFFER):
                    viable = False

                if not viable:
                    logger.debug(
                        f"[Capital] Skipping {symbol}: scaled grid is not economically viable "
                        f"at {available_balance:.2f} USDT."
                    )
                    continue

                logger.info(
                    f"[Capital] Scaled {symbol} grid from {required:.2f} to {available_balance:.2f} "
                    f"USDT (factor: {scale_factor:.2f})."
                )
                signal = scaled_signal
                required = available_balance

            # S3: liquidity pre-check
            try:
                ob = await self.exchange.get_orderbook(symbol=symbol)
                if ob["spread_pct"] > 0.005 or ob["bid_depth_usdt"] < required * 3:
                    await self._log_and_notify(
                        "WARNING", "strategy",
                        f"{symbol} low liquidity spread={ob['spread_pct']:.4f}, depth={ob['bid_depth_usdt']:.1f}",
                    )
                    continue
            except Exception as exc:
                logger.error("{}: orderbook check failed: {}", symbol, exc)
                continue

            selected.append((symbol, signal, score))
            if is_new:
                new_selections += 1
            available_balance -= required

        if selected:
            summary = [
                {
                    "symbol": symbol,
                    "score": round(score, 2),
                    "atr_pct": round(signal.atr_pct * 100, 3),
                    "adx": round(signal.adx_value, 2),
                }
                for symbol, signal, score in selected
            ]
            await self._log_event(
                "INFO", "strategy", "Multi-pair selection",
                payload={"selected": summary},
            )

        for symbol, signal, _score in selected:
            manager = self.order_managers[symbol]
            if not signal.levels:
                await self._log_event(
                    "ERROR", "strategy",
                    f"{symbol} selected with empty levels; skipping placement",
                    payload={
                        "pause_new_grid": bool(signal.pause_new_grid),
                        "reason": signal.reason,
                        "trend_bias": str(signal.trend_bias),
                    },
                )
                continue
            try:
                placed = await manager.place_grid_orders(
                    levels=signal.levels,
                    buy_spacing_pct=signal.buy_spacing_pct,
                    sell_spacing_pct=signal.sell_spacing_pct,
                )
                
                # SG4: Persist anchor values upon successful placement
                if placed:
                    manager.grid_anchor_price = signal.current_price
                    manager.grid_created_at = datetime.now(UTC)
                    
                # R5: set exchange-side hard stop-loss below lowest buy level
                if placed:
                    buy_orders = [o for o in placed if o.side == OrderSide.BUY]
                    if buy_orders:
                        lowest_price = min(o.price for o in buy_orders)
                        total_qty = sum(o.qty for o in buy_orders)
                        stop_price = lowest_price * (1 - self.settings.risk.max_drawdown_pct * 2)
                        try:
                            await self.exchange.set_stop_loss_hard(
                                symbol=symbol,
                                trigger_price=stop_price,
                                qty=total_qty,
                            )
                        except Exception as sl_exc:
                            await self._log_and_notify(
                                "WARNING", "risk",
                                f"{symbol} hard SL placement failed: {sl_exc}",
                            )
            except Exception as exc:
                # M4: clean up partial orders on the exchange
                try:
                    await manager.cancel_all()
                except Exception:
                    pass
                # M4: detect price-limit errors (Bybit 170193) and apply longer cooldown
                exc_str = str(exc)
                if "170193" in exc_str or "price limit" in exc_str.lower():
                    cooldown_secs = self._price_limit_cooldown_seconds
                else:
                    cooldown_secs = self._placement_cooldown_seconds
                self._placement_cooldown_until[symbol] = datetime.now(UTC) + timedelta(
                    seconds=cooldown_secs,
                )
                await self._log_and_notify(
                    "ERROR", "order_manager",
                    f"{symbol} grid placement failed (cooldown {cooldown_secs}s): {exc}",
                )

    async def _compute_portfolio_equity(
        self, signals: dict[str, StrategySignal],
    ) -> float:
        """Compute total portfolio equity using batch balance + signal prices.

        Uses already-fetched signal prices to avoid extra API calls.
        """
        base_coins = set()
        for symbol in self.symbols:
            base = symbol.replace(self.quote_coin, "").replace("USDT", "")
            if base:
                base_coins.add(base)
        coins_to_fetch = list(base_coins | {self.quote_coin})
        try:
            balances = await self.exchange.get_balances(coins_to_fetch)
        except Exception as exc:
            logger.warning("Batch balance fetch failed, falling back: {}", exc)
            try:
                quote_bal = await self.exchange.get_balance(self.quote_coin)
                return quote_bal
            except Exception:
                return 0.0

        total = balances.get(self.quote_coin, 0.0)
        for symbol, signal in signals.items():
            base = symbol.replace(self.quote_coin, "").replace("USDT", "")
            coin_balance = balances.get(base, 0.0)
            if coin_balance > 0 and signal.current_price > 0:
                total += coin_balance * signal.current_price
        return total

    async def _send_daily_summary(self) -> None:
        """Compile and send daily performance summary via Telegram."""
        try:
            stats = self.db.get_trade_stats()
            uptime = "N/A"
            if self.state.started_at:
                delta = datetime.now(UTC) - self.state.started_at
                hours = delta.total_seconds() / 3600
                uptime = f"{hours:.1f}h"
            payload = {
                "pnl_daily": stats.get("pnl_24h", 0.0),
                "pnl_total": stats.get("total_pnl", 0.0),
                "drawdown_pct": 0.0,
                "total_trades": stats.get("total_trades", 0),
                "uptime": uptime,
            }
            await self.notifier.send_daily_summary(payload)
        except Exception as exc:
            logger.error("Daily summary failed: {}", exc)

    @staticmethod
    def _round_down_qty(value: Decimal, step: Decimal) -> Decimal:
        """Round a quantity down to the nearest valid step size."""
        if step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _score_signal(signal: StrategySignal, all_signals: dict[str, StrategySignal]) -> float:
        """Rank candidate symbols using z-score relative scoring (S1)."""
        import math
        active = [s for s in all_signals.values() if not s.pause_new_grid]
        if not active:
            return 0.0

        atr_values = [s.atr_pct for s in active]
        adx_values = [s.adx_value for s in active]
        atr_mean = sum(atr_values) / len(atr_values)
        adx_mean = sum(adx_values) / len(adx_values)

        atr_std = math.sqrt(sum((x - atr_mean)**2 for x in atr_values) / len(atr_values)) + 1e-9
        adx_std = math.sqrt(sum((x - adx_mean)**2 for x in adx_values) / len(adx_values)) + 1e-9

        z_atr = (signal.atr_pct - atr_mean) / atr_std
        z_adx = (signal.adx_value - adx_mean) / adx_std

        trend_bonus = 2.0 if str(signal.trend_bias).lower() == "long" else 0.0
        return z_atr - z_adx + trend_bonus

    async def _persist_grid_states(self, signals: dict[str, StrategySignal]) -> None:
        def _sync_persist():
            for symbol, manager in self.order_managers.items():
                signal = signals.get(symbol)
                if signal is None:
                    continue
                state = manager.to_grid_state(
                    buy_spacing_pct=signal.buy_spacing_pct,
                    sell_spacing_pct=signal.sell_spacing_pct,
                    trend_bias=str(signal.trend_bias),
                )
                self.db.save_grid_state(state)
        await asyncio.to_thread(_sync_persist)

    async def _execute_grid_refresh(self, symbol: str, manager: OrderManager, refresh_state: RefreshState) -> None:
        """
        Safely cancel stale entry orders and allow grid re-creation next cycle.
        Aborts if inventory is unhedged. Updates refresh_state in place.
        """
        if manager.has_unhedged_inventory():
            logger.warning("[REFRESH ABORT] {} — unhedged inventory, skipping cancel", symbol)
            refresh_state.refresh_fail_count += 1
            return

        refresh_state.refresh_in_progress = True
        try:
            cancelled = await manager.cancel_entry_orders_only()
            logger.info("[REFRESH OK] {} — cancelled {} entry orders", symbol, cancelled)
            
            refresh_state.last_refresh_at = datetime.now(UTC)
            refresh_state.stale_cycle_count = 0
            refresh_state.refresh_fail_count = 0
            refresh_state.refresh_count_today += 1
            
        except Exception as e:
            logger.error("[REFRESH ERROR] {} — {}", symbol, e, exc_info=True)
            refresh_state.refresh_fail_count += 1
        finally:
            refresh_state.refresh_in_progress = False

    # ── Risk ──────────────────────────────────────────────────────────

    async def _handle_risk_decision(self, decision: RiskDecision) -> None:
        if decision.emergency_stop:
            await self.emergency_stop(decision.reason)
            return
        if not decision.allow_trading and decision.paused_until:
            self.db.update_bot_state(
                status=BotStatus.PAUSED,
                message=decision.reason,
                paused_until=decision.paused_until,
            )
            await self._log_and_notify(
                "WARNING", "risk",
                f"Trading paused until {decision.paused_until.isoformat()} "
                f"({decision.reason})",
            )

    # ── WebSocket Handlers ────────────────────────────────────────────

    async def _on_market_message(self, message: dict[str, Any]) -> None:
        """Process public market data from WebSockets."""
        topic = message.get("topic", "")
        if topic.startswith("kline."):
            data = message.get("data", [])
            for kline in data:
                # WKS-1: Only trigger signal eval on confirmed candle close
                if kline.get("confirm"):
                    logger.debug("WS candle closed for {}; triggering eval", topic)
                    self._signal_eval_event.set()

    async def _on_private_message(self, message: dict[str, Any]) -> None:
        topic = message.get("topic", "")
        if topic.startswith("order"):
            data = message.get("data", [])
            events = data if isinstance(data, list) else [data]
            for event in events:
                if event.get("orderStatus") != "Filled":
                    continue
                symbol = str(event.get("symbol", "")).upper()
                managers: list[OrderManager]
                if symbol and symbol in self.order_managers:
                    managers = [self.order_managers[symbol]]
                else:
                    managers = list(self.order_managers.values())
                for manager in managers:
                    trade = await manager.handle_fill(event)
                    if trade:
                        await asyncio.to_thread(self.db.insert_trade, trade)
                        # E2 fix: log only fill events, not all private updates
                        await self._log_event(
                            "INFO", "exchange_fill",
                            f"Fill: {trade.symbol} {trade.side} {trade.qty}@{trade.price}",
                            payload=event,
                        )
                        break

    async def _handle_ws_failure(self, reason: str) -> None:
        await self._log_and_notify(
            "CRITICAL", "exchange", f"WebSocket failure: {reason}",
        )
        await self.emergency_stop(reason=f"ws_failure:{reason}")

    # ── Control Commands ──────────────────────────────────────────────

    async def _process_control_commands(self) -> None:
        commands = self.db.fetch_pending_commands(limit=20)
        for command in commands:
            command_id = int(command["id"])
            action = str(command["command"]).lower()
            payload = command.get("payload_json", {})
            try:
                if action == "stop":
                    self.state.running = False
                elif action == "pause":
                    self.state.manual_paused = True
                    self.db.update_bot_state(
                        status=BotStatus.PAUSED, message="manual_pause",
                    )
                elif action == "resume":
                    self.state.manual_paused = False
                    self.risk_manager.clear_pause()
                    self.db.update_bot_state(
                        status=BotStatus.RUNNING, message="manual_resume",
                    )
                elif action == "emergency":
                    await self.emergency_stop(reason="manual_emergency")
                elif action == "start":
                    self.state.manual_paused = False
                    self.state.running = True
                elif action == "pause_hours":
                    hours = int(payload.get("hours", 24))
                    self.risk_manager.force_pause(hours=hours)
                self.db.mark_command_processed(command_id, status="processed")
            except Exception as exc:
                self.db.mark_command_processed(command_id, status="failed")
                await self._log_and_notify(
                    "ERROR", "bot",
                    f"Failed to process command {action}: {exc}",
                )

    # ── Persistence ───────────────────────────────────────────────────

    async def _persist_metrics(self, *, equity: float) -> None:
        def _sync_persist() -> None:
            stats = self.db.get_trade_stats()
            pnl_total = float(stats["gross_profit"] - stats["gross_loss"])
            daily = self.db.get_daily_pnl(limit=2)
            pnl_daily = float(daily[-1]["pnl"]) if daily else 0.0
            drawdown = self.risk_manager.drawdown_pct(equity)
            sharpe = self._compute_sharpe_from_daily_pnl(self.db.get_daily_pnl(limit=90))
    
            metrics = PerformanceMetrics(
                timestamp=datetime.now(UTC),
                equity=equity,
                pnl_total=pnl_total,
                pnl_daily=pnl_daily,
                drawdown_pct=drawdown,
                win_rate=float(stats["win_rate"]),
                sharpe_ratio=sharpe,
                total_trades=int(stats["total_trades"]),
            )
            self.db.insert_metrics(metrics)
            self.db.record_equity(capital=equity, drawdown_pct=drawdown)
        await asyncio.to_thread(_sync_persist)

    @staticmethod
    def _compute_sharpe_from_daily_pnl(rows: list[dict[str, Any]]) -> float:
        returns = np.array([float(row["pnl"]) for row in rows], dtype=float)
        if returns.size < 2:
            return 0.0
        std = returns.std()
        if std == 0:
            return 0.0
        return float((returns.mean() / std) * np.sqrt(365))

    async def _update_heartbeat(self) -> None:
        heartbeat_file: Path = self.settings.heartbeat_full_path
        heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        payload = datetime.now(UTC).isoformat()
        heartbeat_file.write_text(payload, encoding="utf-8")

    # ── Logging ───────────────────────────────────────────────────────

    async def _log_event(
        self,
        level: str,
        module: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = EventLogRecord(
            timestamp=datetime.now(UTC),
            level=level,
            module=module,
            message=message,
            payload=payload or {},
        )
        await asyncio.to_thread(self.db.log_event, event)

    async def _log_and_notify(
        self,
        level: str,
        module: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._log_event(level, module, message, payload=payload)
        if level in {"ERROR", "CRITICAL", "WARNING"}:
            await self.notifier.send_alert(f"[{level}] {message}")
        else:
            await self.notifier.send_info(message)


# ── CLI Entrypoint ────────────────────────────────────────────────────


async def run_bot() -> None:
    settings = Settings()
    logger.remove()
    logger.add(
        str(settings.log_full_path),
        rotation="10 MB",
        retention="14 days",
        level=settings.log_level,
        enqueue=True,
    )
    logger.add(lambda msg: print(msg, end=""), level=settings.log_level)

    db = Database(settings.db_full_path)
    db.init_schema()

    active_symbols = settings.active_symbols

    exchange = BybitExchangeClient(
        api_key=settings.exchange.api_key,
        api_secret=settings.exchange.api_secret,
        testnet=settings.exchange.testnet,
        symbol=active_symbols[0],
        timeframe="1",
        domain=settings.exchange.domain,
        tld=settings.exchange.tld,
    )
    logger.info(f"BOOT ENV: domain={settings.exchange.domain}, tld={settings.exchange.tld}, api_key={settings.exchange.api_key[:5]}...")

    strategies: dict[str, GridStrategy] = {}
    order_managers: dict[str, OrderManager] = {}
    for symbol in active_symbols:
        strategy_args = settings.strategy_dict()
        strategy_args["symbol"] = symbol
        strategies[symbol] = GridStrategy(StrategyConfig(**strategy_args))
        order_managers[symbol] = OrderManager(exchange=exchange, symbol=symbol)

    risk_manager = RiskManager(**settings.risk_dict())
    notifier = TelegramNotifier(
        enabled=settings.telegram.enabled,
        token=settings.telegram.bot_token,
        chat_id=settings.telegram.chat_id,
    )
    health_monitor = HealthMonitor(
        url=settings.healthcheck.ping_url,
        interval_seconds=settings.healthcheck.interval_seconds,
    )

    bot = TradingBot(
        settings=settings,
        db=db,
        exchange=exchange,
        strategies=strategies,
        risk_manager=risk_manager,
        order_managers=order_managers,
        notifier=notifier,
        health_monitor=health_monitor,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    async def _request_shutdown(reason: str) -> None:
        logger.info("Shutdown requested: {}", reason)
        await bot.shutdown(reason=reason)
        stop_event.set()

    def _sync_signal_handler(sig: int, frame: object | None) -> None:
        del frame
        asyncio.create_task(_request_shutdown(reason=f"signal:{sig}"))

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(
                    _request_shutdown(reason=f"signal:{s}")
                ),
            )
        except NotImplementedError:
            signal.signal(sig, _sync_signal_handler)

    bot_task = asyncio.create_task(bot.run(), name="trading-bot-main-loop")
    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-wait")
    await asyncio.wait([bot_task, stop_task], return_when=asyncio.FIRST_COMPLETED)

    if not bot_task.done():
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)

    if not stop_task.done():
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)

    db.close()


if __name__ == "__main__":
    asyncio.run(run_bot())
