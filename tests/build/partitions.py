#!/usr/bin/env python3

import avocado
import os
import sys
import tarfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine import utils
from seine.partition import PartitionHandler

class GptPartitionTable(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: gpt
                    partitions:
                        - label: rootfs
                          where: /
            """)
            build.parse()
        except:
            self.fail("parsing of a specification with a 'gpt' partition table failed!")

class MsDosPartitionTable(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: msdos
                    partitions:
                        - label: rootfs
                          where: /
            """)
            build.parse()
        except:
            self.fail("parsing of a specification with a 'msdos' partition table failed!")

class UnsupportedPartitionTable(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: unsupported-partition-table
                    partitions:
                        - label: rootfs
                          where: /
            """)
            build.parse()
            self.fail("parsing should have failed (invalid partition table)!")
        except ValueError as e:
            if str(e) != "'unsupported-partition-table' is not a supported partition table!":
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))

class PartitionMissingLabel(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    partitions:
                          where: /
            """)
            build.parse()
            self.fail("parsing should have failed (missing partition label)!")
        except ValueError as e:
            if str(e) != "one of the partitions does not have a 'label' defined!":
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))

class RoFsRequiresGptTable(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: msdos
                    partitions:
                        - label: rootfs
                          where: /
                        - label: usr
                          where: /usr
                          type: squashfs
            """)
            build.parse()
            self.fail("parsing should have failed (read-only type needs a gpt table)!")
        except ValueError as e:
            if "needs a 'gpt' partition table" not in str(e):
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))

class BootAndXbootldrRequireVfat(avocado.Test):
    def _refused(self, flag, fstype):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: gpt
                    partitions:
                        - label: p
                          where: /boot
                          type: %s
                          flags: [%s]
                        - label: rootfs
                          where: /
            """ % (fstype, flag))
            build.parse()
            self.fail("parsing should have failed ('%s' needs 'vfat')!" % flag)
        except ValueError as e:
            if "can only read from a 'vfat' partition" not in str(e):
                self.fail("parsing did not return the error we expected: %s" % e)

    def test_boot_flag_needs_vfat(self):
        self._refused("boot", "ext4")

    def test_xbootldr_flag_needs_vfat(self):
        self._refused("xbootldr", "ext4")

    def test_vfat_is_accepted(self):
        for flag in ("boot", "xbootldr"):
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: gpt
                    partitions:
                        - label: p
                          where: /boot
                          type: vfat
                          flags: [%s]
                        - label: rootfs
                          where: /
            """ % flag)
            build.parse()

class RoFsOnGptTableIsAccepted(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: gpt
                    partitions:
                        - label: rootfs
                          where: /
                        - label: usr
                          where: /usr
                          type: squashfs
            """)
            build.parse()
        except:
            self.fail("parsing of a read-only partition on a 'gpt' table failed!")

