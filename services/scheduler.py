"""
Task scheduler for periodic bot operations.

Uses APScheduler AsyncIOScheduler for native async support
within the bot's event loop: health pings, order sync, daily summaries,
equity recording, and log cleanup.
"""

from __future__ import annotations

from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger


class TaskScheduler:
    """
    Async-native task scheduler for periodic operations.

    Uses AsyncIOScheduler to run jobs directly on the event loop
    instead of spawning background threads, avoiding thread-safety issues.
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 60,
            }
        )
        self._running = False
        logger.info("TaskScheduler initialized (async mode)")

    def add_interval_job(
        self,
        func: Callable,
        seconds: int,
        job_id: str,
        name: Optional[str] = None,
    ) -> None:
        """
        Add a job that runs at a fixed interval.

        Args:
            func: Function to execute (can be sync or async).
            seconds: Interval in seconds.
            job_id: Unique job identifier.
            name: Human-readable job name.
        """
        self._scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=seconds),
            id=job_id,
            name=name or job_id,
            replace_existing=True,
        )
        logger.info(f"Scheduled job '{job_id}' every {seconds}s")

    def add_daily_job(
        self,
        func: Callable,
        hour: int,
        minute: int,
        job_id: str,
        name: Optional[str] = None,
    ) -> None:
        """
        Add a job that runs daily at a specific time (UTC).

        Args:
            func: Function to execute (can be sync or async).
            hour: Hour (0-23 UTC).
            minute: Minute (0-59).
            job_id: Unique job identifier.
            name: Human-readable job name.
        """
        self._scheduler.add_job(
            func,
            trigger=CronTrigger(hour=hour, minute=minute),
            id=job_id,
            name=name or job_id,
            replace_existing=True,
        )
        logger.info(f"Scheduled daily job '{job_id}' at {hour:02d}:{minute:02d} UTC")

    def start(self) -> None:
        """Start the scheduler."""
        if not self._running:
            self._scheduler.start()
            self._running = True
            logger.info("TaskScheduler started")

    def stop(self) -> None:
        """Stop the scheduler gracefully."""
        if self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            logger.info("TaskScheduler stopped")

    def remove_job(self, job_id: str) -> None:
        """Remove a scheduled job."""
        try:
            self._scheduler.remove_job(job_id)
            logger.info(f"Removed job '{job_id}'")
        except Exception:
            logger.debug(f"Job '{job_id}' not found for removal")

    @property
    def is_running(self) -> bool:
        """Whether the scheduler is active."""
        return self._running

    def get_jobs_info(self) -> list[dict]:
        """Get information about all scheduled jobs."""
        jobs = self._scheduler.get_jobs()
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            for job in jobs
        ]
