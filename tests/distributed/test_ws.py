# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import asyncio
import os
import shutil
import tempfile
import time
from collections import deque
from unittest import mock

from avocado import Test
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database
from seine.distributed.server.ws import BroadcastHub


class BroadcastHubTest(Test):
    """Tests for BroadcastHub: connect, disconnect, broadcast, history replay."""

    def setUp(self):
        self.hub = BroadcastHub()
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        self.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _make_ws(self):
        """Return a mock WebSocket that records sent JSON payloads."""
        ws = mock.AsyncMock()
        ws.sent = []
        ws.send_json = mock.AsyncMock(side_effect=lambda msg: ws.sent.append(msg))
        ws.accept = mock.AsyncMock()
        return ws

    def test_connect_registers_listener(self):
        ws = self._make_ws()
        self._run(self.hub.connect("bld-001", ws))
        self.assertTrue(self.hub.has_listeners("bld-001"))
        # The endpoint accepts the socket before it authenticates it.
        ws.accept.assert_not_awaited()

    def test_disconnect_removes_listener(self):
        ws = self._make_ws()
        self._run(self.hub.connect("bld-001", ws))
        self.hub.disconnect("bld-001", ws)
        # Listener set should be gone after the last client disconnects.
        self.assertNotIn("bld-001", self.hub._listeners)

    def test_broadcast_delivers_to_all_listeners(self):
        ws1 = self._make_ws()
        ws2 = self._make_ws()
        self._run(self.hub.connect("bld-002", ws1))
        self._run(self.hub.connect("bld-002", ws2))
        msg = {"build_id": "bld-002", "source": "stdout", "text": "hello"}
        self._run(self.hub.broadcast("bld-002", msg))
        self.assertIn(msg, ws1.sent)
        self.assertIn(msg, ws2.sent)

    def test_broadcast_does_not_cross_builds(self):
        ws_a = self._make_ws()
        ws_b = self._make_ws()
        self._run(self.hub.connect("bld-A", ws_a))
        self._run(self.hub.connect("bld-B", ws_b))
        self._run(self.hub.broadcast("bld-A", {"text": "only for A"}))
        # ws_b should receive nothing from bld-A.
        self.assertEqual(ws_b.sent, [])

    def test_history_replayed_on_late_connect(self):
        ws_early = self._make_ws()
        self._run(self.hub.connect("bld-003", ws_early))
        msg1 = {"build_id": "bld-003", "text": "first"}
        msg2 = {"build_id": "bld-003", "text": "second"}
        self._run(self.hub.broadcast("bld-003", msg1))
        self._run(self.hub.broadcast("bld-003", msg2))

        ws_late = self._make_ws()
        self._run(self.hub.connect("bld-003", ws_late))
        # Late joiner must receive all prior messages via history replay.
        self.assertIn(msg1, ws_late.sent)
        self.assertIn(msg2, ws_late.sent)

    def test_broadcast_drops_failed_listener(self):
        ws_good = self._make_ws()
        ws_bad = self._make_ws()
        ws_bad.send_json = mock.AsyncMock(side_effect=RuntimeError("connection lost"))

        self._run(self.hub.connect("bld-004", ws_good))
        self._run(self.hub.connect("bld-004", ws_bad))
        # Should not raise; broken listener is silently dropped.
        self._run(self.hub.broadcast("bld-004", {"text": "ping"}))
        self.assertNotIn(ws_bad, self.hub._listeners.get("bld-004", set()))

    def test_history_per_build_is_bounded(self):
        hub = BroadcastHub(max_chunks=5)
        for i in range(20):
            self._run(hub.broadcast("bld-b", {"text": str(i)}))
        self.assertEqual([c["text"] for c in hub._history["bld-b"]], ["15", "16", "17", "18", "19"])

    def test_default_history_bound_is_10000_chunks(self):
        for i in range(10050):
            self._run(self.hub.broadcast("bld-big", {"text": "x"}))
        self.assertEqual(len(self.hub._history["bld-big"]), 10000)

    def test_retained_builds_are_bounded_lru(self):
        hub = BroadcastHub(max_builds=3)
        for name in ("b1", "b2", "b3"):
            self._run(hub.broadcast(name, {"text": name}))
        # Touching b1 makes b2 the least recently used.
        self._run(hub.broadcast("b1", {"text": "again"}))
        self._run(hub.broadcast("b4", {"text": "b4"}))
        self.assertEqual(set(hub._history), {"b1", "b3", "b4"})

    def test_default_build_bound_is_200(self):
        for i in range(300):
            self._run(self.hub.broadcast(f"bld-{i}", {"text": "x"}))
        self.assertEqual(len(self.hub._history), 200)
        self.assertIn("bld-299", self.hub._history)
        self.assertNotIn("bld-0", self.hub._history)

    def test_forget_clears_history_and_listeners(self):
        ws = self._make_ws()
        self._run(self.hub.connect("bld-f", ws))
        self._run(self.hub.broadcast("bld-f", {"text": "x"}))
        self.hub.forget("bld-f")
        self.assertNotIn("bld-f", self.hub._history)
        self.assertFalse(self.hub.has_listeners("bld-f"))
        self.hub.forget("bld-unknown")


