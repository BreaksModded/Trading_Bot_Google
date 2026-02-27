"""
External health monitoring via healthchecks.io.

Sends periodic pings to a healthchecks.io endpoint. If the ping
stops arriving, healthchecks.io sends an alert via email/webhook.
"""

from __future__ import annotations

import time

import httpx
from loguru import logger


class HealthMonitor:
    """
    Health monitoring integration with healthchecks.io.

    Sends HTTP GET pings at configured intervals. The healthchecks.io
    service will alert if pings stop arriving.

    Args:
        url: Healthchecks.io ping URL.
        interval_seconds: Seconds between pings.
    """

    def __init__(
        self,
        *,
        url: str = "",
        interval_seconds: int = 60,
    ) -> None:
        self._ping_url = url
        self._enabled = bool(url)
        self._interval = interval_seconds
        self._consecutive_failures = 0
        self._max_failures = 5
        self._last_ping: float = 0.0

        if self._enabled:
            logger.info(f"HealthMonitor enabled (interval: {self._interval}s)")
        else:
            logger.info("HealthMonitor disabled (no ping URL configured)")

    async def ping_if_due(self) -> bool:
        """Ping only if enough time has elapsed since the last ping."""
        if not self._enabled:
            return True
        now = time.monotonic()
        if now - self._last_ping < self._interval:
            return True
        self._last_ping = now
        return await self.ping_async()

    async def ping_async(self) -> bool:
        """Send an async health check ping."""
        if not self._enabled:
            return True

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(self._ping_url)
                if response.status_code == 200:
                    self._consecutive_failures = 0
                    logger.debug("Health ping sent successfully")
                    return True
                else:
                    self._consecutive_failures += 1
                    logger.warning(
                        f"Health ping returned {response.status_code} "
                        f"({self._consecutive_failures}/{self._max_failures})"
                    )
                    return False
        except Exception as e:
            self._consecutive_failures += 1
            logger.warning(f"Health ping failed: {e}")
            return False

    def ping_fail(self, message: str = "") -> bool:
        """Signal a failure to healthchecks.io."""
        if not self._enabled:
            return True
        try:
            url = f"{self._ping_url}/fail"
            with httpx.Client(timeout=10) as client:
                client.post(url, content=message)
                return True
        except Exception as e:
            logger.warning(f"Health fail ping failed: {e}")
            return False

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def interval(self) -> int:
        return self._interval
