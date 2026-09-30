#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import http.server
import json
import os
import sys
import threading
from unittest import mock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.credentials import (
    CredentialError, CredentialNotFound, CredentialSource,
    DEFAULT_S3_ACCESS_KEY_CHAIN, DEFAULT_S3_SECRET_KEY_CHAIN,
    s3_credential_source, probe_s3,
)


def _start_s3_server():
    state = {"status": 200, "headers_seen": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            state["headers_seen"].append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(state["status"])
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state


def _stop_s3_server(server, thread):
    server.shutdown()
    thread.join()


class S3ChainsAndMasking(avocado.Test):
    def test_default_chains_inspect_expected_backends(self):
        self.assertIn("keyring:s3-access-key", DEFAULT_S3_ACCESS_KEY_CHAIN)
        self.assertIn("settings:s3-access-key", DEFAULT_S3_ACCESS_KEY_CHAIN)
        self.assertIn("env:SEINE_S3_ACCESS_KEY", DEFAULT_S3_ACCESS_KEY_CHAIN)
        self.assertIn("env:AWS_ACCESS_KEY_ID", DEFAULT_S3_ACCESS_KEY_CHAIN)

        self.assertIn("keyring:s3-secret-key", DEFAULT_S3_SECRET_KEY_CHAIN)
        self.assertIn("settings:s3-secret-key", DEFAULT_S3_SECRET_KEY_CHAIN)
        self.assertIn("env:SEINE_S3_SECRET_KEY", DEFAULT_S3_SECRET_KEY_CHAIN)
        self.assertIn("env:AWS_SECRET_ACCESS_KEY", DEFAULT_S3_SECRET_KEY_CHAIN)

    def test_secret_key_is_masked(self):
        self.assertIn("secret_key", CredentialSource.SECRET_FIELDS)
        self.assertIn("password", CredentialSource.SECRET_FIELDS)
        self.assertIn("token", CredentialSource.SECRET_FIELDS)


class S3CredentialResolution(avocado.Test):
    def test_resolution_from_env(self):
        missing_settings = os.path.join(self.workdir, "missing.json")
        env = {
            "SEINE_CREDENTIALS_FILE": missing_settings,
            "AWS_ACCESS_KEY_ID": "aws-key-123",
            "AWS_SECRET_ACCESS_KEY": "aws-sec-456",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            src = s3_credential_source()
            values = src.get()
            self.assertEqual(values["access_key"], "aws-key-123")
            self.assertEqual(values["secret_key"], "aws-sec-456")

    def test_resolution_from_settings_file(self):
        settings_path = os.path.join(self.workdir, "credentials.json")
        with open(settings_path, "w") as f:
            json.dump({
                "s3-access-key": "set-key-abc",
                "s3-secret-key": "set-sec-xyz",
            }, f)

        with mock.patch.dict(os.environ, {"SEINE_CREDENTIALS_FILE": settings_path}, clear=True):
            src = s3_credential_source()
            values = src.get()
            self.assertEqual(values["access_key"], "set-key-abc")
            self.assertEqual(values["secret_key"], "set-sec-xyz")

    def test_custom_auth_chain(self):
        env = {"CUSTOM_ACCESS": "custom-acc", "CUSTOM_SECRET": "custom-sec"}
        with mock.patch.dict(os.environ, env, clear=True):
            auth = {
                "access_key": "env:CUSTOM_ACCESS",
                "secret_key": "env:CUSTOM_SECRET",
            }
            src = s3_credential_source(auth=auth)
            values = src.get()
            self.assertEqual(values["access_key"], "custom-acc")
            self.assertEqual(values["secret_key"], "custom-sec")

    def test_prompt_and_commit(self):
        settings_path = os.path.join(self.workdir, "credentials.json")

        def prompt(context, fields):
            self.assertTrue(fields["secret_key"][1])
            self.assertFalse(fields["access_key"][1])
            return {"access_key": "prompted-key", "secret_key": "prompted-sec"}

        with mock.patch.dict(os.environ, {"SEINE_CREDENTIALS_FILE": settings_path}, clear=True):
            src = s3_credential_source(prompt=prompt)
            values = src.get()
            self.assertEqual(values["access_key"], "prompted-key")
            self.assertEqual(values["secret_key"], "prompted-sec")
            src.commit()

        with open(settings_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["s3-access-key"], "prompted-key")
        self.assertEqual(saved["s3-secret-key"], "prompted-sec")


class ProbeS3(avocado.Test):
    def test_probe_success_sends_sigv4(self):
        server, thread, state = _start_s3_server()
        try:
            state["status"] = 200
            endpoint = f"http://127.0.0.1:{server.server_port}"
            ok = probe_s3(endpoint, "my-bucket", "acc123", "sec456", "garage")
            self.assertTrue(ok)
            self.assertEqual(len(state["headers_seen"]), 1)
            hdr = state["headers_seen"][0]
            self.assertIn("authorization", hdr)
            self.assertTrue(hdr["authorization"].startswith("AWS4-HMAC-SHA256 Credential=acc123/"))
            self.assertIn("x-amz-date", hdr)
            self.assertIn("x-amz-content-sha256", hdr)
        finally:
            _stop_s3_server(server, thread)

    def test_probe_rejected_401(self):
        server, thread, state = _start_s3_server()
        try:
            state["status"] = 401
            endpoint = f"http://127.0.0.1:{server.server_port}"
            self.assertFalse(probe_s3(endpoint, "my-bucket", "acc", "wrong"))
        finally:
            _stop_s3_server(server, thread)

    def test_probe_rejected_403(self):
        server, thread, state = _start_s3_server()
        try:
            state["status"] = 403
            endpoint = f"http://127.0.0.1:{server.server_port}"
            self.assertFalse(probe_s3(endpoint, "my-bucket", "acc", "wrong"))
        finally:
            _stop_s3_server(server, thread)

    def test_probe_bucket_not_found_404_raises(self):
        server, thread, state = _start_s3_server()
        try:
            state["status"] = 404
            endpoint = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(CredentialError) as caught:
                probe_s3(endpoint, "nonexistent-bucket", "acc", "sec")
            self.assertIn("bucket 'nonexistent-bucket' not found", str(caught.exception))
        finally:
            _stop_s3_server(server, thread)

    def test_probe_unreachable_raises_credential_error(self):
        with self.assertRaises(CredentialError):
            probe_s3("http://127.0.0.1:1", "bucket", "acc", "sec", timeout=1)
