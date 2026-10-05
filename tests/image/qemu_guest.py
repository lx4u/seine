# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import re
import subprocess
import threading
import time

END = "@@END@@"

# Terminal escape sequences, and the start of one that has not ended yet.
ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PARTIAL = re.compile(r"\x1b(\[[0-9;?]*)?$")

# Drives a program through its standard input and output, which is a
# shell on a serial console when the program is QEMU with '-serial stdio'.
# Everything the program prints is also appended to 'log'.
class Console:
    def __init__(self, argv, log, **kwargs):
        self.log = open(log, "ab")
        self.text = ""
        self.cursor = 0
        self.changed = threading.Condition()
        self.process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, **kwargs)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        pending = ""
        for chunk in iter(lambda: self.process.stdout.read1(4096), b""):
            self.log.write(chunk)
            self.log.flush()
            pending = ESCAPE.sub("", pending + chunk.decode(errors="replace"))
            held = PARTIAL.search(pending)
            ready, pending = (pending[:held.start()], pending[held.start():]) \
                if held else (pending, "")
            with self.changed:
                self.text += ready.replace("\r", "")
                self.changed.notify_all()

    def send(self, text):
        self.process.stdin.write(text.encode())
        self.process.stdin.flush()

    # Waits for 'pattern' (a regular expression) after the last match.
    # Returns the match and drops what came before it.
    def expect(self, pattern, timeout=60):
        deadline = time.time() + timeout
        with self.changed:
            while True:
                found = re.compile(pattern).search(self.text, self.cursor)
                if found:
                    self.cursor = found.end()
                    return found
                left = deadline - time.time()
                if left <= 0 or self.process.poll() is not None:
                    raise TimeoutError(
                        "'%s' not seen, the output ends with: %r" % (
                            pattern, self.text[-300:]))
                self.changed.wait(min(left, 1))

    # Runs a shell command. Returns its exit status and output.
    def run(self, command, timeout=60):
        start = self.cursor
        self.send("%s\necho %s$?\n" % (command, END))
        status = int(self.expect(r"%s(\d+)\n" % END, timeout).group(1))
        output = self.text[start:self.cursor]
        # What came before the marker, without the marker line itself.
        return status, output[:output.rindex(END)].strip()

    # Pokes the shell until it answers, as it can be busy booting. With
    # a 'prompt' (a regular expression) it first sends only Enter until the
    # prompt shows: a boot menu takes other keys as commands.
    def wait_shell(self, timeout=120, prompt=None):
        deadline = time.time() + timeout
        while prompt:
            self.send("\n")
            try:
                self.expect(prompt, 3)
                break
            except TimeoutError:
                if time.time() > deadline or self.process.poll() is not None:
                    raise
        while True:
            self.send('echo REA""DY\n')
            try:
                self.expect(r"(?m)^READY$", 3)
                break
            except TimeoutError:
                if time.time() > deadline or self.process.poll() is not None:
                    raise
        self.cursor = len(self.text)

    def close(self):
        self.process.kill()
        self.process.wait()
        self.log.close()

OVMF = "/usr/share/OVMF"

# A qcow2 file on top of the raw 'base', which is never written, so a
# guest can write and reboot.
def overlay(base, path):
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-b", base, "-F", "raw", path],
        check=True, stdout=subprocess.DEVNULL)

# The QEMU command line: serial console on stdio, 'disk' (qcow2) and the
# UEFI variables file 'variables'. The Secure Boot firmware needs SMM and a
# locked flash. 'share' is a host directory the guest sees as 9p 'host'.
def qemu_argv(disk, variables, secure_boot=False, share=None):
    code = "OVMF_CODE_4M.secboot.fd" if secure_boot else "OVMF_CODE_4M.fd"
    argv = [
        "qemu-system-x86_64", "-machine",
        "q35,smm=on" if secure_boot else "q35",
        "-enable-kvm", "-cpu", "host", "-m", "2048", "-smp", "2",
        "-display", "none", "-monitor", "none", "-serial", "stdio",
        "-drive", "if=pflash,format=raw,readonly=on,file=%s" % os.path.join(OVMF, code),
        "-drive", "if=pflash,format=raw,file=%s" % variables,
        "-drive", "file=%s,format=qcow2,if=virtio" % disk,
    ]
    if secure_boot:
        argv += ["-global", "driver=cfi.pflash01,property=secure,value=on"]
    if share:
        argv += ["-virtfs", "local,path=%s,mount_tag=host,security_model=none,id=host" % share]
    return argv

# What the root shell of the guest prints before it is silenced.
PROMPT = r"root@\S+:\S*# "

# Where the guest mounts 'share'. It must be under /var: /host cannot be
# created on an ostree root.
SHARE = "/var/host"

# A QEMU guest with a root shell on its serial console.
class Guest(Console):
    def __init__(self, disk, variables, log, secure_boot=False, share=None):
        self.share = share
        super().__init__(qemu_argv(disk, variables, secure_boot, share), log)

    # Waits for the shell and silences it, so that long lines are not echoed
    # back garbled.
    def login(self, timeout=240):
        self.wait_shell(timeout, PROMPT)
        self.send("export SYSTEMD_PAGER=cat; stty -echo cols 300; bind 'set enable-bracketed-paste off'; PS1=''\n")
        self.wait_shell(30)
        if self.share:
            self.run("modprobe 9pnet_virtio 9p; mkdir -p %s; mount -t 9p host %s"
                     % (SHARE, SHARE))

    # With 'login' false, returns as soon as the guest restarts.
    def reboot(self, timeout=240, login=True):
        self.send("systemctl reboot\n")
        # A stop job (the serial getty) may run into systemd's 90 s timeout.
        self.expect(r"reboot: Restarting system", 150)
        if login:
            self.login(timeout)
