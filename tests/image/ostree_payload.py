#!/usr/bin/env python3

import avocado
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

import ostree_boot
from seine.container import ContainerEngine
from seine.imager import ostree
from seine.imager import payload
from seine.imager import uki
from seine.utils import HOST_ARCH
from tests.testutils import prune_on_pass

# Stands for the tools container: records the commands and leaves the files
# the real ones would, with command lines taken from 'cmdlines' by UKI name.
class FakeTools:
    def __init__(self, checksum="c0ffee", cmdlines=None):
        self.commands = []
        self.checksum = checksum
        self.cmdlines = cmdlines or {}

    def __call__(self, args, volumes):
        self.commands.append(args)
        guest = {g.split(":")[0]: h for h, g in volumes.items()}
        if args[0] == "objcopy":
            name = os.path.basename(args[3])
            with open(os.path.join(guest["/out"], "cmdline.txt"), "w") as f:
                f.write(self.cmdlines[name])
            return
        top = os.path.join(guest["/payload"], payload.REPO_DIR)
        if "init" in args:
            os.makedirs(os.path.join(top, "objects"))
        elif "pull-local" in args:
            self.write_ref(top, args[-1])
        elif "refs" in args:
            self.write_ref(top, args[-2].split("=", 1)[1])
        elif "summary" in args:
            with open(os.path.join(top, "summary"), "wb") as f:
                f.write(b"summary")

    def write_ref(self, top, ref):
        path = os.path.join(top, "refs", "heads", ref)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(self.checksum + "\n")

class PayloadTest(avocado.Test):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="seine-update-test-", dir="/var/tmp")
        self.payload = os.path.join(self.dir, "disk-payload")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def ship(self, version, checksum="c0ffee", ref="debian/amd64"):
        top = os.path.join(self.payload, payload.REPO_DIR, "refs", "heads")
        path = os.path.join(top, payload.version_ref(ref, version))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(checksum + "\n")
        uki_dir = os.path.join(self.payload, payload.UKI_DIR)
        os.makedirs(uki_dir, exist_ok=True)
        with open(os.path.join(uki_dir, "debian-%s.efi" % version), "wb") as f:
            f.write(b"uki " + version.encode())

    def uki(self, text=b"new uki"):
        path = os.path.join(self.dir, "rebuilt.efi")
        with open(path, "wb") as f:
            f.write(text)
        return path

class PayloadDirectory(avocado.Test):
    def setUp(self):
        self.saved = ContainerEngine.deploy_dir
        ContainerEngine.deploy_dir = staticmethod(
            lambda release=None: "/deploy/%s" % release)

    def tearDown(self):
        ContainerEngine.deploy_dir = self.saved

    def test_default_is_named_after_the_image_and_sits_next_to_it(self):
        self.assertEqual(
            payload.payload_dir("/deploy/trixie/pc.img", None, "trixie"),
            "/deploy/trixie/pc-payload")
        self.assertEqual(
            payload.payload_dir("/x/pc.raw.img", {}, "trixie"), "/x/pc.raw-payload")

    def test_relative_path_goes_under_the_deploy_directory(self):
        self.assertEqual(
            payload.payload_dir("/x/pc.img", {"path": "fleet"}, "trixie"),
            "/deploy/trixie/fleet")

    def test_absolute_path_is_taken_as_it_is(self):
        self.assertEqual(
            payload.payload_dir("/x/pc.img", {"path": "/srv/fleet"}, "trixie"),
            "/srv/fleet")

    def test_ref_of_a_version(self):
        self.assertEqual(payload.version_ref("debian/amd64", "23"), "debian/amd64.v23")

class PayloadChecks(PayloadTest):
    def check(self, version, deltas=()):
        payload.check_payload(
            self.payload, "debian/amd64", "debian", version, list(deltas))

    def test_an_empty_payload_takes_anything(self):
        self.check("1")

    def test_versions_are_read_in_version_order(self):
        for version in ("9", "10", "2"):
            self.ship(version)
        self.assertEqual(payload.uki_versions(self.payload, "debian"), ["2", "9", "10"])

    def test_an_older_version_is_refused(self):
        self.ship("22")
        with self.assertRaises(RuntimeError) as cm:
            self.check("21")
        self.assertIn("older than version 22", str(cm.exception))

    def test_the_same_or_a_newer_version_passes(self):
        self.ship("22")
        self.check("22")
        self.check("23")

    def test_a_baseline_needs_its_uki_and_its_ref(self):
        self.ship("20")
        self.check("23", ["20"])
        with self.assertRaises(RuntimeError) as cm:
            self.check("23", ["19"])
        self.assertIn("'deltas-from: 19'", str(cm.exception))
        os.remove(os.path.join(self.payload, payload.REPO_DIR, "refs", "heads",
                               "debian", "amd64.v20"))
        with self.assertRaises(RuntimeError):
            self.check("23", ["20"])

    def test_the_esp_needs_three_ukis_and_some_room(self):
        mib = 1024 * 1024
        payload.check_esp(3 * 40 * mib + 16 * mib, 40 * mib)
        with self.assertRaises(RuntimeError) as cm:
            payload.check_esp(3 * 40 * mib + 16 * mib - 1, 40 * mib)
        self.assertIn("3 UKIs", str(cm.exception))

