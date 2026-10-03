# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for storage housekeeping: TTL and pressure eviction of dev artifacts."""

import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from unittest import mock

from avocado import Test

from seine.distributed.server import housekeeping
from seine.distributed.server.db import Database
from seine.distributed.server.housekeeping import HousekeepingBusy, merge_lifecycle, run_housekeeping
from seine.distributed.server.settings import EnvRetention, Retention, Settings, Threshold
from seine.storage.s3.client import S3ClientError

NOW = 1_000_000.0
HOUR = 3600.0
GB = 1024**3


class FakeProvider:
    """A bucket held in a dict of key to size."""

    def __init__(self):
        self.objects = {}
        self.failing = set()
        self.gate = None
        self.modified = {}
        self.rules = []
        self.puts = []
        self.lifecycle_error = None

    def list_objects(self, prefix=""):
        stamp = lambda k: datetime.fromtimestamp(self.modified[k], timezone.utc).isoformat() if k in self.modified else None
        return [{"key": k, "size": s, "last_modified": stamp(k)} for k, s in sorted(self.objects.items()) if k.startswith(prefix)]

    def lifecycle_rules(self):
        if self.lifecycle_error:
            raise self.lifecycle_error
        return list(self.rules)

    def set_lifecycle_rules(self, rules):
        self.puts.append(rules)
        self.rules = rules

    def usage(self, prefix=""):
        if self.gate:
            self.gate.wait(5)
        return sum(s for k, s in self.objects.items() if k.startswith(prefix))

    def delete_prefix(self, prefix):
        if prefix in self.failing:
            raise S3ClientError("boom")
        if any(prefix == f"artifacts/core/{b}/" for b in self.failing):
            raise S3ClientError("boom")
        gone = {k: s for k, s in self.objects.items() if k.startswith(prefix)}
        for k in gone:
            del self.objects[k]
        return len(gone), sum(gone.values())


def env(**kw):
    values = dict(worktrees=None, artifacts=None, cache=None, high_water=None, low_water=None, min_age=HOUR)
    values.update(kw)
    return EnvRetention(**values)


