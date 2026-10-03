# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import io
import os
import shutil
import tempfile
from unittest import mock

import requests
from avocado import Test

from seine.build import BuildCmd
from seine.distributed.client import remote
from seine.distributed.client.remote import RemoteBuild, build_remote


def sha(data):
    return hashlib.sha256(data).hexdigest()


class TestClientArtifactDownload(Test):
    """Unit tests for remote build artifact downloading and verification."""

    def setUp(self):
        self.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="seine-test-dl-"))
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp_dir)
        self.env = mock.patch.dict(
            os.environ, {"SEINE_TOKEN": "pat-test", "SEINE_CA_CERT": ""}
        )
        self.env.start()

        self.spec_file = os.path.join(self.tmp_dir, "test.yaml")
        with open(self.spec_file, "w", encoding="utf-8") as f:
            f.write("distribution:\n  release: bookworm\n  architecture: amd64\n")

        archive = os.path.join(self.tmp_dir, "bundle.tar.zst")
        self.patches = [
            mock.patch("seine.distributed.client.worktree.pack_worktree",
                       side_effect=lambda root: (self._bundle(archive), "digest-123")),
            mock.patch.object(remote, "upload_worktree", return_value={"digest": "digest-123"}),
            mock.patch.object(remote, "WsClient"),
            mock.patch.object(remote, "ws_ssl_context", return_value=None),
            mock.patch("requests.post"),
            mock.patch("requests.get", side_effect=self._http_get),
        ]
        started = [p.start() for p in self.patches]
        self.post, self.get = started[4], started[5]
        started[2].return_value.recv.side_effect = TimeoutError()
        self.post.return_value = self._json({
            "build_id": "bld-test1", "status": "queued", "project": "demo", "target_arch": "amd64",
        })
        self.build = {}
        self.contents = {}
        self.failures = {}
        self.storage_gets = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        os.chdir(self.old_cwd)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @staticmethod
    def _bundle(path):
        with open(path, "wb") as f:
            f.write(b"zst")
        return path

    @staticmethod
    def _json(body, code=200):
        resp = mock.MagicMock()
        resp.status_code = code
        resp.json.return_value = body
        return resp

    def _http_get(self, url, **kwargs):
        if "/api/v1/builds/" in url:
            return self._json(self.build)
        self.storage_gets.append((url, kwargs))
        name = url.rsplit("/", 1)[-1]
        resp = mock.MagicMock()
        failure = self.failures.get(name)
        if isinstance(failure, int):
            resp.status_code = failure
            return resp
        resp.status_code = 200
        chunks = [self.contents[name][:4], self.contents[name][4:]]

        def stream(chunk_size=None):
            yield chunks[0]
            if failure:
                raise failure
            yield chunks[1]

        resp.iter_content.side_effect = stream
        return resp

    def _complete(self, files, manifest=None, base="https://storage.example/demo"):
        """Make the server report files (name -> bytes), with a manifest for each."""
        self.contents.update(files)
        self.build = {
            "id": "bld-test1",
            "status": "completed",
            "artifact_urls": [f"artifacts/demo/bld-test1/{n}" for n in files],
            "download_urls": {n: f"{base}/{n}" for n in files},
            "artifacts": [
                {"name": n, "size": len(b), "sha256": sha(b)} for n, b in files.items()
            ] if manifest is None else manifest,
        }

    def _run(self, options=None, server_url="http://localhost:8000"):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            ret = build_remote(
                server_url=server_url, project="demo", spec_files=[self.spec_file],
                options=options, root_dir=self.tmp_dir,
            )
        return ret, out.getvalue(), err.getvalue()

    def _leftovers(self, directory):
        return sorted(
            f for _, _, files in os.walk(directory) for f in files if f.endswith(".part")
        )

    def test_download_on_remote_completion(self):
        self._complete({"pc-image.raw": b"binary-raw-image-bytes"})
        ret, out, _ = self._run()
        self.assertEqual(ret, 0)

        expected_dest = os.path.join(self.tmp_dir, "deploy", "bookworm", "pc-image.raw")
        with open(expected_dest, "rb") as f:
            self.assertEqual(f.read(), b"binary-raw-image-bytes")
        self.assertIn("Downloading pc-image.raw", out)
        self.assertIn("Downloaded 1 of 1 artifact(s)", out)

    def test_success_renames_the_part_file_into_place(self):
        self._complete({"disk.raw": b"disk-content"})
        dest = os.path.join(self.tmp_dir, "out", "disk.raw")
        with mock.patch("os.replace", wraps=os.replace) as replace:
            ret, _, _ = self._run({"dest_dir": os.path.join(self.tmp_dir, "out")})
        self.assertEqual(ret, 0)
        replace.assert_called_once_with(f"{dest}.part", dest)
        self.assertEqual(os.listdir(os.path.dirname(dest)), ["disk.raw"])

    def test_dest_dir_custom_destination(self):
        custom_dest = os.path.join(self.tmp_dir, "custom", "artifacts")
        self._complete({"disk.raw": b"disk-content"})
        ret, _, _ = self._run({"dest_dir": custom_dest})
        self.assertEqual(ret, 0)
        with open(os.path.join(custom_dest, "disk.raw"), "rb") as f:
            self.assertEqual(f.read(), b"disk-content")

    def test_companion_files_are_verified_by_the_manifest(self):
        self._complete({"app.img": b"payload", "app.img.digest": b"not-even-a-digest"})
        out_dir = os.path.join(self.tmp_dir, "out")
        ret, out, _ = self._run({"dest_dir": out_dir})
        self.assertEqual(ret, 0)
        self.assertIn("Downloaded 2 of 2 artifact(s)", out)
        self.assertNotIn("recipe digest", out)

    def test_name_without_manifest_entry_is_an_error(self):
        self._complete({"a.img": b"aaaaaaaa", "b.img": b"bbbbbbbb"})
        self.build["artifacts"] = [a for a in self.build["artifacts"] if a["name"] == "a.img"]
        out_dir = os.path.join(self.tmp_dir, "out")
        ret, out, err = self._run({"dest_dir": out_dir})
        self.assertEqual(ret, 1)
        self.assertIn("b.img", err)
        self.assertIn("no checksum", err)
        self.assertEqual(sorted(os.listdir(out_dir)), ["a.img"])
        self.assertEqual([u for u, _ in self.storage_gets], ["https://storage.example/demo/a.img"])

    def test_sha256_mismatch_keeps_corrupt_file_and_fails(self):
        self._complete({"disk.raw": b"tampered-bytes"})
        self.build["artifacts"][0]["sha256"] = sha(b"what the worker built")
        out_dir = os.path.join(self.tmp_dir, "out")
        ret, _, err = self._run({"dest_dir": out_dir})
        self.assertEqual(ret, 1)
        self.assertIn("disk.raw is corrupt", err)
        self.assertEqual(os.listdir(out_dir), ["disk.raw.corrupt"])
        with open(os.path.join(out_dir, "disk.raw.corrupt"), "rb") as f:
            self.assertEqual(f.read(), b"tampered-bytes")

    def test_size_mismatch_keeps_corrupt_file_and_fails(self):
        self._complete({"disk.raw": b"twelve-bytes"})
        self.build["artifacts"][0]["size"] = 99
        out_dir = os.path.join(self.tmp_dir, "out")
        ret, _, err = self._run({"dest_dir": out_dir})
        self.assertEqual(ret, 1)
        self.assertIn("99 bytes", err)
        self.assertEqual(os.listdir(out_dir), ["disk.raw.corrupt"])

    def _events(self):
        events = []
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            RemoteBuild(
                "http://localhost:8000", "demo", self.spec_file, token="t",
                root_dir=self.tmp_dir, on_download=lambda *event: events.append(event),
            ).run()
        return events

    def test_download_progress_is_reported(self):
        self._complete({"a.raw": b"x" * 10, "b.raw": b"y" * 5})
        events = self._events()
        self.assertEqual(events[:2], [("queue", "bld-test1", "a.raw", 10),
                                      ("queue", "bld-test1", "b.raw", 5)])
        a = [e for e in events if e[2] == "a.raw" and e[0] != "queue"]
        self.assertEqual(a[0][0], "start")
        self.assertEqual(a[-1][0], "done")
        self.assertEqual({e[0] for e in a[1:-1]}, {"bytes"})
        self.assertEqual(sum(e[3] for e in a if e[0] == "bytes"), 10)
        self.assertEqual(events.index(("done", "bld-test1", "a.raw", 0)) <
                         events.index(("start", "bld-test1", "b.raw", 0)), True)

    def test_a_corrupt_download_is_reported_as_failed(self):
        self._complete({"disk.raw": b"tampered"})
        self.build["artifacts"][0]["sha256"] = "0" * 64
        kinds = [e[0] for e in self._events() if e[2] == "disk.raw"]
        self.assertEqual(kinds[:2], ["queue", "start"])
        self.assertEqual(kinds[-1], "failed")
        self.assertNotIn("done", kinds)

    def test_a_callback_that_refuses_stops_that_download_and_cleans_up(self):
        from seine.distributed.client.remote import DownloadError
        self._complete({"a.raw": b"x" * 10, "b.raw": b"y" * 5})
        events = []

        def refuse_b(kind, build_id, name, n):
            events.append((kind, name))
            if name == "b.raw" and kind in ("start", "bytes"):
                raise DownloadError("b.raw: download cancelled")

        out_dir = os.path.join(self.tmp_dir, "out")
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            RemoteBuild(
                "http://localhost:8000", "demo", self.spec_file, token="t",
                root_dir=self.tmp_dir, on_download=refuse_b,
                options={"dest_dir": out_dir},
            ).run()
        self.assertEqual(sorted(os.listdir(out_dir)), ["a.raw"])
        self.assertIn(("failed", "b.raw"), events)
        self.assertIn(("done", "a.raw"), events)

    def test_corrupt_download_does_not_replace_a_good_file(self):
        self._complete({"disk.raw": b"tampered-bytes"})
        self.build["artifacts"][0]["sha256"] = "0" * 64
        out_dir = os.path.join(self.tmp_dir, "out")
        os.makedirs(out_dir)
        with open(os.path.join(out_dir, "disk.raw"), "wb") as f:
            f.write(b"earlier good build")
        self._run({"dest_dir": out_dir})
        with open(os.path.join(out_dir, "disk.raw"), "rb") as f:
            self.assertEqual(f.read(), b"earlier good build")

    def test_hostile_names_are_refused(self):
        names = ["../evil", "a/b", "/etc/passwd", "..", ".", "", "a\\b", "x\0y", "sub/../../up"]
        out_dir = os.path.join(self.tmp_dir, "nest", "out")
        for name in names:
            self.storage_gets.clear()
            self._complete({"good.img": b"good-bytes"})
            self.contents[name] = b"evil-bytes"
            self.build["download_urls"][name] = f"https://storage.example/demo/{name}"
            self.build["artifacts"].append(
                {"name": name, "size": len(b"evil-bytes"), "sha256": sha(b"evil-bytes")}
            )
            with self.subTest(name=name):
                ret, _, err = self._run({"dest_dir": out_dir})
                self.assertEqual(ret, 1)
                self.assertIn("unsafe name", err)
                self.assertEqual(
                    [u for u, _ in self.storage_gets], ["https://storage.example/demo/good.img"]
                )
        for _, _, files in os.walk(self.tmp_dir):
            self.assertNotIn("evil", files)
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "nest", "evil")))

    def test_name_that_is_a_symlink_out_of_the_directory_is_refused(self):
        outside = os.path.join(self.tmp_dir, "outside")
        with open(outside, "wb") as f:
            f.write(b"precious")
        out_dir = os.path.join(self.tmp_dir, "out")
        os.makedirs(out_dir)
        os.symlink(outside, os.path.join(out_dir, "link.img"))
        self._complete({"link.img": b"evil-bytes"})
        ret, _, err = self._run({"dest_dir": out_dir})
        self.assertEqual(ret, 1)
        self.assertIn("would leave", err)
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"precious")
        self.assertEqual(self.storage_gets, [])

    def test_plain_http_storage_url_is_refused(self):
        self._complete({"disk.raw": b"disk-content"}, base="http://storage.example/demo")
        out_dir = os.path.join(self.tmp_dir, "out")
        ret, _, err = self._run({"dest_dir": out_dir}, server_url="https://server.example")
        self.assertEqual(ret, 1)
        self.assertIn("refusing plain http", err)
        self.assertEqual(self.storage_gets, [])
        self.assertEqual(os.listdir(out_dir), [])

    def test_plain_http_storage_url_is_allowed_with_insecure(self):
        self._complete({"disk.raw": b"disk-content"}, base="http://storage.example/demo")
        ret, _, _ = self._run(
            {"dest_dir": os.path.join(self.tmp_dir, "out"), "insecure": True},
            server_url="https://server.example",
        )
        self.assertEqual(ret, 0)
        self.assertEqual(len(self.storage_gets), 1)

    def test_plain_http_to_loopback_storage_is_allowed(self):
        self._complete({"disk.raw": b"disk-content"}, base="http://127.0.0.1:3900/demo")
        ret, _, _ = self._run(
            {"dest_dir": os.path.join(self.tmp_dir, "out")}, server_url="https://server.example"
        )
        self.assertEqual(ret, 0)

    def test_https_storage_url_is_allowed(self):
        self._complete({"disk.raw": b"disk-content"}, base="https://storage.example/demo")
        ret, _, _ = self._run(
            {"dest_dir": os.path.join(self.tmp_dir, "out")}, server_url="https://server.example"
        )
        self.assertEqual(ret, 0)

    def test_download_uses_the_servers_ca_and_no_redirects(self):
        self._complete({"disk.raw": b"disk-content"}, base="https://storage.example/demo")
        ret, _, _ = self._run(
            {"dest_dir": os.path.join(self.tmp_dir, "out"), "ca_cert": "/etc/seine-ca.pem"},
            server_url="https://server.example",
        )
        self.assertEqual(ret, 0)
        [(_, kwargs)] = self.storage_gets
        self.assertEqual(kwargs["verify"], "/etc/seine-ca.pem")
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertNotIn("headers", kwargs)

    def test_default_verify_without_ca_cert(self):
        self._complete({"disk.raw": b"disk-content"})
        self._run({"dest_dir": os.path.join(self.tmp_dir, "out")})
        self.assertIs(self.storage_gets[0][1]["verify"], True)

    def test_http_error_leaves_no_part_file_and_fails(self):
        self._complete({"good.bin": b"good-bytes", "bad.bin": b"bad-bytes"})
        self.failures["bad.bin"] = 500
        out_dir = os.path.join(self.tmp_dir, "out")
        ret, out, err = self._run({"dest_dir": out_dir})
        self.assertEqual(ret, 1)
        self.assertIn("bad.bin: storage answered HTTP 500", err)
        self.assertIn("Downloaded 1 of 2 artifact(s)", out)
        self.assertEqual(os.listdir(out_dir), ["good.bin"])

    def test_connection_dropped_midway_leaves_no_part_file(self):
        self._complete({"disk.raw": b"disk-content"})
        self.failures["disk.raw"] = requests.ConnectionError("reset by peer")
        out_dir = os.path.join(self.tmp_dir, "out")
        ret, _, err = self._run({"dest_dir": out_dir})
        self.assertEqual(ret, 1)
        self.assertIn("reset by peer", err)
        self.assertEqual(os.listdir(out_dir), [])

    def test_stale_part_file_is_replaced(self):
        self._complete({"disk.raw": b"disk-content"})
        out_dir = os.path.join(self.tmp_dir, "out")
        os.makedirs(out_dir)
        with open(os.path.join(out_dir, "disk.raw.part"), "wb") as f:
            f.write(b"leftover from an interrupted run")
        ret, _, _ = self._run({"dest_dir": out_dir})
        self.assertEqual(ret, 0)
        self.assertEqual(os.listdir(out_dir), ["disk.raw"])
        with open(os.path.join(out_dir, "disk.raw"), "rb") as f:
            self.assertEqual(f.read(), b"disk-content")

    def test_no_download_flag(self):
        self._complete({"pc-image.raw": b"binary-raw-image-bytes"})
        ret, out, _ = self._run({"no_download": True})
        self.assertEqual(ret, 0)
        self.assertEqual(self.storage_gets, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, "deploy")))
        self.assertIn("artifacts/demo/bld-test1/pc-image.raw", out)

    def test_expired_artifacts_are_reported_not_downloaded(self):
        self._complete({"disk.raw": b"disk-content"})
        self.build.update(
            download_urls={}, artifacts=[], artifact_urls=[],
            artifacts_expired_at=1234.0, artifacts_expired_reason="pressure",
        )
        ret, _, err = self._run()
        self.assertEqual(ret, 1)
        self.assertIn(
            "artifacts of build bld-test1 expired (storage pressure); "
            "rebuild to get them again", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.storage_gets, [])

    def _evict_during_download(self, **expired):
        self._complete({"a.img": b"aaaaaaaa", "b.img": b"bbbbbbbb"})
        listing = dict(self.build)
        self.failures = {"a.img": 404, "b.img": 404}
        calls = []

        def get(url, **kwargs):
            if "/api/v1/builds/" in url:
                calls.append(url)
                if len(calls) == 1:
                    return self._json(listing)
                if expired.get("raises"):
                    raise requests.ConnectionError("down")
                return self._json({**listing, **{k: v for k, v in expired.items() if k != "raises"}})
            return self._http_get(url, **kwargs)

        self.get.side_effect = get

    def test_eviction_during_download_reports_expiry_once(self):
        self._evict_during_download(
            artifacts_expired_at=1234.0, artifacts_expired_reason="pressure")
        ret, _, err = self._run()
        self.assertEqual(ret, 1)
        self.assertEqual(err.count("artifacts of build bld-test1 expired (storage pressure)"), 1)
        self.assertNotIn("storage answered", err)
        self.assertEqual(len(self.storage_gets), 1)

    def test_not_found_on_a_live_build_keeps_failing_per_artifact(self):
        self._evict_during_download()
        ret, _, err = self._run()
        self.assertEqual(ret, 1)
        self.assertEqual(err.count("storage answered HTTP 404"), 2)
        self.assertEqual(len(self.storage_gets), 2)

    def test_failing_build_reread_falls_back_to_the_download_error(self):
        self._evict_during_download(raises=True)
        ret, _, err = self._run()
        self.assertEqual(ret, 1)
        self.assertEqual(err.count("storage answered HTTP 404"), 2)

    def test_failed_build_downloads_nothing(self):
        self._complete({"disk.raw": b"disk-content"})
        self.build["status"] = "failed"
        ret, _, _ = self._run()
        self.assertEqual(ret, 1)
        self.assertEqual(self.storage_gets, [])

    @mock.patch("seine.distributed.client.remote.build_remote")
    def test_build_cmd_cli_download_flags(self, mock_build_remote):
        mock_build_remote.return_value = 0
        cmd = BuildCmd()

        with self.assertRaises(SystemExit) as ctx:
            cmd.main([
                "--remote", "http://server:8000",
                "--dest-dir", "/tmp/custom-artifacts",
                "--no-download",
                self.spec_file,
            ])
        self.assertEqual(ctx.exception.code, 0)

        mock_build_remote.assert_called_once()
        kwargs = mock_build_remote.call_args[1]
        options = kwargs["options"]
        self.assertEqual(options["dest_dir"], "/tmp/custom-artifacts")
        self.assertTrue(options["no_download"])
