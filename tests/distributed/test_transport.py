#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the helpers the clients share to reach the server."""

import os
import sys
import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.distributed.common.transport import resolve_download


class ResolveDownloadTest(avocado.Test):

    def test_relative_url_is_the_servers_and_carries_the_token(self):
        url, headers = resolve_download("https://seine.lan:8443/", "tok", "/api/v1/builds/b/artifacts/a.raw")
        self.assertEqual(url, "https://seine.lan:8443/api/v1/builds/b/artifacts/a.raw")
        self.assertEqual(headers, {"Authorization": "Bearer tok"})

    def test_relative_url_without_a_token_carries_no_header(self):
        self.assertEqual(resolve_download("https://s", None, "/x")[1], {})

    def test_absolute_url_is_storage_and_never_gets_the_token(self):
        url, headers = resolve_download("https://s", "tok", "https://s3.lan/bucket/a.raw?sig=1")
        self.assertEqual((url, headers), ("https://s3.lan/bucket/a.raw?sig=1", {}))

    ENDPOINT = "https://arti.lan:8443"
    TOKEN = {"token": "user-tok"}

    def test_the_users_credential_goes_to_the_storage_endpoint_it_belongs_to(self):
        url, headers = resolve_download("https://s", "seine-tok", f"{self.ENDPOINT}/r/a.raw", (self.ENDPOINT, self.TOKEN))
        self.assertEqual((url, headers), (f"{self.ENDPOINT}/r/a.raw", {"Authorization": "Bearer user-tok"}))

    def test_a_user_and_password_are_sent_as_basic_auth(self):
        _, headers = resolve_download("https://s", "t", f"{self.ENDPOINT}/a",
                                      (self.ENDPOINT, {"user": "alice", "password": "pw"}))
        self.assertEqual(headers, {"Authorization": "Basic YWxpY2U6cHc="})

    def test_it_is_never_sent_to_another_origin(self):
        storage = (self.ENDPOINT, self.TOKEN)
        for url in ("https://evil.example/r/a.raw", "https://arti.lan:9443/r/a.raw",
                    "https://arti.lan/r/a.raw", "http://arti.lan:8443/r/a.raw",
                    "https://arti.lan.evil.example:8443/r/a.raw"):
            self.assertEqual(resolve_download("https://s", "t", url, storage)[1], {}, url)

    def test_it_is_refused_over_plain_http_even_to_its_own_endpoint(self):
        with self.assertRaises(ValueError):
            resolve_download("https://s", "t", "http://arti.lan/a", ("http://arti.lan", self.TOKEN))

    def test_plain_http_to_a_loopback_storage_is_allowed(self):
        headers = resolve_download("https://s", "t", "http://127.0.0.1:8081/a",
                                   ("http://127.0.0.1:8081", self.TOKEN))[1]
        self.assertEqual(headers, {"Authorization": "Bearer user-tok"})

    def test_a_server_relative_url_still_gets_the_seine_token_only(self):
        url, headers = resolve_download("https://s", "seine-tok", "/api/v1/builds/b/artifacts/a",
                                        (self.ENDPOINT, self.TOKEN))
        self.assertEqual((url, headers), ("https://s/api/v1/builds/b/artifacts/a",
                                          {"Authorization": "Bearer seine-tok"}))


if __name__ == "__main__":
    avocado.main()
