# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for capability-aware build scheduler and package fan-out."""

import os
import shutil
import tempfile
import threading
import time

from avocado import Test

from seine.distributed.common.models import WorkerModel
from seine.distributed.server.db import Database
from seine.distributed.server.scheduler import (
    CROSS_ARCH_SCORE,
    EMULATION_ARCH_SCORE,
    NATIVE_ARCH_SCORE,
    BuildScheduler,
    check_arch_constraints,
    decompose_build,
    decompose_multiconfig_targets,
    evaluate_arch_score,
    extract_package_name,
    is_worker_eligible,
    rank_candidate_workers,
    select_best_worker,
)


class WorkerCandidateRankingTest(Test):
    """Test worker candidate scoring, filtering, and priority ranking."""

    def test_arch_score_evaluation(self):
        worker_dict = {
            "id": "w-x86",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0, "arm64": 0.7, "riscv64": 0.3},
        }
        self.assertEqual(evaluate_arch_score(worker_dict, "amd64"), NATIVE_ARCH_SCORE)
        self.assertEqual(evaluate_arch_score(worker_dict, "arm64"), CROSS_ARCH_SCORE)
        self.assertEqual(evaluate_arch_score(worker_dict, "riscv64"), EMULATION_ARCH_SCORE)
        self.assertEqual(evaluate_arch_score(worker_dict, "mips64el"), 0.0)

        worker_model = WorkerModel(
            id="w-arm",
            hostname="arm.lan",
            native_arch="arm64",
            arch_scores={"arm64": 1.0, "amd64": 0.3},
            concurrency_slots=2,
            free_disk_gb=50.0,
            token="tok-arm",
            last_seen=time.time(),
            created_at=time.time(),
        )
        self.assertEqual(evaluate_arch_score(worker_model, "arm64"), 1.0)
        self.assertEqual(evaluate_arch_score(worker_model, "amd64"), 0.3)
        self.assertEqual(evaluate_arch_score(worker_model, "riscv64"), 0.0)

    def test_prefer_native_ranking(self):
        w_native = {
            "id": "w-native-arm",
            "native_arch": "arm64",
            "arch_scores": {"arm64": 1.0},
            "concurrency_slots": 1,
            "free_disk_gb": 40.0,
            "status": "online",
        }
        w_cross = {
            "id": "w-cross-arm",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0, "arm64": 0.7},
            "concurrency_slots": 1,
            "free_disk_gb": 80.0,
            "status": "online",
        }
        w_emu = {
            "id": "w-emu-arm",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0, "arm64": 0.3},
            "concurrency_slots": 1,
            "free_disk_gb": 100.0,
            "status": "online",
        }
        w_incompat = {
            "id": "w-riscv",
            "native_arch": "riscv64",
            "arch_scores": {"riscv64": 1.0},
            "concurrency_slots": 1,
            "free_disk_gb": 50.0,
            "status": "online",
        }

        workers = [w_emu, w_incompat, w_cross, w_native]
        ranked = rank_candidate_workers(workers, target_arch="arm64")

        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0]["id"], "w-native-arm")
        self.assertEqual(ranked[1]["id"], "w-cross-arm")
        self.assertEqual(ranked[2]["id"], "w-emu-arm")

    def test_require_native_constraint(self):
        w_native = {
            "id": "w-native",
            "native_arch": "arm64",
            "arch_scores": {"arm64": 1.0},
            "concurrency_slots": 1,
            "free_disk_gb": 20.0,
            "status": "online",
        }
        w_cross = {
            "id": "w-cross",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0, "arm64": 0.7},
            "concurrency_slots": 1,
            "free_disk_gb": 40.0,
            "status": "online",
        }
        workers = [w_cross, w_native]

        opts_dict = {"require_native": True}
        ranked = rank_candidate_workers(workers, target_arch="arm64", options=opts_dict)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["id"], "w-native")

        opts_cli = {"--require-native": True}
        ranked_cli = rank_candidate_workers(workers, target_arch="arm64", options=opts_cli)
        self.assertEqual(len(ranked_cli), 1)
        self.assertEqual(ranked_cli[0]["id"], "w-native")

    def test_min_arch_score_constraint(self):
        w_native = {
            "id": "w-native",
            "native_arch": "arm64",
            "arch_scores": {"arm64": 1.0},
            "concurrency_slots": 1,
            "free_disk_gb": 20.0,
            "status": "online",
        }
        w_cross = {
            "id": "w-cross",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0, "arm64": 0.7},
            "concurrency_slots": 1,
            "free_disk_gb": 40.0,
            "status": "online",
        }
        w_emu = {
            "id": "w-emu",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0, "arm64": 0.3},
            "concurrency_slots": 1,
            "free_disk_gb": 60.0,
            "status": "online",
        }
        workers = [w_emu, w_cross, w_native]

        opts = {"min_arch_score": 0.5}
        ranked = rank_candidate_workers(workers, target_arch="arm64", options=opts)
        self.assertEqual(len(ranked), 2)
        self.assertEqual([w["id"] for w in ranked], ["w-native", "w-cross"])

        opts_cli = {"--min-arch-score": 0.8}
        ranked_cli = rank_candidate_workers(workers, target_arch="arm64", options=opts_cli)
        self.assertEqual(len(ranked_cli), 1)
        self.assertEqual(ranked_cli[0]["id"], "w-native")

    def test_ranking_tie_breaker_by_slots_and_disk(self):
        w1 = {
            "id": "w1",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0},
            "concurrency_slots": 2,
            "free_disk_gb": 30.0,
            "status": "online",
        }
        w2 = {
            "id": "w2",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0},
            "concurrency_slots": 2,
            "free_disk_gb": 90.0,
            "status": "online",
        }
        active_counts = {"w1": 1, "w2": 0}
        ranked = rank_candidate_workers(
            [w1, w2],
            target_arch="amd64",
            active_job_counts=active_counts,
        )
        self.assertEqual(ranked[0]["id"], "w2")

        active_equal = {"w1": 0, "w2": 0}
        ranked_disk = rank_candidate_workers(
            [w1, w2],
            target_arch="amd64",
            active_job_counts=active_equal,
        )
        self.assertEqual(ranked_disk[0]["id"], "w2")


