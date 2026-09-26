#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import os
import shutil
import sys
import tempfile
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.extends import uefi_keys
from seine.build import BuildCmd
from seine.packages import Builder
from seine.sbuild import BuilderImage

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
os.environ["SEINE_BUILD_DIR"] = tempfile.mkdtemp(prefix="seine-tests-build-")
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_BUILD_DIR"],
                ignore_errors=True)

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

PACKAGE = """
                distribution:
                    release: trixie
                packages:
                    - name: uefi-provision-keys
                      version: "1"
                      extends:
                          uefi-keys:
%s
"""

def parse_package(extra):
    return parse(PACKAGE % extra).image.packages[0]

class SigningKeyFillsEveryRole(avocado.Test):
    def test(self):
        package = parse_package(
            "                              signing-key: vault:uefi-secureboot")
        self.assertEqual(package.uefi_keys_pk, "uefi-secureboot")
        self.assertEqual(package.uefi_keys_kek, ["uefi-secureboot"])
        self.assertEqual(package.uefi_keys_db, ["uefi-secureboot"])
        self.assertEqual(package.uefi_keys_dbx, [])
        self.assertIsNone(package.source)
        self.assertEqual(package.upstream_version, "1")

class ExplicitRolesOverrideTheFallback(avocado.Test):
    def test(self):
        package = parse_package(
            "                              signing-key: vault:fallback\n"
            "                              pk: vault:prod-pk\n"
            "                              kek:\n"
            "                                  - vault:prod-kek\n"
            "                                  - vault:backup-kek\n"
            "                              db: vault:prod-db\n"
            "                              dbx:\n"
            "                                  - vault:retired-key\n")
        self.assertEqual(package.uefi_keys_pk, "prod-pk")
        self.assertEqual(package.uefi_keys_kek, ["prod-kek", "backup-kek"])
        self.assertEqual(package.uefi_keys_db, ["prod-db"])
        self.assertEqual(package.uefi_keys_dbx, ["retired-key"])

class MissingPkWithNoFallbackIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_package("                              db: vault:x\n"
                          "                              kek: vault:x\n")
        self.assertIn("'pk' or a fallback 'signing-key'", str(refused.exception))

class MissingKekWithNoFallbackIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_package("                              pk: vault:x\n"
                          "                              db: vault:x\n")
        self.assertIn("'kek' or a fallback 'signing-key'", str(refused.exception))

class MissingDbWithNoFallbackIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_package("                              pk: vault:x\n"
                          "                              kek: vault:x\n")
        self.assertIn("'db' or a fallback 'signing-key'", str(refused.exception))

class NonVaultReferenceIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_package("                              signing-key: /a/key.pem\n")
        self.assertIn("'extends: uefi-keys: signing-key'", str(refused.exception))

