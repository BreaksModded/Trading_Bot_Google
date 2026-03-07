
import sqlite3
import json
from pathlib import Path

db_path = Path(r"C:\Users\diego\Documents\Bot Trading Google\Trading_Bot_Google\data\trading_bot.db")

def audit_db():
    if not db_path.exists():
        print("Database not found.")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # T1.1 - PnL Stats
    cur.execute("SELECT COUNT(*) as total, SUM(pnl) as pnl_sum FROM trades WHERE pnl != 0")
    res = cur.fetchone()
    total_closed = res['total']
    total_pnl = res['pnl_sum'] or 0.0

    cur.execute("SELECT DATE(timestamp) as day, SUM(pnl) as pnl_day FROM trades WHERE pnl != 0 GROUP BY day ORDER BY day DESC")
    days_pnl = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT COUNT(*) as winners FROM trades WHERE pnl > 0")
    winners = cur.fetchone()['winners']
    win_rate = (winners / total_closed * 100) if total_closed > 0 else 0.0

    cur.execute("SELECT MAX(drawdown_pct) as max_dd FROM performance_snapshots")
    max_dd = cur.fetchone()['max_dd'] or 0.0

    # T1.2 - Phase V Evidence
    cur.execute("SELECT symbol, position_qty, avg_cost FROM grid_states ORDER BY timestamp DESC LIMIT 1")
    last_state = cur.fetchone()
    
    # Check for potential duplicates (BUG-3) - orders with multiple fills in trades
    cur.execute("SELECT exchange_order_id, COUNT(*) as fill_count FROM trades GROUP BY exchange_order_id HAVING fill_count > 1")
    potential_dups = [dict(r) for r in cur.fetchall()]

    print(f"--- Trading Stats ---")
    print(f"Total Closed Fills: {total_closed}")
    print(f"Total PnL Accum: {total_pnl:.4f} USDT")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Max Drawdown: {max_dd:.2f}%")
    print(f"PnL by Day: {json.dumps(days_pnl, indent=2)}")
    print(f"\n--- Grid State ---")
    if last_state:
        print(f"Symbol: {last_state['symbol']} | Qty: {last_state['position_qty']:.6f} | AvgCost: {last_state['avg_cost']:.4f}")
    
    print(f"\n--- BUG-3 (Duplicates) Check ---")
    print(f"Duplicate fills found: {len(potential_dups)}")
    for dup in potential_dups[:5]:
        print(f"  Order {dup['exchange_order_id']}: {dup['fill_count']} fills")

    conn.close()

if __name__ == "__main__":
    audit_db()
