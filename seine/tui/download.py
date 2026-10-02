# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import threading
import time

# Seconds between two redraws asked for by a download in progress.
TICK = 0.2


def redraw(app):
    """Refresh the status bar chip and the cockpit's artifacts list; UI thread only."""
    app.refresh_indicators()
    screen = app.screen
    if getattr(screen, "active_tab", None) == 3 and hasattr(screen, "update_body"):
        screen.update_body()


class DownloadState:
    """Progress of artifact downloads: written by worker threads, read by the UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self._batch = set()
        self._last_tick = 0.0
        self._cancelled = False

    def queue(self, build_id, name, total):
        with self._lock:
            if not self._active():
                self._batch = set()
                self._cancelled = False
            key = (build_id, name)
            self._items[key] = {"state": "queued", "read": 0, "total": total or 0}
            self._batch.add(key)

    def start(self, build_id, name):
        with self._lock:
            self._items[(build_id, name)]["state"] = "downloading"

    def advance(self, build_id, name, nbytes):
        """Add nbytes read; True when the UI is due a redraw."""
        with self._lock:
            self._items[(build_id, name)]["read"] += nbytes
            now = time.monotonic()
            if now - self._last_tick < TICK:
                return False
            self._last_tick = now
            return True

    def finish(self, build_id, name, failed=False):
        with self._lock:
            item = self._items[(build_id, name)]
            item["state"] = "failed" if failed else "done"
            if not failed and item["total"]:
                item["read"] = item["total"]

    def cancel(self):
        """Ask the downloads in progress to stop; workers poll `cancelled`."""
        with self._lock:
            self._cancelled = True

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    def snapshot(self):
        with self._lock:
            return {key: dict(item) for key, item in self._items.items()}

    @property
    def active(self):
        with self._lock:
            return self._active()

    def percent(self):
        """Share of the current batch downloaded, None while no size is known."""
        with self._lock:
            items = [self._items[key] for key in self._batch]
            total = sum(item["total"] for item in items)
            if total <= 0:
                return None
            read = sum(min(item["read"], item["total"]) for item in items if item["total"])
            return min(100, read * 100 // total)

    def _active(self):
        return any(item["state"] in ("queued", "downloading") for item in self._items.values())
