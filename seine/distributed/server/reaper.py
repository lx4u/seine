# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Background thread that requeues the jobs of silent workers and drops stale secrets."""

from __future__ import annotations

import logging
import threading
import time

from seine.distributed.server.db import Database
from seine.distributed.server.housekeeping import HousekeepingBusy, UnknownProject, run_housekeeping
from seine.distributed.server.settings import Settings
from seine.distributed.server.transient import TransientSecrets

_TERMINAL_STATES = ("completed", "failed", "cancelled")
_STOP_TIMEOUT = 30

logger = logging.getLogger("seine.server.reaper")


class Reaper(threading.Thread):
    """Call reap_stale every interval seconds until stopped.

    With a 'retention:' section it also runs storage housekeeping, in a
    second thread so that a long sweep never delays the reaping of workers.
    """

    def __init__(
        self,
        db: Database,
        stale_after: float,
        interval: float,
        secrets: TransientSecrets | None = None,
        settings: Settings | None = None,
    ):
        super().__init__(name="seine-reaper", daemon=True)
        self.db = db
        self.stale_after = stale_after
        self.interval = interval
        self.secrets = secrets
        self.settings = settings
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._housekeeper = None
        if settings is not None and settings.retention is not None:
            self._housekeeper = threading.Thread(
                target=self._housekeeping_loop, name="seine-housekeeping", daemon=True
            )

    def start(self) -> None:
        super().start()
        if self._housekeeper is not None:
            self._housekeeper.start()

    def request_housekeeping(self, project: str) -> None:
        """Ask for a sweep of one project soon; requests for the same project coalesce."""
        if self._housekeeper is None:
            return
        with self._pending_lock:
            self._pending.add(project)
        self._wake.set()

    def _housekeeping_loop(self) -> None:
        interval = self.settings.retention.interval
        next_sweep = time.monotonic() + interval
        while not self._stopping.is_set():
            self._wake.wait(max(0.0, next_sweep - time.monotonic()))
            if self._stopping.is_set():
                break
            self._wake.clear()
            with self._pending_lock:
                projects, self._pending = self._pending, set()
            if time.monotonic() >= next_sweep:
                projects = {None}
                next_sweep = time.monotonic() + interval
            for project in sorted(projects, key=str):
                self._sweep(project)

    def _sweep(self, project: str | None) -> None:
        try:
            run_housekeeping(self.db, self.settings, project=project)
        except HousekeepingBusy:
            # Retry a requested project on the next wake; the periodic sweep covers the rest.
            logger.info("Housekeeping is busy, skipping %s", project or "all projects")
            if project is not None:
                with self._pending_lock:
                    self._pending.add(project)
        except UnknownProject:
            logger.info("Project %s was deleted before its sweep", project)
        except Exception:
            logger.exception("Housekeeping failed for %s", project or "all projects")

    def drop_stale_secrets(self) -> None:
        """Drop the secrets that expired or belong to a build that is over."""
        self.secrets.purge_expired()
        for build_id in self.secrets.build_ids():
            build = self.db.get_build(build_id)
            if build is None or build["status"] in _TERMINAL_STATES:
                self.secrets.pop(build_id)

    def run(self) -> None:
        while not self._stopping.wait(self.interval):
            if self.secrets is not None:
                try:
                    self.drop_stale_secrets()
                except Exception:
                    logger.exception("Dropping stale build secrets failed")
            try:
                reaped = self.db.scheduler.reap_stale(stale_after=self.stale_after)
            except Exception:
                logger.exception("Reaping stale workers failed")
                continue
            if reaped:
                logger.warning("Requeued jobs of stale workers: %s", ", ".join(reaped))

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self.is_alive():
            self.join()
        if self._housekeeper is not None and self._housekeeper.is_alive():
            self._housekeeper.join(_STOP_TIMEOUT)
            if self._housekeeper.is_alive():
                logger.warning("Housekeeping is still running after %d s, not waiting for it", _STOP_TIMEOUT)
