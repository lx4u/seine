#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
#
# Builds helper binaries (such as bbolt-normalize) for debian packaging
# using the pinned golang container image, ensuring full build-reproducibility
# and avoiding any dependency on host Go tooling.

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from seine.container import ContainerEngine
from seine.utils import HOST_ARCH

GOLANG_IMAGE = "docker.io/library/golang:1.26.6"

HOSTARCH = os.environ.get("HOSTARCH", HOST_ARCH)
REFRESH = os.environ.get("REFRESH") == "1"

LOCK_FILE = os.path.join(HERE, "debian", "build-vault-image.lock")

GO_ARCH_MAP = {
    "amd64": ("amd64", ""),
    "arm64": ("arm64", ""),
    "armhf": ("arm", "7"),
    "i386": ("386", ""),
}


def load_lock():
    lock = {}
    if not os.path.exists(LOCK_FILE):
        return lock
    with open(LOCK_FILE) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            image, arch, digest = line.split()
            lock[(image, arch)] = digest
    return lock


def manifest_digest(image, arch):
    out = ContainerEngine.check_output(["manifest", "inspect", image])
    manifest = json.loads(out)
    for entry in manifest.get("manifests", []):
        platform = entry.get("platform") or {}
        if platform.get("os") == "linux" and platform.get("architecture") == arch:
            return entry["digest"]
    raise RuntimeError("no linux/%s manifest for %s" % (arch, image))


def pin_base_image(image, lock):
    if REFRESH:
        lock[(image, HOSTARCH)] = manifest_digest(image, HOSTARCH)

    digest = lock.get((image, HOSTARCH))
    if digest is None:
        raise RuntimeError(
            "no pinned digest for %s/%s in %s" % (image, HOSTARCH, LOCK_FILE))
    ref = "%s@%s" % (image.rsplit(":", 1)[0], digest)
    ContainerEngine.run(["pull", ref], check=True)
    ContainerEngine.run(["tag", ref, image], check=True)


def main():
    if len(sys.argv) < 2:
        print("usage: build-tools.py <out-binary-path>", file=sys.stderr)
        sys.exit(1)

    out_binary = os.path.abspath(sys.argv[1])
    out_dir = os.path.dirname(out_binary)
    os.makedirs(out_dir, exist_ok=True)

    lock = load_lock()
    pin_base_image(GOLANG_IMAGE, lock)

    tools_src = os.path.join(HERE, "tools", "bbolt-normalize")
    go_arch, go_arm = GO_ARCH_MAP.get(HOSTARCH, (HOSTARCH, ""))

    env_args = [
        "-e", "CGO_ENABLED=0",
        "-e", "GOOS=linux",
        "-e", f"GOARCH={go_arch}",
        "-e", "GOTOOLCHAIN=local",
    ]
    if go_arm:
        env_args += ["-e", f"GOARM={go_arm}"]

    ContainerEngine.run([
        "run", "--rm",
        "-v", f"{tools_src}:/src:ro",
        "-v", f"{out_dir}:/out",
        "-w", "/src",
        *env_args,
        GOLANG_IMAGE,
        "go", "build", "-trimpath", "-ldflags=-s -w -buildid=", "-o", f"/out/{os.path.basename(out_binary)}", "main.go",
    ], check=True)


if __name__ == "__main__":
    main()