class ConcurrencySlotsTest(Test):
    """Test concurrency slot tracking and busy worker exclusion."""

    def test_concurrency_slot_limits(self):
        w_single = {
            "id": "w-single",
            "native_arch": "arm64",
            "arch_scores": {"arm64": 1.0},
            "concurrency_slots": 1,
            "status": "online",
        }
        self.assertTrue(is_worker_eligible(w_single, "arm64", active_jobs=0))
        self.assertFalse(is_worker_eligible(w_single, "arm64", active_jobs=1))

        w_multi = {
            "id": "w-multi",
            "native_arch": "arm64",
            "arch_scores": {"arm64": 1.0},
            "concurrency_slots": 3,
            "status": "online",
        }
        self.assertTrue(is_worker_eligible(w_multi, "arm64", active_jobs=0))
        self.assertTrue(is_worker_eligible(w_multi, "arm64", active_jobs=2))
        self.assertFalse(is_worker_eligible(w_multi, "arm64", active_jobs=3))

    def test_busy_native_worker_fallback_to_cross(self):
        w_native = {
            "id": "w-native-arm",
            "native_arch": "arm64",
            "arch_scores": {"arm64": 1.0},
            "concurrency_slots": 1,
            "status": "online",
        }
        w_cross = {
            "id": "w-cross-arm",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0, "arm64": 0.7},
            "concurrency_slots": 2,
            "status": "online",
        }
        workers = [w_native, w_cross]

        active_counts = {"w-native-arm": 1, "w-cross-arm": 0}
        best = select_best_worker(
            workers,
            target_arch="arm64",
            active_job_counts=active_counts,
        )
        self.assertIsNotNone(best)
        self.assertEqual(best["id"], "w-cross-arm")

        active_counts["w-native-arm"] = 0
        best_freed = select_best_worker(
            workers,
            target_arch="arm64",
            active_job_counts=active_counts,
        )
        self.assertEqual(best_freed["id"], "w-native-arm")

    def test_offline_workers_are_excluded(self):
        w_offline = {
            "id": "w-offline",
            "native_arch": "arm64",
            "arch_scores": {"arm64": 1.0},
            "concurrency_slots": 4,
            "status": "offline",
        }
        self.assertFalse(is_worker_eligible(w_offline, "arm64", active_jobs=0))
        self.assertIsNone(select_best_worker([w_offline], "arm64"))


