#!/usr/bin/env python3

import atexit
import avocado
import os
import shutil
import sys
import tempfile
import time

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

# BuildCmd() now reads settings.py's jobs default -- pointed at an
# empty, per-run directory so a developer's real settings.json can never
# change how many of these run in parallel.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="seine-build-tests-config-")
atexit.register(shutil.rmtree, os.environ["XDG_CONFIG_HOME"], ignore_errors=True)

from seine import analyze
from seine import settings
from seine import tasks
from seine.build import BuildCmd
from seine.container import ContainerEngine

# BuildCmd's jobs default: 1 unless a persisted setting overrides it;
# an explicit -j/--jobs still wins either way.
class DefaultJobCount(avocado.Test):
    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def test_one_with_no_settings_file(self):
        self.assertEqual(BuildCmd().options["jobs"], 1)

    def test_the_persisted_value_otherwise(self):
        current = settings.load()
        current["jobs"] = 3
        settings.save(current)
        self.assertEqual(BuildCmd().options["jobs"], 3)

# 'resources' stays None until '--resource' is given, not {}.
class ResourceOptionIsParsed(avocado.Test):
    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        self.spec = os.path.join(self.workdir, "spec.yaml")
        with open(self.spec, "w") as f:
            f.write(MINIMAL)

    def test_unset_by_default(self):
        self.assertIsNone(BuildCmd().options["resources"])

    def test_one_class(self):
        build = BuildCmd()
        with self.assertRaises(SystemExit):
            build.main(["--resource", "net=2", "-D", self.spec])
        self.assertEqual(build.options["resources"], {"net": 2})

    def test_repeated_flags_accumulate(self):
        build = BuildCmd()
        with self.assertRaises(SystemExit):
            build.main(["--resource", "net=2", "--resource", "io=4",
                       "-D", self.spec])
        self.assertEqual(build.options["resources"], {"net": 2, "io": 4})

    def test_not_a_number_is_rejected(self):
        import contextlib
        import io

        build = BuildCmd()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                build.main(["--resource", "net=nope", self.spec])
        self.assertIn("--resource expects", err.getvalue())

    def test_zero_is_rejected(self):
        import contextlib
        import io

        build = BuildCmd()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                build.main(["--resource", "net=0", self.spec])
        self.assertIn("at least 1", err.getvalue())

MINIMAL = """
image:
    filename: simple-test.img
    partitions:
        - label: rootfs
          where: /
"""

