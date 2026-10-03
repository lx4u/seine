# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import asyncio
import os
import shutil
import tempfile
from unittest import mock

from avocado import Test
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from seine.distributed.server.api import create_app
from seine.distributed.server.db import Database
from seine.distributed.server.events import ProjectEvents, build_event
from seine.distributed.server.ws import (
    CLOSE_FORBIDDEN,
    CLOSE_NOT_FOUND,
    CLOSE_UNAUTHENTICATED,
)

URL = "/api/v1/projects/demo/events"


class ProjectEventsTest(Test):
    """Tests for the fan-out of build events to project followers."""

    def test_publish_reaches_only_the_project_subscribers(self):
        events = ProjectEvents()

        async def scenario():
            mine, other = events.subscribe("demo"), events.subscribe("elsewhere")
            await asyncio.get_running_loop().run_in_executor(
                None, events.publish, "demo", {"type": "build_status"})
            self.assertEqual((await asyncio.wait_for(mine.get(), 2))["type"], "build_status")
            self.assertTrue(other.empty())

        asyncio.run(scenario())

    def test_a_full_queue_drops_events_instead_of_blocking(self):
        events = ProjectEvents()

        async def scenario():
            queue = events.subscribe("demo")
            for n in range(150):
                events._put(queue, {"n": n})
            self.assertEqual(queue.qsize(), 100)

        asyncio.run(scenario())

    def test_unsubscribe_forgets_the_project(self):
        events = ProjectEvents()

        async def scenario():
            queue = events.subscribe("demo")
            events.unsubscribe("demo", queue)

        asyncio.run(scenario())
        self.assertEqual(events._subscribers, {})

    def test_build_event_carries_the_spec_digest_and_duration(self):
        event = build_event({
            "id": "b", "project": "p", "target_arch": "arm64", "status": "completed",
            "created_at": 1.0, "started_at": 10.0, "finished_at": 25.0, "spec_digest": "abc",
            "artifact_meta": [{"name": "a.img", "size": 3, "sha256": "x", "key": "k"}],
        })
        self.assertEqual(event["spec_digest"], "abc")
        self.assertEqual(event["duration"], 15.0)
        self.assertEqual(event["artifacts"], [{"name": "a.img", "size": 3, "sha256": "x"}])
        self.assertFalse(event["artifacts_expired"])


class EventsEndpointTest(Test):
    """Integration tests for the /api/v1/projects/{project}/events WebSocket endpoint."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-events-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.app = create_app(db=self.db, enrollment_token="tok")
        self.client = TestClient(self.app)
        self.client.__enter__()
        for name in ("member", "outsider"):
            self.db.users.create(name)
        self.db.projects.create("demo")
        self.db.projects.add_member("demo", "member", "developer")
        self.token = self.db.tokens.issue(user_id="member")["token"]
        self.outsider = self.db.tokens.issue(user_id="outsider")["token"]
        self.db.create_build(
            build_id="bld-1", project="demo", target_arch="amd64",
            worktree_digest="d", spec_digest="abc")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _open(self, ws, token):
        ws.send_json({"auth": token})
        return ws.receive_json()

    def test_a_member_is_told_when_a_build_finishes(self):
        with self.client.websocket_connect(URL) as ws:
            self.assertEqual(self._open(ws, self.token)["type"], "subscribed")
            self.db.builds.update_status("bld-1", "completed", started_at=1.0, finished_at=4.0)
            event = ws.receive_json()
        self.assertEqual(
            (event["type"], event["build_id"], event["status"], event["spec_digest"]),
            ("build_status", "bld-1", "completed", "abc"))
        self.assertEqual(event["duration"], 3.0)

    def test_a_claimed_build_is_announced_as_running(self):
        self.db.upsert_worker("w1", "h", "amd64", {"amd64": 1.0}, 10.0, token="wtok")
        with self.client.websocket_connect(URL) as ws:
            self._open(ws, self.token)
            self.db.scheduler.claim_job("w1")
            self.assertEqual(ws.receive_json()["status"], "running")

    def test_expired_artifacts_are_announced(self):
        with self.client.websocket_connect(URL) as ws:
            self._open(ws, self.token)
            self.db.builds.mark_artifacts_expired("bld-1", "ttl")
            self.assertTrue(ws.receive_json()["artifacts_expired"])

    def test_other_projects_stay_quiet(self):
        self.db.projects.create("other")
        self.db.create_build(build_id="bld-2", project="other", target_arch="amd64",
                             worktree_digest="d")
        with self.client.websocket_connect(URL) as ws:
            self._open(ws, self.token)
            self.db.builds.update_status("bld-2", "failed")
            self.db.builds.update_status("bld-1", "failed")
            self.assertEqual(ws.receive_json()["build_id"], "bld-1")

    def test_a_failing_listener_does_not_break_the_update(self):
        self.db.builds.listeners.insert(0, mock.Mock(side_effect=RuntimeError("boom")))
        self.db.builds.update_status("bld-1", "failed")
        self.assertEqual(self.db.get_build("bld-1")["status"], "failed")

    def _refused(self, token):
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect(URL) as ws:
                ws.send_json({"auth": token})
                ws.receive_json()
        return ctx.exception.code

    def test_refusals(self):
        self.assertEqual(self._refused("nope"), CLOSE_UNAUTHENTICATED)
        self.assertEqual(self._refused(self.outsider), CLOSE_FORBIDDEN)
        self.db.upsert_worker("w1", "h", "amd64", {"amd64": 1.0}, 10.0, token="wtok")
        self.assertEqual(self._refused("wtok"), CLOSE_UNAUTHENTICATED)

    def test_an_unknown_project_is_404(self):
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect("/api/v1/projects/ghost/events") as ws:
                ws.send_json({"auth": self.token})
                ws.receive_json()
        self.assertEqual(ctx.exception.code, CLOSE_NOT_FOUND)

    def test_a_subscriber_that_talks_is_cut_off(self):
        with self.client.websocket_connect(URL) as ws:
            self._open(ws, self.token)
            ws.send_text("hello")
            with self.assertRaises(WebSocketDisconnect) as ctx:
                ws.receive_json()
        self.assertEqual(ctx.exception.code, CLOSE_FORBIDDEN)

    def test_leaving_unsubscribes(self):
        with self.client.websocket_connect(URL) as ws:
            self._open(ws, self.token)
        self.client.__exit__(None, None, None)
        self.assertEqual(self.app.state.events._subscribers, {})
        self.client.__enter__()
