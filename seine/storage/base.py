# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import contextlib
import json
import os


class StorageError(Exception):
    """Base class for storage provider errors."""


class StorageNotFoundError(StorageError):
    """Raised when an artifact or cache key is not found."""


class StorageOfflineError(StorageError):
    """Raised when network storage is required but unreachable."""


class StorageProvider:
    """Abstract interface for caching and artifact storage providers."""

    def pull(self, kind, key, dest=None):
        """Pull needed cache object into local storage."""
        raise NotImplementedError

    def push(self, kind, key, path, spec=None, recipe=None):
        """Push a newly produced object to storage."""
        raise NotImplementedError

    def touch(self, kind, key):
        """Update last-used heartbeat for a cached object."""
        raise NotImplementedError

    def explain(self, kind, key):
        """Return the recorded recipe [(label, hash), ...] for key, if any."""
        raise NotImplementedError

    def ensure_bucket(self):
        """Verify storage bucket existence and accessibility."""
        pass

    def push_worktree(self, project: str, digest: str, path: str):
        """Push a staged project worktree archive to storage."""
        raise NotImplementedError

    def has_worktree(self, project: str, digest: str) -> bool:
        """Return True if the worktree bundle is staged in storage."""
        raise NotImplementedError

    def pull_worktree(self, project: str, digest: str, dest_dir: str):
        """Pull and unpack a staged project worktree from storage into dest_dir."""
        raise NotImplementedError


def for_build(options=None, spec=None):
    """Instantiate the configured StorageProvider for a build."""
    options = options or {}
    spec = spec or {}

    s3_cfg = spec.get("storage", {}).get("s3") if isinstance(spec, dict) else None
    use_s3 = options.get("s3_cache") or (s3_cfg is not None)

    if not use_s3:
        from .local import LocalStorageProvider
        return LocalStorageProvider()

    from .s3 import S3StorageProvider
    from .s3.client import S3Client
    from seine import credentials

    s3_cfg = s3_cfg or {}
    endpoint = options.get("s3_endpoint") or s3_cfg.get("endpoint")
    bucket = options.get("s3_bucket") or s3_cfg.get("bucket", "seine-cache")
    region = options.get("s3_region") or s3_cfg.get("region", "garage")
    offline_mode = options.get("s3_offline_mode", "fallback")
    cache_rootfs = options.get("cache_rootfs", False)

    if not endpoint:
        # Fall back to credentials.json if endpoint was not in spec/cli
        cfg_path = os.environ.get("SEINE_CREDENTIALS_FILE", os.path.expanduser("~/.config/seine/credentials.json"))
        if os.path.isfile(cfg_path):
            with contextlib.suppress(Exception):
                with open(cfg_path) as f:
                    data = json.load(f)
                    endpoint = data.get("s3-endpoint")
                    bucket = bucket or data.get("s3-bucket", "seine-cache")
                    region = region or data.get("s3-region", "garage")

    src = credentials.s3_credential_source(auth=s3_cfg.get("auth"))
    creds = src.get()
    client = S3Client(endpoint, creds["access_key"], creds["secret_key"], region=region)

    return S3StorageProvider(
        client, bucket, offline_mode=offline_mode, cache_rootfs=cache_rootfs, options=options)