class PayloadCommit(PayloadTest):
    def add(self, tools, version="23", checksum="c0ffee", deltas=()):
        payload.add_commit(tools, self.payload, os.path.join(self.dir, "export"),
                           "debian/amd64", checksum, version, list(deltas))

    def test_a_first_commit_creates_the_repo_and_the_version_ref(self):
        tools = FakeTools()
        self.add(tools)
        verbs = [[a for a in c if not a.startswith("--repo")][1:3] for c in tools.commands]
        self.assertEqual(verbs[0], ["fsck", "--quiet"])
        self.assertIn("init", tools.commands[1])
        self.assertIn("--mode=archive", tools.commands[1])
        self.assertEqual(tools.commands[2][-3:], ["pull-local", "/src/export", "debian/amd64"])
        self.assertIn("--create=debian/amd64.v23", tools.commands[3])
        self.assertIn("summary", tools.commands[-1])
        self.assertEqual(payload.ref_commit(self.payload, "debian/amd64.v23"), "c0ffee")

    def test_an_existing_repo_is_not_initialised_again(self):
        self.add(FakeTools())
        tools = FakeTools(checksum="beef")
        self.add(tools, "24", "beef")
        self.assertFalse(any("init" in c for c in tools.commands))

    def test_deltas_name_both_version_refs(self):
        self.ship("20")
        tools = FakeTools()
        self.add(tools, deltas=["20"])
        delta = [c for c in tools.commands if "static-delta" in c][0]
        self.assertEqual(delta[-2:], ["--from=debian/amd64.v20", "--to=debian/amd64.v23"])

    def test_the_same_version_with_the_same_commit_is_accepted(self):
        self.ship("23")
        tools = FakeTools()
        self.add(tools)
        self.assertFalse(any("refs" in c for c in tools.commands))

    def test_the_same_version_with_another_commit_is_refused(self):
        self.ship("23", "c0ffee")
        tools = FakeTools()
        with self.assertRaises(RuntimeError) as cm:
            self.add(tools, checksum="beef")
        self.assertIn("already shipped with another commit", str(cm.exception))
        self.assertEqual(tools.commands, [])

class PayloadUki(PayloadTest):
    def add(self, tools, version="23", text=b"new uki"):
        payload.add_uki(tools, self.payload, "debian", version, self.uki(text), self.dir)

    def test_the_first_uki_is_copied_in(self):
        self.add(FakeTools())
        with open(os.path.join(self.payload, "uki", "debian-23.efi"), "rb") as f:
            self.assertEqual(f.read(), b"new uki")
        self.assertEqual(os.listdir(os.path.join(self.payload, "uki")), ["debian-23.efi"])

    def test_the_command_line_is_compared_without_ostree(self):
        self.ship("22")
        self.add(FakeTools(cmdlines={
            "rebuilt.efi": "root=PARTUUID=a rw ostree=/ostree/debian-2",
            "debian-22.efi": "root=PARTUUID=a rw ostree=/ostree/debian-1"}))
        self.assertTrue(os.path.exists(os.path.join(self.payload, "uki", "debian-23.efi")))

    def test_a_changed_command_line_needs_a_reflash(self):
        self.ship("22")
        with self.assertRaises(RuntimeError) as cm:
            self.add(FakeTools(cmdlines={
                "rebuilt.efi": "root=PARTUUID=b rw ostree=/ostree/debian-2",
                "debian-22.efi": "root=PARTUUID=a rw ostree=/ostree/debian-1"}))
        self.assertIn("needs a reflash", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.payload, "uki", "debian-23.efi")))

