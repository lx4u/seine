# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import contextlib
import datetime
import hashlib
import hmac
import os
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import requests


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


class S3Client:
    """Minimal S3 REST client with AWS SigV4 authentication."""

    def __init__(self, endpoint, access_key, secret_key, region="garage",
                 session=None, timeout=30):
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "garage"
        self.session = session or requests.Session()
        self.timeout = timeout

    # Generates AWS SigV4 signed headers for an S3 HTTP request.
    def _sign(self, method, path, query="", payload=b"", extra_headers=None):
        parsed = urllib.parse.urlsplit(self.endpoint)
        host = parsed.netloc
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        payload_hash = hashlib.sha256(payload).hexdigest()
        canonical_uri = path if path.startswith("/") else f"/{path}"
        headers = {
            "host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }
        if extra_headers:
            for k, v in extra_headers.items():
                headers[k.lower()] = v.strip()

        sorted_keys = sorted(headers.keys())
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted_keys)
        signed_headers = ";".join(sorted_keys)
        canonical_request = (
            f"{method}\n{canonical_uri}\n{query}\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )

        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )

        def hmac_sha256(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        k_date = hmac_sha256(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k_region = hmac.new(k_date, self.region.encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        sig = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        auth = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}"
        )
        headers["authorization"] = auth
        return headers

    def _request(self, method, bucket, key="", query="", payload=b"",
                 extra_headers=None, stream=False):
        clean_key = key.lstrip("/")
        path = f"/{bucket}/{clean_key}" if clean_key else f"/{bucket}"
        headers = self._sign(method, path, query=query, payload=payload,
                             extra_headers=extra_headers)
        url = f"{self.endpoint}{path}"
        if query:
            url = f"{url}?{query}"
        try:
            resp = self.session.request(
                method, url, headers=headers, data=payload if payload else None,
                stream=stream, timeout=self.timeout)
        except requests.RequestException as e:
            raise S3ClientError(f"connection error to {url}: {e}") from e

        if resp.status_code == 404:
            raise S3NotFoundError(f"not found: {path} (404)", status_code=404)
        if resp.status_code == 412:
            raise S3ConditionFailedError(f"condition failed: {path} (412)", status_code=412)
        if not resp.ok:
            raise S3ClientError(f"{method} {path} failed: HTTP {resp.status_code}",
                                status_code=resp.status_code)
        return resp

    def head_bucket(self, bucket):
        """Check if bucket exists and is accessible."""
        self._request("HEAD", bucket)
        return True

    def head_object(self, bucket, key):
        """Return headers dict if object exists, or None if not found."""
        try:
            resp = self._request("HEAD", bucket, key)
            return dict(resp.headers)
        except S3NotFoundError:
            return None

    def exists(self, bucket, key):
        """Return True if object exists."""
        return self.head_object(bucket, key) is not None

    def get_object(self, bucket, key):
        """Fetch full object content as bytes."""
        resp = self._request("GET", bucket, key)
        return resp.content

    # Streams object body to disk atomically via .partial temporary file.
    def download_file(self, bucket, key, target_path):
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        partial = f"{target_path}.partial"
        resp = self._request("GET", bucket, key, stream=True)
        failed = True
        try:
            with open(partial, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            os.replace(partial, target_path)
            failed = False
        finally:
            if failed:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(partial)

    def put_object(self, bucket, key, data, metadata=None, if_none_match=False):
        """Upload raw bytes to S3 object."""
        payload = data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8")
        headers = {}
        if if_none_match:
            headers["if-none-match"] = "*"
        if metadata:
            for k, v in metadata.items():
                headers[f"x-amz-meta-{k.lower()}"] = str(v)
        self._request("PUT", bucket, key, payload=payload, extra_headers=headers)

    def upload_file(self, bucket, key, file_path, metadata=None, if_none_match=False):
        """Upload file content to S3 object."""
        with open(file_path, "rb") as f:
            data = f.read()
        self.put_object(bucket, key, data, metadata=metadata, if_none_match=if_none_match)

    def delete_object(self, bucket, key):
        """Delete an object."""
        try:
            self._request("DELETE", bucket, key)
        except S3NotFoundError:
            pass

    def list_objects_v2(self, bucket, prefix="", continuation_token=None, max_keys=1000):
        """List objects in bucket matching prefix."""
        params = {"list-type": "2", "max-keys": str(max_keys)}
        if prefix:
            params["prefix"] = prefix
        if continuation_token:
            params["continuation-token"] = continuation_token
        sorted_params = sorted(
            (urllib.parse.quote(str(k), safe=""), urllib.parse.quote(str(v), safe=""))
            for k, v in params.items()
        )
        query = "&".join(f"{k}={v}" for k, v in sorted_params)

        resp = self._request("GET", bucket, query=query)
        root = ET.fromstring(resp.content)

        contents = []
        for item in root.findall(".//{*}Contents"):
            key = item.findtext("{*}Key")
            size_str = item.findtext("{*}Size")
            etag = (item.findtext("{*}ETag") or "").strip('"')
            last_mod = item.findtext("{*}LastModified")
            contents.append({
                "key": key,
                "size": int(size_str) if size_str and size_str.isdigit() else 0,
                "etag": etag,
                "last_modified": last_mod,
            })

        next_token = root.findtext(".//{*}NextContinuationToken")
        is_truncated = root.findtext(".//{*}IsTruncated") == "true"
        return {
            "contents": contents,
            "is_truncated": is_truncated,
            "next_continuation_token": next_token if is_truncated else None,
        }

    def list_all_keys(self, bucket, prefix=""):
        """Yield all object keys matching prefix."""
        token = None
        while True:
            res = self.list_objects_v2(bucket, prefix=prefix, continuation_token=token)
            for item in res["contents"]:
                yield item["key"]
            if not res["is_truncated"]:
                break
            token = res["next_continuation_token"]
