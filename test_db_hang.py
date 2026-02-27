import asyncio
import os
from data.database import Database
from config.settings import Settings
from datetime import datetime, UTC
from data.models import BotStatus, ConfigSnapshot

def run_db_test():
    print("Testing DB connection...")
    s = Settings()
    db = Database(s.db_full_path)
    
    print("Executing update_bot_state...")
    db.update_bot_state(
        status=BotStatus.RUNNING, started_at=datetime.now(UTC)
    )
    print("update_bot_state finished!")
    
    print("Executing save_config_snapshot...")
    db.save_config_snapshot(
        ConfigSnapshot(
            timestamp=datetime.now(UTC),
            values=s.public_dict(),
        )
    )
    print("save_config_snapshot finished!")

if __name__ == "__main__":
    run_db_test()
