# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Build a storage provider from explicit S3 settings, never from ambient credentials."""

from __future__ import annotations

from typing import Any, Optional

from seine.distributed.common.models import JobS3


def provider_from(s3: JobS3, options: Optional[dict[str, Any]] = None) -> Any:
    """Return an S3 storage provider for one bucket with the given key pair."""
    from seine.storage.s3 import S3StorageProvider
    from seine.storage.s3.client import S3Client

    client = S3Client(s3.endpoint, s3.access_key, s3.secret_key, region=s3.region)
    return S3StorageProvider(client, s3.bucket, options=options or {})
