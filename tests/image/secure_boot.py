# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import fcntl
import glob
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

import ostree_boot
import qemu_boot
import qemu_guest
from seine.imager import ostree
from seine.utils import HOST_ARCH
from tests.testutils import prune_on_pass

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# The owner of every key and signature list made here.
OWNER = "6c4a7c2e-3f0f-4f0a-9b1f-5d2d5b6f4a11"

def run(argv):
    subprocess.run(argv, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)

# Throw-away PK, KEK and db: PK signs the KEK list and KEK signs the db
# list, as firmware expects, and db signs the boot files.
class Keys:
    SIGNED_BY = {"PK": "PK", "KEK": "PK", "db": "KEK"}

    def __init__(self, directory):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        for name, signer in self.SIGNED_BY.items():
            run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", self.path(name, "key"), "-out", self.path(name, "crt"),
                 "-subj", "/CN=seine-test-%s/" % name, "-days", "2"])
        for name, signer in self.SIGNED_BY.items():
            run(["cert-to-efi-sig-list", "-g", OWNER,
                 self.path(name, "crt"), self.path(name, "esl")])
            run(["sign-efi-sig-list", "-g", OWNER, "-k", self.path(signer, "key"),
                 "-c", self.path(signer, "crt"), name, self.path(name, "esl"),
                 self.path(name, "auth")])

    def path(self, name, suffix):
        return os.path.join(self.directory, "%s.%s" % (name, suffix))

    # Signs 'source' with db into 'target'.
    def sign(self, source, target):
        run(["sbsign", "--key", self.path("db", "key"),
             "--cert", self.path("db", "crt"), "--output", target, source])

    # A dbx update that revokes 'image' (a PE file), signed by the KEK.
    def revocation(self, image, target):
        esl = target + ".esl"
        run(["hash-to-efi-sig-list", image, esl])
        run(["sign-efi-sig-list", "-a", "-g", OWNER, "-k", self.path("KEK", "key"),
             "-c", self.path("KEK", "crt"), "dbx", esl, target])

# The tools a Secure Boot test needs on the host, or why not.
def missing_tool():
    for tool in ("openssl", "sbsign", "ukify", "qemu-img", "cert-to-efi-sig-list",
                 "sign-efi-sig-list", "hash-to-efi-sig-list"):
        if shutil.which(tool) is None:
            return "%s is needed" % tool
    if not os.path.isfile(os.path.join(qemu_guest.OVMF, "OVMF_CODE_4M.secboot.fd")):
        return "the Secure Boot firmware OVMF_CODE_4M.secboot.fd is missing"
    return None

# A UKI of 'kernel' and 'initrd' with its own 'cmdline', and an os-release
# of its own: sd-boot sorts by IMAGE_VERSION. Signed with db if 'keys'. Each
# of 'profiles' ({id: command line}) is a profile of the file, which boots
# with that command line when picked as '<file>@<id>'.
def build_uki(target, kernel, initrd, cmdline, version, keys=None, profiles=None):
    osrel = target + ".osrel"
    with open(osrel, "w") as f:
        f.write("ID=debian\nPRETTY_NAME=seine test\nIMAGE_VERSION=%s\n" % version)
    unsigned = target + ".unsigned"
    argv = ["ukify", "build", "--linux", kernel, "--initrd", initrd,
            "--cmdline", cmdline, "--os-release", "@%s" % osrel, "--output", unsigned]
    if profiles:
        # The base is the first profile: it needs an ID of its own.
        argv += ["--profile", "ID=main\nTITLE=main"]
    for name, line in (profiles or {}).items():
        profile = "%s.%s" % (target, name)
        run(["ukify", "build", "--profile", "ID=%s\nTITLE=%s" % (name, name),
             "--cmdline", line, "--output", profile])
        argv += ["--join-profile", profile]
    run(argv)
    if keys:
        keys.sign(unsigned, target)
    else:
        shutil.move(unsigned, target)

# A boot test image has an ESP, a sysroot and /var, systemd-boot, a UKI
# and a root shell on the serial console, and tools to enrol keys.
IMAGE = ostree_boot.GROUP_SYSTEMD_BOOT + """    - name: tools for the guest
      priority: 850
      tasks:
          - name: install efitools
            apt:
                name: [efitools]
                state: present
          # The boot check asks systemd over the bus, which init only recommends.
          - name: install dbus
            apt:
                name: [dbus]
                state: present
    - name: a root shell on the serial console
      priority: 960
      tasks:
          - name: make the drop-in directory
            file:
                path: /etc/systemd/system/serial-getty@ttyS0.service.d
                state: directory
          - name: log in as root
            copy:
                content: "[Service]\\nExecStart=\\nExecStart=-/sbin/agetty --autologin root --noclear %I $TERM\\n"
                dest: /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
""" + ostree_boot.UKI + ostree_boot.SINGLE_DISK

