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
from seine.container import ContainerEngine
from seine.extends import module
from seine.extends import uki_addon
from seine.packages import Builder
from seine.sbuild import BuilderImage

DISTRO = {"source": "debian", "release": "trixie",
         "architecture": "amd64", "uri": "http://example.com/debian"}

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
os.environ["SEINE_BUILD_DIR"] = tempfile.mkdtemp(prefix="seine-tests-build-")
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_BUILD_DIR"],
                ignore_errors=True)

# 'extends: uki-addon' needs systemd-stub addon support (systemd >=
# 254), so every spec below targets trixie explicitly -- unlike
# tests/build/uki.py, the default release (bookworm) is exactly what
# BookwormIsRejected exercises.
DEPLOYED_INITRD = os.path.join(
    ContainerEngine.deploy_root(), "trixie", "minimal.img")
os.makedirs(os.path.dirname(DEPLOYED_INITRD), exist_ok=True)
open(DEPLOYED_INITRD, "w").close()

IMAGE = """
                image:
                    filename: packages-test.img
                    partitions:
                        - label: rootfs
                          where: /
"""

def parse(packages):
    build = BuildCmd()
    build.loads(packages + IMAGE)
    build.parse()
    return build

PARENT = """
                distribution:
                    release: trixie
                packages:
                    - name: linux-uki-amd64
                      version: "1"
                      extends:
                          uki:
                              tool: ukify
                              linux-image: linux-image-amd64
                              initrd: minimal.img
"""

# Appended after PARENT, continuing the same 'packages:' list.
ADDON = """
                    - name: linux-uki-amd64-quiet
                      version: "1"
                      extends:
                          uki-addon:
                              uki: linux-uki-amd64
                              cmdline: "quiet loglevel=0"
%s
"""

def parse_addon(extra=""):
    return parse(PARENT + ADDON % extra)

# PARENT with an optional 'signing-key:' line spliced into its 'uki:'
# block, same indent as 'initrd:' -- for the inheritance tests below.
def parent_with_key(key=None):
    extra = ("                              signing-key: vault:%s\n" % key
             if key else "")
    return PARENT.replace(
        "                              initrd: minimal.img\n",
        "                              initrd: minimal.img\n" + extra)

def addon_package(build):
    for package in build.image.packages:
        if package.name == "linux-uki-amd64-quiet":
            return package
    raise AssertionError("addon package not found")

class UkiAddonExtension(avocado.Test):
    def test(self):
        build = parse_addon()
        package = addon_package(build)
        self.assertEqual(package.uki_addon, True)
        self.assertEqual(package.uki_addon_uki, "linux-uki-amd64")
        self.assertEqual(package.uki_addon_cmdline, "quiet loglevel=0")
        self.assertIsNone(package.source)
        self.assertEqual(package.upstream_version, "1")

