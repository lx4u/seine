#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import copy
import os
import shutil
import sys
import tempfile
import yaml

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.extends import registry
from seine.extends import templates
from seine.packages import Builder
from seine.utils import HOST_ARCH
from seine.sbuild import BuilderImage

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
os.environ["SEINE_BUILD_DIR"] = tempfile.mkdtemp(prefix="seine-tests-build-")
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"], ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_BUILD_DIR"], ignore_errors=True)

DISTRO = {"source": "debian", "release": "trixie", "architecture": "amd64",
          "uri": "http://example.com/debian"}

IMAGE = {"filename": "digest-test.img",
         "partitions": [{"label": "rootfs", "where": "/"}]}

# One package per kind, and the settings whose change must be a rebuild.
CASES = {
    "go": {
        "spec": {"source": "git://h/k3s.git;rev=abc", "name": "k3s",
                 "version": "1", "extends": {"go": {
                     "toolchain": "1.22.4", "toolchain-sha256": {
                         HOST_ARCH: "a" * 64},
                     "build": "a", "cgo": False, "ldflags": "-s",
                     "tags": "x", "build-depends": ["x"],
                     "runtime-depends": ["y"], "runtime-suggests": ["z"],
                     "commands": [{"package": "./a", "binary": "a"}]}}},
        "changes": {"toolchain": "1.23.0",
                    "toolchain-sha256": {HOST_ARCH: "b" * 64},
                    "build": "b", "cgo": True, "ldflags": "-w", "tags": "y",
                    "build-depends": ["w"], "runtime-depends": ["w"],
                    "runtime-suggests": ["w"], "copyright": "Foo",
                    "commands": [{"package": "./b", "binary": "b"}]},
        "data": "go"},
    "module": {
        "spec": {"source": "git://h/nvidia.git;rev=abc", "name": "mod",
                 "version": "1", "extends": {"module": {
                     "build": "a", "target": "modules", "modules": ["a"],
                     "build-depends": ["x"], "runtime-depends": ["y"],
                     "make-vars": {"A": "1"},
                     "amd64-kernels": ["apt://linux-headers-6.12.1-amd64"],
                     "signing-key": "vault:one"}}},
        "changes": {"build": "b", "target": "all", "modules": ["a", "b"],
                    "build-depends": ["z"], "runtime-depends": ["z"],
                    "make-vars": {"A": "2"},
                    "amd64-kernels": ["apt://linux-headers-6.12.2-amd64"],
                    "signing-key": "vault:two", "copyright": "Foo"},
        "data": "module"},
    "uki": {
        "spec": {"name": "uki-a", "version": "1", "extends": {"uki": {
            "tool": "ukify", "linux-image": "linux-image-a",
            "initrd": "@initrd", "cmdline": "quiet",
            "signing-key": "vault:one"}}},
        "changes": {"tool": "efibootguard", "linux-image": "linux-image-b",
                    "cmdline": "debug", "signing-key": "vault:two",
                    "copyright": "Foo"},
        "data": "uki-ukify"},
    "uki-addon": {
        "spec": {"name": "addon-a", "version": "1", "extends": {"uki-addon": {
            "uki": "uki-a", "cmdline": "quiet", "signing-key": "vault:one"}}},
        "changes": {"cmdline": "debug", "signing-key": "vault:two",
                    "copyright": "Foo"},
        "data": "uki-addon"},
    "uefi-keys": {
        "spec": {"name": "keys-a", "version": "1", "extends": {"uefi-keys": {
            "pk": "vault:pk", "kek": ["vault:kek"], "db": ["vault:db"],
            "dbx": ["vault:dbx"], "reboot": False}}},
        "changes": {"pk": "vault:pk2", "kek": ["vault:kek2"],
                    "db": ["vault:db2"], "dbx": ["vault:dbx2"],
                    "reboot": True, "copyright": "Foo"},
        "data": "uefi-keys"},
}

UKI_PARENT = {"name": "uki-a", "version": "1", "extends": {"uki": {
    "tool": "ukify", "linux-image": "linux-image-a", "initrd": "@initrd"}}}

