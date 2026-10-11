#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager import ostree

INITRD = "/boot/initrd.img-6.12.1"
CONF = "usr/lib/ostree/prepare-root.conf"

# Plain Python, no guestfs appliance involved -- answers the calls
# check_composefs() makes and records the commands.
class FakeGuestfs:
    def __init__(self, features="composefs ex-fsverity", conf="[composefs]\nenabled = yes\n",
                 initrd=("usr/lib/ostree/prepare-root.conf", "erofs.ko.xz")):
        self.features, self.initrd = features, initrd
        self.files = {"/" + CONF: conf} if conf is not None else {}
        self.commands = []
        self.written = {}

    def command(self, argv):
        self.commands.append(argv)
        if argv[0] == ostree.LSINITRD:
            return "\n".join(self.initrd)
        if argv[-1] == "--version":
            return "ostree:\n Features:\n  %s\n" % self.features
        return "abc123\n"

    def is_file(self, path):
        return path in self.files

    def read_file(self, path):
        return self.files[path].encode()

    def ls(self, path):
        return ["initrd.img-6.12.1", "vmlinuz-6.12.1"]

    def write(self, path, content):
        self.written[path] = content

    def mkdir_p(self, path):
        pass

    def chmod(self, mode, path):
        pass

    def rm_rf(self, path):
        pass

class CheckComposefs(avocado.Test):
    def refused(self, g, text):
        with self.assertRaises(RuntimeError) as cm:
            ostree.check_composefs(g)
        self.assertIn(text, str(cm.exception))

    def test_a_ready_root_passes(self):
        g = FakeGuestfs()
        ostree.check_composefs(g)
        self.assertEqual(g.commands[-1], [ostree.LSINITRD, INITRD])

    def test_an_ostree_without_composefs_is_refused(self):
        self.refused(FakeGuestfs(features="ex-fsverity"), "built with composefs")

    def test_a_missing_conf_is_refused(self):
        self.refused(FakeGuestfs(conf=None), "needs '/usr/lib/ostree/prepare-root.conf'")

    def test_a_conf_that_does_not_enable_it_is_refused(self):
        self.refused(FakeGuestfs(conf="[composefs]\nenabled = maybe\n"), "enabled = maybe")
        self.refused(FakeGuestfs(conf="[sysroot]\nreadonly = true\n"), "enabled = no")

    def test_verity_is_not_supported_yet(self):
        self.refused(FakeGuestfs(conf="[composefs]\nenabled = verity\n"), "only 'yes'")

    def test_a_conf_missing_from_the_initramfs_is_refused(self):
        self.refused(FakeGuestfs(initrd=("erofs.ko.xz",)), "initramfs lacks")

    def test_erofs_missing_from_the_initramfs_is_refused(self):
        self.refused(FakeGuestfs(initrd=(CONF,)), "initramfs lacks")

class CommitComposefs(avocado.Test):
    def argv(self, **kwargs):
        g = FakeGuestfs()
        ostree.commit(g, "debian/amd64", 1, **kwargs)
        return g.commands[-1]

    def test_the_metadata_is_generated_for_composefs_only(self):
        self.assertIn("--generate-composefs-metadata", self.argv(composefs=True))
        self.assertNotIn("--generate-composefs-metadata", self.argv())
