# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""In-memory store of build secrets: never persisted, dropped after a time to live."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterator


class TransientSecrets:
    """Secrets by build id, each expiring ttl seconds after it was stored."""

    def __init__(self, ttl: float, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def __setitem__(self, build_id: str, secrets: dict[str, Any]) -> None:
        with self._lock:
            self._items[build_id] = (self._clock() + self.ttl, dict(secrets))

    def get(self, build_id: str, default: Any = None) -> Any:
        """Return the secrets of build_id, or default once absent or expired."""
        with self._lock:
            entry = self._items.get(build_id)
            if entry is None:
                return default
            if entry[0] <= self._clock():
                del self._items[build_id]
                return default
            return entry[1]

    def __getitem__(self, build_id: str) -> dict[str, Any]:
        secrets = self.get(build_id)
        if secrets is None:
            raise KeyError(build_id)
        return secrets

    def __contains__(self, build_id: str) -> bool:
        return self.get(build_id) is not None

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"<TransientSecrets {len(self)} build(s)>"

    def pop(self, build_id: str, default: Any = None) -> Any:
        with self._lock:
            entry = self._items.pop(build_id, None)
        return default if entry is None or entry[0] <= self._clock() else entry[1]

    def build_ids(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._items))

    def purge_expired(self) -> int:
        """Drop every expired entry and return how many there were."""
        now = self._clock()
        with self._lock:
            gone = [b for b, (expires, _) in self._items.items() if expires <= now]
            for build_id in gone:
                del self._items[build_id]
        return len(gone)
