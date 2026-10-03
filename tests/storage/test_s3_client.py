#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import datetime
import hashlib
import io
import os
import sys
from urllib.parse import parse_qs, urlsplit

from botocore.stub import Stubber

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine import vault
from seine.storage.s3.client import (
    S3Client, S3ClientError, S3NotFoundError, S3ConditionFailedError,
)


class S3ClientOperations(avocado.Test):
    def setUp(self):
        self.client = S3Client("http://127.0.0.1:1", "test-access", "test-secret",
                               region="garage", timeout=1)
        self.stub = Stubber(self.client._s3)
        self.stub.activate()

    def tearDown(self):
        self.stub.deactivate()

    def test_credentials_are_registered_as_secrets(self):
        self.assertIn("test-access", vault.secrets())
        self.assertIn("test-secret", vault.secrets())

    def test_head_bucket_success(self):
        self.stub.add_response("head_bucket", {}, {"Bucket": "my-bucket"})
        self.assertTrue(self.client.head_bucket("my-bucket"))

    def test_head_bucket_not_found(self):
        self.stub.add_client_error("head_bucket", "404", "Not Found", 404)
        with self.assertRaises(S3NotFoundError):
            self.client.head_bucket("missing-bucket")

    def test_create_bucket(self):
        self.stub.add_response("create_bucket", {}, {"Bucket": "new-bucket"})
        self.client.create_bucket("new-bucket")
        self.stub.assert_no_pending_responses()

    def test_head_object_and_exists(self):
        self.stub.add_response(
            "head_object",
            {"ContentLength": 42, "ETag": '"e1"', "Metadata": {"hash": "abc123"}},
            {"Bucket": "my-bucket", "Key": "obj.txt"})
        headers = self.client.head_object("my-bucket", "obj.txt")
        self.assertEqual(headers.get("x-amz-meta-hash"), "abc123")
        self.assertEqual(headers.get("content-length"), "42")

        self.stub.add_client_error("head_object", "404", "Not Found", 404)
        self.assertIsNone(self.client.head_object("my-bucket", "missing.txt"))
        self.stub.add_client_error("head_object", "404", "Not Found", 404)
        self.assertFalse(self.client.exists("my-bucket", "missing.txt"))

    def test_get_object(self):
        from botocore.response import StreamingBody
        payload = b"Hello from S3 storage!"
        self.stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(payload), len(payload))},
            {"Bucket": "my-bucket", "Key": "file.bin"})
        self.assertEqual(self.client.get_object("my-bucket", "file.bin"), payload)

    def test_get_object_missing_raises_not_found(self):
        self.stub.add_client_error("get_object", "NoSuchKey", "nope", 404)
        with self.assertRaises(S3NotFoundError):
            self.client.get_object("my-bucket", "missing")

    def test_download_file_delegates_to_boto(self):
        calls = []
        self.client._s3.download_file = lambda *a, **kw: calls.append(a)
        target = os.path.join(self.workdir, "sub", "downloaded.bin")
        self.client.download_file("my-bucket", "file.bin", target)
        self.assertEqual(calls, [("my-bucket", "file.bin", target)])
        self.assertTrue(os.path.isdir(os.path.dirname(target)))

    def test_put_object_with_metadata_and_if_none_match(self):
        self.stub.add_response(
            "put_object", {},
            {"Bucket": "my-bucket", "Key": "test.dat", "Body": b"payload-bytes",
             "ChecksumAlgorithm": "SHA256", "Metadata": {"sha256": "abcdef"},
             "IfNoneMatch": "*"})
        self.client.put_object("my-bucket", "test.dat", b"payload-bytes",
                               metadata={"sha256": "abcdef"}, if_none_match=True)
        self.stub.assert_no_pending_responses()

    def test_put_object_accepts_str(self):
        self.stub.add_response(
            "put_object", {},
            {"Bucket": "b", "Key": "k", "Body": b"text", "ChecksumAlgorithm": "SHA256"})
        self.client.put_object("b", "k", "text")

    def test_condition_failed_412_raises_s3_condition_failed(self):
        self.stub.add_client_error("put_object", "PreconditionFailed", "exists", 412)
        with self.assertRaises(S3ConditionFailedError) as ctx:
            self.client.put_object("my-bucket", "exists.txt", b"new", if_none_match=True)
        self.assertEqual(ctx.exception.status_code, 412)

    def test_other_errors_raise_s3_client_error(self):
        self.stub.add_client_error("put_object", "AccessDenied", "no", 403)
        with self.assertRaises(S3ClientError) as ctx:
            self.client.put_object("my-bucket", "k", b"x")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertNotIsInstance(ctx.exception, S3NotFoundError)

    def test_upload_file_stores_sha256_metadata(self):
        source = os.path.join(self.workdir, "local.txt")
        with open(source, "wb") as f:
            f.write(b"local content to upload")
        calls = []
        self.client._s3.upload_file = lambda *a, **kw: calls.append((a, kw))
        returned = self.client.upload_file("my-bucket", "uploaded.txt", source)
        args, kwargs = calls[0]
        self.assertEqual(args, (source, "my-bucket", "uploaded.txt"))
        self.assertEqual(kwargs["ExtraArgs"]["Metadata"]["sha256"],
                         hashlib.sha256(b"local content to upload").hexdigest())
        self.assertEqual(kwargs["ExtraArgs"]["ChecksumAlgorithm"], "SHA256")
        self.assertEqual(returned, kwargs["ExtraArgs"]["Metadata"]["sha256"])

    def test_delete_object(self):
        self.stub.add_response("delete_object", {}, {"Bucket": "b", "Key": "del.txt"})
        self.client.delete_object("b", "del.txt")
        self.stub.add_client_error("delete_object", "NoSuchKey", "gone", 404)
        self.client.delete_object("b", "missing.txt")

    def test_delete_objects_batches_of_1000(self):
        keys = [f"k{i}" for i in range(2500)]
        for n in (1000, 1000, 500):
            self.stub.add_response("delete_objects", {}, {
                "Bucket": "b", "Delete": {
                    "Objects": [{"Key": k} for k in keys[:n]], "Quiet": True}})
            keys = keys[n:]
        self.client.delete_objects("b", [f"k{i}" for i in range(2500)])
        self.stub.assert_no_pending_responses()

    def test_delete_objects_empty_is_a_noop(self):
        self.client.delete_objects("b", [])

    def test_delete_objects_raises_on_errors_in_a_200(self):
        errors = [{"Key": f"k{i}", "Code": "InternalError", "Message": "boom"}
                  for i in range(5)]
        self.stub.add_response("delete_objects", {"Errors": errors})
        with self.assertRaises(S3ClientError) as ctx:
            self.client.delete_objects("b", ["k0", "k1"])
        self.assertIn("5 keys", str(ctx.exception))
        self.assertIn("k0, k1, k2", str(ctx.exception))
        self.assertNotIn("k3", str(ctx.exception))

    def test_delete_objects_ignores_missing_keys(self):
        self.stub.add_response("delete_objects", {"Errors": [
            {"Key": "gone", "Code": "NoSuchKey", "Message": "no"}]})
        self.client.delete_objects("b", ["gone"])

    def test_list_multipart_uploads_follows_the_markers(self):
        self.stub.add_response(
            "list_multipart_uploads",
            {"Uploads": [{"Key": "a", "UploadId": "u1"}], "IsTruncated": True,
             "NextKeyMarker": "a", "NextUploadIdMarker": "u1"}, {"Bucket": "b"})
        self.stub.add_response(
            "list_multipart_uploads",
            {"Uploads": [{"Key": "b", "UploadId": "u2"}]},
            {"Bucket": "b", "KeyMarker": "a", "UploadIdMarker": "u1"})
        self.assertEqual(list(self.client.list_multipart_uploads("b")),
                         [("a", "u1"), ("b", "u2")])

    def test_abort_multipart_upload_ignores_a_vanished_upload(self):
        self.stub.add_response("abort_multipart_upload", {},
                               {"Bucket": "b", "Key": "a", "UploadId": "u1"})
        self.client.abort_multipart_upload("b", "a", "u1")
        self.stub.add_client_error("abort_multipart_upload", "NoSuchUpload", "gone", 404)
        self.client.abort_multipart_upload("b", "a", "u2")

    def test_list_all_objects_sums_sizes_across_pages(self):
        self.stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "a", "Size": 100}, {"Key": "b", "Size": 5}],
             "IsTruncated": True, "NextContinuationToken": "t2"},
            {"Bucket": "b", "MaxKeys": 1000, "Prefix": "p/"})
        self.stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "c", "Size": 20}], "IsTruncated": False},
            {"Bucket": "b", "MaxKeys": 1000, "Prefix": "p/", "ContinuationToken": "t2"})
        total = sum(o["size"] for o in self.client.list_all_objects("b", "p/"))
        self.assertEqual(total, 125)

    def test_refresh_object_resends_metadata_with_a_new_marker(self):
        self.stub.add_response(
            "head_object", {"ContentLength": 3, "ETag": '"e"', "Metadata": {"sha256": "abc", "refreshed": "old"}},
            {"Bucket": "b", "Key": "k"})
        sent = {}

        def check(params, **kwargs):
            sent.update(params["Metadata"])
            self.assertEqual(params["MetadataDirective"], "REPLACE")
            self.assertEqual(params["CopySource"], {"Bucket": "b", "Key": "k"})

        self.client._s3.meta.events.register("provide-client-params.s3.CopyObject", check)
        self.stub.add_response("copy_object", {})
        self.assertTrue(self.client.refresh_object("b", "k"))
        self.assertEqual(sent["sha256"], "abc")
        self.assertNotEqual(sent["refreshed"], "old")
        self.stub.assert_no_pending_responses()

    def test_refresh_object_missing_key_returns_false(self):
        self.stub.add_client_error("head_object", "404", "Not Found", 404)
        self.assertFalse(self.client.refresh_object("b", "gone"))

    def test_get_bucket_lifecycle(self):
        rules = [{"ID": "r", "Status": "Enabled", "Filter": {"Prefix": "p/"}, "Expiration": {"Days": 3}}]
        self.stub.add_response("get_bucket_lifecycle_configuration", {"Rules": rules}, {"Bucket": "b"})
        self.assertEqual(self.client.get_bucket_lifecycle("b"), rules)

    def test_get_bucket_lifecycle_without_configuration_is_empty(self):
        self.stub.add_client_error(
            "get_bucket_lifecycle_configuration", "NoSuchLifecycleConfiguration", "none", 404)
        self.assertEqual(self.client.get_bucket_lifecycle("b"), [])

    def test_put_bucket_lifecycle_sends_the_rules(self):
        rules = [{"ID": "r", "Status": "Enabled", "Filter": {"Prefix": ""},
                  "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]
        self.stub.add_response("put_bucket_lifecycle_configuration", {}, {
            "Bucket": "b", "LifecycleConfiguration": {"Rules": rules}})
        self.client.put_bucket_lifecycle("b", rules)
        self.stub.assert_no_pending_responses()

    def test_put_bucket_lifecycle_without_rules_deletes_the_configuration(self):
        self.stub.add_response("delete_bucket_lifecycle", {}, {"Bucket": "b"})
        self.client.put_bucket_lifecycle("b", [])
        self.stub.assert_no_pending_responses()

    def test_presign_get(self):
        url = self.client.presign_get("my-bucket", "artifacts/a b.bin", expires_in=120)
        parts = urlsplit(url)
        self.assertEqual(parts.netloc, "127.0.0.1:1")
        self.assertTrue(parts.path.startswith("/my-bucket/artifacts/"))
        query = parse_qs(parts.query)
        self.assertEqual(query["X-Amz-Expires"], ["120"])
        self.assertTrue(query["X-Amz-Credential"][0].startswith("test-access/"))
        self.assertNotIn("test-secret", url)

    def test_list_objects_v2_and_pagination(self):
        when = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc)
        self.stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "cache/item1", "Size": 100, "ETag": '"etag1"',
                           "LastModified": when}],
             "IsTruncated": True, "NextContinuationToken": "token-page-2"},
            {"Bucket": "my-bucket", "MaxKeys": 1000, "Prefix": "cache/"})
        self.stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "cache/item2", "Size": 200, "ETag": '"etag2"',
                           "LastModified": when}],
             "IsTruncated": False},
            {"Bucket": "my-bucket", "MaxKeys": 1000, "Prefix": "cache/",
             "ContinuationToken": "token-page-2"})
        keys = list(self.client.list_all_keys("my-bucket", prefix="cache/"))
        self.assertEqual(keys, ["cache/item1", "cache/item2"])

    def test_list_objects_v2_shape(self):
        self.stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "k", "Size": 7, "ETag": '"e"'}], "IsTruncated": False},
            {"Bucket": "b", "MaxKeys": 1000})
        res = self.client.list_objects_v2("b")
        self.assertEqual(res["contents"][0]["key"], "k")
        self.assertEqual(res["contents"][0]["size"], 7)
        self.assertEqual(res["contents"][0]["etag"], "e")
        self.assertFalse(res["is_truncated"])
        self.assertIsNone(res["next_continuation_token"])


class S3ClientConnection(avocado.Test):
    def test_connection_error_raises_s3_client_error(self):
        bad_client = S3Client("http://127.0.0.1:1", "acc", "sec", timeout=1)
        with self.assertRaises(S3ClientError):
            bad_client.head_bucket("bkt")
