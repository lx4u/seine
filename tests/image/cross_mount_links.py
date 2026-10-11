#!/usr/bin/env python3

import avocado
import io
import os
import sys
import tarfile
from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager.imager import Imager
from seine.partition import PartitionHandler

MODIFIED = 1000000000
PARENT_MODIFIED = 1000000100


def _handler():
    ph = PartitionHandler()
    ph.parse({"image": {"filename": "disk.img", "partitions": [
        {"label": "rootfs", "where": "/", "type": "ext4"},
        {"label": "boot", "where": "/boot", "type": "ext4"},
        {"label": "efi", "where": "/efi", "type": "vfat"},
    ]}})
    return ph


def _write_tar(path):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        def add(name, kind=tarfile.REGTYPE, data=b"", link="", mode=0o644, mtime=MODIFIED):
            info = tarfile.TarInfo(name)
            info.type, info.linkname, info.mode, info.mtime = kind, link, mode, mtime
            info.size = len(data)
            if kind == tarfile.REGTYPE and name == "boot/vmlinuz-1":
                info.uid, info.gid = 1234, 4321
            tar.addfile(info, io.BytesIO(data))
        for d in ["boot", "efi", "usr", "usr/bin", "usr/lib", "usr/lib/modules"]:
            add(d, tarfile.DIRTYPE, mode=0o755)
        add("usr/lib/modules/1", tarfile.DIRTYPE, mode=0o755, mtime=PARENT_MODIFIED)
        add("boot/vmlinuz-1", data=b"K" * 5000, mode=0o4751)
        add("usr/lib/modules/1/vmlinuz", tarfile.LNKTYPE, link="boot/vmlinuz-1")
        add("usr/lib/modules/1/vmlinuz2", tarfile.LNKTYPE, link="boot/vmlinuz-1")
        add("usr/bin/a", data=b"a" * 100)
        add("usr/bin/b", tarfile.LNKTYPE, link="usr/bin/a")
        add("efi/f", data=b"f" * 100)
        add("efi/g", tarfile.LNKTYPE, link="efi/f")
        add("usr/we ird [x]", tarfile.LNKTYPE, link="boot/vmlinuz-1")
        add("usr/bad", data=b"decoy")


class LinksScript(avocado.Test):
    def test_each_step_keeps_the_mtime_of_its_directory(self):
        script = Imager._links_script([
            ("cp", "boot/k", "usr/lib/k", {}, 5),
            ("ln", "usr/lib/k", "usr/lib/k2", {}, 0),
        ])
        lines = script.splitlines()
        self.assertEqual(lines[0], "set -e")
        self.assertIn("cp -a /sysroot/boot/k /sysroot/usr/lib/k", lines)
        self.assertIn("ln /sysroot/usr/lib/k /sysroot/usr/lib/k2", lines)
        self.assertEqual(lines.count("touch -r /sysroot/usr/lib \"$ref\""), 2)
        self.assertEqual(lines.count("touch -r \"$ref\" /sysroot/usr/lib"), 2)

    def test_names_are_quoted(self):
        script = Imager._links_script([("cp", "boot/a b", "usr/it's [x]", {}, 5)])
        self.assertIn("cp -a '/sysroot/boot/a b' '/sysroot/usr/it'\"'\"'s [x]'", script)


class ExtractWithLinks(avocado.Test):
    def _imager(self, tarball):
        imager = Imager.__new__(Imager)
        imager.source = mock.Mock()
        imager.source._tarball_for = mock.Mock(return_value=tarball)
        imager._tar_shares = {os.path.dirname(tarball): "seine_rootfs0"}
        return imager

    def test_planned_links_are_left_out_of_the_extraction(self):
        tarball = os.path.join(self.workdir, "rootfs.tar")
        _write_tar(tarball)
        g = mock.Mock()

        self._imager(tarball)._extract_tarball(g, None, _handler())

        scripts = [call.args[1][0] for call in g.debug.call_args_list]
        tar_cmd = next(s for s in scripts if s.startswith("tar -xf"))
        self.assertIn("--no-wildcards --anchored --exclude-from=/run/seine-links.excl", tar_cmd)
        self.assertTrue(any("seine-links.sh" in s for s in scripts[-1:]))
        self.assertLess(scripts.index(tar_cmd), len(scripts) - 1)
        g.write.assert_not_called()

    def test_without_planned_links_the_tar_runs_plain(self):
        tarball = os.path.join(self.workdir, "rootfs.tar")
        with tarfile.open(tarball, "w"):
            pass
        g = mock.Mock()

        self._imager(tarball)._extract_tarball(g, None, _handler())

        scripts = [call.args[1][0] for call in g.debug.call_args_list]
        self.assertEqual(len(scripts), 3)
        self.assertNotIn("exclude", scripts[1])

    def test_the_ostree_staging_does_not_plan_links(self):
        tarball = os.path.join(self.workdir, "rootfs.tar")
        _write_tar(tarball)
        g = mock.Mock()

        self._imager(tarball)._extract_tarball(g, None)

        scripts = [call.args[1][0] for call in g.debug.call_args_list]
        self.assertEqual(len(scripts), 3)
        self.assertNotIn("exclude", scripts[1])


