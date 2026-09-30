#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import http.server
import os
import sys
import threading
from urllib.parse import urlsplit

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.storage.s3.client import (
    S3Client, S3ClientError, S3NotFoundError, S3ConditionFailedError,
)


def _start_mock_s3():
    state = {
        "routes": {},
        "requests": [],
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _handle(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            req_info = {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
            state["requests"].append(req_info)

            # Match method and path in routes
            route_key = (self.command, self.path.split("?")[0])
            if route_key in state["routes"]:
                status, headers, resp_body = state["routes"][route_key]
                if callable(resp_body):
                    status, headers, resp_body = resp_body(self.path)
                self.send_response(status)
                for hk, hv in (headers or {}).items():
                    self.send_header(hk, hv)
                self.end_headers()
                if resp_body:
                    self.wfile.write(resp_body)
                return

            self.send_response(404)
            self.end_headers()

        do_HEAD = _handle
        do_GET = _handle
        do_PUT = _handle
        do_DELETE = _handle

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state


def _stop_mock_s3(server, thread):
    server.shutdown()
    thread.join()


class S3ClientOperations(avocado.Test):
    def setUp(self):
        self.server, self.thread, self.state = _start_mock_s3()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.client = S3Client(self.endpoint, "test-access", "test-secret", region="garage")

    def tearDown(self):
        _stop_mock_s3(self.server, self.thread)

    def test_head_bucket_success(self):
        self.state["routes"][("HEAD", "/my-bucket")] = (200, {}, b"")
        self.assertTrue(self.client.head_bucket("my-bucket"))

    def test_head_bucket_not_found(self):
        with self.assertRaises(S3NotFoundError):
            self.client.head_bucket("missing-bucket")

    def test_head_object_and_exists(self):
        self.state["routes"][("HEAD", "/my-bucket/obj.txt")] = (
            200, {"x-amz-meta-hash": "abc123", "content-length": "42"}, b"")
        headers = self.client.head_object("my-bucket", "obj.txt")
        self.assertIsNotNone(headers)
        self.assertEqual(headers.get("x-amz-meta-hash"), "abc123")
        self.assertTrue(self.client.exists("my-bucket", "obj.txt"))

        self.assertIsNone(self.client.head_object("my-bucket", "missing.txt"))
        self.assertFalse(self.client.exists("my-bucket", "missing.txt"))

    def test_get_object_and_download_file(self):
        payload = b"Hello from S3 storage!"
        self.state["routes"][("GET", "/my-bucket/file.bin")] = (200, {}, payload)

        data = self.client.get_object("my-bucket", "file.bin")
        self.assertEqual(data, payload)

        target = os.path.join(self.workdir, "downloaded.bin")
        self.client.download_file("my-bucket", "file.bin", target)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_put_object_with_metadata_and_if_none_match(self):
        self.state["routes"][("PUT", "/my-bucket/test.dat")] = (200, {}, b"")
        self.client.put_object("my-bucket", "test.dat", b"payload-bytes",
                               metadata={"sha256": "abcdef"}, if_none_match=True)

        self.assertEqual(len(self.state["requests"]), 1)
        req = self.state["requests"][0]
        self.assertEqual(req["method"], "PUT")
        self.assertEqual(req["body"], b"payload-bytes")
        self.assertEqual(req["headers"].get("if-none-match"), "*")
        self.assertEqual(req["headers"].get("x-amz-meta-sha256"), "abcdef")
        self.assertIn("authorization", req["headers"])
        self.assertTrue(req["headers"]["authorization"].startswith("AWS4-HMAC-SHA256 Credential=test-access/"))

    def test_upload_file(self):
        self.state["routes"][("PUT", "/my-bucket/uploaded.txt")] = (200, {}, b"")
        source = os.path.join(self.workdir, "local.txt")
        with open(source, "wb") as f:
            f.write(b"local content to upload")

        self.client.upload_file("my-bucket", "uploaded.txt", source)
        req = self.state["requests"][-1]
        self.assertEqual(req["body"], b"local content to upload")

    def test_delete_object(self):
        self.state["routes"][("DELETE", "/my-bucket/del.txt")] = (204, {}, b"")
        self.client.delete_object("my-bucket", "del.txt")
        self.assertEqual(self.state["requests"][-1]["method"], "DELETE")

        # Deleting nonexistent object does not raise
        self.client.delete_object("my-bucket", "missing.txt")

    def test_condition_failed_412_raises_s3_condition_failed(self):
        self.state["routes"][("PUT", "/my-bucket/exists.txt")] = (412, {}, b"")
        with self.assertRaises(S3ConditionFailedError):
            self.client.put_object("my-bucket", "exists.txt", b"new", if_none_match=True)

    def test_list_objects_v2_and_pagination(self):
        page1_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
            <Contents>
                <Key>cache/item1</Key>
                <Size>100</Size>
                <ETag>"etag1"</ETag>
                <LastModified>2026-09-30T12:00:00.000Z</LastModified>
            </Contents>
            <IsTruncated>true</IsTruncated>
            <NextContinuationToken>token-page-2</NextContinuationToken>
        </ListBucketResult>"""

        page2_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
            <Contents>
                <Key>cache/item2</Key>
                <Size>200</Size>
                <ETag>"etag2"</ETag>
                <LastModified>2026-09-30T12:01:00.000Z</LastModified>
            </Contents>
            <IsTruncated>false</IsTruncated>
        </ListBucketResult>"""

        class MultiPageHandler:
            def __init__(self, p1, p2):
                self.calls = 0
                self.p1 = p1
                self.p2 = p2

            def get_response(self, path):
                if "continuation-token=token-page-2" in path:
                    return 200, {}, self.p2
                return 200, {}, self.p1

        mph = MultiPageHandler(page1_xml, page2_xml)
        self.state["routes"][("GET", "/my-bucket")] = (
            200, {}, lambda path: mph.get_response(path))

        keys = list(self.client.list_all_keys("my-bucket", prefix="cache/"))
        self.assertEqual(keys, ["cache/item1", "cache/item2"])

    def test_connection_error_raises_s3_client_error(self):
        bad_client = S3Client("http://127.0.0.1:1", "acc", "sec", timeout=1)
        with self.assertRaises(S3ClientError):
            bad_client.head_bucket("bkt")
