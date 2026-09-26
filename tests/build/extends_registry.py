#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.extends import registry
from seine.packages import Builder
from seine.packages import Package
from seine.sbuild import BuilderImage

DISTRO = {"source": "debian", "release": "trixie", "architecture": "amd64",
          "uri": "http://example.com/debian"}

UKI = {"tool": "ukify", "linux-image": "linux-image-x", "initrd": "/i"}
KEYS = {"signing-key": "vault:k"}
MODULE = {"modules": ["a"], "amd64-kernels": ["apt://linux-headers-x"]}

def package(kind, settings, **spec):
    spec.setdefault("name", "pkg")
    return Package({"extends": {kind: settings}, **spec}, 1)

class TheTable(avocado.Test):
    def test_every_kind_is_there_once(self):
        names = [extension.name for extension in registry.EXTENSIONS]
        self.assertEqual(sorted(names), sorted(set(names)))
        self.assertEqual(sorted(names), [
            "kernel", "module", "uefi-keys", "uki", "uki-addon"])

class KindsAreChecked(avocado.Test):
    def test_an_unknown_kind_lists_the_known_ones(self):
        with self.assertRaises(ValueError) as refused:
            package("bogus", {}, source="apt://x")
        self.assertIn("no 'bogus' build type", str(refused.exception))
        self.assertIn("uki-addon", str(refused.exception))

    def test_an_unknown_setting_lists_the_known_ones(self):
        with self.assertRaises(ValueError) as refused:
            package("uki", {"bogus": 1}, version="1")
        self.assertIn("'extends: uki' has no 'bogus' setting",
                      str(refused.exception))

    def test_a_module_names_its_kernels_per_architecture(self):
        built = package("module", MODULE, source="apt://x", version="1")
        self.assertEqual(built.ext["module"].kernels,
                         {"amd64": ["apt://linux-headers-x"]})

    def test_the_extra_setting_is_named_in_the_error(self):
        with self.assertRaises(ValueError) as refused:
            package("module", {"bogus": 1}, source="apt://x", version="1")
        self.assertIn("<architecture>-kernels", str(refused.exception))

class WhatAKindGenerates(avocado.Test):
    def test_a_generated_kind_has_no_source(self):
        for kind, settings in [("uki", UKI), ("uefi-keys", KEYS)]:
            built = package(kind, settings, version="1")
            self.assertEqual(registry.generator(built).name, kind)
            self.assertFalse(registry.no_changelog(built))

    def test_a_module_is_fetched_and_has_no_changelog(self):
        built = package("module", MODULE, source="apt://x", version="1")
        self.assertIsNone(registry.generator(built))
        self.assertTrue(registry.no_changelog(built))

    def test_a_plain_package_uses_no_kind(self):
        built = Package({"source": "apt://busybox"}, 1)
        self.assertEqual(registry.in_use(built), [])
        self.assertIsNone(registry.generator(built))

class VersionIsNeeded(avocado.Test):
    def test_every_generated_kind_asks_for_it(self):
        for kind, settings in [("uki", UKI), ("uefi-keys", KEYS)]:
            with self.assertRaises(ValueError) as refused:
                package(kind, settings)
            self.assertIn("'version' is not set", str(refused.exception))

    def test_a_module_asks_for_it_when_it_builds(self):
        with self.assertRaises(ValueError) as refused:
            package("module", MODULE, source="apt://x")
        self.assertIn("out-of-tree module", str(refused.exception))

    def test_an_entry_that_only_describes_a_package_does_not(self):
        package("module", MODULE)

    def test_a_kernel_never_does(self):
        package("kernel", {}, source="apt://linux")

ADDON = {"uki": "uki-a", "cmdline": "quiet"}

class CopyrightIsShared(avocado.Test):
    def test_every_kind_that_writes_packaging_takes_it(self):
        for kind, settings, spec in [
                ("module", MODULE, {"source": "apt://x"}),
                ("uki", UKI, {}), ("uki-addon", ADDON, {}),
                ("uefi-keys", KEYS, {})]:
            built = package(kind, dict(settings, copyright="Foo"),
                            version="1", **spec)
            self.assertEqual(built.ext[kind].copyright, "Foo")

    def test_it_is_optional(self):
        self.assertIsNone(package("uki-addon", ADDON, version="1")
                          .ext["uki-addon"].copyright)

    def test_a_kernel_keeps_debians_own(self):
        with self.assertRaises(ValueError) as refused:
            package("kernel", {"copyright": "Foo"}, source="apt://linux")
        self.assertIn("'extends: kernel' has no 'copyright' setting",
                      str(refused.exception))

    def test_it_is_text(self):
        for bad in ["", 1, ["Foo"]]:
            with self.assertRaises(ValueError) as refused:
                package("uki-addon", dict(ADDON, copyright=bad), version="1")
            self.assertIn("'extends: uki-addon: copyright'",
                          str(refused.exception))

class CopyrightIsWritten(avocado.Test):
    def written(self, **settings):
        built = package("uki-addon", dict(ADDON, **settings), version="1")
        builder = Builder(DISTRO, {}, BuilderImage(DISTRO, {}))
        registry.extend_all(builder, built, self.workdir, 946684800)
        return os.path.join(self.workdir, "debian", "copyright")

    def test_the_text_is_kept_as_written(self):
        with open(self.written(copyright="Files: *\nLicense: MIT")) as f:
            self.assertEqual(f.read(), "Files: *\nLicense: MIT\n")

    def test_no_text_no_file(self):
        self.assertFalse(os.path.exists(self.written()))

if __name__ == "__main__":
    avocado.main()