class HousekeepingTest(Test):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-hk-")
        self.db = Database(os.path.join(self.tmp_dir, "hk.db"))
        self.db.projects.create("core")
        self.dev = FakeProvider()
        self.prod = FakeProvider()
        patcher = mock.patch.object(housekeeping, "provider_for", self._provider_for)
        patcher.start()
        self.patcher = patcher

    def tearDown(self):
        self.patcher.stop()
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _provider_for(self, settings, project, bucket, env_name):
        return self.dev if env_name == "dev" else self.prod

    def _settings(self, dev=None, prod=None):
        return Settings(retention=Retention(interval=60, dev=dev or env(), prod=prod or env()))

    def _build(self, id, age_h, size=GB, status="completed", release=False, provider=None):
        """A build finished age_h hours before NOW holding one artifact of size bytes."""
        provider = provider or (self.prod if release else self.dev)
        self.db.builds.create(id, "core", is_release=release, artifact_urls=[f"s3://b/{id}/a"])
        self.db.builds.update_status(
            id, status, finished_at=NOW - age_h * HOUR, artifact_meta=[{"name": "a", "size": size}]
        )
        provider.objects[f"artifacts/core/{id}/a"] = size

    def _run(self, settings, **kw):
        return run_housekeeping(self.db, settings, now=NOW, **kw)[0]

    def _expired(self, id):
        return self.db.builds.get(id)["artifacts_expired_reason"]

    def test_retention_none_does_nothing(self):
        self._build("b1", 1000)
        self.assertEqual(run_housekeeping(self.db, Settings(), now=NOW), [])
        self.assertEqual(len(self.dev.objects), 1)

    def test_ttl_evicts_and_marks(self):
        self._build("old", 48)
        self._build("new", 2)
        report = self._run(self._settings(env(artifacts=24 * HOUR)))
        self.assertEqual(report.evicted, [("old", "ttl", GB)])
        self.assertEqual(self._expired("old"), "ttl")
        self.assertIsNone(self._expired("new"))
        self.assertEqual(list(self.dev.objects), ["artifacts/core/new/a"])
        self.assertEqual((report.usage_before, report.usage_after), (2 * GB, GB))

    def test_pressure_oldest_first_stops_at_low_water(self):
        for i, age in enumerate((50, 40, 30, 20)):
            self._build(f"b{i}", age)
        high, low = Threshold(3 * GB, False), Threshold(2 * GB, False)
        report = self._run(self._settings(env(high_water=high, low_water=low)))
        self.assertEqual([e[0] for e in report.evicted], ["b0", "b1"])
        self.assertEqual({e[1] for e in report.evicted}, {"pressure"})
        self.assertEqual(report.usage_after, 2 * GB)
        self.assertIsNone(self._expired("b2"))

    def test_hysteresis_between_low_and_high(self):
        self._build("b0", 50)
        self._build("b1", 40)
        high, low = Threshold(3 * GB, False), Threshold(1 * GB, False)
        report = self._run(self._settings(env(high_water=high, low_water=low)))
        self.assertEqual(report.evicted, [])
        self.assertEqual(len(self.dev.objects), 2)

    def test_min_age_protects_recent_build(self):
        self._build("old", 5)
        self._build("fresh", 0.5)
        report = self._run(self._settings(env(high_water=Threshold(GB, False))))
        self.assertEqual([e[0] for e in report.evicted], ["old"])
        self.assertIsNone(self._expired("fresh"))

    def test_release_running_and_prod_untouched(self):
        self._build("rel", 100, release=True)
        self.db.builds.create("run", "core", artifact_urls=["s3://b/run/a"])
        self.db.builds.update_status("run", "running", started_at=1.0)
        self.dev.objects["artifacts/core/run/a"] = GB
        rules = dict(artifacts=HOUR, high_water=Threshold(1, False))
        self._run(self._settings(env(**rules), env(**rules)))
        self.assertIn("artifacts/core/rel/a", self.prod.objects)
        self.assertIn("artifacts/core/run/a", self.dev.objects)
        self.assertIsNone(self._expired("rel"))
        self.assertIsNone(self._expired("run"))

    def test_prod_above_high_water_only_warns(self):
        self._build("rel", 100, release=True)
        with self.assertLogs("seine.server.housekeeping", "WARNING") as logs:
            self._run(self._settings(prod=env(high_water=Threshold(1, False))))
        self.assertIn("above high_water", logs.output[0])
        self.assertEqual(len(self.prod.objects), 1)

    def test_percent_without_quota_is_skipped(self):
        self._build("old", 50)
        settings = self._settings(env(high_water=Threshold(1, True)))
        with self.assertLogs("seine.server.housekeeping", "WARNING"):
            report = self._run(settings)
        self.assertIn("quota", report.skipped_reason)
        self.assertEqual(report.evicted, [])

    def test_percent_with_quota(self):
        self.db.projects.set_quota("core", 4)
        for i, age in enumerate((50, 40, 30)):
            self._build(f"b{i}", age)
        settings = self._settings(env(high_water=Threshold(50, True), low_water=Threshold(25, True)))
        report = self._run(settings)
        self.assertEqual((report.high_water_bytes, report.low_water_bytes), (2 * GB, GB))
        self.assertEqual([e[0] for e in report.evicted], ["b0", "b1"])

    def test_candidates_exhausted_logs_error(self):
        self._build("only", 0.1)
        with self.assertLogs("seine.server.housekeeping", "ERROR"):
            report = self._run(self._settings(env(high_water=Threshold(1, False))))
        self.assertEqual(report.evicted, [])

    def test_failed_delete_does_not_stop_others(self):
        for i, age in enumerate((50, 40, 30)):
            self._build(f"b{i}", age)
        self.dev.failing = {"b0"}
        with self.assertLogs("seine.server.housekeeping", "ERROR"):
            report = self._run(self._settings(env(artifacts=HOUR)))
        self.assertEqual([e[0] for e in report.evicted], ["b1", "b2"])
        self.assertEqual([f[0] for f in report.failures], ["b0"])
        self.assertIsNone(self._expired("b0"))
        self.assertEqual(self._expired("b1"), "ttl")

    def test_dry_run_changes_nothing(self):
        self._build("old", 48)
        self._build("mid", 30)
        self._build("new", 2)
        rules = env(artifacts=40 * HOUR, high_water=Threshold(2 * GB, False), low_water=Threshold(GB, False))
        report = self._run(self._settings(rules), dry_run=True)
        self.assertTrue(report.dry_run)
        self.assertEqual([e[:2] for e in report.evicted], [("old", "ttl"), ("mid", "pressure")])
        self.assertEqual(report.usage_after, GB)
        self.assertEqual(len(self.dev.objects), 3)
        self.assertIsNone(self._expired("old"))

    def test_busy_while_running(self):
        self._build("b", 1)
        self.dev.gate = threading.Event()
        settings = self._settings()
        thread = threading.Thread(target=run_housekeeping, args=(self.db, settings))
        thread.start()
        try:
            for _ in range(100):
                if housekeeping._lock.locked():
                    break
                threading.Event().wait(0.05)
            with self.assertRaises(HousekeepingBusy):
                run_housekeeping(self.db, settings)
        finally:
            self.dev.gate.set()
            thread.join()
        self.assertFalse(housekeeping._lock.locked())


