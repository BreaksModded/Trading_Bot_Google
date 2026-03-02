"""
Telegram notification service for the trading bot.

Sends alerts for: bot start/stop, circuit breakers, daily summaries,
critical errors, and DMS activations. Supports interactive commands.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from loguru import logger


class TelegramNotifier:
    """
    Telegram bot for alerts and interactive commands.

    Sends formatted messages to a configured chat ID and can handle
    incoming commands like /status, /stop, /start.

    Args:
        enabled: Whether notifications are active.
        token: Telegram bot token.
        chat_id: Telegram chat ID for alerts.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        token: str = "",
        chat_id: str = "",
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self.enabled = enabled and bool(token) and bool(chat_id)
        self._bot: Any = None

        if self.enabled:
            try:
                from telegram import Bot
                self._bot = Bot(token=self._token)
                logger.info("Telegram notifier initialized")
            except ImportError:
                logger.warning("python-telegram-bot not installed -- notifications disabled")
                self.enabled = False
            except Exception as e:
                logger.error(f"Telegram init failed: {e}")
                self.enabled = False
        else:
            logger.info("Telegram notifications disabled (missing token or chat_id)")

    async def send_alert(self, message: str) -> bool:
        """Send an alert-level message asynchronously."""
        return await self._send(message)

    async def send_info(self, message: str) -> bool:
        """Send an info-level message asynchronously."""
        return await self._send(message)

    async def send_daily_summary(self, payload: dict[str, Any]) -> bool:
        """Format and send daily summary."""
        pnl = payload.get("pnl_daily", 0.0)
        total_pnl = payload.get("pnl_total", 0.0)
        drawdown = payload.get("drawdown_pct", 0.0)
        trades = payload.get("total_trades", 0)
        uptime = payload.get("uptime", "N/A")
        pnl_emoji = "+" if pnl >= 0 else ""
        msg = (
            f"Daily Summary\n"
            f"PnL today: {pnl_emoji}{pnl:.4f}\n"
            f"PnL total: {total_pnl:.4f}\n"
            f"Drawdown: {drawdown:.2f}%\n"
            f"Trades: {trades}\n"
            f"Uptime: {uptime}"
        )
        return await self._send(msg)

    async def run_command_bot(self, db: Any) -> None:
        """Run Telegram command bot for /status, /stop, /start, /resume, /config."""
        if not self.enabled:
            return
        try:
            from telegram import Update
            from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
        except ImportError:
            logger.warning("python-telegram-bot is not installed; command bot disabled.")
            return

        async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            state = db.get_bot_state()
            await update.message.reply_text(
                f"Estado: {state['status']}\n"
                f"Última actualización: {state['updated_at']}\n"
                f"Mensaje: {state['message'] or '-'}"
            )

        async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            db.enqueue_command("stop")
            await update.message.reply_text("Comando STOP encolado.")

        async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            db.enqueue_command("start")
            await update.message.reply_text("Comando START encolado.")

        async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            db.enqueue_command("resume")
            await update.message.reply_text("Comando RESUME encolado.")

        async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            snapshot = db.get_latest_config_snapshot()
            values = snapshot["values_json"] if snapshot else {}
            await update.message.reply_text(f"Config actual:\n{values}")

        tg_app = ApplicationBuilder().token(self._token).build()
        tg_app.add_handler(CommandHandler("status", status_cmd))
        tg_app.add_handler(CommandHandler("stop", stop_cmd))
        tg_app.add_handler(CommandHandler("start", start_cmd))
        tg_app.add_handler(CommandHandler("resume", resume_cmd))
        tg_app.add_handler(CommandHandler("config", config_cmd))
        tg_app.add_error_handler(self._on_telegram_error)

        try:
            await tg_app.initialize()
            await tg_app.start()
            await tg_app.updater.start_polling(drop_pending_updates=True)
            self._telegram_available = True
            logger.info("Telegram command bot started.")
        except Exception as e:
            logger.warning(
                f"[Notifier] Telegram polling failed to start: {e}. "
                f"Bot will operate without Telegram commands.",
                exc_info=True
            )
            self._telegram_available = False
            return

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()

    async def _on_telegram_error(self, update: Any, context: Any) -> None:
        """Called by python-telegram-bot on polling errors."""
        logger.warning(f"[Notifier] Telegram API error (non-fatal): {context.error}")

    async def _send(self, message: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled or not getattr(self, "_telegram_available", True) or not self._bot:
            logger.debug(f"Telegram disabled, message not sent: {message[:50]}...")
            return False
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode=parse_mode,
            )
            logger.debug(f"Telegram message sent: {message[:50]}...")
            return True
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
            return False

    # ── Formatted Messages ────────────────────────────────────────────

    async def notify_bot_started(self, testnet: bool = True) -> None:
        mode = "TESTNET" if testnet else "MAINNET"
        await self._send(
            f"Bot Started\nMode: {mode}\n"
            f"Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    async def notify_bot_stopped(self, reason: str = "Manual") -> None:
        await self._send(
            f"Bot Stopped\nReason: {reason}\n"
            f"Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    async def notify_circuit_breaker(self, breaker_type: str, details: str) -> None:
        await self._send(
            f"CIRCUIT BREAKER ACTIVATED\n"
            f"Type: {breaker_type}\nDetail: {details}\n"
            f"Action: All orders cancelled"
        )

    async def notify_error(self, module: str, error: str) -> None:
        await self._send(
            f"Critical Error\nModule: {module}\nError: {error}\n"
            f"Time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    @property
    def is_enabled(self) -> bool:
        return self.enabled
