# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Background thread that requeues the jobs of silent workers and drops stale secrets."""

from __future__ import annotations

import logging
import threading

from seine.distributed.server.db import Database
from seine.distributed.server.transient import TransientSecrets

_TERMINAL_STATES = ("completed", "failed", "cancelled")

logger = logging.getLogger("seine.server.reaper")


class Reaper(threading.Thread):
    """Call reap_stale every interval seconds until stopped."""

    def __init__(
        self,
        db: Database,
        stale_after: float,
        interval: float,
        secrets: TransientSecrets | None = None,
    ):
        super().__init__(name="seine-reaper", daemon=True)
        self.db = db
        self.stale_after = stale_after
        self.interval = interval
        self.secrets = secrets
        self._stopping = threading.Event()

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
        if self.is_alive():
            self.join()
