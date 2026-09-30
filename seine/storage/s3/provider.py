# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import contextlib
import datetime
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile

from ..base import StorageProvider, StorageError, StorageOfflineError
from .client import S3Client, S3NotFoundError, S3ConditionFailedError
from seine.cache import check_clean_chroot, CleanChrootViolation


# Compresses payload using zstandard level 3.
def _compress_zstd(data, level=3):
    try:
        import zstandard as zstd
        cctx = zstd.ZstdCompressor(level=level)
        return cctx.compress(data)
    except ImportError:
        proc = subprocess.Popen(
            ["zstd", f"-{level}", "-c"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = proc.communicate(input=data)
        return out


# Decompresses zstandard payload.
def _decompress_zstd(data):
    try:
        import zstandard as zstd
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data)
    except ImportError:
        proc = subprocess.Popen(
            ["zstd", "-dc"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = proc.communicate(input=data)
        return out


class S3StorageProvider(StorageProvider):
    """Storage provider backed by S3/Garage network object storage."""

    def __init__(self, client, bucket, prefix="cache",
                 offline_mode="fallback", cache_rootfs=False, options=None):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.offline_mode = offline_mode
        self.cache_rootfs = cache_rootfs
        self.options = options or {}

    def _object_key(self, kind, key, ext=".tar.zst"):
        return f"{self.prefix}/{kind}/{key}{ext}"

    def _recipe_key(self, kind, key):
        return f"{self.prefix}/{kind}/{key}.recipe"

    def _touch_key(self, kind, key):
        return f"{self.prefix}/{kind}/{key}.touch"

    def push(self, kind, key, path, spec=None, recipe=None):
        """Compress, verify clean-chroot, and push object to S3."""
        if kind in ("chroot", "rootfs") and isinstance(path, str):
            try:
                check_clean_chroot(path, spec=spec)
            except CleanChrootViolation as e:
                from seine.cache_index import say
                say(self.options, f"push {kind} {key} refused: {e}")
                raise

        if isinstance(path, (bytes, bytearray)):
            payload = _compress_zstd(path)
        elif os.path.isfile(path) and path.endswith(".zst"):
            with open(path, "rb") as f:
                payload = f.read()
        elif os.path.isfile(path):
            with open(path, "rb") as f:
                payload = _compress_zstd(f.read())
        elif os.path.isdir(path):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                tar.add(path, arcname=".")
            payload = _compress_zstd(buf.getvalue())
        else:
            raise StorageError(f"push failed: path '{path}' does not exist")

        sha256 = hashlib.sha256(payload).hexdigest()
        obj_key = self._object_key(kind, key)
        metadata = {"sha256": sha256}

        try:
            self.client.put_object(self.bucket, obj_key, payload,
                                   metadata=metadata, if_none_match=True)
        except S3ConditionFailedError:
            pass
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 push failed for {obj_key}: {e}") from e
            from seine.cache_index import say
            say(self.options, f"push {kind} {key} failed: {e}")
            return False

        if recipe:
            recipe_body = "".join(f"{label}\t{val}\n" for label, val in recipe).encode()
            with contextlib.suppress(Exception):
                self.client.put_object(self.bucket, self._recipe_key(kind, key), recipe_body)

        self.touch(kind, key)
        return True

    def pull(self, kind, key, dest=None):
        """Pull cached object from S3 into local storage."""
        obj_key = self._object_key(kind, key)
        try:
            if not self.client.head_object(self.bucket, obj_key):
                return None
            data = self.client.get_object(self.bucket, obj_key)
        except S3NotFoundError:
            return None
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 pull failed for {obj_key}: {e}") from e
            return None

        if dest is None:
            return _decompress_zstd(data)

        if dest.endswith(".zst"):
            decompressed = data
        else:
            decompressed = _decompress_zstd(data)

        if os.path.isdir(dest):
            with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r|*") as tar:
                tar.extractall(dest)
            for derived in ["Packages", "Packages.gz", "Sources", "Sources.gz"]:
                p = os.path.join(dest, derived)
                if not os.path.exists(p):
                    open(p, "wb").close()
        else:
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            temp_dest = f"{dest}.partial"
            with open(temp_dest, "wb") as f:
                f.write(decompressed)
            os.replace(temp_dest, dest)

        with contextlib.suppress(Exception):
            recipe_data = self.client.get_object(self.bucket, self._recipe_key(kind, key))
            if recipe_data and not os.path.isdir(dest):
                recipe_dest = f"{dest[:-7]}.recipe" if dest.endswith(".digest") else f"{dest}.recipe"
                with open(recipe_dest, "wb") as f:
                    f.write(recipe_data)

        self.touch(kind, key)
        return dest

    def touch(self, kind, key):
        """Update heartbeat timestamp file on S3."""
        touch_key = self._touch_key(kind, key)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().encode()
        try:
            self.client.put_object(self.bucket, touch_key, now)
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 touch failed for {touch_key}: {e}") from e

    def explain(self, kind, key):
        """Retrieve and parse remote .recipe sidecar from S3."""
        recipe_key = self._recipe_key(kind, key)
        try:
            data = self.client.get_object(self.bucket, recipe_key)
            recipe = []
            for line in data.decode("utf-8", errors="ignore").splitlines():
                if not line:
                    continue
                label, _, val = line.partition("\t")
                recipe.append((label, val))
            return recipe
        except S3NotFoundError:
            return None
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 explain failed for {recipe_key}: {e}") from e
            return None

    def ensure_bucket(self):
        """Verify bucket exists, attempting creation if permitted."""
        try:
            self.client.head_bucket(self.bucket)
        except S3NotFoundError:
            try:
                self.client._request("PUT", self.bucket)
            except Exception as e:
                raise StorageError(f"could not create bucket '{self.bucket}': {e}") from e
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 bucket check failed for {self.bucket}: {e}") from e
