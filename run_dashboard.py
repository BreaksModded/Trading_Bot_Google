"""
Dashboard runner — Entry point for the web dashboard.

Usage:
    python run_dashboard.py
"""

from __future__ import annotations

import uvicorn
from config.settings import load_settings


def main() -> None:
    """Start the dashboard server."""
    settings = load_settings()
    print(f">> Starting Dashboard at http://{settings.dashboard.host}:{settings.dashboard.port}")
    print(f"   Mode: {'TESTNET' if settings.exchange.testnet else 'MAINNET'}")

    uvicorn.run(
        "api.app:app",
        host=settings.dashboard.host,
        port=settings.dashboard.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
