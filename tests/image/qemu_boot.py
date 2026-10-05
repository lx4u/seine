# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import subprocess
import time

OVMF = "/usr/share/OVMF"

# Why a boot test cannot run here, or None.
def cannot_boot():
    if shutil.which("qemu-system-x86_64") is None:
        return "qemu-system-x86_64 is needed to boot an image"
    if not os.access("/dev/kvm", os.W_OK):
        return "/dev/kvm is needed to boot an image in reasonable time"
    for name in ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"):
        if not os.path.isfile(os.path.join(OVMF, name)):
            return "OVMF firmware is missing (%s)" % name
    return None

# Boots 'disk' (never written: -snapshot) under OVMF with its serial
# console in 'workdir'/serial.log, until 'pattern' shows there or
# 'timeout' seconds pass. Returns (found, log text).
def boot_until(disk, workdir, pattern, timeout=240):
    log = os.path.join(workdir, "serial.log")
    variables = os.path.join(workdir, "OVMF_VARS.fd")
    shutil.copy(os.path.join(OVMF, "OVMF_VARS_4M.fd"), variables)
    # -snapshot keeps its temporary files in TMPDIR.
    environment = dict(os.environ, TMPDIR=workdir)
    qemu = subprocess.Popen([
        "qemu-system-x86_64", "-machine", "q35", "-enable-kvm", "-cpu", "host",
        "-m", "2048", "-smp", "2", "-display", "none", "-serial", "file:%s" % log,
        "-drive", "if=pflash,format=raw,readonly=on,file=%s" % os.path.join(OVMF, "OVMF_CODE_4M.fd"),
        "-drive", "if=pflash,format=raw,file=%s" % variables,
        "-drive", "file=%s,format=raw,if=virtio,snapshot=on" % disk,
    ], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    found = False
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and qemu.poll() is None:
            time.sleep(1)
            if os.path.exists(log):
                with open(log, errors="replace") as f:
                    if pattern in f.read():
                        found = True
                        break
    finally:
        qemu.kill()
        qemu.wait()
    text = ""
    if os.path.exists(log):
        with open(log, errors="replace") as f:
            text = f.read()
    return found, text
