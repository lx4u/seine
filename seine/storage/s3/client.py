# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import time

import boto3
import botocore.exceptions
from boto3.exceptions import S3UploadFailedError
from botocore.config import Config
from boto3.s3.transfer import TransferConfig

from seine import vault

_NOT_FOUND = ("404", "NoSuchKey", "NoSuchBucket", "NotFound")
_PRECONDITION = ("412", "PreconditionFailed")


class S3ClientError(Exception):
    """Raised when an S3 API operation fails."""
    def __init__(self, message, status_code=None, error_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class S3NotFoundError(S3ClientError):
    """Raised when an S3 bucket or object does not exist."""


class S3ConditionFailedError(S3ClientError):
    """Raised when an S3 conditional request fails (e.g. key already exists)."""


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class S3Client:
    """Thin wrapper over a boto3 S3 client, with the verbs seine uses."""

    def __init__(self, endpoint, access_key, secret_key, region="garage",
                 timeout=30):
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "garage"
        vault.record_secret(access_key)
        vault.record_secret(secret_key)
        # Garage's multipart SHA256 is not the S3 composite value
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=timeout, read_timeout=timeout,
            response_checksum_validation="when_required")
        self._s3 = boto3.session.Session().client(
            "s3", endpoint_url=self.endpoint, region_name=self.region,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            config=config)

    def _call(self, what, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except botocore.exceptions.ClientError as e:
            err = e.response.get("Error", {})
            code = str(err.get("Code", ""))
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            msg = f"{what} failed: {code or status} {err.get('Message', '')}".strip()
            if code in _NOT_FOUND or status == 404:
                raise S3NotFoundError(msg, status_code=404, error_code=code) from e
            if code in _PRECONDITION or status == 412:
                raise S3ConditionFailedError(msg, status_code=412, error_code=code) from e
            raise S3ClientError(msg, status_code=status, error_code=code) from e
        except (botocore.exceptions.BotoCoreError, S3UploadFailedError) as e:
            raise S3ClientError(f"{what} failed: {e}") from e

    def head_bucket(self, bucket):
        """Check if bucket exists and is accessible."""
        self._call(f"HEAD {bucket}", self._s3.head_bucket, Bucket=bucket)
        return True

    def create_bucket(self, bucket):
        self._call(f"create bucket {bucket}", self._s3.create_bucket, Bucket=bucket)

    def head_object(self, bucket, key):
        """Return object headers (user metadata as x-amz-meta-*), None if missing."""
        try:
            resp = self._call(f"HEAD {bucket}/{key}", self._s3.head_object,
                              Bucket=bucket, Key=key)
        except S3NotFoundError:
            return None
        headers = {
            "content-length": str(resp.get("ContentLength", 0)),
            "etag": resp.get("ETag", "").strip('"'),
        }
        for k, v in resp.get("Metadata", {}).items():
            headers[f"x-amz-meta-{k.lower()}"] = v
        return headers

    def exists(self, bucket, key):
        return self.head_object(bucket, key) is not None

    def get_object(self, bucket, key):
        """Fetch full object content as bytes."""
        def fetch():
            return self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return self._call(f"GET {bucket}/{key}", fetch)

    def download_file(self, bucket, key, target_path):
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        self._call(f"download {bucket}/{key}", self._s3.download_file,
                   bucket, key, target_path)

    # Garage ignores If-None-Match (a second PUT overwrites)
    def put_object(self, bucket, key, data, metadata=None, if_none_match=False):
        """Upload bytes; SHA256 is checked by the server on single PUTs."""
        body = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        args = {"Bucket": bucket, "Key": key, "Body": body,
                "ChecksumAlgorithm": "SHA256"}
        if metadata:
            args["Metadata"] = {k: str(v) for k, v in metadata.items()}
        if if_none_match:
            args["IfNoneMatch"] = "*"
        self._call(f"PUT {bucket}/{key}", self._s3.put_object, **args)

    def upload_file(self, bucket, key, file_path, metadata=None):
        """Stream a file, multipart when large; stores its sha256 and returns it."""
        meta = {k: str(v) for k, v in (metadata or {}).items()}
        meta.setdefault("sha256", _file_sha256(file_path))
        self._call(f"upload {bucket}/{key}", self._s3.upload_file,
                   file_path, bucket, key,
                   ExtraArgs={"Metadata": meta, "ChecksumAlgorithm": "SHA256"},
                   Config=TransferConfig())
        return meta["sha256"]

    def delete_object(self, bucket, key):
        try:
            self._call(f"DELETE {bucket}/{key}", self._s3.delete_object,
                       Bucket=bucket, Key=key)
        except S3NotFoundError:
            pass

    def delete_objects(self, bucket, keys):
        """Delete keys in batches of 1000; keys that are already gone are fine."""
        keys = list(keys)
        failed = []
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            resp = self._call(
                f"delete {len(batch)} objects in {bucket}", self._s3.delete_objects,
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
            # a 200 reply can still carry per-key errors
            failed += [e for e in resp.get("Errors", [])
                       if e.get("Code") not in _NOT_FOUND]
        if failed:
            names = ", ".join(e.get("Key", "?") for e in failed[:3])
            first = failed[0]
            raise S3ClientError(
                f"delete in {bucket} failed for {len(failed)} keys ({names}): "
                f"{first.get('Code', '')} {first.get('Message', '')}".strip(),
                error_code=first.get("Code"))

    def list_multipart_uploads(self, bucket):
        """Yield (key, upload_id) of every incomplete multipart upload."""
        args = {"Bucket": bucket}
        while True:
            resp = self._call(f"list uploads of {bucket}",
                              self._s3.list_multipart_uploads, **args)
            for u in resp.get("Uploads", []):
                yield u["Key"], u["UploadId"]
            if not resp.get("IsTruncated"):
                break
            args["KeyMarker"] = resp.get("NextKeyMarker")
            args["UploadIdMarker"] = resp.get("NextUploadIdMarker")

    def abort_multipart_upload(self, bucket, key, upload_id):
        try:
            self._call(f"abort upload of {bucket}/{key}", self._s3.abort_multipart_upload,
                       Bucket=bucket, Key=key, UploadId=upload_id)
        except S3NotFoundError:
            pass

    def refresh_object(self, bucket, key):
        """Restart an object's age by copying it onto itself; False if it is missing.

        A copy with REPLACE drops the metadata, so the old values are sent
        again (pulls verify sha256). S3 refuses a no-op copy, hence the marker.
        """
        headers = self.head_object(bucket, key)
        if headers is None:
            return False
        meta = {k[len("x-amz-meta-"):]: v for k, v in headers.items()
                if k.startswith("x-amz-meta-")}
        meta["refreshed"] = f"{time.time():.6f}"
        try:
            self._call(f"refresh {bucket}/{key}", self._s3.copy_object,
                       Bucket=bucket, Key=key,
                       CopySource={"Bucket": bucket, "Key": key},
                       MetadataDirective="REPLACE", Metadata=meta)
        except S3NotFoundError:
            return False
        return True

    def get_bucket_lifecycle(self, bucket):
        """Return the bucket's lifecycle rules, [] when it has none."""
        try:
            resp = self._call(f"get lifecycle of {bucket}",
                              self._s3.get_bucket_lifecycle_configuration, Bucket=bucket)
        except S3ClientError as e:
            if e.error_code == "NoSuchLifecycleConfiguration":
                return []
            raise
        return resp.get("Rules", [])

    def put_bucket_lifecycle(self, bucket, rules):
        """Replace the bucket's lifecycle rules; no rules removes the configuration."""
        if not rules:
            self._call(f"delete lifecycle of {bucket}",
                       self._s3.delete_bucket_lifecycle, Bucket=bucket)
            return
        self._call(f"put lifecycle of {bucket}",
                   self._s3.put_bucket_lifecycle_configuration,
                   Bucket=bucket, LifecycleConfiguration={"Rules": rules})

    def presign_get(self, bucket, key, expires_in=3600):
        return self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in)

    def list_objects_v2(self, bucket, prefix="", continuation_token=None, max_keys=1000):
        """List objects in bucket matching prefix."""
        args = {"Bucket": bucket, "MaxKeys": max_keys}
        if prefix:
            args["Prefix"] = prefix
        if continuation_token:
            args["ContinuationToken"] = continuation_token
        resp = self._call(f"list {bucket}", self._s3.list_objects_v2, **args)
        contents = [{
            "key": item["Key"],
            "size": item.get("Size", 0),
            "etag": item.get("ETag", "").strip('"'),
            "last_modified": item["LastModified"].isoformat() if "LastModified" in item else None,
        } for item in resp.get("Contents", [])]
        truncated = bool(resp.get("IsTruncated"))
        return {
            "contents": contents,
            "is_truncated": truncated,
            "next_continuation_token": resp.get("NextContinuationToken") if truncated else None,
        }

    def list_all_objects(self, bucket, prefix=""):
        """Yield every object (key, size, ...) matching prefix, across pages."""
        token = None
        while True:
            res = self.list_objects_v2(bucket, prefix=prefix, continuation_token=token)
            yield from res["contents"]
            if not res["is_truncated"]:
                break
            token = res["next_continuation_token"]

    def list_all_keys(self, bucket, prefix=""):
        """Yield all object keys matching prefix."""
        for item in self.list_all_objects(bucket, prefix):
            yield item["key"]
