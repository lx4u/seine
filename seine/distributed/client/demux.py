# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

"""Split a streamed build log into one file per task."""

import os
from typing import IO, Optional

GENERAL_LOG = "build.log"


class LogDemux:
    """Append task output to <log_dir>/<task>.log, anything else to build.log."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self._files: dict[str, IO[str]] = {}
        os.makedirs(log_dir, exist_ok=True)

    def write(self, text: str, task: Optional[str] = None) -> None:
        if not text:
            return
        # The task name comes off the wire: it must not leave log_dir.
        name = GENERAL_LOG if task is None else f"{task.replace(os.sep, '_')}.log"
        handle = self._files.get(name)
        if handle is None:
            handle = open(os.path.join(self.log_dir, name), "a", errors="replace")
            self._files[name] = handle
        handle.write(text)
        handle.flush()

    def close(self) -> None:
        for handle in self._files.values():
            handle.close()
        self._files.clear()
