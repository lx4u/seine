#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.join(path_to_sources, "tests", "build"))

from seine import analyze
from seine.imager.imager import Imager
from seine.partition import PartitionHandler
from partitions import ostree_spec

class FakeSource:
    def __init__(self, spec):
        self.spec = spec
        self.partitionHandler = PartitionHandler()
        self.partitionHandler.parse(spec)

    def spec_digest(self):
        return analyze.spec_digest(self.spec)

def sysroot_id(spec, late=None):
    imager = Imager.__new__(Imager)
    imager.source = FakeSource(spec)
    if late:
        late(imager.source.spec)
    imager._seed = None
    return imager._uuid_for("partition", "sysroot")

def fat_serial(spec, reproducible=False):
    imager = Imager.__new__(Imager)
    imager.source = FakeSource(spec)
    imager.reproducible = reproducible
    imager._seed = None
    return imager._pins_fat_serial(), imager._fat_serial("esp", "/dev/sda1")

def base():
    spec = ostree_spec()
    spec["distribution"]["architecture"] = "amd64"
    spec["image"]["ostree"].update({
        "version": "1", "stateroot": "debian", "gpg-key": "vault:ostree",
        "manifest-key": "vault:manifest"})
    spec["playbooks"] = [{"tasks": [{"apt": {"name": "vim"}}]}]
    return spec

def changed(edit):
    spec = base()
    edit(spec)
    return spec

class OstreeIdentity(avocado.Test):
    def same(self, edit):
        self.assertEqual(sysroot_id(base()), sysroot_id(changed(edit)))

    def differs(self, edit):
        self.assertNotEqual(sysroot_id(base()), sysroot_id(changed(edit)))

    def test_a_new_version_keeps_it(self):
        self.same(lambda s: s["image"]["ostree"].update(version="2"))

    def test_the_payload_and_its_keys_keep_it(self):
        self.same(lambda s: s["image"]["ostree"].update(
            {"payload": {"path": "out"}, "manifest-key": "vault:other"}))

    def test_the_gpg_key_keeps_it(self):
        self.same(lambda s: s["image"]["ostree"].update(
            {"gpg-key": "vault:other"}))

    def test_the_file_name_keeps_it(self):
        self.same(lambda s: s["image"].update(filename="other.img"))

    def test_a_playbook_and_a_package_keep_it(self):
        self.same(lambda s: s["playbooks"][0]["tasks"].append(
            {"apt": {"name": "nano"}}))
        self.same(lambda s: s.update(packages=["nano"]))

    def test_a_partition_label_changes_it(self):
        self.differs(lambda s: s["image"]["partitions"][2].update(label="data"))

    def test_the_stateroot_changes_it(self):
        self.differs(lambda s: s["image"]["ostree"].update(stateroot="other"))

    def test_the_ref_changes_it(self):
        self.differs(lambda s: s["image"]["ostree"].update(ref="other/ref"))

    def test_a_partition_size_changes_it(self):
        self.differs(lambda s: s["image"]["partitions"][0].update(size="96MiB"))

    def test_the_architecture_changes_it(self):
        self.differs(lambda s: s["distribution"].update(architecture="arm64"))

    def test_the_esp_serial_is_pinned_and_keeps_it(self):
        pinned, serial = fat_serial(base())
        self.assertTrue(pinned)
        self.assertEqual(serial, fat_serial(changed(
            lambda s: s["image"]["ostree"].update(version="2")))[1])

class PlainIdentity(avocado.Test):
    def plain(self):
        spec = base()
        del spec["image"]["ostree"]
        return spec

    def test_a_playbook_changes_it(self):
        other = self.plain()
        other["playbooks"][0]["tasks"].append({"apt": {"name": "nano"}})
        self.assertNotEqual(sysroot_id(self.plain()), sysroot_id(other))

    def test_the_file_name_changes_it(self):
        other = self.plain()
        other["image"]["filename"] = "other.img"
        self.assertNotEqual(sysroot_id(self.plain()), sysroot_id(other))

    def test_the_esp_serial_is_pinned_only_when_reproducible(self):
        self.assertFalse(fat_serial(self.plain())[0])
        self.assertTrue(fat_serial(self.plain(), reproducible=True)[0])

if __name__ == "__main__":
    avocado.main()
