# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import platform
import shutil
from typing import Any

from seine.distributed.common.models import WorkerCapabilities


ARCH_MAP = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "armv7l": "armhf",
    "riscv64": "riscv64",
}


def detect_native_arch() -> str:
    machine = platform.machine()
    return ARCH_MAP.get(machine, machine)


def detect_capabilities(work_dir: str = ".") -> WorkerCapabilities:
    """Probe native arch, binfmt_misc emulators, cross-compilers, and disk."""
    native = detect_native_arch()
    scores: dict[str, float] = {native: 1.0}

    # Inspect binfmt_misc for foreign emulators
    binfmt_dir = "/proc/sys/fs/binfmt_misc"
    if os.path.isdir(binfmt_dir):
        try:
            for entry in os.listdir(binfmt_dir):
                low = entry.lower()
                if "aarch64" in low or "arm64" in low:
                    scores.setdefault("arm64", 0.3)
                elif "x86_64" in low or "amd64" in low:
                    scores.setdefault("amd64", 0.3)
                elif "riscv64" in low:
                    scores.setdefault("riscv64", 0.3)
                elif "arm" in low:
                    scores.setdefault("armhf", 0.3)
        except Exception:
            pass

    # Inspect cross-compilers
    if native != "arm64" and shutil.which("aarch64-linux-gnu-gcc"):
        scores["arm64"] = max(scores.get("arm64", 0.0), 0.7)
    if native != "amd64" and shutil.which("x86_64-linux-gnu-gcc"):
        scores["amd64"] = max(scores.get("amd64", 0.0), 0.7)

    # Check tools
    tools = {
        "podman": shutil.which("podman") is not None,
        "kvm": os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK),
    }

    # Measure free disk in work_dir
    os.makedirs(work_dir, exist_ok=True)
    usage = shutil.disk_usage(work_dir)
    free_gb = round(usage.free / (1024 ** 3), 2)

    return WorkerCapabilities(
        native_arch=native,
        arch_scores=scores,
        concurrency_slots=1,
        free_disk_gb=free_gb,
        tools=tools,
    )
