# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Streaming helpers for file uploads received by seine-server."""

from __future__ import annotations

import os
import tempfile
from typing import Any, AsyncIterator

from fastapi import HTTPException, status


async def save_upload(chunks: AsyncIterator[bytes], hasher: Any, max_bytes: int) -> tuple[str, int]:
    """Copy streamed chunks to a temp file, feeding the hasher; return (path, size)."""
    fd, path = tempfile.mkstemp(prefix="seine-upload-")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in chunks:
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Upload exceeds {max_bytes} bytes",
                    )
                hasher.update(chunk)
                out.write(chunk)
    except BaseException:
        os.unlink(path)
        raise
    return path, total