class PkMustBeASingleKey(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_package(
                "                              signing-key: vault:x\n"
                "                              pk:\n"
                "                                  - vault:a\n"
                "                                  - vault:b\n")
        self.assertIn("'extends: uefi-keys: pk'", str(refused.exception))

class IncludeStandardDbxIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_package(
                "                              signing-key: vault:x\n"
                "                              include-standard-dbx: true\n")
        self.assertIn("no bundled UEFI Forum revocation list", str(refused.exception))

class IncludeMicrosoftKeysIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_package(
                "                              signing-key: vault:x\n"
                "                              include-microsoft-keys: true\n")
        self.assertIn("no bundled Microsoft certificates", str(refused.exception))

class RebootDefaultsToFalse(avocado.Test):
    def test(self):
        package = parse_package(
            "                              signing-key: vault:x\n")
        self.assertEqual(package.uefi_keys_reboot, False)

    def test_parsed(self):
        package = parse_package(
            "                              signing-key: vault:x\n"
            "                              reboot: true\n")
        self.assertEqual(package.uefi_keys_reboot, True)

class RebootNotABooleanIsRejected(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_package(
                "                              signing-key: vault:x\n"
                "                              reboot: yes-please\n")
        self.assertIn("'extends: uefi-keys: reboot'", str(refused.exception))

class UnknownSetting(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse_package(
                "                              signing-key: vault:x\n"
                "                              bogus: yes\n")
        self.assertIn("'extends: uefi-keys' has no 'bogus' setting",
                      str(refused.exception))

class SourceIsRefused(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse("""
                distribution:
                    release: trixie
                packages:
                    - source: apt://uefi-provision-keys
                      name: uefi-provision-keys
                      version: "1"
                      extends:
                          uefi-keys:
                              signing-key: vault:x
            """)
        self.assertIn("without a 'source:'", str(refused.exception))

class NotExtendedLeavesDefaults(avocado.Test):
    def test(self):
        build = parse("""
                distribution:
                    release: trixie
                packages:
                    - source: apt://busybox
                      name: busybox
        """)
        package = build.image.packages[0]
        self.assertEqual(package.uefi_keys, False)
        self.assertIsNone(package.uefi_keys_pk)
        self.assertEqual(package.uefi_keys_kek, [])
        self.assertEqual(package.uefi_keys_db, [])
        self.assertEqual(package.uefi_keys_dbx, [])


DISTRO = {"source": "debian", "release": "trixie",
         "architecture": "amd64", "uri": "http://example.com/debian"}

# Builder.fetch()/_fetch_key()/source_date_epoch() each special-case
# uki/uki-addon packages (no 'source:' to fetch from) -- uefi-keys is
# the same shape and needs the same three branches, or fetch() falls
# through to code that assumes a real 'source:' and crashes on the
# None it finds instead. extend()-level tests (below) never reach
# these call sites, so they get their own.
class BuilderFetchFixture(avocado.Test):
    def builder(self):
        return Builder(DISTRO, {}, BuilderImage(DISTRO, {}))

    def package(self):
        return parse_package(
            "                              signing-key: vault:x\n")

class FetchCreatesAnEmptySourcedir(BuilderFetchFixture):
    def test(self):
        sourcedir = self.builder().fetch(self.package(), self.workdir)
        self.assertTrue(os.path.isdir(sourcedir))
        self.assertEqual(os.listdir(sourcedir), [])

class FetchKeyIsNeverShared(BuilderFetchFixture):
    def test(self):
        package = self.package()
        self.assertEqual(self.builder()._fetch_key(package),
                         ("uefi-keys", package.name))

class SourceDateEpochFallsBackWithNoChangelog(BuilderFetchFixture):
    def test(self):
        from seine.packages import FALLBACK_EPOCH
        sourcedir = os.path.join(self.workdir, "unwritten")
        self.assertEqual(
            self.builder().source_date_epoch(self.package(), sourcedir),
            FALLBACK_EPOCH)


# Builder.stamp() decides whether a cached .deb is reused; a build
# went out with a stale unit file (systemd ordering cycle already
# fixed in the templates) because nothing here folded 'extends:
# uefi-keys' settings or the packaging templates into the digest --
# every edit looked like a no-op to the cache. Each of these proves
# one input actually changes the stamp.
def uefi_keys_stamp(extra):
    build = parse(PACKAGE % extra)
    builder = Builder(DISTRO, {}, BuilderImage(DISTRO, {}))
    stamps = {p.name: os.path.basename(s).rsplit("_", 1)[1]
             for p, a, s in builder.stamps(build.image.packages)}
    return stamps["uefi-provision-keys"]

class SigningKeyChangesTheStamp(avocado.Test):
    def test(self):
        self.assertNotEqual(
            uefi_keys_stamp("                              signing-key: vault:key-one\n"),
            uefi_keys_stamp("                              signing-key: vault:key-two\n"))

class DbListOrderChangesTheStamp(avocado.Test):
    def test(self):
        base = "                              signing-key: vault:x\n"
        forward = base + (
            "                              db:\n"
            "                                  - vault:a\n"
            "                                  - vault:b\n")
        reversed_ = base + (
            "                              db:\n"
            "                                  - vault:b\n"
            "                                  - vault:a\n")
        self.assertNotEqual(uefi_keys_stamp(forward), uefi_keys_stamp(reversed_))

class DbxChangesTheStamp(avocado.Test):
    def test(self):
        base = "                              signing-key: vault:x\n"
        self.assertNotEqual(
            uefi_keys_stamp(base),
            uefi_keys_stamp(base + "                              dbx:\n"
                                  "                                  - vault:retired\n"))

class RebootChangesTheStamp(avocado.Test):
    def test(self):
        base = "                              signing-key: vault:x\n"
        self.assertNotEqual(
            uefi_keys_stamp(base),
            uefi_keys_stamp(base + "                              reboot: true\n"))

class PackagingTemplateContentChangesTheStamp(avocado.Test):
    def test(self):
        extra = "                              signing-key: vault:x\n"
        before = uefi_keys_stamp(extra)
        edited = os.path.join(self.workdir, "uefi-keys")
        shutil.copytree(uefi_keys.UEFI_KEYS_PACKAGING, edited)
        with open(os.path.join(edited, "provision-keys"), "a") as f:
            f.write("\n# edited\n")
        original = uefi_keys.UEFI_KEYS_PACKAGING
        uefi_keys.UEFI_KEYS_PACKAGING = edited
        uefi_keys.uefi_keys_packaging.cache_clear()
        try:
            after = uefi_keys_stamp(extra)
        finally:
            uefi_keys.UEFI_KEYS_PACKAGING = original
            uefi_keys.uefi_keys_packaging.cache_clear()
        self.assertNotEqual(before, after)


# extend() writes a debian/ tree: certs fetched through a fake vault
# (host side, matches kernel/apply.py's TRUSTED_CERT_PATH precedent --
# never a real network call), then packaging rendered around them.
class FakeVault:
    def sbsign_cert(self, name):
        return "-- cert %s --\n" % name

class FakeBuilder:
    def _vault(self):
        return FakeVault()

class ExtendFixture(avocado.Test):
    def package(self, pk="pk-key", kek=None, db=None, dbx=None, reboot=False):
        package = types.SimpleNamespace(
            name="uefi-provision-keys", source=None,
            upstream_version="1", uefi_keys=True,
            uefi_keys_pk=pk, uefi_keys_kek=kek or ["kek-key"],
            uefi_keys_db=db or ["db-key"], uefi_keys_dbx=dbx or [],
            uefi_keys_reboot=reboot)
        return package

    def extend(self, package):
        sourcedir = os.path.join(self.workdir, "src")
        os.makedirs(sourcedir)
        uefi_keys.extend(FakeBuilder(), package, sourcedir, 946684800)
        return sourcedir

class CertsAreWrittenPerRole(ExtendFixture):
    def test(self):
        sourcedir = self.extend(self.package(
            dbx=["retired-1", "retired-2"]))
        certdir = os.path.join(sourcedir, "debian", "certs")
        with open(os.path.join(certdir, "pk.pem")) as f:
            self.assertEqual(f.read(), "-- cert pk-key --\n")
        with open(os.path.join(certdir, "kek-0.pem")) as f:
            self.assertEqual(f.read(), "-- cert kek-key --\n")
        with open(os.path.join(certdir, "db-0.pem")) as f:
            self.assertEqual(f.read(), "-- cert db-key --\n")
        with open(os.path.join(certdir, "dbx-0.pem")) as f:
            self.assertEqual(f.read(), "-- cert retired-1 --\n")
        with open(os.path.join(certdir, "dbx-1.pem")) as f:
            self.assertEqual(f.read(), "-- cert retired-2 --\n")

class RulesConvertsAndConcatenatesEveryRole(ExtendFixture):
    def test(self):
        sourcedir = self.extend(self.package(
            kek=["kek-a", "kek-b"], dbx=["retired"]))
        with open(os.path.join(sourcedir, "debian", "rules")) as f:
            rules = f.read()
        self.assertIn(
            "cert-to-efi-sig-list -g $(OWNER_GUID) debian/certs/pk.pem "
            "debian/$(PACKAGE)/usr/share/$(PACKAGE)/pk.auth", rules)
        self.assertIn(
            "cert-to-efi-sig-list -g $(OWNER_GUID) debian/certs/kek-0.pem "
            "debian/certs/kek-0.esl", rules)
        self.assertIn(
            "cert-to-efi-sig-list -g $(OWNER_GUID) debian/certs/kek-1.pem "
            "debian/certs/kek-1.esl", rules)
        self.assertIn(
            "cat debian/certs/kek-0.esl debian/certs/kek-1.esl > "
            "debian/$(PACKAGE)/usr/share/$(PACKAGE)/kek.esl", rules)
        self.assertIn(
            "cat debian/certs/dbx-0.esl > "
            "debian/$(PACKAGE)/usr/share/$(PACKAGE)/dbx.esl", rules)

class NoDbxSkipsItsRole(ExtendFixture):
    def test(self):
        sourcedir = self.extend(self.package())
        with open(os.path.join(sourcedir, "debian", "rules")) as f:
            rules = f.read()
        self.assertNotIn("dbx", rules)
        certdir = os.path.join(sourcedir, "debian", "certs")
        self.assertFalse(os.path.exists(os.path.join(certdir, "dbx-0.pem")))

class ServiceUnitIsNamedForThePackage(ExtendFixture):
    def test(self):
        sourcedir = self.extend(self.package(reboot=True))
        with open(os.path.join(sourcedir, "debian",
                               "uefi-provision-keys.service")) as f:
            unit = f.read()
        self.assertIn("ExecStart=/usr/libexec/uefi-provision-keys/provision-keys",
                      unit)
        self.assertIn("ConditionSecurity=!uefi-secureboot", unit)

class ProvisionKeysReboots(ExtendFixture):
    def test(self):
        sourcedir = self.extend(self.package(reboot=True))
        with open(os.path.join(sourcedir, "debian", "provision-keys")) as f:
            script = f.read()
        self.assertIn("systemctl --no-block reboot", script)

    def test_not_when_unset(self):
        sourcedir = self.extend(self.package(reboot=False))
        with open(os.path.join(sourcedir, "debian", "provision-keys")) as f:
            script = f.read()
        self.assertNotIn("reboot", script)

class OwnerGuidIsStableAcrossRuns(ExtendFixture):
    def test(self):
        first = self.extend(self.package())
        with open(os.path.join(first, "debian", "rules")) as f:
            once = f.read()
        second = os.path.join(self.workdir, "src2")
        os.makedirs(second)
        uefi_keys.extend(FakeBuilder(), self.package(), second, 946684800)
        with open(os.path.join(second, "debian", "rules")) as f:
            twice = f.read()
        self.assertEqual(once, twice)

class NotExtendedTouchesNothing(ExtendFixture):
    def test(self):
        package = self.package()
        package.uefi_keys = False
        sourcedir = os.path.join(self.workdir, "src")
        os.makedirs(sourcedir)
        uefi_keys.extend(FakeBuilder(), package, sourcedir, 946684800)
        self.assertEqual(os.listdir(sourcedir), [])

if __name__ == "__main__":
    avocado.main()