class CoarseTargetSchedulingTest(Test):
    """Test coarse-grained scheduling for single and multiconfig image targets."""

    def test_single_target_dispatch_to_optimal_worker(self):
        w_x86 = {
            "id": "worker-x86",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0},
            "concurrency_slots": 2,
            "status": "online",
        }
        w_arm = {
            "id": "worker-arm",
            "native_arch": "arm64",
            "arch_scores": {"arm64": 1.0},
            "concurrency_slots": 2,
            "status": "online",
        }
        pool = [w_x86, w_arm]

        spec_arm = {"architecture": "arm64"}
        jobs_arm = decompose_multiconfig_targets("bld-1", spec_arm)
        self.assertEqual(len(jobs_arm), 1)
        self.assertEqual(jobs_arm[0]["kind"], "image")
        self.assertEqual(jobs_arm[0]["target_arch"], "arm64")

        matched = select_best_worker(pool, target_arch=jobs_arm[0]["target_arch"])
        self.assertEqual(matched["id"], "worker-arm")

    def test_multiconfig_matrix_decomposition_and_routing(self):
        spec_multi = {
            "multiconfig": {
                "rescue": {"arch": "amd64"},
                "primary": {"arch": "arm64"},
            }
        }
        jobs = decompose_multiconfig_targets("bld-matrix", spec_multi)
        self.assertEqual(len(jobs), 2)

        job_targets = {j["target_name"]: j["target_arch"] for j in jobs}
        self.assertEqual(job_targets, {"rescue": "amd64", "primary": "arm64"})

        w_x86 = {
            "id": "worker-x86",
            "native_arch": "amd64",
            "arch_scores": {"amd64": 1.0},
            "concurrency_slots": 1,
            "status": "online",
        }
        w_arm = {
            "id": "worker-arm",
            "native_arch": "arm64",
            "arch_scores": {"arm64": 1.0},
            "concurrency_slots": 1,
            "status": "online",
        }
        pool = [w_x86, w_arm]

        for job in jobs:
            worker = select_best_worker(pool, target_arch=job["target_arch"])
            if job["target_name"] == "rescue":
                self.assertEqual(worker["id"], "worker-x86")
            elif job["target_name"] == "primary":
                self.assertEqual(worker["id"], "worker-arm")


class PackageFanOutSchedulingTest(Test):
    """Test fine-grained package decomposition and dependency sequencing."""

    def test_extract_package_name(self):
        self.assertEqual(extract_package_name("busybox"), "busybox")
        self.assertEqual(extract_package_name({"name": "hello"}), "hello")
        self.assertEqual(extract_package_name({"source": "apt://busybox"}), "busybox")
        self.assertEqual(extract_package_name({"source": "apt://busybox=1.37.0"}), "busybox")
        self.assertEqual(extract_package_name({"source": "git://host/repo/app.git"}), "app")
        self.assertEqual(
            extract_package_name({"source": "https://deb.debian.org/pool/main/t/tool_1.0.dsc"}),
            "tool",
        )

    def test_decompose_build_uncached_filtering(self):
        spec = {
            "packages": [
                {"source": "apt://busybox"},
                {"source": "git://github.com/example/custom-tool.git"},
                {"source": "https://deb.debian.org/tool_1.0.dsc"},
            ]
        }

        all_jobs = decompose_build("bld-full", spec, target_arch="arm64")
        self.assertEqual(len(all_jobs), 4)
        pkg_names = [j["package_name"] for j in all_jobs if j["kind"] == "package"]
        self.assertEqual(pkg_names, ["busybox", "custom-tool", "tool"])
        image_jobs = [j for j in all_jobs if j["kind"] == "image"]
        self.assertEqual(len(image_jobs), 1)
        self.assertEqual(image_jobs[0]["target_arch"], "arm64")

        cached = {"busybox"}
        filtered_jobs = decompose_build("bld-filtered", spec, target_arch="arm64", cached_packages=cached)
        self.assertEqual(len(filtered_jobs), 3)
        filtered_pkgs = [j["package_name"] for j in filtered_jobs if j["kind"] == "package"]
        self.assertEqual(filtered_pkgs, ["custom-tool", "tool"])


