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

    def push_artifact(self, project: str, build_id: str, file_path: str, artifact_name: str | None = None) -> str:
        """Push a build deliverable to storage."""
        raise NotImplementedError

    def push_artifact_info(self, project: str, build_id: str, file_path: str, artifact_name: str | None = None) -> dict:
        """Push a build deliverable and return its name, key, sha256 and size."""
        raise NotImplementedError

    def pull_artifact(self, project: str, build_id: str, artifact_name: str, dest_path: str) -> str:
        """Pull a build deliverable from storage to dest_path."""
        raise NotImplementedError

    def generate_download_url(self, project: str, key_or_artifact: str, expires_in: int = 3600) -> str:
        """Generate a temporary direct download URL for an artifact."""
        raise NotImplementedError


BaseStorageProvider = StorageProvider


def resolve_shared_cache_policy(is_release, user_flag=None):
    """Whether a remote build uses the shared cache: dev yes, release no."""
    return (not is_release) if user_flag is None else bool(user_flag)


def for_build(options=None, spec=None):
    """Instantiate the configured StorageProvider for a build."""
    options = options or {}
    spec = spec or {}
    storage_spec = spec.get("storage", {}) if isinstance(spec, dict) else {}

    s3_cfg = storage_spec.get("s3")
    arti_cfg = storage_spec.get("artifactory")
    backend = options.get("storage_backend")
    if backend is None:
        if arti_cfg is not None or options.get("artifactory_endpoint"):
            backend = "artifactory"
        elif s3_cfg is not None:
            backend = "s3"
    use_shared = options.get("shared_cache")
    if use_shared is None:
        use_shared = backend is not None

    if not use_shared:
        from .local import LocalStorageProvider
        return LocalStorageProvider()

    if backend == "artifactory" or (backend is None and arti_cfg is not None):
        return _artifactory_for_build(options, arti_cfg or {})

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


def _artifactory_for_build(options, arti_cfg):
    """Instantiate an ArtifactoryStorageProvider from options and spec."""
    from .artifactory import ArtifactoryStorageProvider
    from .artifactory.client import ArtifactoryClient
    from seine import credentials

    endpoint = options.get("artifactory_endpoint") or arti_cfg.get("endpoint")
    repo = options.get("artifactory_repo") or arti_cfg.get("repo", "seine-shared")
    prefix = arti_cfg.get("path_prefix", "cache")
    offline_mode = options.get("offline_mode", options.get("s3_offline_mode", "fallback"))
    cache_rootfs = options.get("cache_rootfs", False)

    if not endpoint:
        # Fall back to credentials.json if endpoint was not in spec/cli
        cfg_path = os.environ.get("SEINE_CREDENTIALS_FILE", os.path.expanduser("~/.config/seine/credentials.json"))
        if os.path.isfile(cfg_path):
            with contextlib.suppress(Exception):
                with open(cfg_path) as f:
                    data = json.load(f)
                    endpoint = data.get("artifactory-endpoint")
                    repo = repo or data.get("artifactory-repo", "seine-shared")

    if not endpoint:
        raise StorageError(
            "no artifactory endpoint: use --artifactory-endpoint, "
            "storage.artifactory.endpoint in the spec, or artifactory-endpoint "
            "in credentials.json")

    src = credentials.artifactory_credential_source(auth=arti_cfg.get("auth"))
    creds = src.get()
    if creds.get("token"):
        client = ArtifactoryClient(endpoint, repo, token=creds["token"])
    else:
        client = ArtifactoryClient(endpoint, repo, user=creds.get("user"),
                                   password=creds.get("password"))

    return ArtifactoryStorageProvider(
        client, repo, prefix=prefix, offline_mode=offline_mode,
        cache_rootfs=cache_rootfs, options=options)
