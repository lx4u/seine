#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.deb import patches as debpatches

RULES_PATCH = """\
--- a/debian/rules
+++ b/debian/rules
@@ -1 +1,2 @@
 one
+two
"""

UPSTREAM_PATCH = """\
--- a/main.c
+++ b/main.c
@@ -1 +1,2 @@
 one
+two
"""

class WithTemporaryDirectory(avocado.Test):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="seine-test-debpatches-",
                                        dir="/var/tmp")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

class ChangedPaths(WithTemporaryDirectory):
    def paths(self, text):
        return debpatches.changed_paths(self.write("a.patch", text))

    def test_plain_patch(self):
        self.assertEqual(self.paths(RULES_PATCH), ["debian/rules"])

    def test_new_and_deleted_files(self):
        text = ("--- /dev/null\n+++ b/debian/new\n@@ -0,0 +1 @@\n+x\n"
                "--- a/old.c\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        self.assertEqual(self.paths(text), ["debian/new", "old.c"])

    def test_rename(self):
        text = ("diff --git a/a b/b\nrename from a\nrename to b\n")
        self.assertEqual(self.paths(text), ["a", "b"])

    def test_timestamps_are_ignored(self):
        text = "--- a/x\t2026-01-01 00:00:00\n+++ b/x\t2026-01-02\n"
        self.assertEqual(self.paths(text), ["x"])

# Runs what the builder image would, on the host: the container path
# for the work directory is swapped for the real one.
class HostBuilderImage:
    def __init__(self):
        self.scripts = []

    def exec(self, args, volumes=None, workdir=None, environment=None, **kw):
        from seine.packages import WORKDIR
        host = volumes[0][0]
        script = args[2].replace(WORKDIR, host)
        subprocess.run(["sh", "-c", script], check=True,
                       cwd=workdir.replace(WORKDIR, host))

class QuiltPatching(WithTemporaryDirectory):
    def tree(self):
        source = os.path.join(self.tmp, "pkg-1")
        self.write("pkg-1/debian/source/format", "3.0 (quilt)\n")
        self.write("pkg-1/debian/rules", "one\n")
        self.write("pkg-1/main.c", "one\n")
        return source

    def patch(self, source, *patches):
        from seine.build import BuildCmd
        from seine.packages import Builder
        spec = ("packages:\n    - source: file://pkg-1\n      patches:\n"
                + "".join(f"          - {p}\n" for p in patches)
                + "image:\n    filename: x.img\n    partitions:\n"
                  "        - label: rootfs\n          where: /\n")
        build = BuildCmd()
        build.loads(spec)
        build.parse()
        package = build.image.packages[0]
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        Builder(distro, {}, HostBuilderImage()).patch(package, source, 0)

    def read(self, source, name):
        with open(os.path.join(source, name)) as f:
            return f.read()

    def test_a_debian_patch_skips_the_series(self):
        source = self.tree()
        path = self.write("rules.patch", RULES_PATCH)
        self.patch(source, path)
        self.assertEqual(self.read(source, "debian/rules"), "one\ntwo\n")
        self.assertFalse(os.path.exists(
            os.path.join(source, "debian", "patches", "series")))
        leftovers = [n for n in os.listdir(os.path.join(source, "debian"))
                     if n.endswith((".orig", ".rej"))]
        self.assertEqual(leftovers, [])

    def test_an_upstream_patch_joins_the_series(self):
        source = self.tree()
        path = self.write("main.patch", UPSTREAM_PATCH)
        self.patch(source, path)
        self.assertEqual(self.read(source, "main.c"), "one\n")
        self.assertEqual(self.read(source, "debian/patches/series"),
                         "main.patch\n")

    def test_a_mixed_patch_is_refused(self):
        source = self.tree()
        path = self.write("both.patch", RULES_PATCH + UPSTREAM_PATCH)
        with self.assertRaisesRegex(ValueError, "both.patch.*split it"):
            self.patch(source, path)