# A real bug, found live: 'PartitionHandler.compute_sizes()' (called by
# the 'disk' task, seine/image.py's '_prepare_disk()') writes
# '_size'/'_start_mib'/'_end_mib' straight onto the same partition dicts
# the specification holds -- the same mistake already found and fixed
# once for playbooks (ansible_runner.py's '_run_playbooks()', commit
# 27b0267). 'Image.build()' used to take its digest *after* 'tasks.run()'
# had already run every task including 'disk', so a real build's record
# was filed under a digest 'seine plan'/'seine analyze' on freshly
# reloaded files -- which never runs 'disk' -- could never compute again.
class RecordedDigestSurvivesTaskMutation(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        self.real_run = tasks.run

        build = BuildCmd()
        build.loads(MINIMAL)
        build.parse()
        self.build = build

        # Stands in for what the real 'disk' task does mid-build, without
        # a real disk or a real partition table.
        def mutating_run(steps, jobs=1, resources=None, verbose=False,
                         logs=None, display=None, echo=False):
            self.build.image.partitionHandler.compute_sizes()
            for step in steps:
                step.started = step.ended = time.time()
                step.failed = False
        tasks.run = mutating_run

    def tearDown(self):
        tasks.run = self.real_run

    def test_the_recorded_digest_matches_a_fresh_reload(self):
        fresh = BuildCmd()
        fresh.loads(MINIMAL)
        fresh.parse()
        expected = analyze.spec_digest(fresh.spec)

        self.build.build()

        [run] = analyze.runs(expected)
        self.assertTrue(run["ok"])

IMAGELESS = """
distribution:
    release: trixie
    architecture: amd64
    uri: http://example.com/debian
packages:
    - source: apt://busybox
"""

VENDOR_ONLY = """
distribution:
    release: bookworm
    architecture: amd64
    uri: http://example.com/debian
vendor:
    - name: openssl
"""

# A real bug, found live: a spec with no 'image:' section crashed with
# a bare TypeError, since 'BuildCmd.parse()' skipped 'Image.parse()'
# entirely -- hit by a 'multiconfig:' group, which owns no disk of its
# own. Such a spec now parses fully and deploys its root tarball as
# real output.
class AnImageLessSpecificationWithSomethingToBuild(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_BUILD_DIR"] = self.workdir
        self.spec = os.path.join(self.workdir, "main.yaml")
        with open(self.spec, "w") as f:
            f.write(IMAGELESS)

    def parsed(self):
        build = BuildCmd()
        build.options["files"] = [self.spec]
        build.load_all([self.spec])
        build.parse()
        return build

    def test_it_parses_instead_of_staying_unparsed(self):
        build = self.parsed()
        self.assertIsNotNone(build.image.spec)
        self.assertEqual(len(build.image.packages), 1)

    # Named from the spec file's own basename, scoped under the
    # release, the same as an image-bearing specification's own
    # 'filename:' -- see Image._rootfs_output().
    def test_the_tarball_is_named_after_the_spec_file(self):
        build = self.parsed()
        self.assertEqual(
            build.image._output,
            os.path.join(ContainerEngine.deploy_root(), "trixie", "main.tar"))

    def test_the_rootfs_task_is_the_output_instead_of_a_disk(self):
        names = {t.name for t in self.parsed().image.tasks()}
        self.assertIn("rootfs", names)
        self.assertNotIn("deploy-rootfs", names)
        self.assertNotIn("disk", names)
        self.assertNotIn("appliance", names)

    # 'tasks.run()' is stubbed the way this file's own
    # 'RecordedDigestSurvivesTaskMutation' stubs it: what is under test
    # is that 'Image.build()' reaches it at all instead of crashing
    # first, not a real container build.
    def test_a_real_build_no_longer_crashes(self):
        build = self.parsed()

        def fake_run(steps, jobs=1, resources=None, verbose=False,
                    logs=None, display=None, echo=False):
            for step in steps:
                step.started = step.ended = time.time()
                step.failed = False
        real_run, tasks.run = tasks.run, fake_run
        try:
            build.build()
        finally:
            tasks.run = real_run

# 'rootfs' skips the playbooks when the deployed tarball and its
# '.digest' are current.
class TheRootfsTarballIsReusedWhileItsInputsAreUnchanged(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_BUILD_DIR"] = self.workdir
        self.spec = os.path.join(self.workdir, "main.yaml")
        with open(self.spec, "w") as f:
            f.write(IMAGELESS)
        self.image = self.parsed().image
        self.digest = self.image._rootfs_digest(None)

    def parsed(self):
        build = BuildCmd()
        build.options["files"] = [self.spec]
        build.load_all([self.spec])
        build.parse()
        build.image._from = "base"
        return build

    def deploy(self, digest):
        with open(self.image._rootfs, "w") as f:
            f.write("stands in for a real exported root file-system")
        with open(self.image._digest_file(), "w") as f:
            f.write(digest + "\n")

    def test_a_matching_digest_is_current(self):
        self.deploy(self.digest)
        self.assertTrue(self.image._rootfs_current(self.digest))

    def test_a_different_digest_is_not(self):
        self.deploy("something else")
        self.assertFalse(self.image._rootfs_current(self.digest))

    def test_a_missing_tarball_is_not(self):
        self.deploy(self.digest)
        os.unlink(self.image._rootfs)
        self.assertFalse(self.image._rootfs_current(self.digest))

    def test_a_missing_digest_is_not(self):
        self.deploy(self.digest)
        os.unlink(self.image._digest_file())
        self.assertFalse(self.image._rootfs_current(self.digest))

    def test_the_task_returns_without_a_container_when_current(self):
        self.deploy(self.digest)
        self.image.hostBootstrap = None
        self.image.rootfs()
        self.assertEqual(self.image._tarball, self.image._rootfs)

    def test_a_playbook_change_changes_the_digest(self):
        self.image.spec["playbook"].append({"tasks": []})
        self.assertNotEqual(self.image._rootfs_digest(None), self.digest)

    def test_a_disk_only_change_does_not(self):
        self.image.spec["image"] = {"filename": "other.img"}
        self.image.spec["containers"] = [{"image": "x"}]
        self.assertEqual(self.image._rootfs_digest(None), self.digest)

    def test_a_host_file_a_playbook_copies_changes_the_digest(self):
        with open(os.path.join(self.workdir, "motd"), "w") as f:
            f.write("one")
        self.image.spec["playbook"] = [{"tasks": [{"copy": {"src": "motd"}}]}]
        before = self.image._rootfs_digest(None)
        with open(os.path.join(self.workdir, "motd"), "w") as f:
            f.write("two")
        self.assertNotEqual(self.image._rootfs_digest(None), before)

    def test_a_vars_file_a_playbook_loads_changes_the_digest(self):
        with open(os.path.join(self.workdir, "v.yaml"), "w") as f:
            f.write("a: 1\n")
        self.image.spec["playbook"] = [{"vars_files": ["v.yaml"], "tasks": []}]
        before = self.image._rootfs_digest(None)
        with open(os.path.join(self.workdir, "v.yaml"), "w") as f:
            f.write("a: 2\n")
        self.assertNotEqual(self.image._rootfs_digest(None), before)

    def test_the_vendor_lock_changes_the_digest(self):
        self.assertNotEqual(self.image._rootfs_digest("lock"), self.digest)

WITH_IMAGE = """
distribution:
    release: trixie
    architecture: amd64
    uri: http://example.com/debian
image:
    filename: test.img
    partitions:
        - label: root
          type: ext4
          where: /
packages:
    - source: apt://busybox
"""

# 'disk' and 'image' skip disk allocation, appliance unpacking and guestfs
# when the deployed disk image and its '.digest' are current.
class TheDiskImageIsReusedWhileItsInputsAreUnchanged(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_BUILD_DIR"] = self.workdir
        self.spec = os.path.join(self.workdir, "main.yaml")
        with open(self.spec, "w") as f:
            f.write(WITH_IMAGE)
        self.image = self.parsed().image
        self.digest = self.image._image_digest()

    def parsed(self):
        build = BuildCmd()
        build.options["files"] = [self.spec]
        build.load_all([self.spec])
        build.parse()
        build.image._from = "base"
        return build

    def deploy(self, digest):
        with open(self.image._output, "w") as f:
            f.write("stands in for a real disk image")
        with open(self.image._image_digest_file(), "w") as f:
            f.write(digest + "\n")

    def test_a_matching_digest_is_current(self):
        self.deploy(self.digest)
        self.assertTrue(self.image._image_current(self.digest))

    def test_a_different_digest_is_not(self):
        self.deploy("something else")
        self.assertFalse(self.image._image_current(self.digest))

    def test_a_missing_image_is_not(self):
        self.deploy(self.digest)
        os.unlink(self.image._output)
        self.assertFalse(self.image._image_current(self.digest))

    def test_a_missing_digest_is_not(self):
        self.deploy(self.digest)
        os.unlink(self.image._image_digest_file())
        self.assertFalse(self.image._image_current(self.digest))

    def test_prepare_disk_is_skipped_when_current(self):
        self.deploy(self.digest)
        self.image._empty_disk = None
        self.image._prepare_disk()
        self.assertIsNone(self.image._image)

    def test_imager_prepare_and_build_skip_when_current(self):
        self.deploy(self.digest)
        from seine.imager import Imager
        imager = Imager(self.image)
        imager._prepare()
        self.assertIsNone(imager._output_dir)
        imager.create = None
        imager._build()

    def test_an_image_spec_change_changes_the_digest(self):
        self.image.spec["image"]["size"] = "4GiB"
        self.assertNotEqual(self.image._image_digest(), self.digest)

    def test_a_rootfs_change_changes_the_image_digest(self):
        self.image.spec["packages"].append({"source": "apt://vim"})
        self.assertNotEqual(self.image._image_digest(), self.digest)

# The other side of the same fix: a specification with neither 'image:'
# nor anything to build ('packages:'/'playbook:') is still refused --
# with a clean message now, instead of the same crash.
class AVendorOnlySpecificationStillRefusesToBuild(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_BUILD_DIR"] = self.workdir

    def test_parse_still_leaves_it_unparsed(self):
        build = BuildCmd()
        build.loads(VENDOR_ONLY)
        build.parse()
        self.assertIsNone(build.image.spec)

    def test_build_refuses_cleanly_instead_of_crashing(self):
        build = BuildCmd()
        build.loads(VENDOR_ONLY)
        build.parse()
        with self.assertRaises(ValueError) as raised:
            build.build()
        self.assertIn("no 'image:' section", str(raised.exception))

if __name__ == "__main__":
    avocado.main()
