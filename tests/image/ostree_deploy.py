#!/usr/bin/env python3

import avocado
import os
import shutil
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager import ostree

# Plain Python, no guestfs appliance involved -- records what is run.
class FakeGuestfs:
    def __init__(self):
        self.commands = []
        self.written = {}
        self.removed = []
        self.dirs = []
        self.free = 0
        self.sizes = {}

    def command(self, argv):
        self.commands.append(argv)
        return "abc123\n"

    def write(self, path, content):
        self.written[path] = content

    def mkdir_p(self, path):
        self.dirs.append(path)

    def chmod(self, mode, path):
        pass

    def rm_rf(self, path):
        self.removed.append(path)

    def ls(self, path):
        if path.endswith("loader/entries"):
            return ["ostree-2.conf", "ostree-1.conf", "other"]
        if path.startswith("/boot/ostree"):
            return ["vmlinuz", "initramfs.img"]
        return ["abc.0", "abc.0.origin"]

    def cat(self, path):
        return "title %s\nlinux /ostree/x/vmlinuz\ninitrd /ostree/x/initrd\noptions rw ostree=/y\n" % path

    def statvfs(self, path):
        return {"bsize": 4096, "bavail": self.free // 4096}

    def stat(self, path):
        return {"size": self.sizes[path]}

    def cp(self, src, dst):
        self.copied = getattr(self, "copied", []) + [(src, dst)]

    def read_file(self, path):
        return self.files[path]

    def set_e2attrs(self, path, attrs, clear=False):
        self.unlocked = getattr(self, "unlocked", []) + [(path, attrs, clear)]

class MountPaths(avocado.Test):
    def test_var_belongs_to_the_stateroot(self):
        self.assertEqual(ostree.target_path("/var/", "debian"),
                         "/sysroot/ostree/deploy/debian/var")
        self.assertEqual(ostree.target_path("/var/home/", "debian"),
                         "/sysroot/ostree/deploy/debian/var/home")

    def test_other_mounts_sit_under_the_sysroot(self):
        self.assertEqual(ostree.target_path("/efi/", "debian"), "/sysroot/efi")
        self.assertEqual(ostree.target_path("/boot/", "debian"), "/sysroot/boot")

    def test_a_name_starting_like_var_is_not_var(self):
        self.assertEqual(ostree.target_path("/variable/", "debian"),
                         "/sysroot/variable")

class Commands(avocado.Test):
    def test_sysroot_is_initialised_for_the_stateroot(self):
        g = FakeGuestfs()
        ostree.init_sysroot(g, "main")
        self.assertEqual(g.commands, [
            ["/usr/bin/ostree", "admin", "init-fs", "/sysroot"],
            ["/usr/bin/ostree", "admin", "os-init", "--sysroot=/sysroot", "main"],
            ["/usr/bin/ostree", "config", "--repo=/sysroot/ostree/repo", "set",
             "sysroot.bootloader", "none"],
        ])

    def test_commit_skips_the_mounts_and_pins_the_time(self):
        g = FakeGuestfs()
        checksum = ostree.commit(g, "debian/amd64", 1700000000)
        self.assertEqual(checksum, "abc123")
        argv = g.commands[0]
        self.assertIn("--tree=dir=/", argv)
        self.assertIn("--timestamp=@1700000000", argv)
        self.assertIn("--repo=/sysroot/ostree/repo", argv)
        skipped = g.written[ostree.SKIP_LIST].decode().split()
        for path in ("/sysroot", "/proc", "/dev", "/sys", "/run"):
            self.assertIn(path, skipped)

    def test_commit_restores_the_skipped_directories(self):
        g = FakeGuestfs()
        ostree.commit(g, "debian/amd64", 0)
        for name in ("sysroot", "dev", "proc", "sys", "run"):
            self.assertIn("%s/%s" % (ostree.SKELETON, name), g.dirs)
        self.assertIn("--tree=dir=%s" % ostree.SKELETON, g.commands[0])

    def test_commit_leaves_nothing_behind_on_the_sysroot(self):
        g = FakeGuestfs()
        ostree.commit(g, "debian/amd64", 0)
        self.assertEqual(sorted(g.removed),
                         sorted([ostree.SKIP_LIST, ostree.SKELETON]))

    def test_deploy_passes_the_kernel_args_before_the_ref(self):
        g = FakeGuestfs()
        ostree.deploy(g, "main", "main/amd64", ["root=PARTUUID=abc", "rw", "console=ttyS0"])
        self.assertEqual(g.commands, [
            ["/usr/bin/ostree", "admin", "deploy", "--sysroot=/sysroot",
             "--os=main", "--karg=root=PARTUUID=abc", "--karg=rw",
             "--karg=console=ttyS0", "main/amd64"]])

    def test_deploy_names_stateroot_and_ref(self):
        g = FakeGuestfs()
        ostree.deploy(g, "main", "main/amd64")
        self.assertEqual(g.commands, [
            ["/usr/bin/ostree", "admin", "deploy", "--sysroot=/sysroot",
             "--os=main", "main/amd64"]])

class FakeVault:
    def pgp_detach_sign(self, name, data, timestamp):
        self.args = (name, data, timestamp)
        return b"signature"

class SignedCommit(avocado.Test):
    def setUp(self):
        try:
            import gi.repository.GLib
        except ImportError as e:
            self.cancel("python3-gi is missing: %s" % e)


    def test_the_commit_file_is_signed_and_its_signature_kept_beside_it(self):
        g = FakeGuestfs()
        path = "/sysroot/ostree/repo/objects/ab/cdef.commit"
        g.files = {path: b"commit"}
        vault = FakeVault()
        ostree.sign_commit(g, vault, "abcdef", "vault:ostree-commits", 1700000000)
        self.assertEqual(vault.args, ("ostree-commits", b"commit", 1700000000))
        self.assertEqual(
            g.written, {path[:-6] + "commitmeta": ostree.commitmeta([b"signature"])})

    def test_commitmeta_holds_the_signatures_under_the_ostree_key(self):
        from gi.repository import GLib
        meta = GLib.Variant.new_from_bytes(
            GLib.VariantType("a{sv}"),
            GLib.Bytes.new(ostree.commitmeta([b"one", b"two"])), False)
        sigs = meta.lookup_value("ostree.gpgsigs", GLib.VariantType("aay"))
        self.assertEqual([bytes(s) for s in sigs], [b"one", b"two"])

class Unlock(avocado.Test):
    def test_deployment_roots_lose_the_immutable_flag(self):
        g = FakeGuestfs()
        ostree.unlock_deployments(g, "main")
        self.assertEqual(g.unlocked, [
            ("/sysroot/ostree/deploy/main/deploy/abc.0", "i", True)])

class FindDeployment(avocado.Test):
    def test_the_origin_file_is_not_a_deployment(self):
        self.assertEqual(ostree.find_deployment(FakeGuestfs(), "main"), "abc.0")

    def test_no_deployment_is_an_error(self):
        g = FakeGuestfs()
        g.ls = lambda path: []
        with self.assertRaisesRegex(RuntimeError, "found 0"):
            ostree.find_deployment(g, "main")

    def test_several_deployments_are_an_error(self):
        g = FakeGuestfs()
        g.ls = lambda path: ["abc.0", "abc.0.origin", "def.0", "def.0.origin"]
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            ostree.find_deployment(g, "main")

ENTRY = {"title": "Debian (ostree:0)", "linux": "/ostree/debian-1/vmlinuz-6.1",
         "initrd": "/ostree/debian-1/initramfs-6.1.img",
         "options": "root=PARTUUID=abc rw ostree=/ostree/boot.1/debian/1/0"}

class BootEntries(avocado.Test):
    def test_an_entry_is_read_key_by_key(self):
        entry = ostree.parse_entry("# note\ntitle A b\nversion 1\noptions rw x=y\n\n")
        self.assertEqual(entry, {"title": "A b", "version": "1", "options": "rw x=y"})

    def test_entries_are_read_in_file_name_order_and_only_conf_files(self):
        entries = ostree.read_entries(FakeGuestfs())
        self.assertEqual([e["title"] for e in entries], [
            "/sysroot/boot/loader/entries/ostree-1.conf",
            "/sysroot/boot/loader/entries/ostree-2.conf"])
        self.assertEqual(entries[0]["_file"], "ostree-1.conf")

class GrubMenu(avocado.Test):
    def group(self, label="debian", uuid="u1", boot="/boot"):
        return {"label": label, "uuid": uuid, "boot": boot, "entries": [ENTRY]}

    def test_an_entry_finds_its_file_system_and_adds_the_boot_directory(self):
        menu = ostree.grub_menuentries([self.group()])
        self.assertEqual(menu,
            "menuentry 'Debian (ostree:0)' {\n"
            "    search --no-floppy --fs-uuid --set=root u1\n"
            "    linux /boot/ostree/debian-1/vmlinuz-6.1 root=PARTUUID=abc rw "
            "ostree=/ostree/boot.1/debian/1/0\n"
            "    initrd /boot/ostree/debian-1/initramfs-6.1.img\n"
            "}\n\n")

    def test_a_boot_partition_of_its_own_has_no_boot_prefix(self):
        menu = ostree.grub_menuentries([self.group(boot="")])
        self.assertIn("    linux /ostree/debian-1/vmlinuz-6.1 ", menu)

    def test_several_groups_are_told_apart_by_title_and_file_system(self):
        menu = ostree.grub_menuentries([self.group("main", "u1"), self.group("other", "u2")])
        self.assertLess(menu.index("'main: Debian"), menu.index("'other: Debian"))
        self.assertIn("--set=root u1", menu)
        self.assertIn("--set=root u2", menu)

    def test_a_quote_in_a_title_is_dropped(self):
        group = self.group()
        group["entries"] = [dict(ENTRY, title="it's")]
        self.assertIn("menuentry 'its' {", ostree.grub_menuentries([group]))

class SystemdBoot(avocado.Test):
    def group(self, label="main"):
        return {"label": label, "entries": [dict(ENTRY, _file="ostree-1.conf")]}

    def test_one_group_keeps_its_titles_but_not_its_file_names(self):
        files = ostree.systemd_boot_entries([self.group()])
        self.assertEqual(list(files), ["main-ostree-1.conf"])
        self.assertIn("title Debian (ostree:0)\n", files["main-ostree-1.conf"])
        self.assertIn("linux /ostree/debian-1/vmlinuz-6.1\n", files["main-ostree-1.conf"])
        self.assertNotIn("_file", files["main-ostree-1.conf"])

    def test_several_groups_do_not_collide(self):
        files = ostree.systemd_boot_entries([self.group("main"), self.group("other")])
        self.assertEqual(sorted(files), ["main-ostree-1.conf", "other-ostree-1.conf"])
        self.assertIn("title other: Debian", files["other-ostree-1.conf"])

    def test_the_kernel_directories_come_once(self):
        group = self.group()
        group["entries"].append(dict(ENTRY, _file="ostree-2.conf"))
        self.assertEqual(ostree.boot_directories(group), ["/ostree/debian-1"])

    def test_the_owner_is_the_default_entry(self):
        self.assertEqual(ostree.loader_conf("main"), "default main-*\ntimeout 5\n")

    def test_kernel_files_are_copied_when_they_fit(self):
        g = FakeGuestfs()
        g.free = 100 * 4096
        g.sizes = {"/boot/ostree/a/vmlinuz": 5000, "/boot/ostree/a/initramfs.img": 4096}
        ostree.copy_boot_files(g, {"/boot/ostree/a": "/efi/ostree/a"}, "/efi")
        self.assertEqual(g.copied, [
            ("/boot/ostree/a/vmlinuz", "/efi/ostree/a/vmlinuz"),
            ("/boot/ostree/a/initramfs.img", "/efi/ostree/a/initramfs.img")])

    def test_nothing_is_copied_when_they_do_not_fit(self):
        g = FakeGuestfs()
        g.free = 2 * 4096
        g.sizes = {"/boot/ostree/a/vmlinuz": 5000, "/boot/ostree/a/initramfs.img": 4096}
        with self.assertRaisesRegex(RuntimeError, "need 1 MiB"):
            ostree.copy_boot_files(g, {"/boot/ostree/a": "/efi/ostree/a"}, "/efi")
        self.assertFalse(hasattr(g, "copied"))

class UkiBoot(avocado.Test):
    def group(self, label="main"):
        return {"label": label, "uuid": "u1", "boot": "/boot",
                "entries": [dict(ENTRY, _file="ostree-1.conf")],
                "ukis": ["%s-os.efi" % label]}

    def test_grub_chainloads_the_uki_instead_of_the_kernel(self):
        menu = ostree.grub_menuentries([self.group()])
        self.assertEqual(menu,
            "menuentry 'main-os' {\n"
            "    search --no-floppy --file --set=root /EFI/Linux/main-os.efi\n"
            "    chainloader /EFI/Linux/main-os.efi\n"
            "}\n\n")

    def test_a_group_without_uki_keeps_its_entry(self):
        plain = self.group("other")
        del plain["ukis"]
        menu = ostree.grub_menuentries([self.group(), plain])
        self.assertIn("menuentry 'main: main-os'", menu)
        self.assertIn("menuentry 'other: Debian (ostree:0)'", menu)

    def test_systemd_boot_gets_no_entry_for_a_uki(self):
        plain = self.group("other")
        del plain["ukis"]
        files = ostree.systemd_boot_entries([self.group(), plain])
        self.assertEqual(list(files), ["other-ostree-1.conf"])

class UkiRebuild(avocado.Test):
    class Fake(ostree.OstreeSysroot):
        def __init__(self, output_dir):
            self._output_dir = output_dir
            self.rebuilt = []

        def _rebuild_uki(self, workdir, original, extra):
            self.rebuilt.append((original, extra))
            return "rebuilt.efi"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="seine-test-uki-")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def guest(self):
        g = FakeGuestfs()
        g.ls = lambda path: ["os.efi", "notes.txt"]
        g.is_dir = lambda path: True
        g.download = lambda src, dst: open(dst, "w").close()
        g.rm = lambda path: g.removed.append(path)
        return g

    def test_only_efi_files_leave_the_root_file_system(self):
        g = self.guest()
        ukis = self.Fake(self.dir)._take_ukis(g)
        self.assertEqual([name for name, _ in ukis], ["os.efi"])
        self.assertEqual(g.removed, ["/boot/EFI/Linux/os.efi"])

    def test_no_uki_directory_means_no_uki(self):
        g = self.guest()
        g.is_dir = lambda path: False
        self.assertEqual(self.Fake(self.dir)._take_ukis(g), [])

    def test_the_uki_gets_the_deployment_options_and_the_stateroot_name(self):
        sysroot = self.Fake(self.dir)
        group = {"entries": [dict(ENTRY)]}
        sysroot._rebuild_ukis(group, "main", [("os.efi", "/w/os.efi")])
        self.assertEqual(sysroot.rebuilt, [("/w/os.efi", ENTRY["options"])])
        self.assertEqual(group["ukis"], ["main-os.efi"])

    def test_several_boot_entries_are_refused(self):
        group = {"entries": [dict(ENTRY), dict(ENTRY)]}
        with self.assertRaises(RuntimeError):
            self.Fake(self.dir)._rebuild_ukis(group, "main", [("os.efi", "/w/os.efi")])
