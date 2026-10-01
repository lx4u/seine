# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import time
from typing import Optional

from seine import vault
from seine.distributed.common.transport import ws_ssl_context
from seine.distributed.common.wsclient import WsClient

# The server drops messages above 64 KiB; stay well below.
MAX_CHUNK = 32 * 1024
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0


def split_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split text into parts of at most limit UTF-8 bytes, never inside a character."""
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return [text]
    parts = []
    start = 0
    while start < len(data):
        end = min(start + limit, len(data))
        while end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(data[start:end].decode("utf-8"))
        start = end
    return parts


def redacting(send):
    """Wrap a log sender so known secret values never leave the agent."""
    def redacted(source: str, text: str) -> None:
        for secret in vault.secrets():
            text = text.replace(secret, "<redacted>")
        send(source, text)
    return redacted


class LogStreamer:
    """Stream build log chunks to the server WebSocket endpoint.

    Connection errors are caught and logged; a failed send never aborts
    the build subprocess running alongside.
    """

    def __init__(self, server_url: str, build_id: str, token: str, ca_cert: Optional[str] = None):
        ws_base = server_url.replace("http://", "ws://").replace("https://", "wss://")
        self._url = f"{ws_base}/api/v1/builds/{build_id}/stream"
        self._build_id = build_id
        self._token = token
        self._ws = None
        self._ssl = ws_ssl_context(self._url, ca_cert)
        self._backoff = BACKOFF_START
        self._retry_at = 0.0

    def connect(self) -> None:
        """Open the WebSocket connection; log and swallow on failure."""
        try:
            self._ws = WsClient()
            self._ws.connect(self._url, self._ssl)
            self._ws.send(json.dumps({"auth": self._token}))
            self._backoff = BACKOFF_START
        except Exception as exc:
            print(f"[agent] log stream unavailable for {self._build_id}: {exc}")
            self._failed()

    def _failed(self) -> None:
        self.close()
        self._retry_at = time.monotonic() + self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    def send(self, source: str, text: str) -> None:
        """Send log text in chunks; drop it while the connection is down."""
        for part in split_text(text):
            self._send_chunk(source, part)

    def _send_chunk(self, source: str, text: str) -> None:
        if self._ws is None and time.monotonic() >= self._retry_at:
            self.connect()
        if self._ws is None:
            return
        payload = {
            "build_id": self._build_id,
            "source": source,
            "text": text,
            "timestamp": time.time(),
        }
        try:
            self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            self._failed()

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def __enter__(self) -> "LogStreamer":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()