class MissingUki(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(PARENT + """
                    - name: linux-uki-amd64-quiet
                      version: "1"
                      extends:
                          uki-addon:
                              cmdline: "quiet loglevel=0"
            """)
        self.assertIn("'extends: uki-addon: uki'", str(refused.exception))

class MissingCmdline(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(PARENT + """
                    - name: linux-uki-amd64-quiet
                      version: "1"
                      extends:
                          uki-addon:
                              uki: linux-uki-amd64
            """)
        self.assertIn("'extends: uki-addon: cmdline'", str(refused.exception))

class EmptyCmdlineRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_addon('                              cmdline: ""')
        self.assertIn("'extends: uki-addon: cmdline'", str(refused.exception))

class ForbiddenCmdlineCharacter(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(PARENT + """
                    - name: linux-uki-amd64-quiet
                      version: "1"
                      extends:
                          uki-addon:
                              uki: linux-uki-amd64
                              cmdline: "quiet; rm -rf /"
            """)
        self.assertIn("'extends: uki-addon: cmdline'", str(refused.exception))

class UnknownUkiReference(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(PARENT + """
                    - name: linux-uki-amd64-quiet
                      version: "1"
                      extends:
                          uki-addon:
                              uki: no-such-package
                              cmdline: "quiet loglevel=0"
            """)
        self.assertIn("'extends: uki-addon: uki'", str(refused.exception))
        self.assertIn("not an 'extends: uki' package", str(refused.exception))

class UkiReferencesNonUkiPackage(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse("""
                distribution:
                    release: trixie
                packages:
                    - source: apt://linux
                      name: not-a-uki
                      version: "1"
                    - name: linux-uki-amd64-quiet
                      version: "1"
                      extends:
                          uki-addon:
                              uki: not-a-uki
                              cmdline: "quiet loglevel=0"
            """)
        self.assertIn("not an 'extends: uki' package", str(refused.exception))

class UkiReferencesAnotherAddon(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(PARENT + ADDON % "" + """
                    - name: linux-uki-amd64-quiet2
                      version: "1"
                      extends:
                          uki-addon:
                              uki: linux-uki-amd64-quiet
                              cmdline: "debug"
            """)
        self.assertIn("not an 'extends: uki' package", str(refused.exception))

class UkiReferencesEfibootguardIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse("""
                distribution:
                    release: trixie
                packages:
                    - name: linux-uki-amd64
                      version: "1"
                      extends:
                          uki:
                              tool: efibootguard
                              linux-image: linux-image-amd64
                              initrd: minimal.img
                    - name: linux-uki-amd64-quiet
                      version: "1"
                      extends:
                          uki-addon:
                              uki: linux-uki-amd64
                              cmdline: "quiet loglevel=0"
            """)
        self.assertIn("tool: ukify", str(refused.exception))

class BookwormIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(PARENT.replace("release: trixie", "release: bookworm")
                  + ADDON % "")
        self.assertIn("systemd-stub", str(refused.exception))

class SigningKeyParsed(avocado.Test):
    def test(self):
        build = parse_addon(
            "                              signing-key: vault:pc-uki-secureboot")
        self.assertEqual(addon_package(build).uki_addon_signing_key,
                         "pc-uki-secureboot")

    def test_defaults_to_none(self):
        build = parse_addon()
        self.assertEqual(addon_package(build).uki_addon_signing_key, None)

class SigningKeyNotAString(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_addon(
                "                              signing-key: [pc-uki-secureboot]")
        self.assertIn("'extends: uki-addon: signing-key'", str(refused.exception))

class SigningKeyWithoutVaultPrefixIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_addon(
                "                              signing-key: /a/key.pem")
        self.assertIn("'extends: uki-addon: signing-key'", str(refused.exception))

class UnknownSetting(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_addon("                              bogus: yes")
        self.assertIn("'extends: uki-addon' has no 'bogus' setting",
                      str(refused.exception))

class SourceIsRefused(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(PARENT + """
                    - source: apt://linux
                      name: linux-uki-amd64-quiet
                      version: "1"
                      extends:
                          uki-addon:
                              uki: linux-uki-amd64
                              cmdline: "quiet loglevel=0"
            """)
        self.assertIn("without a 'source:'", str(refused.exception))

class MissingVersion(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(PARENT + """
                    - name: linux-uki-amd64-quiet
                      extends:
                          uki-addon:
                              uki: linux-uki-amd64
                              cmdline: "quiet loglevel=0"
            """)
        self.assertIn("'version' is not set", str(refused.exception))

class AddonDependsOnItsParent(avocado.Test):
    def test(self):
        build = parse_addon()
        addon = addon_package(build)
        self.assertEqual([d.name for d in addon.depends], ["linux-uki-amd64"])

def resolved_key(build):
    builder = Builder(DISTRO, {}, BuilderImage(DISTRO, {}))
    # Safe with no 'module:' package in the spec: sets builder.packages
    # and returns before touching hostBootstrap.
    module.resolve_kernels(builder, build.image.packages, None)
    return uki_addon.resolved_signing_key(builder, addon_package(build))

class SigningKeyInheritsFromParent(avocado.Test):
    def test(self):
        build = parse(parent_with_key("parent-key") + ADDON % "")
        self.assertEqual(resolved_key(build), "parent-key")

class SigningKeyOverridesParent(avocado.Test):
    def test(self):
        build = parse(parent_with_key("parent-key") + ADDON % (
            "                              signing-key: vault:addon-key"))
        self.assertEqual(resolved_key(build), "addon-key")

class SigningKeyDefaultsToNoneWhenParentHasNone(avocado.Test):
    def test(self):
        build = parse_addon()
        self.assertIsNone(resolved_key(build))

def addon_stamp(parent_key, addon_key=None):
    extra = ("                              signing-key: vault:%s" % addon_key
             if addon_key else "")
    build = parse(parent_with_key(parent_key) + ADDON % extra)
    builder = Builder(DISTRO, {}, BuilderImage(DISTRO, {}))
    stamps = {p.name: os.path.basename(s).rsplit("_", 1)[1]
             for p, a, s in builder.stamps(build.image.packages)}
    return stamps["linux-uki-amd64-quiet"]

class InheritedSigningKeyChangesTheAddonsStamp(avocado.Test):
    def test(self):
        self.assertNotEqual(addon_stamp("key-one"), addon_stamp("key-two"))

class ExplicitSigningKeyChangesTheAddonsStamp(avocado.Test):
    def test(self):
        self.assertNotEqual(
            addon_stamp("parent-key", "addon-one"),
            addon_stamp("parent-key", "addon-two"))

if __name__ == "__main__":
    avocado.main()