class DigestFixture(avocado.Test):
    def setUp(self):
        self.initrd = os.path.join(self.workdir, "initrd.img")
        with open(self.initrd, "w") as f:
            f.write("initrd")

    def parsed(self, kind, spec=None):
        spec = copy.deepcopy(spec or CASES[kind]["spec"])
        packages = [spec]
        if kind == "uki-addon":
            packages.insert(0, copy.deepcopy(UKI_PARENT))
        text = yaml.dump({"distribution": {"release": "trixie"},
                          "packages": packages, "image": IMAGE})
        text = text.replace("@initrd", self.initrd)
        build = BuildCmd()
        build.loads(text)
        build.parse()
        builder = Builder(DISTRO, {}, BuilderImage(DISTRO, {}))
        return builder, build.image.packages, spec["name"]

    def stamp(self, kind, spec=None):
        builder, packages, name = self.parsed(kind, spec)
        return {p.name: os.path.basename(s).rsplit("_", 1)[1]
                for p, a, s in builder.stamps(packages)}[name]

    def changed(self, kind, setting, value):
        spec = copy.deepcopy(CASES[kind]["spec"])
        spec["extends"][kind][setting] = value
        return self.stamp(kind, spec)

class SettingsChangeTheStamp(DigestFixture):
    def test_every_setting_counts(self):
        for kind, case in CASES.items():
            base = self.stamp(kind)
            self.assertEqual(base, self.stamp(kind))
            for setting, value in case["changes"].items():
                self.assertNotEqual(
                    base, self.changed(kind, setting, value),
                    f"'{kind}: {setting}' does not change the stamp")

class TheInitrdCountsByContent(DigestFixture):
    def test(self):
        before = self.stamp("uki")
        with open(self.initrd, "w") as f:
            f.write("another initrd")
        self.assertNotEqual(before, self.stamp("uki"))

class PackagingChangesTheStamp(DigestFixture):
    def edited(self, name):
        data = os.path.join(self.workdir, "data")
        shutil.copytree(templates.DATA, data)
        with open(os.path.join(data, name), "a") as f:
            f.write("\n# edited\n")
        original = templates.DATA
        templates.DATA = data
        templates.load_templates.cache_clear()
        return original

    def test_each_kinds_own_files_and_the_shared_changelog(self):
        for kind, case in CASES.items():
            base = self.stamp(kind)
            for name in [case["data"] + "/rules", "changelog"]:
                shutil.rmtree(os.path.join(self.workdir, "data"), True)
                original = self.edited(name)
                try:
                    after = self.stamp(kind)
                finally:
                    templates.DATA = original
                    templates.load_templates.cache_clear()
                self.assertNotEqual(base, after, f"{kind}: {name}")

class RevisionChangesTheStamp(DigestFixture):
    def test(self):
        for kind in CASES:
            base = self.stamp(kind)
            position = [e.name for e in registry.EXTENSIONS].index(kind)
            original = registry.EXTENSIONS[position]
            registry.EXTENSIONS[position] = original._replace(
                revision=original.revision + 1)
            try:
                after = self.stamp(kind)
            finally:
                registry.EXTENSIONS[position] = original
            self.assertNotEqual(base, after, kind)

class TheExcerptShowsEveryKind(DigestFixture):
    def excerpt(self, kind):
        builder, packages, name = self.parsed(kind)
        package = [p for p in packages if p.name == name][0]
        return builder.digest_excerpt(package)["extends"]

    def test_go(self):
        shown = self.excerpt("go")["go"]
        self.assertEqual(shown["toolchain"], "1.22.4")
        self.assertEqual(shown["commands"][0]["binary"], "a")
        self.assertNotIn("cgo", shown)

    def test_module(self):
        shown = self.excerpt("module")["module"]
        self.assertEqual(shown["modules"], ["a"])
        self.assertEqual(shown["make-vars"], {"A": "1"})

    def test_uki(self):
        self.assertEqual(self.excerpt("uki")["uki"], {
            "tool": "ukify", "linux-image": "linux-image-a",
            "initrd": self.initrd, "cmdline": "quiet",
            "signing-key": "vault:one"})

    def test_uki_addon(self):
        self.assertEqual(self.excerpt("uki-addon")["uki-addon"], {
            "uki": "uki-a", "cmdline": "quiet", "signing-key": "vault:one"})

    def test_uefi_keys(self):
        self.assertEqual(self.excerpt("uefi-keys")["uefi-keys"], {
            "pk": "vault:pk", "kek": ["vault:kek"], "db": ["vault:db"],
            "dbx": ["vault:dbx"]})

if __name__ == "__main__":
    avocado.main()
