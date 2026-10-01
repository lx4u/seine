# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import contextlib
import datetime
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile

from typing import Optional

from ..base import StorageProvider, StorageError, StorageNotFoundError, StorageOfflineError
from .client import S3NotFoundError
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


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_DIGEST_RE = re.compile(r"[0-9a-f]{16,64}")


# Digest keys never change; other keys are aliases that must be overwritten.
def _is_digest_key(key):
    return _DIGEST_RE.fullmatch(key.rsplit("/", 1)[-1]) is not None


@contextlib.contextmanager
def _zstd_writer(path, level=3):
    with open(path, "wb") as out:
        try:
            import zstandard as zstd
        except ImportError:
            proc = subprocess.Popen(["zstd", f"-{level}", "-c"],
                                    stdin=subprocess.PIPE, stdout=out)
            try:
                yield proc.stdin
            finally:
                proc.stdin.close()
                rc = proc.wait()
            if rc:
                raise StorageError(f"zstd compression failed ({rc})")
        else:
            cctx = zstd.ZstdCompressor(level=level)
            with cctx.stream_writer(out, closefd=False) as writer:
                yield writer


# The sha256 is checked before reading, so the subprocess exit code is ignored.
@contextlib.contextmanager
def _zstd_reader(path):
    with open(path, "rb") as src:
        try:
            import zstandard as zstd
        except ImportError:
            proc = subprocess.Popen(["zstd", "-dc"], stdin=src,
                                    stdout=subprocess.PIPE)
            try:
                yield proc.stdout
            finally:
                proc.stdout.close()
                proc.wait()
        else:
            with zstd.ZstdDecompressor().stream_reader(src) as reader:
                yield reader


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Refuses path traversal, links out of dest and devices.
def _safe_extract(tar, dest):
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")
        return
    root = os.path.abspath(dest)
    for member in tar:
        target = os.path.abspath(os.path.join(root, member.name))
        if not (target == root or target.startswith(root + os.sep)):
            raise tarfile.TarError(f"path traversal: {member.name}")
        if member.isdev() or member.issym() or member.islnk():
            raise tarfile.TarError(f"unsupported member: {member.name}")
        tar.extract(member, root)


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

        if not isinstance(path, (bytes, bytearray)) and not os.path.exists(path):
            raise StorageError(f"push failed: path '{path}' does not exist")

        obj_key = self._object_key(kind, key)
        try:
            # Garage ignores If-None-Match, so skip an existing digest key here
            if not (_is_digest_key(key) and self.client.head_object(self.bucket, obj_key)):
                self._upload(obj_key, path)
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

    def _upload(self, obj_key, path):
        """Compress path into a temp file and stream it to obj_key."""
        if not isinstance(path, (bytes, bytearray)) \
                and os.path.isfile(path) and path.endswith(".zst"):
            self.client.upload_file(self.bucket, obj_key, path)
            return
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = os.path.join(tmpdir, "object.zst")
            with _zstd_writer(archive) as out:
                if isinstance(path, (bytes, bytearray)):
                    out.write(path)
                elif os.path.isfile(path):
                    with open(path, "rb") as f:
                        shutil.copyfileobj(f, out)
                else:
                    with tarfile.open(fileobj=out, mode="w|") as tar:
                        tar.add(path, arcname=".")
            self.client.upload_file(self.bucket, obj_key, archive)

    def pull(self, kind, key, dest=None):
        """Pull cached object from S3 into local storage."""
        obj_key = self._object_key(kind, key)
        parent = None
        if dest is not None:
            parent = os.path.dirname(os.path.abspath(dest.rstrip(os.sep) or dest))
            os.makedirs(parent, exist_ok=True)
        # next to dest: the archive can be large and /tmp may be a tmpfs
        fd, archive = tempfile.mkstemp(prefix=".pull-", dir=parent)
        os.close(fd)
        try:
            try:
                headers = self.client.head_object(self.bucket, obj_key)
                if not headers:
                    return None
                self.client.download_file(self.bucket, obj_key, archive)
            except S3NotFoundError:
                return None
            except Exception as e:
                if self.offline_mode == "strict":
                    raise StorageOfflineError(f"s3 pull failed for {obj_key}: {e}") from e
                return None

            self._verify(kind, key, headers, archive)
            result = self._unpack(kind, key, archive, dest)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(archive)

        if dest is None:
            return result

        with contextlib.suppress(Exception):
            recipe_data = self.client.get_object(self.bucket, self._recipe_key(kind, key))
            if recipe_data and not os.path.isdir(dest):
                recipe_dest = f"{dest[:-7]}.recipe" if dest.endswith(".digest") else f"{dest}.recipe"
                with open(recipe_dest, "wb") as f:
                    f.write(recipe_data)

        self.touch(kind, key)
        return dest

    def _verify(self, kind, key, headers, archive):
        """Reject an archive whose sha256 is missing or differs from its metadata."""
        expected = (headers or {}).get("x-amz-meta-sha256")
        if not expected:
            reason = "no sha256 recorded for the object"
        elif _file_sha256(archive) != expected.lower():
            reason = "sha256 does not match the stored object"
        else:
            return
        from seine.cache_index import say
        say(self.options, f"pull {kind} {key} refused: {reason}")
        raise StorageError(f"pull {kind} {key} refused: {reason}")

    def _unpack(self, kind, key, archive, dest):
        """Place a verified archive at dest (bytes returned when dest is None)."""
        try:
            if dest is None:
                with _zstd_reader(archive) as reader:
                    return reader.read()
            if dest.endswith(".zst"):
                os.replace(archive, dest)
            elif os.path.isdir(dest):
                with _zstd_reader(archive) as reader, \
                        tarfile.open(fileobj=reader, mode="r|*") as tar:
                    _safe_extract(tar, dest)
                for derived in ["Packages", "Packages.gz", "Sources", "Sources.gz"]:
                    p = os.path.join(dest, derived)
                    if not os.path.exists(p):
                        open(p, "wb").close()
            else:
                temp_dest = f"{dest}.partial"
                with _zstd_reader(archive) as reader, open(temp_dest, "wb") as f:
                    shutil.copyfileobj(reader, f)
                os.replace(temp_dest, dest)
        except tarfile.TarError as e:
            from seine.cache_index import say
            say(self.options, f"pull {kind} {key} refused: {e}")
            raise StorageError(f"pull {kind} {key} refused: {e}") from e

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
                self.client.create_bucket(self.bucket)
            except Exception as e:
                raise StorageError(f"could not create bucket '{self.bucket}': {e}") from e
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 bucket check failed for {self.bucket}: {e}") from e

    def push_worktree(self, project: str, digest: str, path: str):
        """Push a staged project worktree archive to S3."""
        if not isinstance(path, (bytes, bytearray)) and not os.path.exists(path):
            raise StorageError(f"push_worktree failed: path '{path}' does not exist")

        key = f"worktrees/{project}/{digest}.tar.zst"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                archive = self._worktree_archive(path, tmpdir)
                if _file_sha256(archive) != digest.lower():
                    raise StorageError(f"push_worktree {project}: archive does not match {digest}")
                # Garage ignores If-None-Match, so skip an existing digest key here
                if not self.client.head_object(self.bucket, key):
                    self.client.upload_file(self.bucket, key, archive)
        except StorageError:
            raise
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 push_worktree failed for {key}: {e}") from e
            from seine.cache_index import say
            say(self.options, f"push_worktree {project} {digest} failed: {e}")
            return False

        return True

    def has_worktree(self, project: str, digest: str) -> bool:
        """Return True if the worktree bundle is staged in the bucket."""
        return bool(self.client.head_object(self.bucket, f"worktrees/{project}/{digest}.tar.zst"))

    @staticmethod
    def _worktree_archive(path, tmpdir):
        """Return a .tar.zst file for bytes, a file or a directory."""
        archive = os.path.join(tmpdir, "worktree.tar.zst")
        if not isinstance(path, (bytes, bytearray)) and os.path.isdir(path):
            from seine.distributed.client.worktree import pack_worktree
            pack_worktree(path, out_path=archive)
            return archive
        if isinstance(path, (bytes, bytearray)):
            payload, path = path, os.path.join(tmpdir, "payload")
            with open(path, "wb") as f:
                f.write(payload)
        with open(path, "rb") as f:
            if f.read(4) == _ZSTD_MAGIC:
                return path
            f.seek(0)
            with _zstd_writer(archive) as out:
                shutil.copyfileobj(f, out)
        return archive

    def pull_worktree(self, project: str, digest: str, dest_dir: str):
        """Pull a staged project worktree, check its sha256 and unpack it into dest_dir."""
        key = f"worktrees/{project}/{digest}.tar.zst"
        dest_dir = os.path.abspath(dest_dir)
        parent = os.path.dirname(dest_dir)
        os.makedirs(parent, exist_ok=True)
        # next to dest: the bundle can be large and /tmp may be a tmpfs
        fd, tmp_path = tempfile.mkstemp(prefix=".pull-", dir=parent)
        os.close(fd)
        try:
            try:
                headers = self.client.head_object(self.bucket, key)
                if not headers:
                    return None
                self.client.download_file(self.bucket, key, tmp_path)
            except S3NotFoundError:
                return None
            except Exception as e:
                if self.offline_mode == "strict":
                    raise StorageOfflineError(f"s3 pull_worktree failed for {key}: {e}") from e
                return None

            self._verify("worktree", key, headers, tmp_path)
            if headers["x-amz-meta-sha256"].lower() != digest.lower():
                raise StorageError(f"pull worktree {key} refused: sha256 is not {digest}")

            from seine.distributed.client.worktree import unpack_worktree
            return unpack_worktree(tmp_path, dest_dir)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def push_artifact(self, project: str, build_id: str, file_path: str,
                      artifact_name: Optional[str] = None) -> str:
        """Upload a build deliverable to S3 under artifacts prefix."""
        return self.push_artifact_info(project, build_id, file_path, artifact_name)["key"]

    def push_artifact_info(self, project: str, build_id: str, file_path: str,
                           artifact_name: Optional[str] = None) -> dict:
        """Upload a deliverable and return its name, key, sha256 and size."""
        if not os.path.isfile(file_path):
            raise StorageError(f"push_artifact failed: path '{file_path}' does not exist")
        name = artifact_name or os.path.basename(file_path)
        key = f"artifacts/{project}/{build_id}/{name}"
        try:
            sha256 = self.client.upload_file(self.bucket, key, file_path)
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 push_artifact failed for {key}: {e}") from e
            raise StorageError(f"s3 push_artifact failed for {key}: {e}") from e
        return {"name": name, "key": key, "sha256": sha256, "size": os.path.getsize(file_path)}

    def pull_artifact(self, project: str, build_id: str, artifact_name: str,
                      dest_path: str) -> str:
        """Download a build deliverable from S3 to dest_path."""
        key = f"artifacts/{project}/{build_id}/{artifact_name}"
        if os.path.isdir(dest_path) or dest_path.endswith(os.sep):
            dest_file = os.path.join(dest_path, artifact_name)
        else:
            dest_file = dest_path
        parent = os.path.dirname(os.path.abspath(dest_file))
        os.makedirs(parent, exist_ok=True)
        # next to dest: the file can be large and /tmp may be a tmpfs
        fd, tmp_path = tempfile.mkstemp(prefix=".pull-", dir=parent)
        os.close(fd)
        try:
            headers = self.client.head_object(self.bucket, key)
            if not headers:
                raise S3NotFoundError(f"{key} does not exist")
            self.client.download_file(self.bucket, key, tmp_path)
        except S3NotFoundError as e:
            raise StorageNotFoundError(f"s3 artifact not found: {key}") from e
        except Exception as e:
            if self.offline_mode == "strict":
                raise StorageOfflineError(f"s3 pull_artifact failed for {key}: {e}") from e
            raise StorageError(f"s3 pull_artifact failed for {key}: {e}") from e
        else:
            self._verify("artifact", key, headers, tmp_path)
            os.replace(tmp_path, dest_file)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)
        return dest_file

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
