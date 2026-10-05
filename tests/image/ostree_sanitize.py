#!/usr/bin/env python3

import avocado
import os
import posixpath
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager.ostree import sanitize

ROOT = "/stage"

# Plain Python, no guestfs appliance involved -- an in-memory tree
# answers the handful of 'g' calls sanitize() makes.
class FakeGuestfs:
    def __init__(self):
        self.nodes = {"/": ("dir",)}

    def _under(self, path):
        prefix = path.rstrip("/") + "/"
        return [p for p in self.nodes if p.startswith(prefix)]

    def mkdir_p(self, path):
        parts = path.strip("/").split("/")
        for i in range(len(parts)):
            self.nodes.setdefault("/" + "/".join(parts[:i + 1]), ("dir",))

    def put(self, path, data=b""):
        self.mkdir_p(posixpath.dirname(path))
        self.nodes[path] = ("file", data)

    def exists(self, path):
        return path in self.nodes and self.nodes[path][0] != "link"

    def is_dir(self, path):
        return self.nodes.get(path, ("",))[0] == "dir"

    def is_symlink(self, path):
        return self.nodes.get(path, ("",))[0] == "link"

    def ls(self, path):
        return sorted(p[len(path.rstrip("/")) + 1:] for p in self._under(path)
                      if "/" not in p[len(path.rstrip("/")) + 1:])

    def find(self, path):
        return sorted(p[len(path.rstrip("/")) + 1:] for p in self._under(path))

    def ln_s(self, target, linkname):
        assert linkname not in self.nodes, linkname
        self.mkdir_p(posixpath.dirname(linkname))
        self.nodes[linkname] = ("link", target)

    def write(self, path, data):
        self.put(path, data)

    def rm_rf(self, path):
        for p in [path] + self._under(path):
            self.nodes.pop(p, None)

    def mv(self, src, dst):
        self.mkdir_p(posixpath.dirname(dst))
        for p in [src] + self._under(src):
            self.nodes[dst + p[len(src):]] = self.nodes.pop(p)

def debian_tree(g, version="6.12.1"):
    for name in ("bin", "sbin", "lib"):
        g.ln_s("usr/%s" % name, "%s/%s" % (ROOT, name))
    for d in ("etc", "home", "root", "mnt", "opt", "srv", "tmp", "boot",
              "usr/local/bin", "usr/lib/modules/%s" % version):
        g.mkdir_p("%s/%s" % (ROOT, d))
    g.put("%s/etc/os-release" % ROOT, b"debian")
    g.put("%s/boot/vmlinuz-%s" % (ROOT, version), b"kernel")
    g.put("%s/boot/initrd.img-%s" % (ROOT, version), b"initrd")
    for link in ("vmlinuz", "initrd.img"):
        g.ln_s("boot/%s-%s" % (link, version), "%s/%s" % (ROOT, link))

class Sanitize(avocado.Test):
    def setUp(self):
        self.g = FakeGuestfs()
        debian_tree(self.g)

    def link(self, path):
        return self.g.nodes[ROOT + path][1]

    def test_usrmerge_links_are_added_when_missing(self):
        sanitize(self.g, ROOT)
        self.assertEqual(self.link("/lib64"), "usr/lib64")
        self.assertEqual(self.link("/bin"), "usr/bin")

    def test_a_real_bin_directory_is_refused(self):
        self.g.rm_rf(ROOT + "/bin")
        self.g.mkdir_p(ROOT + "/bin")
        with self.assertRaises(RuntimeError):
            sanitize(self.g, ROOT)

    def test_sysroot_and_ostree_link(self):
        sanitize(self.g, ROOT)
        self.assertTrue(self.g.is_dir(ROOT + "/sysroot"))
        self.assertEqual(self.link("/ostree"), "sysroot/ostree")

    def test_state_directories_become_links_into_var(self):
        sanitize(self.g, ROOT)
        for path, target in (("/home", "var/home"), ("/opt", "var/opt"),
                             ("/srv", "var/srv"), ("/root", "var/roothome"),
                             ("/mnt", "var/mnt"), ("/tmp", "sysroot/tmp"),
                             ("/usr/local", "../var/usrlocal")):
            self.assertEqual(self.link(path), target, path)

    def test_tmpfiles_creates_the_var_targets(self):
        sanitize(self.g, ROOT)
        conf = self.g.nodes[ROOT + "/usr/lib/tmpfiles.d/seine-ostree.conf"][1].decode()
        self.assertIn("d /var/roothome 0700 root root -", conf)
        self.assertIn("d /var/usrlocal 0755 root root -", conf)

    def test_content_seeds_var_through_the_factory_copy(self):
        self.g.put(ROOT + "/root/.bashrc", b"rc")
        sanitize(self.g, ROOT)
        self.assertEqual(
            self.g.nodes[ROOT + "/usr/share/factory/var/roothome/.bashrc"][1], b"rc")
        conf = self.g.nodes[ROOT + "/usr/lib/tmpfiles.d/seine-ostree.conf"][1].decode()
        self.assertIn("C /var/roothome - - - - /usr/share/factory/var/roothome", conf)

    def test_etc_moves_to_usr_etc(self):
        sanitize(self.g, ROOT)
        self.assertFalse(self.g.is_dir(ROOT + "/etc"))
        self.assertEqual(self.g.nodes[ROOT + "/usr/etc/os-release"][1], b"debian")

    def test_machine_id_is_emptied(self):
        self.g.put(ROOT + "/etc/machine-id", b"abc\n")
        sanitize(self.g, ROOT)
        self.assertEqual(self.g.nodes[ROOT + "/usr/etc/machine-id"][1], b"")

    def test_host_keys_are_refused(self):
        self.g.put(ROOT + "/etc/ssh/ssh_host_ed25519_key", b"secret")
        with self.assertRaises(RuntimeError) as cm:
            sanitize(self.g, ROOT)
        self.assertIn("ssh_host_ed25519_key", str(cm.exception))

    def test_kernel_and_initramfs_move_next_to_the_modules(self):
        sanitize(self.g, ROOT)
        modules = ROOT + "/usr/lib/modules/6.12.1"
        self.assertEqual(self.g.nodes[modules + "/vmlinuz"][1], b"kernel")
        self.assertEqual(self.g.nodes[modules + "/initramfs.img"][1], b"initrd")
        self.assertEqual(self.g.ls(ROOT + "/boot"), [])

    def test_links_into_boot_are_dropped(self):
        sanitize(self.g, ROOT)
        self.assertNotIn(ROOT + "/vmlinuz", self.g.nodes)
        self.assertNotIn(ROOT + "/initrd.img", self.g.nodes)

    def test_two_kernels_are_refused(self):
        self.g.put(ROOT + "/boot/vmlinuz-6.13.0", b"k")
        self.g.put(ROOT + "/boot/initrd.img-6.13.0", b"i")
        with self.assertRaises(RuntimeError):
            sanitize(self.g, ROOT)
