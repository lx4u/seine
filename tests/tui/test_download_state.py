# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import sys
from unittest import mock

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.tui import download
from seine.tui.download import DownloadState


class DownloadStateTest(avocado.Test):
    """
    :avocado: tags=tui
    """

    def test_idle_until_something_is_queued(self):
        state = DownloadState()
        self.assertFalse(state.active)
        self.assertIsNone(state.percent())

    def test_percent_follows_the_bytes_of_the_whole_batch(self):
        state = DownloadState()
        state.queue("b1", "disk.raw", 800)
        state.queue("b1", "rootfs.tar", 200)
        self.assertTrue(state.active)
        self.assertEqual(state.percent(), 0)

        state.start("b1", "disk.raw")
        state.advance("b1", "disk.raw", 400)
        self.assertEqual(state.percent(), 40)
        state.advance("b1", "disk.raw", 400)
        state.finish("b1", "disk.raw")
        self.assertEqual(state.percent(), 80)
        self.assertTrue(state.active)

        state.start("b1", "rootfs.tar")
        state.advance("b1", "rootfs.tar", 200)
        state.finish("b1", "rootfs.tar")
        self.assertEqual(state.percent(), 100)
        self.assertFalse(state.active)

    def test_percent_is_unknown_without_sizes(self):
        state = DownloadState()
        state.queue("b1", "a", None)
        state.start("b1", "a")
        state.advance("b1", "a", 10)
        self.assertTrue(state.active)
        self.assertIsNone(state.percent())

    def test_a_failure_ends_the_activity_and_is_reported(self):
        state = DownloadState()
        state.queue("b1", "a", 100)
        state.start("b1", "a")
        state.advance("b1", "a", 30)
        state.finish("b1", "a", failed=True)
        self.assertFalse(state.active)
        self.assertEqual(state.snapshot()[("b1", "a")]["state"], "failed")
        self.assertEqual(state.snapshot()[("b1", "a")]["read"], 30)

    def test_a_new_batch_does_not_count_the_previous_one(self):
        state = DownloadState()
        state.queue("b1", "old", 1000)
        state.start("b1", "old")
        state.advance("b1", "old", 1000)
        state.finish("b1", "old")
        state.queue("b2", "new", 100)
        state.start("b2", "new")
        state.advance("b2", "new", 25)
        self.assertEqual(state.percent(), 25)
        self.assertEqual(state.snapshot()[("b1", "old")]["state"], "done")

    def test_redraws_are_throttled(self):
        state = DownloadState()
        state.queue("b1", "a", 1000)
        state.start("b1", "a")
        clock = iter([10.0, 10.05, 10.1, 10.3])
        with mock.patch.object(download.time, "monotonic", side_effect=lambda: next(clock)):
            due = [state.advance("b1", "a", 1) for _ in range(4)]
        self.assertEqual(due, [True, False, False, True])

    def test_cancel_is_remembered_until_a_new_batch_starts(self):
        state = DownloadState()
        self.assertFalse(state.cancelled)
        state.queue("b1", "a", 10)
        state.start("b1", "a")
        state.cancel()
        self.assertTrue(state.cancelled)
        state.finish("b1", "a", failed=True)
        self.assertTrue(state.cancelled)
        state.queue("b2", "b", 10)
        self.assertFalse(state.cancelled)

    def test_snapshot_is_a_copy(self):
        state = DownloadState()
        state.queue("b1", "a", 10)
        copy = state.snapshot()
        copy[("b1", "a")]["state"] = "done"
        self.assertEqual(state.snapshot()[("b1", "a")]["state"], "queued")
