# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import json
from unittest import mock

from avocado import Test

from seine.distributed.client import events
from seine.distributed.client.events import EventFollower
from seine.distributed.common.wsclient import WsClosed


class FakeWs:
    """Plays one scripted connection: messages, then an exception or a stop."""

    def __init__(self, script, follower=None):
        self.script = list(script)
        self.follower = follower
        self.sent = []

    def connect(self, url, ssl_context=None):
        self.url = url

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout=None):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


class EventFollowerTest(Test):
    """Tests for the project event follower and its reconnection."""

    def setUp(self):
        self.got = []
        self.follower = EventFollower("https://srv:8000", "demo", "tok", self.got.append)

    def _run(self, connections):
        fakes = iter(connections)
        waits = []

        def wait(delay):
            waits.append(delay)
            return len(waits) >= len(connections)

        self.follower._stop_event = mock.Mock(is_set=lambda: len(waits) >= len(connections),
                                              wait=wait)
        with mock.patch.object(events, "WsClient", lambda: next(fakes)):
            self.follower.run()
        return waits

    def test_url_auth_and_delivery(self):
        sub, ev = json.dumps({"type": "subscribed"}), json.dumps({"type": "build_status"})
        ws = FakeWs([sub, ev, WsClosed(1006)])
        self._run([ws])
        self.assertEqual(ws.url, "wss://srv:8000/api/v1/projects/demo/events")
        self.assertEqual(json.loads(ws.sent[0]), {"auth": "tok"})
        self.assertEqual([m["type"] for m in self.got], ["subscribed", "build_status"])

    def test_retries_back_off_and_reset_after_a_good_connection(self):
        sub = json.dumps({"type": "subscribed"})
        waits = self._run([
            FakeWs([WsClosed(1006)]), FakeWs([WsClosed(1006)]),
            FakeWs([sub, WsClosed(1006)]), FakeWs([WsClosed(1006)])])
        self.assertEqual(waits[:2], [1.0, 2.0])
        self.assertEqual(waits[2], 1.0)

    def test_delay_is_capped(self):
        waits = self._run([FakeWs([OSError("down")]) for _ in range(8)])
        self.assertEqual(max(waits), events.MAX_DELAY)

    def test_a_refusal_ends_the_follower(self):
        fakes = iter([FakeWs([WsClosed(4403)])])
        with mock.patch.object(events, "WsClient", lambda: next(fakes)):
            self.follower.run()
        self.assertEqual(self.got, [])

    def test_garbage_is_skipped(self):
        ws = FakeWs(["not json", json.dumps([1]), json.dumps({"type": "x"}), WsClosed(1006)])
        self._run([ws])
        self.assertEqual(self.got, [{"type": "x"}])

    def test_stop_ends_a_thread_waiting_to_retry(self):
        follower = EventFollower("http://srv", "demo", "tok", self.got.append)
        with mock.patch.object(events, "WsClient", lambda: FakeWs([WsClosed(1006)])):
            follower.start()
            follower.stop()
            follower.join(5)
        self.assertFalse(follower.is_alive())
