#!/usr/bin/env python3

import avocado
import os
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.bootstrap import QEMU_ARCHS, _qemu_fetch

# What HostBootstrap's own qemu-user-static footprint reduction fetches:
# only the interpreter this host needs to cross-bootstrap the other
# architectures, never the whole package. A pure function of HOST_ARCH,
# so this is tested directly rather than through a real podman build --
# HostBootstrap has no dockerfile()-without-building split the way
# TargetBootstrap does (see bootstrap.py's own comment on that), but the
# fetch fragment itself needs no podman to read.
class AnAmd64HostFetchesTheInterpretersItNeeds(avocado.Test):
    def test(self):
        fetched = _qemu_fetch("amd64")
        self.assertIn("for a in aarch64 arm;", fetched)
        # Native CPU compat, no interpreter fetched for either.
        self.assertNotIn("i386", fetched)
        self.assertNotIn("x86_64", fetched)

class AnArm64HostFetchesTheInterpretersItNeeds(avocado.Test):
    def test(self):
        fetched = _qemu_fetch("arm64")
        self.assertIn("for a in x86_64 i386;", fetched)
        # Native CPU compat, no interpreter fetched for either.
        self.assertNotIn("aarch64", fetched)
        self.assertNotIn("qemu-arm", fetched)

class AnEmulatedHostAlsoFetchesItsCompatInterpreter(avocado.Test):
    def test(self):
        fetched = _qemu_fetch("arm64", emulated=True)
        # No real silicon behind an emulated host, so its compat
        # architecture (armhf on arm64) needs an interpreter too.
        self.assertIn("for a in x86_64 i386 arm;", fetched)

        fetched = _qemu_fetch("amd64", emulated=True)
        self.assertIn("for a in aarch64 arm i386;", fetched)

class NeitherHostFetchesItsOwnArchitecture(avocado.Test):
    def test(self):
        self.assertNotIn("amd64", QEMU_ARCHS["arm64"])
        self.assertNotIn("arm64", QEMU_ARCHS["amd64"])

class AnUnlistedHostArchitectureFetchesNothing(avocado.Test):
    def test(self):
        self.assertEqual(_qemu_fetch("riscv64"), "true")

class BothPackageNamesAreDownloadedNeverInstalled(avocado.Test):
    def test(self):
        fetched = _qemu_fetch("amd64")
        self.assertIn("for p in qemu-user-static qemu-user; do apt-get download $p", fetched)
        self.assertNotIn("apt-get install", fetched)

class AMissingPackageDoesNotFailTheFetch(avocado.Test):
    def test(self):
        # forky has no qemu-user-static: it must not stop the other
        # package from being fetched, nor a missing binary the copy.
        fetched = _qemu_fetch("amd64")
        self.assertIn("apt-get download $p || true", fetched)
        self.assertIn("if [ -e /qemu-extract/usr/bin/$f ]", fetched)

class AnInterpreterWithoutTheStaticSuffixIsRenamed(avocado.Test):
    def test(self):
        # forky's qemu-user ships qemu-aarch64, a static binary, and
        # qemu-user-static is gone. The suffixed name wins when both exist.
        fetched = _qemu_fetch("amd64")
        self.assertIn("for f in qemu-$a-static qemu-$a;", fetched)
        self.assertIn("/usr/bin/qemu-$a-static", fetched)

class TheFetchScriptCopiesTheRightInterpreter(avocado.Test):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="seine-test-qemu-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_fetch(self, files):
        extract = os.path.join(self.root, "qemu-extract", "usr", "bin")
        target = os.path.join(self.root, "usr", "bin")
        os.makedirs(extract)
        os.makedirs(target)
        for name, content in files.items():
            with open(os.path.join(extract, name), "w") as f:
                f.write(content)
        # Run only the copy loop, rooted in the scratch directory.
        script = _qemu_fetch("amd64")
        loop = script[script.index("for a in"):script.index(" && cd / &&")]
        loop = loop.replace("/qemu-extract", f"{self.root}/qemu-extract")
        loop = loop.replace("/usr/bin/qemu", f"{self.root}/usr/bin/qemu")
        subprocess.run(["sh", "-ec", loop], check=True)
        return {n: open(os.path.join(target, n)).read()
                for n in os.listdir(target)}

    def test_bare_name_is_copied_with_the_suffix(self):
        got = self.run_fetch({"qemu-aarch64": "bare", "qemu-arm": "bare-arm"})
        self.assertEqual(got, {"qemu-aarch64-static": "bare",
                               "qemu-arm-static": "bare-arm"})

    def test_suffixed_name_wins(self):
        got = self.run_fetch({"qemu-aarch64-static": "static",
                              "qemu-aarch64": "bare"})
        self.assertEqual(got, {"qemu-aarch64-static": "static"})
