# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from ..base import StorageError, StorageOfflineError
from ..object_base import (
    ObjectStorageProvider,
    _compress_zstd,
    _decompress_zstd,
    _file_sha256,
    _is_digest_key,
    _safe_extract,
    _zstd_reader,
    _zstd_writer,
)
from .client import S3NotFoundError


class S3StorageProvider(ObjectStorageProvider):
    """Storage provider backed by S3/Garage network object storage."""

    supports_lifecycle = True
    not_found_errors = (S3NotFoundError,)

    def __init__(self, client, bucket, prefix="cache",
                 offline_mode="fallback", cache_rootfs=False, options=None):
        super().__init__(bucket, prefix, offline_mode, cache_rootfs, options)
        self.client = client

    def _head_object(self, key):
        """Return {"sha256", "size"} for key, None if it is missing."""
        headers = self.client.head_object(self.bucket, key)
        if headers is None:
            return None
        return {
            "sha256": (headers.get("x-amz-meta-sha256") or "").lower() or None,
            "size": int(headers.get("content-length", 0)),
        }

    def _get_object(self, key):
        return self.client.get_object(self.bucket, key)

    def _download_object(self, key, target_path):
        self.client.download_file(self.bucket, key, target_path)

    def _put_small(self, key, data):
        self.client.put_object(self.bucket, key, data)

    def _upload_file(self, key, file_path, sha256=None):
        return self.client.upload_file(self.bucket, key, file_path)

    def _delete_keys(self, keys):
        self.client.delete_objects(self.bucket, keys)

    def _iter_objects(self, prefix=""):
        return self.client.list_all_objects(self.bucket, prefix)

    def ensure_bucket(self):
        """Verify bucket exists, attempting creation if permitted."""
        try:
            self.client.head_bucket(self.bucket)
        except S3NotFoundError:
            try:
                self.client.create_bucket(self.bucket)
            except Exception as e:
                raise StorageError(f"could not create bucket '{self.bucket}': {e}") from e
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 bucket check failed for {self.bucket}: {e}") from e

    def purge(self):
        """Empty the bucket: every object and incomplete upload; return (count, bytes)."""
        count, size = super().purge()
        for key, upload_id in list(self.client.list_multipart_uploads(self.bucket)):
            self.client.abort_multipart_upload(self.bucket, key, upload_id)
        return count, size

    def lifecycle_rules(self):
        return self.client.get_bucket_lifecycle(self.bucket)

    def set_lifecycle_rules(self, rules):
        self.client.put_bucket_lifecycle(self.bucket, rules)

    def refresh_worktree(self, project: str, digest: str) -> bool:
        """Restart the age of a staged worktree; False if it is not staged."""
        return self.client.refresh_object(self.bucket, f"worktrees/{project}/{digest}.tar.zst")

    def generate_download_url(self, project: str, key_or_artifact: str,
                              expires_in: int = 3600) -> str:
        """Generate a temporary direct download URL for an artifact."""
        clean = key_or_artifact.lstrip("/")
        if clean.startswith("artifacts/"):
            key = clean
        else:
            key = f"artifacts/{project}/{clean}"

        try:
            return self.client.presign_get(self.bucket, key, expires_in)
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(
                    f"s3 generate_download_url failed for {key}: {e}"
                ) from e
            raise StorageError(f"s3 generate_download_url failed for {key}: {e}") from e
