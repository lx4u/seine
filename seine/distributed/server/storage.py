# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Per-project, per-environment storage credentials held by the server."""

from __future__ import annotations

from typing import Any

from seine.distributed.common.models import JobArtifactory, JobS3, JobStorage
from seine.distributed.common.storage import provider_from
from seine.distributed.server.settings import Settings


class StorageCredentialsError(ValueError):
    """No storage credentials are configured for the project and environment."""


def env_name(is_release: bool) -> str:
    return "prod" if is_release else "dev"


def storage_type(settings: Settings) -> str:
    """The shared storage backend: 's3' unless the site runs Artifactory."""
    return getattr(settings, "storage_type", "s3") or "s3"


def job_storage(settings: Settings, project: str, bucket: str, env: str) -> JobStorage:
    """Return the access block for one bucket; raise StorageCredentialsError without credentials."""
    if storage_type(settings) == "artifactory":
        keys = settings.artifactory_keys(project, env)
        if not settings.artifactory_endpoint or keys is None:
            raise StorageCredentialsError(
                f"no Artifactory {env} credentials configured for project '{project}'"
            )
        return JobArtifactory(
            endpoint=settings.artifactory_endpoint, bucket=bucket,
            token=keys.get("token"), user=keys.get("user"), password=keys.get("password"),
        )
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
    """Return a storage provider using the project's credentials for env."""
    return provider_from(job_storage(settings, project, bucket, env))
