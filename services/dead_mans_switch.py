"""
Dead Man's Switch — Independent safety process.

Monitors the bot's heartbeat file and cancels all exchange orders
if the bot becomes unresponsive. Runs as a completely separate process
with its own pybit connection.

Usage:
    python -m services.dead_mans_switch
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from loguru import logger

load_dotenv(PROJECT_ROOT / ".env")

# ── Configuration ─────────────────────────────────────────────────────

HEARTBEAT_FILE = PROJECT_ROOT / "bot_heartbeat.txt"
MAX_SILENCE_SEC = int(os.getenv("DMS_MAX_SILENCE_SEC", "120"))
CHECK_INTERVAL_SEC = int(os.getenv("DMS_CHECK_INTERVAL_SEC", "30"))
SYMBOL = os.getenv("DMS_SYMBOL", "BTCUSDT")
TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

# ── Logging ───────────────────────────────────────────────────────────

log_dir = PROJECT_ROOT / "logs"
log_dir.mkdir(exist_ok=True)

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | DMS | {message}")
logger.add(
    str(log_dir / "dead_mans_switch.log"),
    rotation="10 MB",
    retention="30 days",
    level="DEBUG",
)


def cancel_all_emergency() -> bool:
    """
    Cancel all open orders as an emergency measure.

    Uses its own independent pybit connection — does NOT depend on
    the bot's exchange module. This is deliberate.

    Returns:
        True if cancellation was successful.
    """
    try:
        from pybit.unified_trading import HTTP

        api_key = os.getenv("BYBIT_API_KEY", "")
        api_secret = os.getenv("BYBIT_API_SECRET", "")

        if not api_key or not api_secret:
            logger.error("API keys not configured — cannot cancel orders")
            return False

        session = HTTP(
            testnet=TESTNET,
            api_key=api_key,
            api_secret=api_secret,
        )

        logger.warning("⚠️ Executing EMERGENCY order cancellation...")
        resp = session.cancel_all_orders(category="spot", symbol=SYMBOL)
        cancelled = len(resp.get("result", {}).get("list", []))
        logger.info(f"✅ Emergency cancellation complete: {cancelled} orders cancelled")

        return True

    except Exception as e:
        logger.critical(f"❌ EMERGENCY CANCELLATION FAILED: {e}")
        return False


def send_telegram_alert() -> None:
    """Send DMS activation alert via Telegram (independent of bot's notifier)."""
    try:
        import asyncio
        from telegram import Bot

        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        if not token or not chat_id:
            logger.warning("Telegram not configured — skipping DMS alert")
            return

        bot = Bot(token=token)

        async def _send():
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ <b>DEAD MAN'S SWITCH ACTIVADO</b>\n\n"
                    "El bot de trading no respondió al heartbeat.\n"
                    "Todas las órdenes han sido canceladas de emergencia.\n\n"
                    f"Último heartbeat: hace >{MAX_SILENCE_SEC}s\n"
                    f"Hora: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
                ),
                parse_mode="HTML",
            )

        asyncio.run(_send())
        logger.info("DMS alert sent via Telegram")

    except Exception as e:
        logger.error(f"Failed to send Telegram DMS alert: {e}")


def run_dead_mans_switch() -> None:
    """
    Main loop of the Dead Man's Switch.

    Continuously monitors the heartbeat file. If the file hasn't been
    updated within MAX_SILENCE_SEC, triggers emergency cancellation.
    """
    mode = "TESTNET" if TESTNET else "🔴 MAINNET"
    logger.info(
        f"Dead Man's Switch started [{mode}]\n"
        f"  Symbol: {SYMBOL}\n"
        f"  Heartbeat file: {HEARTBEAT_FILE}\n"
        f"  Max silence: {MAX_SILENCE_SEC}s\n"
        f"  Check interval: {CHECK_INTERVAL_SEC}s"
    )

    consecutive_failures = 0
    max_consecutive_failures = 3

    while True:
        time.sleep(CHECK_INTERVAL_SEC)

        try:
            # Check if heartbeat file exists
            if not HEARTBEAT_FILE.exists():
                consecutive_failures += 1
                logger.warning(
                    f"Heartbeat file not found ({consecutive_failures}/{max_consecutive_failures})"
                )

                if consecutive_failures >= max_consecutive_failures:
                    logger.critical("Heartbeat file missing for too long — activating DMS")
                    cancel_all_emergency()
                    send_telegram_alert()
                    consecutive_failures = 0
                    # Don't exit — keep monitoring in case bot restarts
                    time.sleep(MAX_SILENCE_SEC)
                continue

            # Check heartbeat age
            last_beat = HEARTBEAT_FILE.stat().st_mtime
            age = time.time() - last_beat

            if age > MAX_SILENCE_SEC:
                logger.critical(
                    f"HEARTBEAT STALE: {age:.0f}s since last update "
                    f"(limit: {MAX_SILENCE_SEC}s)"
                )
                cancel_all_emergency()
                send_telegram_alert()
                # Reset and keep monitoring
                consecutive_failures = 0
                time.sleep(MAX_SILENCE_SEC)
            else:
                consecutive_failures = 0
                logger.debug(f"Heartbeat OK: {age:.0f}s ago")

        except KeyboardInterrupt:
            logger.info("DMS shutdown requested")
            break
        except Exception as e:
            logger.error(f"DMS check error: {e}")
            consecutive_failures += 1


if __name__ == "__main__":
    run_dead_mans_switch()
