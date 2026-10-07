# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import subprocess
import time

from seine.utils import HOST_ARCH

OVMF = "/usr/share/OVMF"
AAVMF = "/usr/share/AAVMF"

# Why a boot test cannot run here, or None.
def cannot_boot(arch="amd64"):
    if arch == "arm64":
        if shutil.which("qemu-system-aarch64") is None:
            return "qemu-system-aarch64 is needed to boot an arm64 image"
        for name in ("AAVMF_CODE.fd", "AAVMF_VARS.fd"):
            if not os.path.isfile(os.path.join(AAVMF, name)):
                return f"AAVMF firmware is missing ({name})"
        return None
    if shutil.which("qemu-system-x86_64") is None:
        return "qemu-system-x86_64 is needed to boot an image"
    if not os.access("/dev/kvm", os.W_OK):
        return "/dev/kvm is needed to boot an image in reasonable time"
    for name in ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"):
        if not os.path.isfile(os.path.join(OVMF, name)):
            return f"OVMF firmware is missing ({name})"
    return None

# Boots 'disk' (never written: -snapshot) under UEFI with its serial
# console in 'workdir'/serial.log, until 'pattern' shows there or
# 'timeout' seconds pass. Returns (found, log text).
def boot_until(disk, workdir, pattern, timeout=240, watchdog=None, arch="amd64"):
    log = os.path.join(workdir, "serial.log")
    if arch == "arm64":
        variables = os.path.join(workdir, "AAVMF_VARS.fd")
        shutil.copy(os.path.join(AAVMF, "AAVMF_VARS.fd"), variables)
        cmd = [
            "qemu-system-aarch64", "-machine", "virt",
            "-m", "2048", "-smp", "2", "-display", "none", "-serial", f"file:{log}",
            "-drive", f"if=pflash,format=raw,readonly=on,file={os.path.join(AAVMF, 'AAVMF_CODE.fd')}",
            "-drive", f"if=pflash,format=raw,file={variables}",
            "-drive", f"file={disk},format=raw,if=virtio,snapshot=on",
        ]
        if HOST_ARCH == "arm64" and os.access("/dev/kvm", os.W_OK):
            cmd += ["-enable-kvm", "-cpu", "host"]
        else:
            cmd += ["-cpu", "max"]
    else:
        variables = os.path.join(workdir, "OVMF_VARS.fd")
        shutil.copy(os.path.join(OVMF, "OVMF_VARS_4M.fd"), variables)
        cmd = [
            "qemu-system-x86_64", "-machine", "q35", "-enable-kvm", "-cpu", "host",
            "-m", "2048", "-smp", "2", "-display", "none", "-serial", f"file:{log}",
            "-drive", f"if=pflash,format=raw,readonly=on,file={os.path.join(OVMF, 'OVMF_CODE_4M.fd')}",
            "-drive", f"if=pflash,format=raw,file={variables}",
            "-drive", f"file={disk},format=raw,if=virtio,snapshot=on",
        ]
    if watchdog:
        device = "i6300esb" if watchdog is True else watchdog
        cmd += ["-device", device, "-watchdog-action", "reset"]
    # -snapshot keeps its temporary files in TMPDIR.
    environment = dict(os.environ, TMPDIR=workdir)
    qemu = subprocess.Popen(cmd, env=environment,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
