#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import os
import shutil
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.packages import Builder
from seine.sbuild import BuilderImage
from seine.utils import HOST_ARCH

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
os.environ["SEINE_BUILD_DIR"] = tempfile.mkdtemp(prefix="seine-tests-build-")
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"], ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_BUILD_DIR"], ignore_errors=True)

def digest(letter):
    return letter * 64

def defaults(version, letter):
    return f"""
defaults:
    extends:
        go:
            toolchain: "{version}"
            toolchain-sha256: {{{HOST_ARCH}: "{digest(letter)}"}}
"""

MAIN = """
distribution:
    release: trixie
packages:
    - source: git://example.com/hello.git;rev=abc
      name: hello
      version: "1"
      extends:
          go:
%s
              commands: [{package: ., binary: hello}]
    - source: apt://busybox
image:
    filename: defaults-test.img
    partitions:
        - label: rootfs
          where: /
"""

class DefaultsFixture(avocado.Test):
    def build(self, own="", files=None, order=None):
        for name, text in (files or {"golang": defaults("1.22.4", "a")}).items():
            with open(os.path.join(self.workdir, f"{name}.yaml"), "w") as f:
                f.write(text)
        listed = "requires:\n" + "".join(
            f"    - {name}\n" for name in (order or list(files or ["golang"])))
        with open(os.path.join(self.workdir, "main.yaml"), "w") as f:
            f.write(listed + MAIN % own)
        build = BuildCmd()
        build.load(os.path.join(self.workdir, "main.yaml"))
        build.parse()
        return build

    def hello(self, build):
        return [p for p in build.image.packages if p.name == "hello"][0]

    def stamp(self, build):
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        return {p.name: os.path.basename(s).rsplit("_", 1)[1]
                for p, a, s in builder.stamps(build.image.packages)}["hello"]

class TheToolchainComesFromDefaults(DefaultsFixture):
    def test_a_package_that_sets_none_takes_it(self):
        go = self.hello(self.build()).ext["go"]
        self.assertEqual(go.toolchain, "1.22.4")
        self.assertEqual(go.toolchain_sha256, {HOST_ARCH: digest("a")})

    def test_a_package_that_sets_its_own_keeps_it(self):
        own = f"""              toolchain: "1.23.0"
              toolchain-sha256: {{{HOST_ARCH}: "{digest('b')}"}}"""
        go = self.hello(self.build(own)).ext["go"]
        self.assertEqual(go.toolchain, "1.23.0")
        self.assertEqual(go.toolchain_sha256, {HOST_ARCH: digest("b")})

    def test_the_hashes_are_never_taken_for_another_version(self):
        with self.assertRaises(ValueError) as refused:
            self.build('              toolchain: "1.23.0"')
        self.assertIn("'extends: go: toolchain-sha256'", str(refused.exception))

    def test_the_last_file_loaded_wins(self):
        build = self.build(files={"first": defaults("1.22.4", "a"),
                                  "second": defaults("1.24.0", "c")},
                           order=["first", "second"])
        self.assertEqual(self.hello(build).ext["go"].toolchain, "1.24.0")

    def test_the_package_is_not_made_by_a_default(self):
        build = self.build()
        self.assertEqual(
            sorted(p.name for p in build.image.packages), ["busybox", "hello"])
        self.assertNotIn("go", [p for p in build.image.packages
                                if p.name == "busybox"][0].ext)

    def test_the_default_is_part_of_the_stamp(self):
        before = self.stamp(self.build())
        after = self.stamp(self.build(files={"golang": defaults("1.24.0", "c")}))
        self.assertNotEqual(before, after)

class DefaultsAreChecked(DefaultsFixture):
    def refused(self, text):
        with self.assertRaises(ValueError) as refused:
            self.build(files={"golang": text})
        return str(refused.exception)

    def test_a_kind_without_defaults(self):
        message = self.refused(
            "defaults:\n    extends:\n        kernel: {flavour: amd64}\n")
        self.assertIn("'defaults: extends' has no 'kernel' build type", message)
        self.assertIn("expected one of go", message)

    def test_the_toolchain_and_its_hashes_come_together(self):
        message = self.refused(
            "defaults:\n    extends:\n        go: {toolchain: '1.22.4'}\n")
        self.assertIn("shall give toolchain and toolchain-sha256", message)

    def test_nothing_else_is_taken(self):
        message = self.refused(defaults("1.22.4", "a") + "            cgo: true\n")
        self.assertIn("and nothing else", message)

    def test_the_values_are_checked_even_if_nothing_uses_them(self):
        message = self.refused(
            "defaults:\n    extends:\n        go:\n"
            "            toolchain: '1.22.4'\n"
            f"            toolchain-sha256: {{{HOST_ARCH}: nope}}\n")
        self.assertIn("'defaults: extends: go'", message)
        self.assertIn("is not a sha256", message)

if __name__ == "__main__":
    avocado.main()
