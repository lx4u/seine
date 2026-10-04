# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from ..base import StorageError, StorageNotFoundError
from ..object_base import ObjectStorageProvider
from .client import ArtifactoryNotFoundError


class ArtifactoryStorageProvider(ObjectStorageProvider):
    """Storage provider backed by JFrog Artifactory generic repositories.

    Works on the free Community Edition: only the data plane is used
    (deploy, resolve, delete, AQL). Repo creation, properties and folder
    listings need a Pro license and stay admin-side. Touch rewrites the
    small .touch sidecar; worktree age is upload time, and the GC sweep
    spares digests of unfinished builds.
    """

    not_found_errors = (ArtifactoryNotFoundError,)

    def __init__(self, client, repo, prefix="cache",
                 offline_mode="fallback", cache_rootfs=False, options=None):
        super().__init__(repo, prefix, offline_mode, cache_rootfs, options)
        self.client = client
        self.repo = repo

    def _head_object(self, key):
        return self.client.head_object(self.repo, key)

    def _get_object(self, key):
        return self.client.get_object(self.repo, key)

    def _download_object(self, key, target_path):
        self.client.download_file(self.repo, key, target_path)

    def _put_small(self, key, data):
        self.client.put_object(self.repo, key, data)

    def _upload_file(self, key, file_path, sha256=None):
        return self.client.upload_file(self.repo, key, file_path, sha256)

    def _delete_keys(self, keys):
        self.client.delete_paths(self.repo, keys)

    def _iter_objects(self, prefix=""):
        return self.client.list_all_objects(self.repo, prefix)

    def ensure_bucket(self):
        """Check the repo exists; creation is admin-side (Pro-only REST)."""
        try:
            if self.client.repo_info(self.repo) is None:
                raise StorageError(
                    f"artifactory repo '{self.repo}' not found: "
                    f"create a generic local repo named '{self.repo}' in the UI first")
        except StorageError:
            raise
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageError(f"artifactory repo check failed for '{self.repo}': {e}") from e

    def generate_download_url(self, project: str, key_or_artifact: str,
                              expires_in: int = 3600) -> str:
        """Direct download URL; CE has no presigned URLs, so expiry is ignored."""
        clean = key_or_artifact.lstrip("/")
        if clean.startswith("artifacts/"):
            key = clean
        else:
            key = f"artifacts/{project}/{clean}"
        return f"{self.client.endpoint}/{self.repo}/{key}"
