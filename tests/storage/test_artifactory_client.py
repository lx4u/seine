#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import hashlib
import os
import sys
from unittest import mock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

import requests

from seine.storage.artifactory.client import (
    ArtifactoryClient,
    ArtifactoryError,
    ArtifactoryNotFoundError,
)


def response(status=200, headers=None, content=b"", json_data=None):
    resp = mock.MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.content = content
    resp.json = mock.MagicMock(return_value=json_data)
    resp.iter_content = mock.MagicMock(return_value=[content] if content else [])
    return resp


class ArtifactoryClientAuth(avocado.Test):
    def test_bearer_token_sets_header(self):
        with mock.patch("requests.Session") as session_cls:
            ArtifactoryClient("http://a:8081", "repo", token="tok123")
            session = session_cls.return_value
            self.assertEqual(session.headers.__setitem__.call_args_list,
                             [mock.call("Authorization", "Bearer tok123")])

    def test_user_password_sets_basic_auth(self):
        with mock.patch("requests.Session") as session_cls:
            client = ArtifactoryClient("http://a:8081", "repo", user="u", password="p")
            self.assertEqual(client._session.auth, ("u", "p"))

    def test_endpoint_trailing_slash_is_stripped(self):
        with mock.patch("requests.Session"):
            client = ArtifactoryClient("http://a:8081/", "repo")
            self.assertEqual(client.endpoint, "http://a:8081")


class ArtifactoryClientErrors(avocado.Test):
    def setUp(self):
        self._patcher = mock.patch("requests.Session")
        self.session = self._patcher.start().return_value
        self.client = ArtifactoryClient("http://a:8081", "repo")

    def tearDown(self):
        self._patcher.stop()

    def test_404_becomes_not_found(self):
        self.session.request.return_value = response(404)
        with self.assertRaises(ArtifactoryNotFoundError):
            self.client.get_object("repo", "missing.bin")
        self.assertIsNone(self.client.head_object("repo", "missing.bin"))

    def test_500_carries_status(self):
        self.session.request.return_value = response(500, json_data={"errors": [{"message": "boom"}]})
        with self.assertRaises(ArtifactoryError) as ctx:
            self.client.get_object("repo", "x.bin")
        self.assertEqual(ctx.exception.status_code, 500)

    def test_connection_error_becomes_client_error(self):
        self.session.request.side_effect = requests.ConnectionError("down")
        with self.assertRaises(ArtifactoryError):
            self.client.head_object("repo", "x.bin")

    def test_delete_missing_is_fine(self):
        self.session.request.return_value = response(404)
        self.client.delete("repo", "gone.bin")

    def test_repo_info_missing_is_none(self):
        self.session.request.return_value = response(404)
        self.assertIsNone(self.client.repo_info("nope"))


class ArtifactoryClientObjects(avocado.Test):
    def setUp(self):
        self._patcher = mock.patch("requests.Session")
        self.session = self._patcher.start().return_value
        self.client = ArtifactoryClient("http://a:8081", "repo")

    def tearDown(self):
        self._patcher.stop()

    def test_head_parses_checksums(self):
        self.session.request.return_value = response(200, headers={
            "X-Checksum-Sha256": "AA" * 32, "Content-Length": "12"})
        meta = self.client.head_object("repo", "f.bin")
        self.assertEqual(meta, {"sha256": "aa" * 32, "size": 12})
        self.assertTrue(self.client.exists("repo", "f.bin"))

    def test_object_url_is_where_the_repo_serves_it_under_the_artifactory_prefix(self):
        self.assertEqual(self.client.object_url("repo", "d/f.bin"), "http://a:8081/artifactory/repo/d/f.bin")

    def test_open_streams_the_object_and_hands_it_back_open(self):
        resp = response(200)
        self.session.request.return_value = resp
        self.assertIs(self.client.open_object("repo", "d/f.bin"), resp)
        args, kwargs = self.session.request.call_args
        self.assertEqual(args[:2], ("GET", "http://a:8081/artifactory/repo/d/f.bin"))
        self.assertTrue(kwargs["stream"])

    def test_put_sends_checksum_and_returns_recorded(self):
        recorded = "bb" * 32
        self.session.request.return_value = response(
            201, json_data={"checksums": {"sha256": recorded}})
        sha = self.client.put_object("repo", "f.bin", b"data")
        _, kwargs = self.session.request.call_args
        self.assertEqual(kwargs["headers"],
                         {"X-Checksum-Sha256": hashlib.sha256(b"data").hexdigest()})
        self.assertEqual(sha, recorded)

    def test_put_without_recorded_falls_back_to_sent(self):
        self.session.request.return_value = response(201, json_data=None)
        self.session.request.return_value.json.side_effect = ValueError("no json")
        sha = self.client.put_object("repo", "f.bin", b"data", sha256="cc" * 32)
        self.assertEqual(sha, "cc" * 32)

    def test_upload_streams_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"file-data")
            path = f.name
        try:
            self.session.request.return_value = response(
                201, json_data={"checksums": {"sha256": "dd" * 32}})
            sha = self.client.upload_file("repo", "f.bin", path)
            self.assertEqual(sha, "dd" * 32)
            _, kwargs = self.session.request.call_args
            self.assertEqual(kwargs["headers"]["X-Checksum-Sha256"],
                             hashlib.sha256(b"file-data").hexdigest())
        finally:
            os.unlink(path)


class ArtifactoryClientListing(avocado.Test):
    def setUp(self):
        self._patcher = mock.patch("requests.Session")
        self.session = self._patcher.start().return_value
        self.client = ArtifactoryClient("http://a:8081", "repo")

    def tearDown(self):
        self._patcher.stop()

    def _page(self, items):
        return response(200, json_data={"results": items})

    def test_list_filters_prefix(self):
        # CE has no sort/offset: one AQL query, client-side prefix filter.
        found = [
            {"path": "cache/packages", "name": "a.tar.zst", "size": 3,
             "modified": "2026-10-04T10:00:00.000Z", "sha256": "ee" * 32},
            {"path": "worktrees/p", "name": "d.tar.zst", "size": 4,
             "modified": "2026-10-04T10:00:00.000Z", "sha256": "ff" * 32},
        ]
        self.session.request.return_value = self._page(found)
        items = list(self.client.list_all_objects("repo", "cache/"))
        self.assertEqual([i["key"] for i in items], ["cache/packages/a.tar.zst"])
        self.assertEqual(items[0]["size"], 3)
        self.assertEqual(items[0]["sha256"], "ee" * 32)
        query = self.session.request.call_args.kwargs.get("data") or \
            self.session.request.call_args[1].get("data", "")
        self.assertNotIn("offset", query)
        self.assertNotIn("sort", query)

    def test_the_query_names_repo_path_and_name_as_a_non_admin_needs(self):
        self.session.request.return_value = self._page([])
        list(self.client.list_all_objects("repo", "cache/"))
        query = self.session.request.call_args.kwargs.get("data") or ""
        self.assertIn('.include("repo","name","path"', query)


if __name__ == "__main__":
    avocado.main()