DAY = 24 * HOUR
FOREIGN = {"ID": "other", "Status": "Enabled", "Filter": {"Prefix": "x/"}, "Expiration": {"Days": 9}}


class LifecycleMergeTest(Test):
    def _days(self, rules):
        return {r["ID"]: r.get("Expiration", {}).get("Days") for r in rules}

    def test_empty_bucket_gets_both_rules(self):
        rules = merge_lifecycle([], 3 * DAY)
        self.assertEqual(self._days(rules), {"seine-abort-multipart": None, "seine-worktrees-expiry": 3})
        abort = rules[0]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"]
        self.assertEqual(abort, 1)

    def test_days_are_rounded_up_with_a_minimum_of_one(self):
        self.assertEqual(self._days(merge_lifecycle([], 90 * 60))["seine-worktrees-expiry"], 1)
        self.assertEqual(self._days(merge_lifecycle([], DAY + 1))["seine-worktrees-expiry"], 2)
        self.assertEqual(self._days(merge_lifecycle([], 14 * DAY))["seine-worktrees-expiry"], 14)

    def test_foreign_rules_are_kept_and_own_ones_replaced(self):
        old = {"ID": "seine-worktrees-expiry", "Status": "Enabled", "Filter": {"Prefix": "worktrees/"},
               "Expiration": {"Days": 30}}
        rules = merge_lifecycle([FOREIGN, old], 3 * DAY)
        self.assertEqual(rules[0], FOREIGN)
        self.assertEqual(self._days(rules)["seine-worktrees-expiry"], 3)
        self.assertEqual(len(rules), 3)

    def test_no_change_means_no_put(self):
        rules = merge_lifecycle([FOREIGN], 3 * DAY)
        self.assertIsNone(merge_lifecycle(rules, 3 * DAY))

    def test_a_reply_without_status_still_matches(self):
        rules = [{k: v for k, v in r.items() if k != "Status"} for r in merge_lifecycle([], 3 * DAY)]
        self.assertIsNone(merge_lifecycle(rules, 3 * DAY))

    def test_never_removes_the_worktrees_rule_only(self):
        rules = merge_lifecycle([FOREIGN] + merge_lifecycle([], 3 * DAY), None)
        self.assertEqual(self._days(rules), {"other": 9, "seine-abort-multipart": None})
        self.assertIsNone(merge_lifecycle(rules, None))


