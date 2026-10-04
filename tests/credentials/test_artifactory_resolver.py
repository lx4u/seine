#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import sys
from unittest import mock
import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.credentials import (
    CredentialError,
    artifactory_credential_source,
    probe_artifactory,
)


# The user's own credentials file must not leak into a test.
NO_FILE = {"SEINE_CREDENTIALS_FILE": "/nonexistent/seine-credentials.json"}


def http(status=200, text="OK"):
    resp = mock.MagicMock()
    resp.status_code = status
    resp.text = text
    return resp


class ArtifactoryCredentialSource(avocado.Test):
    def test_token_mode_by_default(self):
        with mock.patch.dict(os.environ, {"SEINE_ARTIFACTORY_TOKEN": "env-tok", **NO_FILE}, clear=True):
            src = artifactory_credential_source()
            self.assertEqual(src.get(), {"token": "env-tok"})

    def test_user_mode_when_auth_names_user(self):
        auth = {"user": "env:SEINE_ARTIFACTORY_USER", "password": "env:SEINE_ARTIFACTORY_PASSWORD"}
        with mock.patch.dict(os.environ, {"SEINE_ARTIFACTORY_USER": "u",
                                           "SEINE_ARTIFACTORY_PASSWORD": "p", **NO_FILE}, clear=True):
            src = artifactory_credential_source(auth=auth)
            self.assertEqual(src.get(), {"user": "u", "password": "p"})


class ProbeArtifactory(avocado.Test):
    def test_probe_success(self):
        with mock.patch("requests.get") as get:
            get.side_effect = [http(200, "OK"), http(200)]
            self.assertTrue(probe_artifactory("http://arti:8081", "repo", token="tok"))
            ping = get.call_args_list[0]
            self.assertEqual(ping.args[0], "http://arti:8081/artifactory/api/system/ping")
            self.assertEqual(ping.kwargs["headers"], {"Authorization": "Bearer tok"})

    def test_probe_falls_back_to_root_context(self):
        with mock.patch("requests.get") as get:
            get.side_effect = [http(404), http(200, "OK"), http(200)]
            self.assertTrue(probe_artifactory("http://arti:8081", "repo", token="tok"))
            urls = [c.args[0] for c in get.call_args_list]
            self.assertEqual(urls[0], "http://arti:8081/artifactory/api/system/ping")
            self.assertEqual(urls[1], "http://arti:8081/api/system/ping")
            self.assertEqual(urls[2], "http://arti:8081/api/storage/repo")

    def test_probe_basic_auth(self):
        with mock.patch("requests.get") as get:
            get.side_effect = [http(200, "OK"), http(200)]
            self.assertTrue(probe_artifactory("http://arti:8081", "repo", user="u", password="p"))
            self.assertEqual(get.call_args_list[0].kwargs["auth"], ("u", "p"))

    def test_probe_rejected_401(self):
        with mock.patch("requests.get", return_value=http(401)):
            self.assertFalse(probe_artifactory("http://arti:8081", "repo", token="bad"))

    def test_probe_repo_not_found_404_raises(self):
        with mock.patch("requests.get") as get:
            get.side_effect = [http(200, "OK"), http(404)]
            with self.assertRaises(CredentialError) as caught:
                probe_artifactory("http://arti:8081", "nope", token="tok")
            self.assertIn("repo 'nope' not found", str(caught.exception))

    def test_probe_unreachable_raises_credential_error(self):
        with mock.patch("requests.get", side_effect=Exception("down")):
            with self.assertRaises(CredentialError):
                probe_artifactory("http://arti:8081", "repo", token="tok")


if __name__ == "__main__":
    avocado.main()