class DatabaseSchedulerIntegrationTest(Test):
    """Test scheduler integration with Database, dependency sequencing, and job claims."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-sched-")
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.db = Database(self.db_path)
        self.scheduler = BuildScheduler(self.db)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_dependency_sequencing_blocks_image_job_until_packages_complete(self):
        self.db.projects.create("demo-proj")
        self.db.workers.register(
            id="worker-arm",
            hostname="arm.lan",
            native_arch="arm64",
            arch_scores={"arm64": 1.0},
            free_disk_gb=50.0,
            token="tok-arm",
            concurrency_slots=2,
        )

        spec = {
            "packages": [
                {"source": "apt://busybox"},
                {"source": "git://host/tool.git"},
            ]
        }
        self.db.create_build_with_fanout(
            build_id="bld-seq",
            project="demo-proj",
            spec=spec,
            target_arch="arm64",
        )

        jobs = self.db.builds.list_jobs(build_id="bld-seq")
        self.assertEqual(len(jobs), 3)
        self.assertTrue(self.scheduler.is_image_job_blocked("bld-seq"))

        claimed1 = self.scheduler.claim_job("worker-arm")
        self.assertIsNotNone(claimed1)
        self.assertEqual(claimed1["kind"], "package")
        self.assertEqual(claimed1["package_name"], "busybox")

        claimed2 = self.scheduler.claim_job("worker-arm")
        self.assertIsNotNone(claimed2)
        self.assertEqual(claimed2["kind"], "package")
        self.assertEqual(claimed2["package_name"], "tool")

        self.assertIsNone(self.scheduler.claim_job("worker-arm"))

        self.db.builds.update_job_status(claimed1["job_id"], status="completed")
        self.assertTrue(self.scheduler.is_image_job_blocked("bld-seq"))

        self.assertIsNone(self.scheduler.claim_job("worker-arm"))

        self.db.builds.update_job_status(claimed2["job_id"], status="completed")
        self.assertFalse(self.scheduler.is_image_job_blocked("bld-seq"))

        claimed_image = self.scheduler.claim_job("worker-arm")
        self.assertIsNotNone(claimed_image)
        self.assertEqual(claimed_image["kind"], "image")
        self.assertEqual(claimed_image["target_arch"], "arm64")

        self.db.builds.update_job_status(claimed_image["job_id"], status="completed")
        build_rec = self.db.builds.get("bld-seq")
        self.assertEqual(build_rec["status"], "completed")

    def test_package_failure_aborts_image_job(self):
        self.db.projects.create("demo-fail")
        self.db.workers.register(
            id="worker-1",
            hostname="w1.lan",
            native_arch="amd64",
            arch_scores={"amd64": 1.0},
            free_disk_gb=30.0,
            token="tok-1",
            concurrency_slots=1,
        )

        spec = {"packages": [{"name": "failing-pkg"}]}
        self.db.create_build_with_fanout(
            build_id="bld-fail",
            project="demo-fail",
            spec=spec,
            target_arch="amd64",
        )

        claimed_pkg = self.scheduler.claim_job("worker-1")
        self.assertIsNotNone(claimed_pkg)
        self.assertEqual(claimed_pkg["kind"], "package")

        self.db.builds.update_job_status(claimed_pkg["job_id"], status="failed")

        bld = self.db.builds.get("bld-fail")
        self.assertEqual(bld["status"], "failed")
        self.assertTrue(self.scheduler.is_image_job_blocked("bld-fail"))
        self.assertIsNone(self.scheduler.claim_job("worker-1"))

        img = self.db.builds.get_job("job-bld-fail-img")
        self.assertEqual(img["status"], "cancelled")

    def _worker(self, worker_id="worker-1", slots=1, db=None):
        (db or self.db).workers.register(
            id=worker_id,
            hostname=f"{worker_id}.lan",
            native_arch="amd64",
            arch_scores={"amd64": 1.0},
            free_disk_gb=30.0,
            token=f"tok-{worker_id}",
            concurrency_slots=slots,
        )

    def test_claim_race_across_connections(self):
        self.db.projects.create("race-proj")
        self._worker("worker-a")
        self._worker("worker-b")
        self.db.create_build("bld-race", "race-proj", "amd64", "digest")

        other = Database(self.db_path)
        results = {}
        barrier = threading.Barrier(2)

        def run(name, db):
            barrier.wait()
            results[name] = db.scheduler.claim_job(f"worker-{name}")

        threads = [
            threading.Thread(target=run, args=("a", self.db)),
            threading.Thread(target=run, args=("b", other)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        other.close()

        winners = [r for r in results.values() if r is not None]
        self.assertEqual(len(winners), 1)
        job = self.db.builds.get_job("job-bld-race-img")
        self.assertEqual(job["status"], "claimed")
        self.assertEqual(job["worker_id"], f"worker-{'a' if results['a'] else 'b'}")

    def test_reap_stale_requeues_jobs_and_marks_worker_offline(self):
        self.db.projects.create("reap-proj")
        self._worker("worker-1")
        self._worker("worker-2")
        self.db.create_build("bld-reap", "reap-proj", "amd64", "digest")
        claimed = self.scheduler.claim_job("worker-1")
        self.assertIsNotNone(claimed)

        now = time.time()
        self.db.heartbeat_worker("worker-2", 10.0)
        self.assertEqual(self.scheduler.reap_stale(now=now + 10), [])

        reaped = self.scheduler.reap_stale(now=now + 200, stale_after=120.0)
        self.assertEqual(reaped, [claimed["job_id"]])
        self.assertEqual(self.db.get_worker("worker-1")["status"], "offline")
        self.assertEqual(self.db.get_worker("worker-2")["status"], "offline")

        job = self.db.builds.get_job(claimed["job_id"])
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["worker_id"])
        self.assertIsNone(job["started_at"])
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(self.scheduler.reap_stale(now=now + 400), [])

    def test_reap_stale_fails_job_after_max_attempts(self):
        self.db.projects.create("reap-proj")
        self._worker("worker-1")
        self.db.create_build("bld-max", "reap-proj", "amd64", "digest")
        job_id = "job-bld-max-img"

        now = time.time()
        for attempt in (1, 2):
            self.assertIsNotNone(self.scheduler.claim_job("worker-1"))
            self.assertEqual(self.scheduler.reap_stale(now=now + 1000 * attempt), [job_id])
            self.assertEqual(self.db.builds.get_job(job_id)["status"], "queued")
            self.db.workers.update_status("worker-1", "online")

        self.assertIsNotNone(self.scheduler.claim_job("worker-1"))
        self.assertEqual(self.scheduler.reap_stale(now=now + 5000), [job_id])
        job = self.db.builds.get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["attempts"], 3)
        self.assertEqual(self.db.builds.get("bld-max")["status"], "failed")

    def _lost_setup(self, worker="worker-1"):
        self.db.projects.create("lost-proj")
        self._worker(worker)
        self.scheduler.job_lost_grace = 90.0

    def _claim_started(self, worker="worker-1"):
        job_id = self.scheduler.claim_job(worker)["job_id"]
        return job_id, self.db.builds.get_job(job_id)["started_at"]

    def test_reconcile_requeues_unreported_job_only_after_grace(self):
        self._lost_setup()
        self.db.create_build("bld-lost", "lost-proj", "amd64", "digest")
        job_id, started = self._claim_started()

        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", [], now=started + 89), [])
        self.assertEqual(self.db.builds.get_job(job_id)["status"], "claimed")

        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", [], now=started + 91), [job_id])
        job = self.db.builds.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["worker_id"])
        self.assertIsNone(job["started_at"])
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", [], now=started + 200), [])

    def test_reconcile_boundary_is_exactly_the_grace(self):
        self._lost_setup()
        self.db.create_build("bld-edge", "lost-proj", "amd64", "digest")
        job_id, started = self._claim_started()

        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", [], now=started + 89.999), [])
        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", [], now=started + 90), [job_id])

    def test_reconcile_uses_the_scheduler_clock_by_default(self):
        self._lost_setup()
        self.db.create_build("bld-clock", "lost-proj", "amd64", "digest")
        job_id, started = self._claim_started()

        self.scheduler.clock = lambda: started + 100
        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", []), [job_id])

    def test_reconcile_leaves_reported_running_job_alone(self):
        self._lost_setup()
        self.db.create_build("bld-live", "lost-proj", "amd64", "digest")
        job_id, started = self._claim_started()
        self.db.builds.update_job_status(job_id, "running")

        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", [job_id], now=started + 1000), [])
        self.assertEqual(self.db.builds.get_job(job_id)["status"], "running")
        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", ["other"], now=started + 1000), [job_id])

    def test_reconcile_requeues_running_job(self):
        self._lost_setup()
        self.db.create_build("bld-run", "lost-proj", "amd64", "digest")
        job_id, started = self._claim_started()
        self.db.builds.update_job_status(job_id, "running")

        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", [], now=started + 1000), [job_id])
        self.assertEqual(self.db.builds.get_job(job_id)["status"], "queued")

    def test_reconcile_ignores_other_workers_and_terminal_jobs(self):
        self._lost_setup()
        self._worker("worker-2")
        self.db.create_build("bld-a", "lost-proj", "amd64", "digest")
        self.db.create_build("bld-b", "lost-proj", "amd64", "digest")
        job_a, started = self._claim_started("worker-1")
        job_b, _ = self._claim_started("worker-2")
        self.db.builds.update_job_status(job_b, "completed")

        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-2", [], now=started + 1000), [])
        self.assertEqual(self.db.builds.get_job(job_a)["status"], "claimed")
        self.assertEqual(self.db.builds.get_job(job_b)["status"], "completed")
        self.assertEqual(self.db.builds.get_job(job_b)["attempts"], 0)

    def test_reconcile_fails_job_and_build_after_max_attempts(self):
        self._lost_setup()
        spec = {"packages": [{"name": "pkg-a"}, {"name": "pkg-b"}]}
        self.db.create_build_with_fanout("bld-gone", "lost-proj", spec)
        job_id, started = self._claim_started()
        self.db.conn.execute("UPDATE jobs SET attempts = 2 WHERE id = ?", (job_id,))
        self.db.conn.commit()

        self.assertEqual(self.scheduler.reconcile_worker_jobs("worker-1", [], now=started + 100), [job_id])
        self.assertEqual(self.db.builds.get_job(job_id)["status"], "failed")
        self.assertEqual(self.db.builds.get_job(job_id)["attempts"], 3)
        build = self.db.builds.get("bld-gone")
        self.assertEqual(build["status"], "failed")
        self.assertEqual(build["error_message"], "job lost by its worker")
        statuses = {j["id"]: j["status"] for j in self.db.builds.list_jobs(build_id="bld-gone")}
        self.assertEqual(statuses["job-bld-gone-pkg-pkg-b"], "cancelled")
        self.assertEqual(statuses["job-bld-gone-img"], "cancelled")

    def test_reconcile_logs_each_lost_job(self):
        self._lost_setup()
        self.db.create_build("bld-log", "lost-proj", "amd64", "digest")
        job_id, started = self._claim_started()

        with self.assertLogs("seine.server", level="INFO") as logs:
            self.scheduler.reconcile_worker_jobs("worker-1", [], now=started + 100)
        self.assertEqual(len(logs.output), 1)
        self.assertIn(job_id, logs.output[0])

    def test_failed_package_fails_build_and_cancels_queued_siblings(self):
        self.db.projects.create("fail-proj")
        self._worker("worker-1")
        spec = {"packages": [{"name": "pkg-a"}, {"name": "pkg-b"}]}
        self.db.create_build_with_fanout("bld-sib", "fail-proj", spec)

        claimed = self.scheduler.claim_job("worker-1")
        self.assertEqual(claimed["package_name"], "pkg-a")
        self.db.builds.update_job_status(claimed["job_id"], "failed")

        self.assertEqual(self.db.builds.get("bld-sib")["status"], "failed")
        by_name = {j["id"]: j["status"] for j in self.db.builds.list_jobs(build_id="bld-sib")}
        self.assertEqual(by_name["job-bld-sib-pkg-pkg-a"], "failed")
        self.assertEqual(by_name["job-bld-sib-pkg-pkg-b"], "cancelled")
        self.assertEqual(by_name["job-bld-sib-img"], "cancelled")
        self.assertIsNone(self.scheduler.claim_job("worker-1"))

    def test_job_belongs_to(self):
        self.db.projects.create("own-proj")
        self._worker("worker-1")
        self._worker("worker-2")
        self.db.create_build("bld-own", "own-proj", "amd64", "digest")
        claimed = self.scheduler.claim_job("worker-1")

        self.assertTrue(self.scheduler.job_belongs_to(claimed["job_id"], "worker-1"))
        self.assertFalse(self.scheduler.job_belongs_to(claimed["job_id"], "worker-2"))
        self.assertFalse(self.scheduler.job_belongs_to("job-ghost", "worker-1"))

    def test_request_cancel_queued_build(self):
        self.db.projects.create("can-proj")
        self.db.create_build("bld-q", "can-proj", "amd64", "digest")

        self.assertTrue(self.scheduler.request_cancel("bld-q"))
        self.assertEqual(self.db.builds.get("bld-q")["status"], "cancelled")
        self.assertEqual(self.db.builds.get_job("job-bld-q-img")["status"], "cancelled")
        self.assertFalse(self.scheduler.request_cancel("bld-q"))
        self.assertFalse(self.scheduler.request_cancel("bld-ghost"))

    def test_request_cancel_running_build(self):
        self.db.projects.create("can-proj")
        self._worker("worker-1")
        spec = {"packages": [{"name": "pkg-a"}, {"name": "pkg-b"}]}
        self.db.create_build_with_fanout("bld-run", "can-proj", spec)
        claimed = self.scheduler.claim_job("worker-1")

        self.assertEqual(self.scheduler.cancel_requested_jobs("worker-1"), [])
        self.assertTrue(self.scheduler.request_cancel("bld-run"))

        self.assertEqual(self.scheduler.cancel_requested_jobs("worker-1"), [claimed["job_id"]])
        self.assertEqual(self.scheduler.cancel_requested_jobs("worker-2"), [])
        self.assertEqual(self.db.builds.get("bld-run")["status"], "running")
        statuses = {j["id"]: j["status"] for j in self.db.builds.list_jobs(build_id="bld-run")}
        self.assertEqual(statuses[claimed["job_id"]], "claimed")
        self.assertEqual(statuses["job-bld-run-img"], "cancelled")

        self.db.builds.update_job_status(claimed["job_id"], "cancelled")
        self.assertEqual(self.db.builds.get("bld-run")["status"], "cancelled")
        self.assertEqual(self.scheduler.cancel_requested_jobs("worker-1"), [])


class NativePreferenceTest(Test):
    """Test that claims prefer the best-scoring idle worker for a grace period."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="seine-test-native-")
        self.db = Database(os.path.join(self.tmp_dir, "test.db"))
        self.offset = 0.0
        self.scheduler = BuildScheduler(
            self.db, native_grace=30.0, stale_after=120.0, clock=lambda: time.time() + self.offset
        )
        self.db.projects.create("proj")
        self._worker("x86", "amd64", {"amd64": 1.0, "arm64": 0.7})
        self._worker("arm", "arm64", {"arm64": 1.0})

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _worker(self, worker_id, native_arch, scores, slots=1):
        self.db.workers.register(
            id=worker_id, hostname=f"{worker_id}.lan", native_arch=native_arch,
            arch_scores=scores, free_disk_gb=30.0, token=f"tok-{worker_id}",
            concurrency_slots=slots,
        )

    def _build(self, build_id="bld-1", options=None):
        self.db.create_build(build_id, "proj", "arm64", "digest", options=options or {})

    def test_native_worker_wins_even_if_cross_worker_polls_first(self):
        self._build()
        self.assertIsNone(self.scheduler.claim_job("x86"))
        self.assertIsNotNone(self.scheduler.claim_job("arm"))

    def test_cross_worker_takes_the_job_after_the_grace(self):
        self._build()
        self.assertIsNone(self.scheduler.claim_job("x86"))
        self.offset = 31.0
        self.assertEqual(self.scheduler.claim_job("x86")["build_id"], "bld-1")

    def test_busy_native_worker_does_not_block_the_cross_worker(self):
        self._build("bld-1")
        self._build("bld-2")
        self.assertIsNotNone(self.scheduler.claim_job("arm"))
        self.assertEqual(self.scheduler.claim_job("x86")["build_id"], "bld-2")

    def test_offline_better_worker_does_not_block(self):
        self._build()
        with self.db.conn:
            self.db.conn.execute("UPDATE workers SET status = 'offline' WHERE id = 'arm'")
        self.assertIsNotNone(self.scheduler.claim_job("x86"))

    def test_stale_better_worker_does_not_block(self):
        self._build()
        self.offset = 121.0
        self.db.heartbeat_worker("x86", 30.0)
        self.scheduler.native_grace = 1000.0
        self.assertIsNotNone(self.scheduler.claim_job("x86"))

    def test_grace_zero_disables_the_preference(self):
        self.scheduler.native_grace = 0.0
        self._build()
        self.assertIsNotNone(self.scheduler.claim_job("x86"))

    def test_equal_workers_are_unaffected(self):
        self._worker("arm2", "arm64", {"arm64": 1.0})
        self._build()
        self.assertIsNotNone(self.scheduler.claim_job("arm2"))

    def test_require_native_still_refuses_cross_workers(self):
        self._build(options={"require_native": True})
        self.offset = 1000.0
        self.assertIsNone(self.scheduler.claim_job("x86"))
        self.assertIsNotNone(self.scheduler.claim_job("arm"))
