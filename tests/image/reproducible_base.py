# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import glob
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from tests.testutils import prune_on_pass

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# Every member of a tarball, with what makes two tarballs differ.
def tar_manifest(tarball):
    found = {}
    with tarfile.open(tarball) as tar:
        for member in tar.getmembers():
            content = tar.extractfile(member).read() if member.isfile() else None
            found[member.name] = (
                member.mode, member.mtime, member.type,
                member.linkname, member.uid, member.gid,
                hashlib.sha256(content).hexdigest() if content is not None else None)
    return found

# Says which member differs and how, instead of a bare 'not equal'.
def explain_tar_difference(first, second):
    one, two = tar_manifest(first), tar_manifest(second)
    only_first = sorted(set(one) - set(two))
    only_second = sorted(set(two) - set(one))
    if only_first or only_second:
        return (f"member lists differ: only in the first build: "
                f"{only_first[:5]}; only in the second: {only_second[:5]}")
    for name in sorted(one):
        if one[name] != two[name]:
            fields = ["mode", "mtime", "type", "linkname", "uid", "gid", "sha256"]
            changed = [f for f, a, b in zip(fields, one[name], two[name]) if a != b]
            return f"'{name}' differs: {', '.join(changed)}"
    return ("every member matches (name, mode, mtime, type, owner and "
            "content) but the tarballs' own bytes still differ -- header "
            "padding or member order is not pinned")

# Shared scaffolding for the reproducible_disk*.py tests: build the same
# spec twice and check the two images, and the rootfs tarballs deployed
# beside them, match byte-for-byte. Not an
# avocado.Test itself, so avocado does not try to run it on its own.
class ReproducibleDiskImage:
    # A real timestamp on snapshot.debian.org, so a subclass's spec does
    # not depend on what bookworm's feeds currently serve.
    SNAPSHOT = "20260801T000000Z"

    # A subclass pinned to a different release (e.g. trixie, for UKI
    # packages bookworm doesn't have) overrides this.
    RELEASE = "bookworm"

    # The byte offset of the first difference, read in chunks rather
    # than all at once -- these images are large enough that a plain
    # 'a == b' would hold two full copies in memory for no reason.
    CHUNK = 4 * 1024 * 1024

    def setUp(self):
        self.spaces = []
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds a disk image twice; "
                        "this takes a while")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build a disk image")

    def tearDown(self):
        for space in self.spaces:
            subprocess.run(["podman", "unshare", "rm", "-rf", space], check=False)
        prune_on_pass(self)

    def space(self, name):
        path = os.path.join(self.workdir, name)
        environment = dict(os.environ)
        # ansible-playbook lives beside the python running the tests when
        # they are run from a virtual environment, and the build needs it.
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        environment["SEINE_CACHE_DIR"] = os.path.join(path, "cache")
        environment["SEINE_BUILD_DIR"] = os.path.join(path, "build")
        self.spaces.append(path)
        return environment

    def seine(self, space, args, log):
        where = os.path.join(self.outputdir, "%s.log" % log)
        with open(where, "w") as f:
            run = subprocess.run(
                [sys.executable, "-u", "./seine.py"] + args,
                cwd=path_to_sources, env=space, stdout=f, stderr=subprocess.STDOUT)
        self.assertEqual(run.returncode, 0,
                         "'%s' failed, see %s" % (" ".join(args), where))

    # A subclass names its own image via self.FILENAME.
    def image(self, space):
        found = glob.glob(os.path.join(
            space["SEINE_BUILD_DIR"], "deploy", self.RELEASE, self.FILENAME))
        self.assertEqual(len(found), 1, "no disk image in %s" % space["SEINE_BUILD_DIR"])
        return found[0]

    # Every tarball deployed by a build, keyed by '<release>/<name>': a
    # multiconfig group deploys its own, possibly under another release.
    def tarballs(self, space):
        deploy = os.path.join(space["SEINE_BUILD_DIR"], "deploy")
        return {os.path.relpath(found, deploy): found
                for found in glob.glob(os.path.join(deploy, "*", "*.tar"))}

    def firstDifference(self, one, two):
        with open(one, "rb") as f, open(two, "rb") as g:
            offset = 0
            while True:
                a, b = f.read(self.CHUNK), g.read(self.CHUNK)
                if a != b:
                    for i in range(min(len(a), len(b))):
                        if a[i] != b[i]:
                            return "byte offset %d differs (0x%02x vs 0x%02x)" % (
                                offset + i, a[i], b[i])
                    return "one image is longer than the other, at offset %d" % (
                        offset + min(len(a), len(b)))
                if not a:
                    return "no byte difference found, but the digests differ"
                offset += len(a)

    # A subclass provides specification(), writing its own spec file(s)
    # and returning their paths.
    def test(self):
        first = self.space("first")
        second = self.space("second")
        spec = self.specification()

        self.seine(first, ["build", "-v", "--reproducible", "--jobs", "2"] + spec, "build-first")
        self.seine(second, ["build", "-v", "--reproducible", "--jobs", "2"] + spec, "build-second")

        one, two = self.image(first), self.image(second)
        with open(one, "rb") as f:
            first_digest = hashlib.file_digest(f, "sha256").hexdigest()
        with open(two, "rb") as f:
            second_digest = hashlib.file_digest(f, "sha256").hexdigest()

        self.assertEqual(
            first_digest, second_digest,
            "two builds pinned to the same snapshot (%s) produced different "
            "disk images: %s" % (self.SNAPSHOT, self.firstDifference(one, two)))

        first_tars, second_tars = self.tarballs(first), self.tarballs(second)
        self.assertTrue(first_tars, f"no rootfs tarball in {first['SEINE_BUILD_DIR']}")
        self.assertEqual(
            sorted(first_tars), sorted(second_tars),
            "the two builds deployed different tarballs")
        for name in sorted(first_tars):
            one, two = first_tars[name], second_tars[name]
            with open(one, "rb") as f:
                first_digest = hashlib.file_digest(f, "sha256").hexdigest()
            with open(two, "rb") as f:
                second_digest = hashlib.file_digest(f, "sha256").hexdigest()
            self.assertEqual(
                first_digest, second_digest,
                f"two builds pinned to the same snapshot ({self.SNAPSHOT}) "
                f"produced different rootfs tarballs, '{name}': "
                f"{explain_tar_difference(one, two)}")