class PayloadManifest(PayloadTest):
    def test_lists_every_uki_sorted_and_signs_the_text(self):
        for version in ("22", "10"):
            self.ship(version)
        signed = []
        payload.write_manifest(
            self.payload, lambda text: signed.append(text) or b"signature")
        uki_dir = os.path.join(self.payload, "uki")
        want = "".join(
            "%s  debian-%s.efi\n" % (hashlib.sha256(b"uki " + v.encode()).hexdigest(), v)
            for v in ("10", "22"))
        with open(os.path.join(uki_dir, "SHA256SUMS")) as f:
            self.assertEqual(f.read(), want)
        self.assertEqual(signed, [want.encode()])
        with open(os.path.join(uki_dir, "SHA256SUMS.gpg"), "rb") as f:
            self.assertEqual(f.read(), b"signature")
        self.assertEqual(sorted(os.listdir(uki_dir)),
                         ["SHA256SUMS", "SHA256SUMS.gpg", "debian-10.efi", "debian-22.efi"])

class FakeGuest:
    def __init__(self, dir):
        self.dir = dir
        self.commands, self.removed, self.copied = [], [], []

    def mkdir_p(self, path):
        pass

    def command(self, argv):
        self.commands.append(argv)
        return ""

    def copy_out(self, path, host):
        self.copied.append((path, host))
        os.makedirs(os.path.join(host, os.path.basename(path)))

    def rm_rf(self, path):
        self.removed.append(path)

class FakeVault:
    def pgp_detach_sign(self, name, data, epoch):
        return ("%s:%d:" % (name, epoch)).encode() + hashlib.sha256(data).digest()

class PayloadTools(PayloadTest):
    class Imager(ostree.OstreeSysroot, uki.UkiAnchor):
        def __init__(self, fail):
            self.fail = fail

        def _run_tool(self, args, volumes, **kwargs):
            if self.fail:
                raise OSError("executable file `ostree` not found")

    def test_an_appliance_without_ostree_says_how_to_rebuild_it(self):
        with self.assertRaises(RuntimeError) as cm:
            self.Imager(True)._need_ostree_tool()
        self.assertIn("imager: rebuild: different", str(cm.exception))

    def test_an_appliance_with_ostree_passes(self):
        self.Imager(False)._need_ostree_tool()

class PayloadWrite(PayloadTest):
    class Source:
        spec = {"distribution": {"release": "trixie"}}
        _output = "/nowhere/disk.img"

        def _epoch(self):
            return 7

    class Imager(ostree.OstreeSysroot, uki.UkiAnchor):
        def __init__(self, test):
            self.source = PayloadWrite.Source()
            self.tools = FakeTools()
            self._output_dir = test.dir
            self.test = test

        def _run_tool(self, args, volumes, **kwargs):
            self.tools(args, volumes)

        def _vault_provider(self):
            return FakeVault()

    def settings(self, **extra):
        return dict({
            "stateroot": "debian", "version": "23", "gpg-key": "vault:commits",
            "manifest-key": "vault:manifest",
            "payload": {"path": self.payload}}, **extra)

    def write(self, settings=None, mounts=(), built=True):
        imager = self.Imager(self)
        group = {"built": [("debian-23.efi", self.dir, "rebuilt.efi")]} if built else {}
        self.uki()
        guest = FakeGuest(self.dir)
        imager._write_payload(guest, settings or self.settings(), list(mounts),
                              group, "debian/amd64", "c0ffee")
        return imager, guest

    def test_exports_in_the_appliance_and_leaves_a_signed_payload(self):
        imager, guest = self.write()
        self.assertIn("--mode=archive", guest.commands[0])
        self.assertIn("pull-local", guest.commands[1])
        self.assertEqual(guest.commands[1][-2:], ["/sysroot/ostree/repo", "debian/amd64"])
        self.assertEqual(guest.removed, [guest.copied[0][0].rsplit("/", 1)[0]])
        uki_dir = os.path.join(self.payload, "uki")
        self.assertEqual(sorted(os.listdir(uki_dir)),
                         ["SHA256SUMS", "SHA256SUMS.gpg", "debian-23.efi"])
        with open(os.path.join(uki_dir, "SHA256SUMS.gpg"), "rb") as f:
            self.assertTrue(f.read().startswith(b"manifest:7:"))
        with open(os.path.join(self.payload, "repo", "summary.sig"), "rb") as f:
            self.assertIn(b"commits:7:", f.read())

    def test_a_build_without_a_uki_is_refused(self):
        with self.assertRaises(RuntimeError) as cm:
            self.write(built=False)
        self.assertIn("needs a UKI", str(cm.exception))

    def test_a_small_esp_is_refused_before_anything_is_written(self):
        esp = {"_prefix": "/efi/", "_size": 1024}
        with self.assertRaises(RuntimeError) as cm:
            self.write(mounts=[esp])
        self.assertIn("the ESP holds", str(cm.exception))
        self.assertFalse(os.path.exists(self.payload))


