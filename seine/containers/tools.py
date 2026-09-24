# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess

GO_ARCH_MAP = {
    "amd64": ("amd64", ""),
    "arm64": ("arm64", ""),
    "armhf": ("arm", "7"),
    "i386": ("386", ""),
}


def ensure_bbolt_normalize(target_arch: str = "amd64") -> str:
    """Returns path to the compiled static bbolt-normalize binary for target_arch."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # 1. System installed location
    system_bin = "/usr/lib/seine/bbolt-normalize"
    if os.path.isfile(system_bin) and os.access(system_bin, os.X_OK):
        return system_bin

    # 2. Local dev tree build
    tools_src = os.path.join(repo_root, "tools", "bbolt-normalize")
    if not os.path.isdir(tools_src):
        return None

    cache_dir = os.environ.get("SEINE_CACHE_DIR") or os.path.join(repo_root, "build", "cache")
    out_dir = os.path.join(cache_dir, "tools", target_arch)
    os.makedirs(out_dir, exist_ok=True)
    out_bin = os.path.join(out_dir, "bbolt-normalize")

    rebuild = not os.path.isfile(out_bin)
    if not rebuild:
        bin_mtime = os.path.getmtime(out_bin)
        for root_d, _, files in os.walk(tools_src):
            for f in files:
                if f.endswith((".go", ".mod", ".sum")):
                    if os.path.getmtime(os.path.join(root_d, f)) > bin_mtime:
                        rebuild = True
                        break

    if rebuild:
        from seine.container import ContainerEngine

        go_arch, go_arm = GO_ARCH_MAP.get(target_arch, (target_arch, ""))
        env_args = [
            "-e", "CGO_ENABLED=0",
            "-e", "GOOS=linux",
            "-e", f"GOARCH={go_arch}",
            "-e", "GOTOOLCHAIN=local",
        ]
        if go_arm:
            env_args += ["-e", f"GOARM={go_arm}"]

        gocache = os.path.join(cache_dir, "go-build")
        gomodcache = os.path.join(cache_dir, "go-mod")
        os.makedirs(gocache, exist_ok=True)
        os.makedirs(gomodcache, exist_ok=True)

        ContainerEngine.run([
            "run", "--rm",
            "-v", f"{tools_src}:/src:ro",
            "-v", f"{out_dir}:/out",
            "-v", f"{gocache}:/root/.cache/go-build",
            "-v", f"{gomodcache}:/go/pkg/mod",
            "-w", "/src",
            *env_args,
            "docker.io/library/golang:1.26.6",
            "go", "build", "-trimpath", "-ldflags=-s -w -buildid=", "-o", "/out/bbolt-normalize", "main.go",
        ], check=True)

    return out_bin