class RoFsOnLvmVolumeIsAccepted(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    partitions:
                        - label: pv
                          flags:
                              - lvm
                          group: main
                          size: 512MiB
                    volumes:
                        - label: usr
                          group: main
                          where: /usr
                          type: squashfs
                          size: 128MiB
            """)
            build.parse()
        except:
            self.fail("parsing of a read-only LVM volume failed!")

# 'source:' routes a partition/volume's content to a declared
# 'multiconfig:' group's own rootfs instead of this specification's own --
# see PartitionHandler._validate_sources().
class SourceMustNameADeclaredGroup(avocado.Test):
    def test(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "disk.img",
                    "partitions": [
                        {"label": "rootfs", "source": "main", "where": "/"},
                    ],
                },
            })
            self.fail("an undeclared 'source:' group was accepted!")
        except ValueError as e:
            self.assertIn("main", str(e))
            self.assertIn("not one of the declared", str(e))

# Groups are side-by-side, non-overlapping OSes -- each declared group
# needs exactly one root, so zero or more than one is a parse-time error,
# not an ambiguous disk.
class EachGroupNeedsExactlyOneRoot(avocado.Test):
    # A declared group nothing references at all (built alongside the
    # disk, not yet routed into it) is left alone -- only a group with
    # a sourced mount and no root among
    # them is the error.
    def test_a_group_nothing_references_is_left_alone(self):
        try:
            PartitionHandler().parse({
                "multiconfig": {"main": ["main.yaml"]},
                "image": {
                    "filename": "disk.img",
                    "partitions": [{"label": "rootfs", "where": "/"}],
                },
            })
        except:
            self.fail("an unreferenced 'multiconfig:' group was rejected!")

    def test_zero_roots_among_a_referenced_groups_mounts_is_an_error(self):
        try:
            PartitionHandler().parse({
                "multiconfig": {"main": ["main.yaml"]},
                "image": {
                    "filename": "disk.img",
                    "partitions": [
                        {"label": "rootfs", "where": "/"},
                        {"label": "main-usr", "source": "main", "where": "/usr"},
                    ],
                },
            })
            self.fail("a referenced group with no root partition was accepted!")
        except ValueError as e:
            self.assertIn("main", str(e))

    def test_two_is_an_error(self):
        try:
            PartitionHandler().parse({
                "multiconfig": {"main": ["main.yaml"]},
                "image": {
                    "filename": "disk.img",
                    "partitions": [
                        {"label": "one", "source": "main", "where": "/"},
                        {"label": "two", "source": "main", "where": "/"},
                    ],
                },
            })
            self.fail("two root partitions for one group were accepted!")
        except ValueError as e:
            self.assertIn("main", str(e))

class SourcedPartitionsAreAccepted(avocado.Test):
    def test(self):
        ph = PartitionHandler()
        ph.parse({
            "multiconfig": {"main": ["main.yaml"], "recovery": ["recovery.yaml"]},
            "image": {
                "filename": "disk.img",
                "partitions": [
                    {"label": "main-root", "source": "main", "where": "/"},
                    {"label": "recovery-root", "source": "recovery", "where": "/"},
                ],
            },
        })
        self.assertEqual(
            {p["label"]: p["source"] for p in ph.partitions},
            {"main-root": "main", "recovery-root": "recovery"})

# A specification with no 'multiconfig:' key declares nothing for
# 'source:' to be one of, so this stays a pure no-op for it -- confirmed
# rather than assumed, since it is what keeps a plain spec byte-identical
# to before 'source:' existed.
class NoMulticonfigKeyIsUnaffected(avocado.Test):
    def test_a_plain_specification_still_parses(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "simple.img",
                    "partitions": [{"label": "rootfs", "where": "/"}],
                },
            })
        except:
            self.fail("a specification with no 'multiconfig:' key was rejected!")

    def test_source_is_never_added_by_default(self):
        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "simple.img",
                "partitions": [{"label": "rootfs", "where": "/"}],
            },
        })
        self.assertNotIn("source", ph.partitions[0])

# 'distribute()' only ever grows the mount whose own 'source' matches the
# one it was called for -- a tar member from one group's tarball must
# never fatten another group's partition, even when both mounts are named
# 'where: "/"' the same way.
class DistributeScopesBySource(avocado.Test):
    def test_a_mount_only_matches_its_own_source(self):
        ph = PartitionHandler()
        ph.parse({
            "multiconfig": {"main": ["main.yaml"], "recovery": ["recovery.yaml"]},
            "image": {
                "filename": "disk.img",
                "partitions": [
                    {"label": "main-root", "source": "main", "where": "/"},
                    {"label": "recovery-root", "source": "recovery", "where": "/"},
                ],
            },
        })
        main_root = next(p for p in ph.mounts if p["label"] == "main-root")
        recovery_root = next(p for p in ph.mounts if p["label"] == "recovery-root")
        recovery_before = recovery_root["_size"]

        member = tarfile.TarInfo("etc/hostname")
        member.size = 4096
        matched = ph.distribute(member, source="main")

        self.assertIs(matched, main_root)
        self.assertEqual(recovery_root["_size"], recovery_before,
                         "recovery's own partition grew from main's content")

# 'verity: true' pairs a read-only partition with a 'verity-hash' one --
# see PartitionHandler._validate_verity()/parse's own comment there.
class VerityHashPartitionIsAccepted(avocado.Test):
    def test(self):
        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "disk.img",
                "table": "gpt",
                "partitions": [
                    {"label": "usr", "where": "/usr", "type": "erofs",
                     "verity": True},
                    {"label": "usr-verity", "type": "verity-hash",
                     "verity-for": "usr", "size": "64MiB"},
                    {"label": "rootfs", "where": "/"},
                ],
            },
        })
        self.assertNotIn("usr-verity", [m["label"] for m in ph.mounts],
                         "a verity-hash partition should never be a mount")

class VerityHashIsNeverMounted(avocado.Test):
    def test(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "disk.img",
                    "table": "gpt",
                    "partitions": [
                        {"label": "usr", "where": "/usr", "type": "erofs",
                         "verity": True},
                        {"label": "usr-verity", "type": "verity-hash",
                         "verity-for": "usr", "size": "64MiB", "where": "/oops"},
                        {"label": "rootfs", "where": "/"},
                    ],
                },
            })
            self.fail("a mounted 'verity-hash' partition was accepted!")
        except ValueError as e:
            self.assertIn("never mounted", str(e))

class VerityHashNeedsVerityForField(avocado.Test):
    def test(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "disk.img",
                    "table": "gpt",
                    "partitions": [
                        {"label": "usr-verity", "type": "verity-hash", "size": "64MiB"},
                        {"label": "rootfs", "where": "/"},
                    ],
                },
            })
            self.fail("a 'verity-hash' partition with no 'verity-for' was accepted!")
        except ValueError as e:
            self.assertIn("verity-for", str(e))

class VerityHashNeedsSize(avocado.Test):
    def test(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "disk.img",
                    "table": "gpt",
                    "partitions": [
                        {"label": "usr", "where": "/usr", "type": "erofs",
                         "verity": True},
                        {"label": "usr-verity", "type": "verity-hash",
                         "verity-for": "usr"},
                        {"label": "rootfs", "where": "/"},
                    ],
                },
            })
            self.fail("a 'verity-hash' partition with no 'size' was accepted!")
        except ValueError as e:
            self.assertIn("'size' of verity-hash partition", str(e))

class VerityNeedsAReadOnlyType(avocado.Test):
    def test(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "disk.img",
                    "table": "gpt",
                    "partitions": [
                        {"label": "usr", "where": "/usr", "type": "ext4",
                         "verity": True},
                        {"label": "usr-verity", "type": "verity-hash",
                         "verity-for": "usr", "size": "64MiB"},
                        {"label": "rootfs", "where": "/"},
                    ],
                },
            })
            self.fail("'verity: true' on an 'ext4' partition was accepted!")
        except ValueError as e:
            self.assertIn("needs a read-only type", str(e))

class VerityOnlySupportedOnRootOrUsr(avocado.Test):
    def test(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "disk.img",
                    "table": "gpt",
                    "partitions": [
                        {"label": "opt", "where": "/opt", "type": "erofs",
                         "verity": True},
                        {"label": "opt-verity", "type": "verity-hash",
                         "verity-for": "opt", "size": "64MiB"},
                        {"label": "rootfs", "where": "/"},
                    ],
                },
            })
            self.fail("'verity: true' on '/opt' was accepted!")
        except ValueError as e:
            self.assertIn("only supported on '/' or '/usr'", str(e))

class VerityHashMustNameAnExistingVerityPartition(avocado.Test):
    def test(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "disk.img",
                    "table": "gpt",
                    "partitions": [
                        {"label": "usr-verity", "type": "verity-hash",
                         "verity-for": "no-such-partition", "size": "64MiB"},
                        {"label": "rootfs", "where": "/"},
                    ],
                },
            })
            self.fail("'verity-for' naming an undeclared partition was accepted!")
        except ValueError as e:
            self.assertIn("not one of the declared partitions", str(e))

class VerityWithNoHashPartitionIsAnError(avocado.Test):
    def test(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "disk.img",
                    "table": "gpt",
                    "partitions": [
                        {"label": "usr", "where": "/usr", "type": "erofs",
                         "verity": True},
                        {"label": "rootfs", "where": "/"},
                    ],
                },
            })
            self.fail("'verity: true' with no paired 'verity-hash' partition was accepted!")
        except ValueError as e:
            self.assertIn("no 'verity-hash' partition names", str(e))

# A 'private-key' starting with 'vault:' names a vault sbsign key:
# the certificate stays in the vault with it, so 'public-cert' may
# be missing, while plain paths still need both.
class SecureBootVaultKey(avocado.Test):
    def parsed(self, secure_boot):
        handler = PartitionHandler()
        handler.parse({
            "image": {
                "filename": "disk.img",
                "partitions": [{"label": "rootfs", "where": "/"}],
                "secure-boot": secure_boot,
            },
        })
        return handler.secure_boot

    def test_a_vault_key_needs_no_cert_file(self):
        parsed = self.parsed({"private-key": "vault:db"})
        self.assertEqual(parsed["private-key"], "vault:db")

    def test_plain_paths_still_need_both(self):
        try:
            self.parsed({"private-key": "db.key"})
            self.fail("a key with no certificate was accepted!")
        except ValueError as e:
            self.assertIn("public-cert", str(e))

if __name__ == "__main__":
    avocado.main()

# 'image: ostree' -- see PartitionHandler._validate_ostree().
def ostree_spec(mode="standard", release="trixie", partitions=None, **image):
    spec = {
        "distribution": {"release": release},
        "image": dict({
            "filename": "disk.img",
            "table": "gpt",
            "ostree": {"mode": mode},
            "partitions": partitions or [
                {"label": "esp", "type": "vfat", "where": "/efi", "size": "64MiB"},
                {"label": "sysroot", "where": "/"},
                {"label": "var", "where": "/var"},
            ],
        }, **image),
    }
    return spec

SIGNING_KEYS = {"gpg-key": "vault:ostree-commits",
                "manifest-key": "vault:update-manifest"}

# A version is shipped, so its two keys come with it.
def versioned(spec, version, **extra):
    spec["image"]["ostree"].update(SIGNING_KEYS, version=version, **extra)
    return spec

class OstreeSpecification(avocado.Test):
    def refuses(self, text, **kwargs):
        with self.assertRaises(ValueError) as cm:
            PartitionHandler().parse(ostree_spec(**kwargs))
        self.assertIn(text, str(cm.exception))

    def test_standard_layout_is_accepted(self):
        ph = PartitionHandler()
        ph.parse(ostree_spec())
        self.assertEqual(ph.ostree["mode"], "standard")

    def test_disabled_is_the_default_and_checks_nothing(self):
        ph = PartitionHandler()
        spec = ostree_spec(release="bookworm", table="msdos")
        del spec["image"]["ostree"]
        ph.parse(spec)
        self.assertEqual(ph.ostree["mode"], "disabled")

    def test_gpg_key_names_a_vault_key(self):
        spec = ostree_spec()
        spec["image"]["ostree"]["gpg-key"] = "vault:ostree-commits"
        ph = PartitionHandler()
        ph.parse(spec)
        self.assertEqual(ph.ostree_for(None)["gpg-key"], "vault:ostree-commits")

    def test_gpg_key_must_be_a_vault_reference(self):
        for key in ("/etc/key.pem", "vault:", "vault:a/b", 3):
            spec = ostree_spec()
            spec["image"]["ostree"]["gpg-key"] = key
            with self.assertRaises(ValueError) as cm:
                PartitionHandler().parse(spec)
            self.assertIn("shall be 'vault:<name>'", str(cm.exception))

    def test_version_is_kept(self):
        spec = versioned(ostree_spec(), "23.1_b")
        ph = PartitionHandler()
        ph.parse(spec)
        self.assertEqual(ph.ostree_for(None)["version"], "23.1_b")

    def test_version_must_be_a_string(self):
        for version in (23, 1.5, ["1"]):
            spec = ostree_spec()
            spec["image"]["ostree"]["version"] = version
            with self.assertRaises(ValueError) as cm:
                PartitionHandler().parse(spec)
            self.assertIn("shall be a string (quote it)", str(cm.exception))

    def test_version_syntax_is_checked(self):
        for version in ("", "v1", "1-2", "1+2", "1~rc", "1^2", "1/2", "1 2",
                        ".1", "1" * 65):
            spec = ostree_spec()
            spec["image"]["ostree"]["version"] = version
            with self.assertRaises(ValueError) as cm:
                PartitionHandler().parse(spec)
            self.assertIn("'image: ostree: version:", str(cm.exception))
        PartitionHandler().parse(versioned(ostree_spec(), "1" * 64))

    def test_version_needs_standard_mode(self):
        for mode in ("disabled", "composefs"):
            spec = ostree_spec(mode=mode, release="forky")
            spec["image"]["ostree"]["version"] = "1"
            with self.assertRaises(ValueError) as cm:
                PartitionHandler().parse(spec)
            self.assertIn("needs 'mode: standard'", str(cm.exception))

    def test_version_is_refused_in_sources(self):
        spec = ostree_spec()
        spec["multiconfig"] = {"a": {}}
        spec["image"]["ostree"]["sources"] = {"a": {"version": "1"}}
        with self.assertRaises(ValueError) as cm:
            PartitionHandler().parse(spec)
        self.assertIn("has no 'version' attribute", str(cm.exception))

    def test_manifest_key_must_be_a_vault_reference(self):
        for key in ("/etc/key.pem", "vault:", "vault:a/b", 3):
            spec = versioned(ostree_spec(), "1")
            spec["image"]["ostree"]["manifest-key"] = key
            with self.assertRaises(ValueError) as cm:
                PartitionHandler().parse(spec)
            self.assertIn("manifest-key", str(cm.exception))
            self.assertIn("shall be 'vault:<name>'", str(cm.exception))

    def test_a_version_needs_both_keys(self):
        for key in SIGNING_KEYS:
            spec = versioned(ostree_spec(), "1")
            del spec["image"]["ostree"][key]
            with self.assertRaises(ValueError) as cm:
                PartitionHandler().parse(spec)
            self.assertIn("needs '%s'" % key, str(cm.exception))

    def test_keys_and_payload_are_refused_in_sources(self):
        for key, value in (("manifest-key", "vault:a"), ("payload", {})):
            spec = ostree_spec()
            spec["multiconfig"] = {"a": {}}
            spec["image"]["ostree"]["sources"] = {"a": {key: value}}
            with self.assertRaises(ValueError) as cm:
                PartitionHandler().parse(spec)
            self.assertIn("has no '%s' attribute" % key, str(cm.exception))

    def test_payload_settings_are_kept(self):
        spec = versioned(ostree_spec(), "23", payload={
            "path": "fleet", "deltas-from": ["20", "22"]})
        ph = PartitionHandler()
        ph.parse(spec)
        self.assertEqual(ph.ostree_for(None)["payload"],
                         {"path": "fleet", "deltas-from": ["20", "22"]})

    def test_payload_needs_a_version(self):
        spec = ostree_spec()
        spec["image"]["ostree"]["payload"] = {"path": "fleet"}
        with self.assertRaises(ValueError) as cm:
            PartitionHandler().parse(spec)
        self.assertIn("'image: ostree: payload' needs 'version'", str(cm.exception))

    def test_bad_payload_settings_are_refused(self):
        for payload, text in (
                ("fleet", "shall be a mapping"),
                ({"dir": "x"}, "has no 'dir' attribute"),
                ({"path": ""}, "path' shall be a non-empty string"),
                ({"path": 3}, "path' shall be a non-empty string"),
                ({"deltas-from": "20"}, "shall be a list of different versions"),
                ({"deltas-from": [20]}, "shall be a list of different versions"),
                ({"deltas-from": ["20", "20"]}, "shall be a list of different versions"),
                ({"deltas-from": ["v1"]}, "shall be a list of different versions"),
                ({"deltas-from": ["23"]}, "is not lower than version '23'"),
                ({"deltas-from": ["24"]}, "is not lower than version '23'")):
            spec = versioned(ostree_spec(), "23", payload=payload)
            with self.assertRaises(ValueError) as cm:
                PartitionHandler().parse(spec)
            self.assertIn(text, str(cm.exception))

    def test_deltas_compare_as_versions_not_as_text(self):
        PartitionHandler().parse(versioned(ostree_spec(), "10", payload={
            "deltas-from": ["9"]}))

    def test_unknown_mode_is_refused(self):
        self.refuses("is not one of", mode="bootc")

    def test_bookworm_is_refused(self):
        self.refuses("needs dracut", release="bookworm")

    def test_composefs_needs_forky(self):
        self.refuses("composefs", mode="composefs")
        PartitionHandler().parse(ostree_spec(mode="composefs", release="forky"))

    def test_msdos_is_refused(self):
        self.refuses("'gpt' partition table", table="msdos")

    def test_missing_var_is_refused(self):
        self.refuses("needs a '/var'", partitions=[
            {"label": "sysroot", "where": "/"}])

    def test_reserved_mount_points_are_refused_with_a_hint(self):
        for where in ("/home", "/srv", "/root", "/mnt", "/opt", "/usr/local"):
            self.refuses("'where: /var%s'" % where, partitions=[
                {"label": "sysroot", "where": "/"},
                {"label": "var", "where": "/var"},
                {"label": "data", "where": where}])

    def test_nested_data_mounts_are_accepted(self):
        PartitionHandler().parse(ostree_spec(partitions=[
            {"label": "sysroot", "where": "/"},
            {"label": "var", "where": "/var"},
            {"label": "home", "where": "/var/home"}]))

    def test_verity_on_the_os_is_refused(self):
        self.refuses("verity: true", partitions=[
            {"label": "sysroot", "where": "/", "type": "erofs", "verity": True},
            {"label": "hash", "type": "verity-hash", "verity-for": "sysroot",
             "size": "64MiB"},
            {"label": "var", "where": "/var"}])

    def test_vfat_boot_is_refused(self):
        self.refuses("vfat '/boot'", partitions=[
            {"label": "sysroot", "where": "/"},
            {"label": "var", "where": "/var"},
            {"label": "boot", "where": "/boot", "type": "vfat"}])

    def test_root_must_be_ext4(self):
        self.refuses("plain 'ext4'", partitions=[
            {"label": "sysroot", "where": "/", "type": "btrfs"},
            {"label": "var", "where": "/var"}])

    def test_bad_stateroot_is_refused(self):
        spec = ostree_spec()
        spec["image"]["ostree"]["stateroot"] = "a/b"
        with self.assertRaises(ValueError):
            PartitionHandler().parse(spec)

    def test_every_rooted_group_needs_its_own_var(self):
        spec = ostree_spec(partitions=[
            {"label": "a-root", "where": "/", "source": "a"},
            {"label": "a-var", "where": "/var", "source": "a"},
            {"label": "b-root", "where": "/", "source": "b"}])
        spec["multiconfig"] = {"a": ["a.yaml"], "b": ["b.yaml"]}
        with self.assertRaises(ValueError) as cm:
            PartitionHandler().parse(spec)
        self.assertIn("group 'b'", str(cm.exception))

    def test_explicit_stateroot_cannot_serve_two_groups(self):
        spec = ostree_spec(partitions=[
            {"label": "a-root", "where": "/", "source": "a"},
            {"label": "a-var", "where": "/var", "source": "a"},
            {"label": "b-root", "where": "/", "source": "b"},
            {"label": "b-var", "where": "/var", "source": "b"}])
        spec["multiconfig"] = {"a": ["a.yaml"], "b": ["b.yaml"]}
        spec["image"]["ostree"]["stateroot"] = "debian"
        with self.assertRaises(ValueError):
            PartitionHandler().parse(spec)

def two_groups(**sources):
    spec = ostree_spec(partitions=[
        {"label": "a-root", "where": "/", "source": "a"},
        {"label": "a-var", "where": "/var", "source": "a"},
        {"label": "b-root", "where": "/", "source": "b", "type": "btrfs"}])
    spec["multiconfig"] = {"a": ["a.yaml"], "b": ["b.yaml"]}
    spec["image"]["ostree"]["sources"] = sources
    return spec

class OstreeVersionNeedsOneStateroot(avocado.Test):
    def test_two_groups_with_ostree_are_refused(self):
        spec = versioned(two_groups(), "1")
        with self.assertRaises(ValueError) as cm:
            PartitionHandler().parse(spec)
        self.assertIn("covers one stateroot", str(cm.exception))

    def test_a_group_with_ostree_off_does_not_count(self):
        PartitionHandler().parse(
            versioned(two_groups(b={"mode": "disabled"}), "1"))


class OstreeVersionOrder(avocado.Test):
    def order(self, *versions):
        return sorted(versions, key=utils.version_key)

    def test_numbers_compare_as_numbers(self):
        self.assertEqual(self.order("10", "9", "100"), ["9", "10", "100"])

    def test_dotted_versions(self):
        self.assertEqual(self.order("1.10", "1.9", "1.9.1", "2"),
                         ["1.9", "1.9.1", "1.10", "2"])

    def test_a_prefix_is_lower(self):
        self.assertEqual(self.order("23a", "23", "23.1"), ["23", "23.1", "23a"])

    def test_letters_compare_as_text(self):
        self.assertEqual(self.order("1b", "1a", "1_c"), ["1_c", "1a", "1b"])

    def test_equal_versions_have_equal_keys(self):
        self.assertEqual(utils.version_key("1.2"), utils.version_key("1.2"))


class OstreePerGroupSettings(avocado.Test):
    def test_a_group_can_opt_out_and_keep_a_plain_layout(self):
        ph = PartitionHandler()
        ph.parse(two_groups(b={"mode": "disabled"}))
        self.assertEqual(ph.ostree_for("a")["mode"], "standard")
        self.assertEqual(ph.ostree_for("b")["mode"], "disabled")

    def test_a_group_without_an_opt_out_is_still_checked(self):
        with self.assertRaises(ValueError) as cm:
            PartitionHandler().parse(two_groups())
        self.assertIn("group 'b'", str(cm.exception))

    def test_stateroot_defaults_to_the_group_name_and_can_be_overridden(self):
        ph = PartitionHandler()
        ph.parse(two_groups(a={"stateroot": "main", "ref": "main/stable"},
                            b={"mode": "disabled"}))
        self.assertEqual(ph.ostree_for("a")["stateroot"], "main")
        self.assertEqual(ph.ostree_for("a")["ref"], "main/stable")
        self.assertEqual(ph.ostree_for("b")["stateroot"], "b")

    def test_two_groups_cannot_share_a_stateroot(self):
        spec = two_groups(a={"stateroot": "os"}, b={"stateroot": "os"})
        spec["image"]["partitions"][2]["type"] = "ext4"
        spec["image"]["partitions"].append(
            {"label": "b-var", "where": "/var", "source": "b"})
        with self.assertRaises(ValueError) as cm:
            PartitionHandler().parse(spec)
        self.assertIn("share the ostree stateroot", str(cm.exception))

    def test_an_unknown_group_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            PartitionHandler().parse(two_groups(c={"mode": "disabled"}))
        self.assertIn("not one of the declared", str(cm.exception))

    def test_only_disabled_groups_leave_the_release_unchecked(self):
        spec = two_groups(a={"mode": "disabled"}, b={"mode": "disabled"})
        spec["distribution"]["release"] = "bookworm"
        PartitionHandler().parse(spec)

class OstreeSysroot(avocado.Test):
    MIB = 1024 * 1024

    def sized(self, root_mib, var_mib, **root):
        ph = PartitionHandler()
        spec = ostree_spec()
        spec["image"]["partitions"][1].update(root)
        ph.parse(spec)
        for path, mib in (("etc/data", root_mib), ("var/data", var_mib)):
            member = tarfile.TarInfo(path)
            member.size = mib * self.MIB
            ph.distribute(member)
        ph.compute_sizes()
        return {m["label"]: m["_size"] // self.MIB for m in ph.mounts}

    def test_the_sysroot_has_room_for_the_repo_and_one_more_deployment(self):
        plain = PartitionHandler()
        spec = ostree_spec()
        del spec["image"]["ostree"]
        plain.parse(spec)
        member = tarfile.TarInfo("etc/data")
        member.size = 100 * self.MIB
        plain.distribute(member)
        plain.compute_sizes()
        plain_root = next(m for m in plain.mounts if m["label"] == "sysroot")
        sizes = self.sized(100, 10)
        # content again for the next deployment, plus /var kept in the repo
        self.assertEqual(sizes["sysroot"], plain_root["_size"] // self.MIB + 100 + 10 + 2)

    def test_an_explicit_size_still_wins_when_larger(self):
        self.assertEqual(self.sized(100, 10, size="2GiB")["sysroot"], 2048)

    def test_read_only_partitions_are_refused(self):
        with self.assertRaises(ValueError) as cm:
            PartitionHandler().parse(ostree_spec(partitions=[
                {"label": "sysroot", "where": "/"},
                {"label": "var", "where": "/var"},
                {"label": "ro", "where": "/data", "type": "squashfs"}]))
        self.assertIn("read-only", str(cm.exception))