# The image is the same for every test: built once, then kept in the
# user's cache (as for the dev vault image) until the sources change.
def cached_image(test):
    imager = sorted(glob.glob(os.path.join(path_to_sources, "seine", "imager", "*.py")))
    digest = hashlib.sha256(IMAGE.encode())
    for path in imager:
        digest.update(open(path, "rb").read())
    where = os.path.join(os.environ.get("XDG_CACHE_HOME")
                         or os.path.join(os.path.expanduser("~"), ".cache"),
                         "seine-tests")
    os.makedirs(where, exist_ok=True)
    prefix = os.path.join(where, "secure-boot-")
    image = "%s%s.img" % (prefix, digest.hexdigest()[:16])
    # One builder at a time, the others wait for the image.
    with open(prefix + "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.isfile(image):
            return image
        partial = "%s.%d.partial" % (image, os.getpid())
        spec = os.path.join(test.workdir, "image.yaml")
        with open(spec, "w") as f:
            f.write(IMAGE.format(
                common=ostree_boot.COMMON,
                pc_image=os.path.join(ostree_boot.EXAMPLES, "pc-image"),
                name="secure-boot-os", disk=partial))
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        environment["SEINE_BUILD_DIR"] = os.path.join(test.workdir, "build")
        log = os.path.join(test.outputdir, "image.log")
        try:
            with open(log, "w") as f:
                built = subprocess.run(
                    [sys.executable, "-u", "./seine.py", "build", "-v", spec],
                    cwd=path_to_sources, env=environment, stdout=f,
                    stderr=subprocess.STDOUT)
            if built.returncode != 0:
                raise RuntimeError("the image build failed, see %s" % log)
            os.replace(partial, image)
            for old in glob.glob(prefix + "*.img"):
                if old != image:
                    os.remove(old)
        finally:
            subprocess.run(["podman", "unshare", "rm", "-rf",
                            os.path.join(test.workdir, "build")], check=False)
            # The build writes its rootfs, digests and reports beside the image.
            for leftover in glob.glob("%s.%d.*" % (image, os.getpid())):
                if os.path.isdir(leftover):
                    shutil.rmtree(leftover)
                else:
                    os.remove(leftover)
    return image

# Shared scaffolding for the Secure Boot tests: a guest on a copy-on-write
# overlay of the cached image, with the firmware in Setup Mode and then
# the throw-away keys enrolled. Not an avocado.Test itself, so avocado
# does not try to run it on its own.
class SecureBootGuest:
    def setUp(self):
        self.guest = None
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel and boot loader are amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        reason = qemu_boot.cannot_boot() or missing_tool()
        if reason:
            self.cancel(reason)
        self.keys = Keys(os.path.join(self.workdir, "keys"))
        self.share = os.path.join(self.workdir, "share")
        os.makedirs(self.share)
        self.disk = os.path.join(self.workdir, "disk.qcow2")
        self.variables = os.path.join(self.workdir, "OVMF_VARS.fd")
        shutil.copy(os.path.join(qemu_guest.OVMF, "OVMF_VARS_4M.fd"), self.variables)
        qemu_guest.overlay(cached_image(self), self.disk)

    def tearDown(self):
        if self.guest:
            self.guest.close()
        prune_on_pass(self)

    # Starts the guest and logs in. The serial log of each start is kept.
    def start(self):
        self.starts = getattr(self, "starts", 0) + 1
        log = os.path.join(self.outputdir, "serial-%d.log" % self.starts)
        self.guest = qemu_guest.Guest(
            self.disk, self.variables, log, secure_boot=True, share=self.share)
        self.guest.login()

    def stop(self):
        self.guest.close()
        self.guest = None

    # Runs a command that must succeed. Returns its output.
    def sh(self, command, timeout=60):
        status, output = self.guest.run(command, timeout)
        self.assertEqual(status, 0, "'%s' failed: %s" % (command, output))
        return output

    # Waits for a command to succeed, as a service can take a while.
    def wait_until(self, command, timeout=90):
        deadline = time.time() + timeout
        while self.guest.run(command)[0] != 0:
            if time.time() > deadline:
                self.fail("'%s' still fails after %d s: %s" % (
                    command, timeout, self.guest.run("ls -l /efi/EFI/Linux")[1]))
            time.sleep(2)

    # Signs the boot files on the ESP (a guestfs session on the disk, which
    # the guest must not have open), then enrols the keys from the guest,
    # which is in Setup Mode, and reboots with Secure Boot on.
    def enable_secure_boot(self):
        self.edit_esp(self.sign_boot_files)
        for name in ("db", "KEK", "PK"):
            shutil.copy(self.keys.path(name, "auth"), self.share)
        self.start()
        self.assertIn("Secure Boot: disabled (setup)", self.sh("bootctl status"))
        for name in ("db", "KEK", "PK"):
            self.sh("efi-updatevar -f %s/%s.auth %s" % (qemu_guest.SHARE, name, name))
        self.guest.reboot()
        self.assertIn("Secure Boot: enabled (user)", self.sh("bootctl status"))

    # Calls 'edit' with a guestfs handle that has the ESP mounted.
    def edit_esp(self, edit):
        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(self.disk, format="qcow2")
        g.launch()
        try:
            g.mount("/dev/sda1", "/")
            edit(g)
        finally:
            g.close()

    # Signs each EFI file of the ESP with db, in place.
    def sign_boot_files(self, g):
        for path in ("/EFI/BOOT/BOOTX64.EFI", "/EFI/systemd/systemd-bootx64.efi",
                     "/EFI/Linux/debian-os.efi"):
            self.replace_signed(g, path)

    def replace_signed(self, g, path):
        with tempfile.TemporaryDirectory(dir=self.workdir) as scratch:
            source = os.path.join(scratch, "in.efi")
            target = os.path.join(scratch, "out.efi")
            g.download(path, source)
            self.keys.sign(source, target)
            g.upload(target, path)

    # The kernel and initramfs of the running deployment, and the command
    # line of its UKI: what the UKIs of later commits are made from.
    def fetch_boot_files(self):
        for name in ("vmlinuz", "initramfs.img"):
            self.sh("cp /usr/lib/modules/*/%s %s/%s" % (name, qemu_guest.SHARE, name))
        self.kernel = os.path.join(self.share, "vmlinuz")
        self.initrd = os.path.join(self.share, "initramfs.img")
        self.cmdline = self.sh("cat /proc/cmdline")

    # The commit a UKI boots, from the command line of the running system.
    def booted_commit(self):
        return self.sh("cat /proc/cmdline").split("ostree=/ostree/debian-")[1].split()[0]

    # A new commit of the branch with a file that tells the versions apart.
    def commit_update(self, version):
        overlay = "/var/seine-update/usr/share/seine-test"
        self.sh("mkdir -p %s && echo %s > %s/version" % (overlay, version, overlay))
        return self.sh("ostree commit --repo=/sysroot/ostree/repo -b debian/amd64 "
                       "--tree=ref=debian/amd64 --tree=dir=/var/seine-update")

    # Makes the deployment of 'commit' and the link its UKI names.
    def deploy_update(self, commit):
        self.sh("ostree admin deploy debian/amd64")
        self.sh("ln -s deploy/debian/deploy/%s.0 /sysroot/ostree/debian-%s"
                % (commit, commit))

    # The command line of a UKI of 'commit': the factory one, with the
    # deployment and a host name that tells the UKIs apart.
    def uki_cmdline(self, commit, marker, extra=""):
        options = ostree.with_ostree_root(self.cmdline, "/ostree/debian-%s" % commit)
        words = [w for w in options.split() if not w.startswith("systemd.hostname=")]
        return " ".join(words + ["systemd.hostname=%s" % marker] + extra.split())

    # Builds a UKI of 'commit' on the host, signed with db, and puts it on the
    # ESP as 'name' (a lowercase name without .efi), counted if 'tries'. The
    # file is renamed last, so that sd-boot never sees a partial one. The host
    # name is 'marker', and 'profiles' are the markers of its profiles.
    def install_uki(self, name, commit, marker, version, tries=None, extra="",
                    profiles=()):
        build_uki(os.path.join(self.share, name + ".efi"), self.kernel, self.initrd,
                  self.uki_cmdline(commit, marker, extra), version, self.keys,
                  {p: self.uki_cmdline(commit, p, extra) for p in profiles})
        final = "%s%s.efi" % (name, "+%d" % tries if tries else "")
        esp = "/efi/EFI/Linux"
        self.sh("cp %s/%s.efi %s/%s.tmp && mv %s/%s.tmp %s/%s && sync" % (
            qemu_guest.SHARE, name, esp, name, esp, name, esp, final))
