import sqlite3
import argparse
from pathlib import Path
from loguru import logger

def migrate_db(db_path: Path):
    if not db_path.exists():
        logger.error(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    # Check if columns already exist to make script idempotent
    cur.execute("PRAGMA table_info(grid_states)")
    columns = [col[1] for col in cur.fetchall()]
    
    updates = 0
    if "position_qty" not in columns:
        logger.info("Adding 'position_qty' column to grid_states...")
        cur.execute("ALTER TABLE grid_states ADD COLUMN position_qty REAL NOT NULL DEFAULT 0.0")
        updates += 1
        
    if "avg_cost" not in columns:
        logger.info("Adding 'avg_cost' column to grid_states...")
        cur.execute("ALTER TABLE grid_states ADD COLUMN avg_cost REAL NOT NULL DEFAULT 0.0")
        updates += 1
        
    if updates > 0:
        conn.commit()
        logger.success(f"Migration completed successfully. Added {updates} columns.")
    else:
        logger.info("Database is already up to date with v3 schema. No changes made.")
        
    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate database to v3 schema")
    parser.add_argument("--db", type=str, default="data/trading_bot.db", help="Path to SQLite DB")
    args = parser.parse_args()
    
    db_path = Path(args.db).resolve()
    logger.info(f"Starting migration on {db_path}...")
    migrate_db(db_path)
