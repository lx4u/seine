#!/usr/bin/env python3

import avocado
import base64
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager import ostree

INITRD = "/boot/initrd.img-6.12.1"
CONF = "usr/lib/ostree/prepare-root.conf"
KEYFILE = "etc/ostree/initramfs-root-binding.key"
PUBLIC = bytes(range(32))

# Plain Python, no guestfs appliance involved -- answers the calls
# check_composefs() makes and records the commands.
class FakeGuestfs:
    def __init__(self, features="composefs ex-fsverity", conf="[composefs]\nenabled = yes\n",
                 initrd=("usr/lib/ostree/prepare-root.conf", "erofs.ko.xz"),
                 shipped_key=""):
        self.features, self.initrd, self.shipped_key = features, initrd, shipped_key
        self.files = {"/" + CONF: conf} if conf is not None else {}
        self.commands = []
        self.written = {}

    def command(self, argv):
        self.commands.append(argv)
        if argv[0] == ostree.LSINITRD and "-f" in argv:
            return self.shipped_key
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

    def test_the_enabled_value_is_returned(self):
        self.assertEqual(ostree.check_composefs(FakeGuestfs()), "yes")
        g = FakeGuestfs(conf="[composefs]\nenabled = verity\n")
        self.assertEqual(ostree.check_composefs(g), "verity")

    def test_a_conf_missing_from_the_initramfs_is_refused(self):
        self.refused(FakeGuestfs(initrd=("erofs.ko.xz",)), "initramfs lacks")

    def test_erofs_missing_from_the_initramfs_is_refused(self):
        self.refused(FakeGuestfs(initrd=(CONF,)), "initramfs lacks")

SIGNED = "[composefs]\nenabled = signed\n"

class CheckSignedComposefs(avocado.Test):
    def ready(self, **kwargs):
        defaults = dict(
            features="composefs ex-fsverity sign-ed25519", conf=SIGNED,
            initrd=(CONF, "erofs.ko.xz", KEYFILE),
            shipped_key=base64.b64encode(PUBLIC).decode() + "\n")
        return FakeGuestfs(**dict(defaults, **kwargs))

    def refused(self, g, text, public_key=PUBLIC):
        with self.assertRaises(RuntimeError) as cm:
            ostree.check_composefs(g, public_key)
        self.assertIn(text, str(cm.exception))

    def test_a_ready_root_passes(self):
        self.assertEqual(ostree.check_composefs(self.ready(), PUBLIC), "signed")

    def test_signed_without_a_key_to_sign_with_is_refused(self):
        self.refused(self.ready(), "go together", None)

    def test_a_key_to_sign_with_needs_signed(self):
        self.refused(FakeGuestfs(), "go together")

    def test_an_ostree_without_ed25519_is_refused(self):
        self.refused(self.ready(features="composefs ex-fsverity"), "ed25519")

    def test_a_key_missing_from_the_initramfs_is_refused(self):
        self.refused(self.ready(initrd=(CONF, "erofs.ko.xz")), "lacks '/%s'" % KEYFILE)

    def test_another_key_in_the_initramfs_is_refused(self):
        other = base64.b64encode(bytes(32)).decode()
        self.refused(self.ready(shipped_key=other), "not the public key")

    def test_the_key_path_of_the_conf_is_followed(self):
        conf = SIGNED + "keypath = /etc/other.key\n"
        self.refused(self.ready(conf=conf), "lacks '/etc/other.key'")

class InitSysroot(avocado.Test):
    def configured(self, **kwargs):
        g = FakeGuestfs()
        ostree.init_sysroot(g, "debian", **kwargs)
        return [c[-1] for c in g.commands if "config" in c]

    def test_fsverity_is_asked_for_in_the_repo_only_when_wanted(self):
        self.assertNotIn("true", self.configured())
        self.assertIn("true", self.configured(fsverity=True))

class CommitComposefs(avocado.Test):
    def argv(self, **kwargs):
        g = FakeGuestfs()
        ostree.commit(g, "debian/amd64", 1, **kwargs)
        return g.commands[-1]

    def test_the_metadata_is_generated_for_composefs_only(self):
        self.assertIn("--generate-composefs-metadata", self.argv(composefs=True))
        self.assertNotIn("--generate-composefs-metadata", self.argv())