class WorktreeHousekeepingTest(HousekeepingTest):
    def _worktree(self, digest, age_d, provider=None, size=10):
        provider = provider or self.dev
        key = f"worktrees/core/{digest}.tar.zst"
        provider.objects[key] = size
        provider.modified[key] = NOW - age_d * DAY

    def _wt_settings(self):
        return self._settings(env(worktrees=3 * DAY), env(worktrees=14 * DAY))

    def test_only_aged_unreferenced_digests_are_deleted(self):
        self._worktree("old", 5)
        self._worktree("fresh", 1)
        self._worktree("busy", 9)
        self._worktree("running", 9)
        self.db.builds.create("q", "core", worktree_digest="busy")
        self.db.builds.create("r", "core", worktree_digest="running")
        self.db.builds.update_status("r", "running", started_at=1.0)
        report = self._run(self._wt_settings())
        self.assertEqual(report.worktrees, [("old", 10)])
        self.assertEqual(sorted(self.dev.objects), [
            "worktrees/core/busy.tar.zst", "worktrees/core/fresh.tar.zst", "worktrees/core/running.tar.zst"])

    def test_a_finished_build_no_longer_protects_its_digest(self):
        self._worktree("done", 5)
        self.db.builds.create("d", "core", worktree_digest="done")
        self.db.builds.update_status("d", "completed", finished_at=NOW)
        self.assertEqual(self._run(self._wt_settings()).worktrees, [("done", 10)])

    def test_prod_uses_its_own_ttl(self):
        self._worktree("a", 5, self.prod)
        self._worktree("b", 20, self.prod)
        self._worktree("c", 5, self.dev)
        report = self._run(self._wt_settings())
        self.assertEqual(report.worktrees, [("c", 10), ("b", 10)])
        self.assertEqual(list(self.prod.objects), ["worktrees/core/a.tar.zst"])

    def test_usage_before_counts_the_worktrees_that_are_swept(self):
        self._worktree("old", 5, size=7)
        self._build("b1", 1)
        report = self._run(self._wt_settings())
        self.assertEqual((report.usage_before, report.usage_after), (GB + 7, GB))

    def test_never_keeps_every_worktree(self):
        self._worktree("old", 100)
        report = self._run(self._settings())
        self.assertEqual(report.worktrees, [])
        self.assertEqual(len(self.dev.objects), 1)

    def test_other_projects_and_prefixes_are_spared(self):
        self.dev.objects["worktrees/core2/x.tar.zst"] = 1
        self.dev.modified["worktrees/core2/x.tar.zst"] = 0
        self.dev.objects["artifacts/core/x"] = 1
        self.assertEqual(self._run(self._wt_settings()).worktrees, [])

    def test_dry_run_lists_and_changes_nothing(self):
        self._worktree("old", 5)
        report = self._run(self._wt_settings(), dry_run=True)
        self.assertEqual(report.worktrees, [("old", 10)])
        self.assertEqual(len(self.dev.objects), 1)
        self.assertEqual((self.dev.puts, self.prod.puts), ([], []))

    def test_a_failed_delete_is_recorded_and_the_rest_goes_on(self):
        self._worktree("a", 5)
        self._worktree("b", 5)
        self.dev.failing = {"worktrees/core/a.tar.zst"}
        with self.assertLogs("seine.server.housekeeping", "ERROR"):
            report = self._run(self._wt_settings())
        self.assertEqual(report.worktrees, [("b", 10)])
        self.assertEqual([f[0] for f in report.failures], ["worktree a"])

    def test_rules_are_installed_in_both_buckets_once(self):
        self.dev.rules = [FOREIGN]
        settings = self._wt_settings()
        self._run(settings)
        self._run(settings)
        self.assertEqual(len(self.dev.puts), 1)
        self.assertEqual(len(self.prod.puts), 1)
        self.assertIn(FOREIGN, self.dev.rules)
        days = {r["ID"]: r.get("Expiration", {}).get("Days") for r in self.prod.rules}
        self.assertEqual(days["seine-worktrees-expiry"], 14)

    def test_lifecycle_failure_is_reported_and_the_sweep_goes_on(self):
        self.dev.lifecycle_error = S3ClientError("denied")
        self._worktree("old", 5)
        with self.assertLogs("seine.server.housekeeping", "WARNING"):
            report = self._run(self._wt_settings())
        self.assertEqual(report.lifecycle, ["dev: denied"])
        self.assertEqual(report.worktrees, [("old", 10)])
        self.assertEqual(len(self.prod.puts), 1)
