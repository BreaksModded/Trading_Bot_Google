"""Read-only audit: compare exchange wallet balances vs bot's tracked positions.

Detects untracked inventory (balance on Bybit that the bot's OrderManager
does not know about). Common after partial recoveries from crashes when
_position_qty was not restored from exchange data.

Usage:
    python scripts/audit_balances.py

Reports per-symbol divergence in USD value and recommends action when
the gap exceeds 1 USDT.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import load_settings
from core.exchange import BybitExchangeClient
from data.database import Database


async def main() -> int:
    settings = load_settings()
    db = Database(settings.db_full_path)

    client = BybitExchangeClient(
        api_key=settings.exchange.api_key,
        api_secret=settings.exchange.api_secret,
        testnet=settings.exchange.testnet,
        domain=settings.exchange.domain,
        tld=settings.exchange.tld,
    )

    quote = settings.quote_coin
    symbols = settings.active_symbols
    base_coins = [s.replace(quote, "").replace("USDT", "") for s in symbols]
    coins = list({*base_coins, quote})

    exchange_balances = await client.get_balances(coins)
    print("─" * 78)
    print(f"EXCHANGE BALANCES (UNIFIED)")
    print("─" * 78)
    for c, v in sorted(exchange_balances.items()):
        print(f"  {c:<8}  {v:>16.8f}")

    snapshot_raw = db.get_runtime_config("portfolio_snapshot")
    snapshot = json.loads(snapshot_raw) if isinstance(snapshot_raw, str) else (snapshot_raw or {})
    holdings = snapshot.get("holdings", {})

    positions_raw = db.get_runtime_config("positions")
    positions = json.loads(positions_raw) if isinstance(positions_raw, str) else (positions_raw or {})

    indicators_raw = db.get_runtime_config("latest_indicators")
    indicators = json.loads(indicators_raw) if isinstance(indicators_raw, str) else (indicators_raw or {})

    print()
    print("─" * 78)
    print(f"DIVERGENCE PER SYMBOL  (exchange vs bot tracked)")
    print("─" * 78)
    print(f"  {'symbol':<10} {'base':<6} {'exchange':>14} {'tracked':>14} {'price':>10} {'gap_usd':>10}")

    total_gap_usd = 0.0
    flagged: list[tuple[str, float]] = []
    for sym in symbols:
        base = sym.replace(quote, "").replace("USDT", "")
        ex_qty = exchange_balances.get(base, 0.0)
        tr_qty = float(positions.get(sym, {}).get("qty", 0.0)) if positions else 0.0
        price = float(indicators.get(sym, {}).get("current_price", 0.0)) if indicators else 0.0

        diff_qty = ex_qty - tr_qty
        gap_usd = diff_qty * price
        total_gap_usd += gap_usd
        if abs(gap_usd) > 1.0:
            flagged.append((sym, gap_usd))

        print(
            f"  {sym:<10} {base:<6} {ex_qty:>14.8f} {tr_qty:>14.8f} "
            f"{price:>10.4f} {gap_usd:>10.2f}"
        )

    print()
    print("─" * 78)
    print(f"SUMMARY")
    print("─" * 78)
    print(f"  free {quote:<5}: {exchange_balances.get(quote, 0.0):.4f}")
    print(f"  tracked free (snapshot): {snapshot.get('free_usdt', 0.0):.4f}")
    print(f"  total untracked gap (USD): {total_gap_usd:.2f}")

    if flagged:
        print()
        print("  ⚠ FLAGGED (gap > 1 USDT):")
        for sym, gap in flagged:
            action = "exchange has untracked inventory — bot will not hedge it" if gap > 0 \
                     else "bot tracked > exchange — possible double-counting"
            print(f"    {sym}: {gap:+.2f} USD — {action}")
        print()
        print("  Recommended action: stop bot, restart so recover_from_crash + ")
        print("  _reconstruct_avg_cost re-build state from DB trade history.")
        print("  If the bot still won't hedge it, consider manual liquidation.")
    else:
        print("  ✓ No significant divergences detected.")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
