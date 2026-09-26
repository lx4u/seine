#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import hashlib
import os
import shutil
import sys
import tempfile
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.extends import texts
from seine.packages import Builder
from seine.sbuild import BuilderImage

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
os.environ["SEINE_BUILD_DIR"] = tempfile.mkdtemp(prefix="seine-tests-build-")
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"], ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_BUILD_DIR"], ignore_errors=True)

BODY = "Files: *\nLicense: MIT\n"
DIGEST = hashlib.sha256(BODY.encode()).hexdigest()
URL = "https://example.com/copyright"

class Package:
    name = "foo"
    def _error(self, message):
        return ValueError(message)

def parsed(value):
    return texts.parse(Package(), "uki", {"copyright": value}, "copyright")

def refused(test, value):
    with test.assertRaises(ValueError) as error:
        parsed(value)
    return str(error.exception)

class Parsing(avocado.Test):
    def test_it_may_be_left_out(self):
        self.assertIsNone(texts.parse(Package(), "uki", {}, "copyright"))

    def test_inline_text_is_kept_as_written(self):
        self.assertEqual(parsed("Foo\nBar"), "Foo\nBar")

    def test_it_is_not_empty(self):
        self.assertIn("'extends: uki: copyright'", refused(self, ""))

    def test_a_file_must_exist(self):
        path = os.path.join(self.workdir, "copyright")
        self.assertIn("which is not a file", refused(self, f"file://{path}"))
        open(path, "w").close()
        self.assertEqual(parsed(f"file://{path}"), f"file://{path}")

    def test_a_download_needs_its_sha256(self):
        for value in [URL, f"{URL};sha256sum=abc", f"{URL};rev=1",
                      f"{URL};sha256sum={DIGEST};rev=1"]:
            self.assertIn("sha256sum=<64 hex digits>", refused(self, value))

    def test_a_download_with_its_sha256_is_kept(self):
        for scheme in ["https", "http"]:
            value = f"{scheme}://example.com/c;sha256sum={DIGEST}"
            self.assertEqual(parsed(value), value)

class Files(avocado.Test):
    def test_a_path_is_relative_to_the_file_naming_it(self):
        self.assertEqual(texts.resolve("file://files/c", "/etc/specs"),
                         "file:///etc/specs/files/c")
        self.assertEqual(texts.resolve("file:///abs/c", "/etc/specs"),
                         "file:///abs/c")
        self.assertEqual(texts.resolve("Some text", "/etc/specs"), "Some text")

    def test_the_files_a_package_reads(self):
        package = types.SimpleNamespace(ext={
            "uki": types.SimpleNamespace(copyright="file:///a/c"),
            "go": types.SimpleNamespace(copyright="Text",
                                        systemd_unit="file:///a/u")})
        self.assertEqual(sorted(texts.files(package)), ["/a/c", "/a/u"])

class Reading(avocado.Test):
    def builder(self, body=BODY):
        self.fetched = []
        image = types.SimpleNamespace(exec=self.exec)
        self.body = body
        return types.SimpleNamespace(builderImage=image)

    def exec(self, args, volumes, workdir):
        self.fetched.append(args[-1])
        with open(os.path.join(volumes[0][0], "text"), "w") as f:
            f.write(self.body)

    def test_inline_and_file(self):
        path = os.path.join(self.workdir, "c")
        with open(path, "w") as f:
            f.write(BODY)
        self.assertIsNone(texts.read(None, None))
        self.assertEqual(texts.read(None, "Foo"), "Foo")
        self.assertEqual(texts.read(None, f"file://{path}"), BODY)

    def test_a_download_is_checked_and_kept(self):
        builder = self.builder()
        value = f"{URL};sha256sum={DIGEST}"
        self.assertEqual(texts.read(builder, value), BODY)
        self.assertEqual(texts.read(builder, value), BODY)
        self.assertEqual(self.fetched, [URL])

    def test_a_download_that_changed_is_refused(self):
        builder = self.builder("Something else\n")
        with self.assertRaises(ValueError) as error:
            texts.read(builder, f"{URL}2;sha256sum={DIGEST}")
        self.assertIn("is not what 'sha256sum' says", str(error.exception))
        self.assertIn(DIGEST, str(error.exception))

class LoadedFromASpecification(avocado.Test):
    SPEC = """
distribution:
    release: trixie
packages:
    - name: keys-a
      version: "1"
      extends:
          uefi-keys:
              signing-key: vault:key
              copyright: file://files/copyright
image:
    filename: texts-test.img
    partitions:
        - label: rootfs
          where: /
"""

    def setUp(self):
        os.makedirs(os.path.join(self.workdir, "files"))
        self.copyright = os.path.join(self.workdir, "files", "copyright")
        self.write(BODY)
        with open(os.path.join(self.workdir, "main.yaml"), "w") as f:
            f.write(self.SPEC)

    def write(self, body):
        with open(self.copyright, "w") as f:
            f.write(body)

    def package(self):
        build = BuildCmd()
        build.load(os.path.join(self.workdir, "main.yaml"))
        build.parse()
        self.build = build
        return [p for p in build.image.packages if p.name == "keys-a"][0]

    def stamp(self):
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        package = self.package()
        return os.path.basename(builder.stamps(self.build.image.packages)[
            self.build.image.packages.index(package)][2])

    def test_the_path_is_relative_to_the_spec(self):
        package = self.package()
        self.assertEqual(package.ext["uefi-keys"].copyright,
                         f"file://{self.copyright}")
        self.assertIn(self.copyright, package.referenced_files())

    def test_the_file_is_part_of_the_stamp(self):
        before = self.stamp()
        self.assertEqual(before, self.stamp())
        self.write("Files: *\nLicense: Apache-2.0\n")
        self.assertNotEqual(before, self.stamp())

if __name__ == "__main__":
    avocado.main()
