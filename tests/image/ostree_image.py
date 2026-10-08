#!/usr/bin/env python3

import avocado
import os
import shutil
import subprocess
import sys
import tempfile
import yaml

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import HOST_ARCH
from tests.testutils import prune_on_pass

COMMON = os.path.join(path_to_sources, "examples", "common")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

SPEC = """
requires:
    - stdlib:debian/amd64.yml
    - stdlib:debian/trixie.yml

playbook:
    - name: dracut and ostree
      priority: 700
      tasks:
          - name: install dracut, ostree and systemd
            apt:
                name: [dracut, dracut-config-generic, ostree, ostree-boot, systemd, systemd-sysv]
                state: present

image:
    filename: {disk}
    table: gpt
    ostree:
        mode: standard
    partitions:
        - label: esp
          type: vfat
          size: 64MiB
          where: /efi
          flags: [boot]
        - label: sysroot
          type: ext4
          where: /
        - label: var
          type: ext4
          size: 256MiB
          where: /var
"""

# A 'standard' ostree image built end to end, then looked into with
# guestfs: one deployment of the committed rootfs, nothing of the build
# left on the sysroot, /var seeded on its own partition.
class StandardOstreeImageBuilds(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel is amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        try:
            import guestfs
        except ImportError as e:
            self.cancel("python3-guestfs is missing: %s" % e)

    def tearDown(self):
        prune_on_pass(self)

    def test_a_commit_signed_with_a_vault_key_verifies(self):
        try:
            from gi.repository import GLib
        except ImportError as e:
            self.cancel("python3-gi is missing: %s" % e)
        if shutil.which("gpg") is None:
            self.cancel("gpg is needed to verify the signature")
        disk = os.path.join(self.workdir, "signed.img")
        spec = os.path.join(self.workdir, "main.yaml")
        with open(spec, "w") as f:
            f.write(SPEC.format(common=COMMON, disk=disk)
                    .replace("requires:", "requires:\n    - {common}/dev-ostree-key"
                             .format(common=COMMON), 1)
                    .replace("mode: standard",
                             "mode: standard\n        gpg-key: vault:ostree-commits"))
        log = os.path.join(self.outputdir, "signed-build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", spec],
                cwd=path_to_sources, stdout=f, stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, "build failed, see %s" % log)

        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            g.mount_ro("/dev/sda2", "/")
            objects = "/ostree/repo/objects"
            commits = [f for f in g.find(objects) if f.endswith(".commit")]
            self.assertEqual(len(commits), 1, commits)
            commit = g.read_file("%s/%s" % (objects, commits[0]))
            meta = g.read_file("%s/%s" % (objects, commits[0][:-6] + "commitmeta"))
            g.umount("/")
        finally:
            g.close()

        signature = bytes(GLib.Variant.new_from_bytes(
            GLib.VariantType("a{sv}"), GLib.Bytes.new(meta), False)
            .lookup_value("ostree.gpgsigs", GLib.VariantType("aay"))[0])
        home = tempfile.mkdtemp(prefix="seine-test-gpg-", dir="/var/tmp")
        try:
            os.chmod(home, 0o700)
            with open(os.path.join(COMMON, "dev-ostree-key.yaml")) as f:
                key = yaml.safe_load(f)["defaults"]["vault"]["ostree-commits"]
            for name, data in (("key.asc", key["private_key"].encode()),
                               ("commit", commit), ("commit.sig", signature)):
                with open(os.path.join(home, name), "wb") as f:
                    f.write(data)
            run = lambda *args: subprocess.run(
                ["gpg", "--homedir", home, "--batch"] + list(args),
                cwd=home, capture_output=True, text=True)
            self.assertEqual(run("--import", "key.asc").returncode, 0)
            verified = run("--verify", "commit.sig", "commit")
            self.assertEqual(verified.returncode, 0, verified.stderr)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_the_sysroot_holds_one_deployment_of_the_commit(self):
        disk = os.path.join(self.workdir, "disk.img")
        spec = os.path.join(self.workdir, "main.yaml")
        with open(spec, "w") as f:
            f.write(SPEC.format(common=COMMON, disk=disk))
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", spec],
                cwd=path_to_sources, stdout=f, stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0, "build failed, see %s" % log)

        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            g.mount_ro("/dev/sda2", "/")
            self.assertIn("mode=bare", g.cat("/ostree/repo/config"))
            deployments = [d for d in g.ls("/ostree/deploy/debian/deploy")
                           if not d.endswith(".origin")]
            self.assertEqual(len(deployments), 1, deployments)
            deployment = "/ostree/deploy/debian/deploy/%s" % deployments[0]

            entries = g.ls("/boot/loader/entries")
            self.assertEqual(len(entries), 1, entries)
            entry = g.cat("/boot/loader/entries/%s" % entries[0])
            options = [l for l in entry.splitlines() if l.startswith("options ")][0]
            partuuid = g.part_get_gpt_guid("/dev/sda", 2).lower()
            self.assertIn("root=PARTUUID=%s rw " % partuuid, options)
            self.assertIn(" ostree=/ostree/boot.1/debian/", options)
            self.assertIn("bootloader=none", g.cat("/ostree/repo/config"))

            self.assertEqual(g.cat("%s/etc/machine-id" % deployment), "",
                             "every device would share this machine-id")
            self.assertFalse(g.is_file("%s/etc/ssh/ssh_host_ed25519_key" % deployment))
            self.assertTrue(g.is_file("%s/usr/bin/ostree" % deployment))
            self.assertEqual(g.readlink("%s/home" % deployment), "var/home")

            # Only the physical mounts: ostree mounts the root itself.
            fstab = [l.split()[1] for l in g.cat("%s/etc/fstab" % deployment).splitlines()]
            self.assertEqual(sorted(fstab), ["/efi/", "/var/"])
            self.assertTrue(g.is_dir("%s/efi" % deployment),
                            "no mount point for the ESP in the commit")

            leftovers = [n for n in g.ls("/") if n.startswith(".seine")]
            self.assertEqual(leftovers, [], "the build left files on the sysroot")
            g.umount("/")

            g.mount_ro("/dev/sda3", "/")
            self.assertTrue(g.is_file("/.ostree-selabeled"),
                            "deploy did not seed the /var partition")
            g.umount("/")
        finally:
            g.close()
