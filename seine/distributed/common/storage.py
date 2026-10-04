# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Build a storage provider from a job's explicit access block, never from ambient credentials."""

from __future__ import annotations

from typing import Any, Optional

from seine.distributed.common.models import JobArtifactory, JobS3, JobStorage


def provider_from(storage: JobStorage, options: Optional[dict[str, Any]] = None) -> Any:
    """Return the storage provider for one bucket with the job's credentials."""
    if isinstance(storage, JobArtifactory):
        from seine.storage.artifactory import ArtifactoryStorageProvider
        from seine.storage.artifactory.client import ArtifactoryClient

        client = ArtifactoryClient(
            storage.endpoint, storage.bucket,
            user=storage.user, password=storage.password, token=storage.token)
        return ArtifactoryStorageProvider(client, storage.bucket, options=options or {})

    from seine.storage.s3 import S3StorageProvider
    from seine.storage.s3.client import S3Client

    client = S3Client(storage.endpoint, storage.access_key, storage.secret_key, region=storage.region)
    return S3StorageProvider(client, storage.bucket, options=options or {})


def secret_values(storage: JobStorage) -> list[Optional[str]]:
    """The values to keep out of logs."""
    if isinstance(storage, JobArtifactory):
        return [storage.token, storage.password]
    return [storage.access_key, storage.secret_key]


def child_env(storage: JobStorage) -> dict[str, str]:
    """Environment the child 'seine build' resolves its credentials from."""
    if isinstance(storage, JobArtifactory):
        env = {"SEINE_ARTIFACTORY_TOKEN": storage.token} if storage.token else {}
        if storage.user is not None:
            env["SEINE_ARTIFACTORY_USER"] = storage.user
            env["SEINE_ARTIFACTORY_PASSWORD"] = storage.password or ""
        return env
    return {"AWS_ACCESS_KEY_ID": storage.access_key, "AWS_SECRET_ACCESS_KEY": storage.secret_key}


def child_flags(storage: JobStorage) -> list[str]:
    """'seine build' flags that point the child at the job's storage."""
    if isinstance(storage, JobArtifactory):
        return ["--shared-cache", "--storage-backend=artifactory",
                f"--artifactory-endpoint={storage.endpoint}",
                f"--artifactory-repo={storage.bucket}"]
    return ["--shared-cache", f"--s3-endpoint={storage.endpoint}",
            f"--s3-bucket={storage.bucket}", f"--s3-region={storage.region}"]