# A setuid file and a file of group 'shadow': what a copy that loses
# ownership would get wrong.
OWNED_FILES = """    - name: files that need their ownership kept
      priority: 910
      tasks:
          - name: a setuid file and a file of group shadow
            shell: |
                echo x > /usr/bin/seine-suid
                chmod 4755 /usr/bin/seine-suid
                echo y > /usr/share/seine-gfile
                chgrp shadow /usr/share/seine-gfile
                chmod 0640 /usr/share/seine-gfile
"""

# What a client does with a payload, in a container of the same release:
# check the whole repo, the signed list, and pull with the commit key.
CHECK = """set -e
apt-get update -qq
apt-get install -y -qq ostree gpgv >/dev/null
ostree --repo=/payload/repo fsck --quiet
cd /payload/uki
gpgv --keyring /keys/manifest-key.gpg SHA256SUMS.gpg SHA256SUMS
sha256sum -c SHA256SUMS
cd /
ostree --repo=/client init --mode=archive
ostree --repo=/client remote add --set=gpg-verify=true \\
    --set=gpg-verify-summary=true --gpg-import=/keys/commit-key.gpg \\
    seine file:///payload/repo
ostree --repo=/client pull seine debian/amd64
for version in "$@"; do ostree --repo=/client pull seine debian/amd64.v$version; done
ostree --repo=/client refs
ostree --repo=/client ls debian/amd64 /usr/bin/seine-suid /usr/share/seine-gfile
"""

class WritesAPayload(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if os.environ.get("SEINE_TEST_PLAN", "") != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel and boot loader are amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")

    def tearDown(self):
        prune_on_pass(self)

    def build(self, version, payload):
        disk = os.path.join(self.workdir, "payload-%s.img" % version)
        group = ostree_boot.GROUP_SYSTEMD_BOOT.replace(
            "imager:\n", "imager:\n    rebuild: different\n").replace("requires:\n", (
            "requires:\n    - {common}/dev-ostree-key\n"
            "    - {common}/dev-update-manifest-key\n"), 1)
        disk_spec = ostree_boot.SINGLE_DISK.replace(
            "        mode: standard\n",
            "        mode: standard\n"
            '        version: "%s"\n'
            "        gpg-key: vault:ostree-commits\n"
            "        manifest-key: vault:update-manifest\n"
            "        payload:\n            path: %s\n" % (version, payload))
        spec = ostree_boot.OstreeImageBoots.write(self, "payload-%s.yaml" % version, (
            group + ostree_boot.UKI + OWNED_FILES + disk_spec).format(
                common=ostree_boot.COMMON, name="payload-os", disk=disk,
                pc_image=os.path.join(ostree_boot.EXAMPLES, "pc-image")))
        ostree_boot.OstreeImageBoots.build(self, spec, "payload-%s" % version)

    def verify(self, payload, versions):
        keys = os.path.join(ostree_boot.COMMON, "ostree-update")
        done = subprocess.run(
            ["podman", "run", "--rm", "-v", "%s:/payload:ro" % payload,
             "-v", "%s:/keys:ro" % keys, "docker.io/library/debian:trixie",
             "sh", "-c", CHECK, "check"] + versions,
            capture_output=True, text=True)
        with open(os.path.join(self.outputdir, "verify-%s.log" % "-".join(versions)), "w") as f:
            f.write(done.stdout + done.stderr)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def test_a_payload_verifies_and_a_later_version_adds_to_it(self):
        payload = os.path.join(self.workdir, "payload")
        self.build("23", payload)
        out = self.verify(payload, ["23"])
        self.assertRegex(out, r"-04755\s+0\s+0\s.*seine-suid")
        self.assertRegex(out, r"-00640\s+0\s+42\s.*seine-gfile")
        self.assertEqual(sorted(os.listdir(os.path.join(payload, "uki"))),
                         ["SHA256SUMS", "SHA256SUMS.gpg", "debian-23.efi"])

        self.build("24", payload)
        out = self.verify(payload, ["23", "24"])
        self.assertIn("debian/amd64.v23", out)
        self.assertIn("debian/amd64.v24", out)
        self.assertEqual(sorted(os.listdir(os.path.join(payload, "uki"))),
                         ["SHA256SUMS", "SHA256SUMS.gpg", "debian-23.efi", "debian-24.efi"])