class LinksInTheGuest(avocado.Test):
    timeout = 600

    def setUp(self):
        try:
            import guestfs
        except ImportError as e:
            self.cancel(f"python3-guestfs is missing: {e}")
        self.g = None
        self.disk = os.path.join(self.workdir, "disk.img")
        with open(self.disk, "wb") as f:
            f.truncate(128 * 1024 * 1024)
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(self.disk, format="raw")
        try:
            g.launch()
        except Exception as e:
            self.cancel(f"libguestfs cannot launch here: {e}")
        self.g = g

    def tearDown(self):
        if self.g is not None:
            self.g.close()

    def test_a_link_across_mounts_is_a_copy_and_the_rest_is_intact(self):
        g, ph = self.g, _handler()
        tarball = os.path.join(self.workdir, "rootfs.tar")
        _write_tar(tarball)
        g.part_init("/dev/sda", "gpt")
        for i in range(3):
            g.part_add("/dev/sda", "p", 2048 + i * 40960, 2048 * 20 + i * 40960 - 1)
        for dev, fs in (("/dev/sda1", "ext4"), ("/dev/sda2", "ext4"), ("/dev/sda3", "vfat")):
            g.mkfs(fs, dev)
        g.mount("/dev/sda1", "/")
        for dev, where in (("/dev/sda2", "/boot"), ("/dev/sda3", "/efi")):
            g.mkmountpoint(where)
            g.mount(dev, where)
        g.upload(tarball, "/rootfs.tar")

        with tarfile.open(tarball) as tar:
            excluded, ops = ph.plan_links(tar.getmembers())
        Imager._guest_file(g, "/run/seine-links.excl", "".join(f"{n}\n" for n in excluded))
        g.debug("sh", ["tar -xf /sysroot/rootfs.tar --xattrs -C /sysroot --no-wildcards"
                       " --anchored --exclude-from=/run/seine-links.excl"])
        Imager._guest_file(g, "/run/seine-links.sh", Imager._links_script(ops))
        g.debug("sh", ["sh /run/seine-links.sh"])

        kernel = g.lstat("/boot/vmlinuz-1")
        copy = g.lstat("/usr/lib/modules/1/vmlinuz")
        other = g.lstat("/usr/lib/modules/1/vmlinuz2")
        self.assertNotEqual(copy["dev"], kernel["dev"])
        self.assertEqual((copy["dev"], copy["ino"]), (other["dev"], other["ino"]))
        self.assertEqual(copy["nlink"], 3)
        self.assertEqual((copy["mode"], copy["uid"], copy["gid"], copy["mtime"]),
                         (kernel["mode"], 1234, 4321, MODIFIED))
        self.assertEqual(g.read_file("/usr/lib/modules/1/vmlinuz"), b"K" * 5000)
        self.assertEqual(g.lstat("/usr/lib/modules/1")["mtime"], PARENT_MODIFIED)
        self.assertEqual(g.lstat("/usr/bin/a")["ino"], g.lstat("/usr/bin/b")["ino"])
        self.assertEqual(g.read_file("/efi/g"), b"f" * 100)
        self.assertEqual(g.lstat("/usr/we ird [x]")["ino"], copy["ino"])
        self.assertTrue(g.is_file("/usr/bad"))
        self.assertFalse(g.exists("/seine-links.excl"))