class LogStreamerTest(Test):
    """Tests for LogStreamer: instantiation, send payload shape, error handling."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-streamer-")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_send_dispatches_correct_payload_shape(self):
        from seine.distributed.agent.stream import LogStreamer

        streamer = LogStreamer("http://localhost:8000", "bld-abc", "wtok")
        sent_payloads = []
        mock_ws = mock.MagicMock()
        mock_ws.send = mock.MagicMock(side_effect=lambda data: sent_payloads.append(data))
        streamer._ws = mock_ws

        streamer.send("stdout", "hello world")

        self.assertEqual(len(sent_payloads), 1)
        import json
        payload = json.loads(sent_payloads[0])
        self.assertEqual(payload["build_id"], "bld-abc")
        self.assertEqual(payload["source"], "stdout")
        self.assertEqual(payload["text"], "hello world")
        self.assertIn("timestamp", payload)

    def test_send_is_noop_when_not_connected(self):
        from seine.distributed.agent.stream import LogStreamer

        # _ws is None (no connect() called) — send must not raise.
        streamer = LogStreamer("http://localhost:8000", "bld-xyz", "wtok")
        streamer.send("stdout", "safe to drop")  # must not raise

    def test_connect_error_is_graceful(self):
        from seine.distributed.agent.stream import LogStreamer

        streamer = LogStreamer("ws://127.0.0.1:19999", "bld-err", "wtok")
        # Nothing is listening on port 19999; connect() must not raise.
        streamer.connect()
        self.assertIsNone(streamer._ws)

    def test_send_clears_ws_on_broken_connection(self):
        from seine.distributed.agent.stream import LogStreamer

        streamer = LogStreamer("http://localhost:8000", "bld-broken", "wtok")
        mock_ws = mock.MagicMock()
        mock_ws.send = mock.MagicMock(side_effect=OSError("pipe broken"))
        streamer._ws = mock_ws

        streamer.send("stderr", "oops")
        # After a send error _ws must be cleared so further sends are no-ops.
        self.assertIsNone(streamer._ws)

    def test_context_manager_closes_ws(self):
        from seine.distributed.agent.stream import LogStreamer

        streamer = LogStreamer("http://localhost:8000", "bld-ctx", "wtok")
        mock_ws = mock.MagicMock()
        streamer._ws = mock_ws

        # Call close() directly to verify it delegates to the underlying ws.
        streamer.close()

        mock_ws.close.assert_called_once()
        self.assertIsNone(streamer._ws)

    def test_connect_sends_worker_token_first(self):
        from seine.distributed.agent.stream import LogStreamer

        with mock.patch("seine.distributed.agent.stream.WsClient") as ws_cls:
            streamer = LogStreamer("https://srv:8443", "bld-auth", "wtok")
            streamer.connect()
        ws_cls.return_value.connect.assert_called_once()
        self.assertEqual(ws_cls.return_value.connect.call_args.args[0], "wss://srv:8443/api/v1/builds/bld-auth/stream")
        import json
        sent = ws_cls.return_value.send.call_args_list
        self.assertEqual(json.loads(sent[0].args[0]), {"auth": "wtok"})

    def test_agent_run_job_streams_with_the_worker_token(self):
        from seine.distributed.agent.daemon import WorkerAgent
        from seine.distributed.common.models import JobManifest

        agent = WorkerAgent("http://localhost:8000", "enroll", self.tmp_dir, "w1")
        agent.worker_token = "wt-test"
        agent.executor = mock.MagicMock()
        agent.executor.execute_job.return_value = 1
        agent.executor.cancelled = False
        manifest = JobManifest(job_id="job-1", build_id="bld-1", project="p", target_arch="amd64")

        with mock.patch("seine.distributed.agent.daemon.LogStreamer") as streamer, \
                mock.patch.object(agent, "update_job_status"):
            agent.run_job(manifest)

        streamer.assert_called_once_with("http://localhost:8000", "bld-1", "wt-test", ca_cert=None)

    def test_long_text_is_split_below_the_server_cap(self):
        import json
        from seine.distributed.agent.stream import LogStreamer, MAX_CHUNK

        with mock.patch("seine.distributed.agent.stream.WsClient") as ws_cls:
            streamer = LogStreamer("http://srv", "bld-big", "wtok")
            streamer.connect()
            streamer.send("stdout", "x" * (MAX_CHUNK * 2 + 10))
        sent = [json.loads(c.args[0]) for c in ws_cls.return_value.send.call_args_list[1:]]
        self.assertEqual([len(m["text"]) for m in sent], [MAX_CHUNK, MAX_CHUNK, 10])

    def test_split_never_cuts_a_multibyte_character(self):
        from seine.distributed.agent.stream import split_text

        text = "h\u00e9llo \u20ac" * 5000
        parts = split_text(text, limit=1000)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode()) <= 1000 for p in parts))

    def test_short_text_is_one_message(self):
        from seine.distributed.agent.stream import split_text

        self.assertEqual(split_text("line\n"), ["line\n"])

    def test_reconnect_waits_with_growing_backoff(self):
        from seine.distributed.agent import stream
        from seine.distributed.agent.stream import LogStreamer

        now = [100.0]
        with mock.patch("seine.distributed.agent.stream.WsClient") as ws_cls, \
                mock.patch("seine.distributed.agent.stream.time.monotonic", side_effect=lambda: now[0]):
            ws_cls.return_value.connect.side_effect = OSError("down")
            streamer = LogStreamer("http://srv", "bld-bo", "wtok")
            streamer.send("stdout", "a")
            streamer.send("stdout", "b")
            self.assertEqual(ws_cls.return_value.connect.call_count, 1)
            now[0] += 1.0
            streamer.send("stdout", "c")
            self.assertEqual(ws_cls.return_value.connect.call_count, 2)
            now[0] += 1.5
            streamer.send("stdout", "d")
            self.assertEqual(ws_cls.return_value.connect.call_count, 2)
            now[0] += 1.0
            streamer.send("stdout", "e")
            self.assertEqual(ws_cls.return_value.connect.call_count, 3)
            for _ in range(10):
                now[0] += stream.BACKOFF_MAX
                streamer.send("stdout", "f")
        self.assertEqual(streamer._backoff, stream.BACKOFF_MAX)

    def test_send_failure_reconnects_later_with_the_auth_message(self):
        import json
        from seine.distributed.agent.stream import LogStreamer

        now = [0.0]
        with mock.patch("seine.distributed.agent.stream.WsClient") as ws_cls, \
                mock.patch("seine.distributed.agent.stream.time.monotonic", side_effect=lambda: now[0]):
            streamer = LogStreamer("http://srv", "bld-re", "wtok")
            streamer.connect()
            ws_cls.return_value.send.side_effect = [OSError("dropped")]
            streamer.send("stdout", "lost")
            self.assertIsNone(streamer._ws)
            ws_cls.return_value.send.side_effect = None
            now[0] += 1.0
            streamer.send("stdout", "kept")
        self.assertEqual(ws_cls.return_value.connect.call_count, 2)
        sent = [json.loads(c.args[0]) for c in ws_cls.return_value.send.call_args_list]
        self.assertEqual(sent[-2], {"auth": "wtok"})
        self.assertEqual(sent[-1]["text"], "kept")

    def test_wss_uses_the_ca_cert(self):
        from seine.distributed.agent.stream import LogStreamer

        with mock.patch("seine.distributed.agent.stream.WsClient") as ws_cls, \
                mock.patch("ssl.create_default_context") as create:
            streamer = LogStreamer("https://srv", "bld-tls", "wtok", ca_cert="/etc/ca.pem")
            streamer.connect()
        create.assert_called_once_with(cafile="/etc/ca.pem")
        self.assertIs(ws_cls.return_value.connect.call_args.args[1], create.return_value)

    def test_ws_url_gets_no_ssl_argument(self):
        from seine.distributed.agent.stream import LogStreamer

        with mock.patch("seine.distributed.agent.stream.WsClient") as ws_cls:
            LogStreamer("http://localhost", "bld-p", "wtok", ca_cert="/etc/ca.pem").connect()
        self.assertIsNone(ws_cls.return_value.connect.call_args.args[1])

    def test_failed_auth_send_leaves_streamer_disconnected(self):
        from seine.distributed.agent.stream import LogStreamer

        with mock.patch("seine.distributed.agent.stream.WsClient") as ws_cls:
            ws_cls.return_value.send.side_effect = OSError("closed")
            streamer = LogStreamer("http://srv", "bld-auth", "wtok")
            streamer.connect()
        self.assertIsNone(streamer._ws)


class WebSocketEndpointTest(Test):
    """Integration tests for the /api/v1/builds/{id}/stream WebSocket endpoint."""

    URL = "/api/v1/builds/bld-ws/stream"

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-ws-")
        db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(db_path)
        self.app = create_app(db=self.db, enrollment_token="tok")
        # Entered as a context so all sockets share one event loop, like in production.
        self.client = TestClient(self.app)
        self.client.__enter__()

        for name in ("member", "outsider"):
            self.db.users.create(name)
        self.db.users.create("sysadmin", is_admin=True)
        self.db.projects.create("demo")
        self.db.projects.add_member("demo", "member", "developer")
        self.db.create_build(
            build_id="bld-ws", project="demo", target_arch="amd64",
            worktree_digest="d", user_id="member",
        )
        self.db.create_build(
            build_id="bld-other", project="demo", target_arch="amd64",
            worktree_digest="d", user_id="member",
        )
        self.member_tok = self.db.tokens.issue(user_id="member")["token"]
        self.outsider_tok = self.db.tokens.issue(user_id="outsider")["token"]
        self.admin_tok = self.db.tokens.issue(user_id="sysadmin")["token"]

        self.db.upsert_worker("w1", "h", "amd64", {"amd64": 1.0}, 10.0, token="wtok1")
        self.db.upsert_worker("w2", "h", "amd64", {"amd64": 1.0}, 10.0, token="wtok2")
        # w1 holds one job: the first queued one (bld-ws).
        self.db.scheduler.claim_job("w1")
        job = self.db.builds.list_jobs(build_id="bld-ws")[0]
        self.assertEqual(job["worker_id"], "w1")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _expect_close(self, ws, code):
        with self.assertRaises(WebSocketDisconnect) as ctx:
            ws.receive_json()
        self.assertEqual(ctx.exception.code, code)

    def test_sender_broadcasts_to_viewers_only(self):
        with self.client.websocket_connect(self.URL) as viewer:
            viewer.send_json({"auth": self.member_tok})
            with self.client.websocket_connect(self.URL) as sender:
                sender.send_json({"auth": "wtok1"})
                sender.send_json({"text": "line1", "source": "stderr"})
                got = viewer.receive_json()

        self.assertEqual(got["text"], "line1")
        self.assertEqual(got["source"], "stderr")
        self.assertEqual(got["build_id"], "bld-ws")
        self.assertIn("timestamp", got)

    def test_agent_log_streamer_end_to_end(self):
        from seine.distributed.agent.stream import LogStreamer

        with self.client.websocket_connect(self.URL) as viewer:
            viewer.send_json({"auth": self.admin_tok})
            streamer = LogStreamer("http://testserver", "bld-ws", "wtok1")
            with self.client.websocket_connect(self.URL) as sender:
                with mock.patch.object(streamer, "connect", lambda: setattr(streamer, "_ws", _SocketAdapter(sender, "wtok1"))):
                    streamer.send("stdout", "from agent")
                got = viewer.receive_json()
        self.assertEqual(got["text"], "from agent")

    def test_history_replayed_after_auth(self):
        with self.client.websocket_connect(self.URL) as sender:
            sender.send_json({"auth": "wtok1"})
            sender.send_json({"text": "early line"})
            # Wait until the hub holds the chunk before a viewer joins.
            deadline = time.time() + 5
            while not self.app.state.hub._history.get("bld-ws") and time.time() < deadline:
                time.sleep(0.01)
            with self.client.websocket_connect(self.URL) as viewer:
                viewer.send_json({"auth": self.member_tok})
                self.assertEqual(viewer.receive_json()["text"], "early line")

    def test_no_replay_before_auth(self):
        self.app.state.hub._history["bld-ws"] = deque([{"text": "secret"}])
        with self.client.websocket_connect(self.URL) as ws:
            ws.send_json({"auth": self.outsider_tok})
            self._expect_close(ws, 4403)

    def test_viewer_roles(self):
        for token in (self.member_tok, self.admin_tok):
            with self.client.websocket_connect(self.URL) as ws:
                ws.send_json({"auth": token})
                deadline = time.time() + 5
                while not self.app.state.hub.has_listeners("bld-ws") and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue(self.app.state.hub.has_listeners("bld-ws"))

    def test_viewer_cannot_send(self):
        with self.client.websocket_connect(self.URL) as ws:
            ws.send_json({"auth": self.member_tok})
            ws.send_json({"text": "forged"})
            self._expect_close(ws, 4403)
        self.assertFalse(self.app.state.hub._history.get("bld-ws"))

    def test_non_member_user_is_rejected(self):
        with self.client.websocket_connect(self.URL) as ws:
            ws.send_json({"auth": self.outsider_tok})
            self._expect_close(ws, 4403)

    def test_worker_of_another_build_is_rejected(self):
        for token in ("wtok2", "wtok1"):
            url = self.URL if token == "wtok2" else "/api/v1/builds/bld-other/stream"
            with self.client.websocket_connect(url) as ws:
                ws.send_json({"auth": token})
                self._expect_close(ws, 4403)

    def test_worker_that_finished_its_job_is_rejected(self):
        job = self.db.builds.list_jobs(build_id="bld-ws")[0]
        self.db.update_job_status(job["id"], "completed")
        with self.client.websocket_connect(self.URL) as ws:
            ws.send_json({"auth": "wtok1"})
            self._expect_close(ws, 4403)

    def test_bad_credentials_close_4401(self):
        for first in ({"auth": "nope"}, {"auth": 12}, {"auth": ""}, {"hello": 1}, [1, 2]):
            with self.client.websocket_connect(self.URL) as ws:
                ws.send_json(first)
                self._expect_close(ws, 4401)

    def test_non_json_first_message_closes_4401(self):
        with self.client.websocket_connect(self.URL) as ws:
            ws.send_text("not json")
            self._expect_close(ws, 4401)

    def test_inactive_user_is_rejected(self):
        self.db.users.update("member", active=False)
        with self.client.websocket_connect(self.URL) as ws:
            ws.send_json({"auth": self.member_tok})
            self._expect_close(ws, 4401)

    def test_auth_timeout_closes_4401(self):
        with mock.patch("seine.distributed.server.ws.AUTH_TIMEOUT", 0.2):
            with self.client.websocket_connect(self.URL) as ws:
                self._expect_close(ws, 4401)

    def test_unknown_build_closes_4404(self):
        url = "/api/v1/builds/bld-none/stream"
        for token in (self.member_tok, "wtok1"):
            with self.client.websocket_connect(url) as ws:
                ws.send_json({"auth": token})
                self._expect_close(ws, 4404)

    def test_sender_message_validation(self):
        bad = ([1], "text", {"nottext": 1}, {"text": 5}, {"text": "x", "source": 5})
        for payload in bad:
            with self.client.websocket_connect(self.URL) as ws:
                ws.send_json({"auth": "wtok1"})
                ws.send_json(payload)
                self._expect_close(ws, 1007)

    def test_sender_text_limit_is_64_kib(self):
        with self.client.websocket_connect(self.URL) as ws:
            ws.send_json({"auth": "wtok1"})
            ws.send_json({"text": "x" * (64 * 1024)})
            ws.send_json({"text": "x" * (64 * 1024 + 1)})
            self._expect_close(ws, 1009)
        self.assertEqual(len(self.app.state.hub._history["bld-ws"]), 1)

    def test_builds_are_isolated(self):
        with self.client.websocket_connect("/api/v1/builds/bld-other/stream") as viewer:
            viewer.send_json({"auth": self.member_tok})
            with self.client.websocket_connect(self.URL) as sender:
                sender.send_json({"auth": "wtok1"})
                sender.send_json({"text": "for bld-ws only"})
                deadline = time.time() + 5
                while not self.app.state.hub._history.get("bld-ws") and time.time() < deadline:
                    time.sleep(0.01)
        self.assertEqual(list(self.app.state.hub._history.get("bld-other", [])), [])

    def test_history_freed_when_build_ends_and_nobody_listens(self):
        hub = self.app.state.hub
        with self.client.websocket_connect(self.URL) as sender:
            sender.send_json({"auth": "wtok1"})
            sender.send_json({"text": "log"})
            deadline = time.time() + 5
            while not hub._history.get("bld-ws") and time.time() < deadline:
                time.sleep(0.01)
        # The sender is gone, the build is still running: history stays.
        self.assertIn("bld-ws", hub._history)

        job = self.db.builds.list_jobs(build_id="bld-ws")[0]
        resp = self.client.post(
            f"/api/v1/workers/jobs/{job['id']}/status",
            json={"worker_id": "w1", "build_id": "bld-ws", "job_id": job["id"], "status": "completed"},
            headers={"Authorization": "Bearer wtok1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("bld-ws", hub._history)

    def test_history_kept_while_a_viewer_listens_then_freed(self):
        hub = self.app.state.hub
        with self.client.websocket_connect(self.URL) as viewer:
            viewer.send_json({"auth": self.member_tok})
            with self.client.websocket_connect(self.URL) as sender:
                sender.send_json({"auth": "wtok1"})
                sender.send_json({"text": "log"})
                viewer.receive_json()
            job = self.db.builds.list_jobs(build_id="bld-ws")[0]
            resp = self.client.post(
                f"/api/v1/workers/jobs/{job['id']}/status",
                json={"worker_id": "w1", "build_id": "bld-ws", "job_id": job["id"], "status": "completed"},
                headers={"Authorization": "Bearer wtok1"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("bld-ws", hub._history)
        # The last viewer left after the build ended.
        deadline = time.time() + 5
        while "bld-ws" in hub._history and time.time() < deadline:
            time.sleep(0.01)
        self.assertNotIn("bld-ws", hub._history)


class _SocketAdapter:
    """Adapt a TestClient websocket to the send() API of the agent's websocket."""

    def __init__(self, ws, token):
        self._ws = ws
        self._ws.send_json({"auth": token})

    def send(self, data):
        self._ws.send_text(data)

    def close(self):
        pass
