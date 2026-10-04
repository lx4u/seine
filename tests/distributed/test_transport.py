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


if __name__ == "__main__":
    avocado.main()
