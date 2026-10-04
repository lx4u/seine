# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Per-project, per-environment storage credentials held by the server."""

from __future__ import annotations

from typing import Any

from seine.distributed.common.models import JobS3
from seine.distributed.common.s3 import provider_from
from seine.distributed.server.settings import Settings


class StorageCredentialsError(ValueError):
    """No storage credentials are configured for the project and environment."""


def env_name(is_release: bool) -> str:
    return "prod" if is_release else "dev"


def storage_type(settings: Settings) -> str:
    """The shared storage backend: 's3' unless the site runs Artifactory."""
    return getattr(settings, "storage_type", "s3") or "s3"


def job_s3(settings: Settings, project: str, bucket: str, env: str) -> JobS3:
    """Return the S3 access for one bucket; raise StorageCredentialsError without a key pair."""
    keys = settings.s3_keys(project, env)
    if not settings.s3_endpoint or keys is None:
        raise StorageCredentialsError(
            f"no S3 {env} credentials configured for project '{project}'"
        )
    return JobS3(
        endpoint=settings.s3_endpoint,
        region=settings.s3_region,
        bucket=bucket,
        access_key=keys["access_key"],
        secret_key=keys["secret_key"],
    )


def provider_for(settings: Settings, project: str, bucket: str, env: str) -> Any:
    """Return a storage provider using the project's key pair for env."""
    if storage_type(settings) == "artifactory":
        from seine.storage.artifactory import ArtifactoryStorageProvider
        from seine.storage.artifactory.client import ArtifactoryClient

        keys = settings.artifactory_keys(project, env)
        if not settings.artifactory_endpoint or keys is None:
            raise StorageCredentialsError(
                f"no Artifactory {env} credentials configured for project '{project}'"
            )
        client = ArtifactoryClient(
            settings.artifactory_endpoint, bucket,
            user=keys.get("user"), password=keys.get("password"), token=keys.get("token"))
        return ArtifactoryStorageProvider(client, bucket)
    return provider_from(job_s3(settings, project, bucket, env))
