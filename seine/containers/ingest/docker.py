# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from typing import List
from seine.containers.ingest.base import ContainerIngestionHandler

DOCKER_NORMALIZE_PYTHON = """\
import os
import shutil
import hashlib

ROOT = "/sysroot{root}"

for d in ["volumes", "network", "containerd", "engine-id", "runc", "tmp",
          "buildkit", "builder", "containers", "plugins", "swarm", "trust"]:
    p = os.path.join(ROOT, d)
    if os.path.islink(p) or os.path.isfile(p):
        os.remove(p)
    elif os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)

layerdb = os.path.join(ROOT, "image", "overlay2", "layerdb", "sha256")
overlay2 = os.path.join(ROOT, "overlay2")
l_dir = os.path.join(overlay2, "l")

if os.path.isdir(layerdb) and os.path.isdir(overlay2):
    chain_to_old_cache = {{}}
    chain_to_new_cache = {{}}
    chain_to_new_link = {{}}
    old_link_to_new_link = {{}}

    for chain_id in sorted(os.listdir(layerdb)):
        chain_dir = os.path.join(layerdb, chain_id)
        cache_id_file = os.path.join(chain_dir, "cache-id")
        if not os.path.isfile(cache_id_file):
            continue
        with open(cache_id_file, "r") as f:
            old_cache = f.read().strip()

        new_cache = hashlib.sha256(("cache:" + chain_id).encode()).hexdigest()
        new_link = hashlib.sha256(("link:" + chain_id).encode()).hexdigest()[:26].upper()

        chain_to_old_cache[chain_id] = old_cache
        chain_to_new_cache[chain_id] = new_cache
        chain_to_new_link[chain_id] = new_link

        old_link_file = os.path.join(overlay2, old_cache, "link")
        if os.path.isfile(old_link_file):
            with open(old_link_file, "r") as f:
                old_link = f.read().strip()
            old_link_to_new_link[old_link] = new_link

    if os.path.isdir(l_dir):
        shutil.rmtree(l_dir, ignore_errors=True)
    os.makedirs(l_dir, exist_ok=True)

    for chain_id, old_cache in chain_to_old_cache.items():
        new_cache = chain_to_new_cache[chain_id]
        old_dir = os.path.join(overlay2, old_cache)
        tmp_dir = os.path.join(overlay2, "tmp_" + new_cache)
        if os.path.exists(old_dir):
            os.rename(old_dir, tmp_dir)

    for chain_id, new_cache in chain_to_new_cache.items():
        tmp_dir = os.path.join(overlay2, "tmp_" + new_cache)
        new_dir = os.path.join(overlay2, new_cache)
        if os.path.exists(tmp_dir):
            os.rename(tmp_dir, new_dir)

        cache_id_file = os.path.join(layerdb, chain_id, "cache-id")
        with open(cache_id_file, "w") as f:
            f.write(new_cache)

        new_link = chain_to_new_link[chain_id]
        link_file = os.path.join(new_dir, "link")
        with open(link_file, "w") as f:
            f.write(new_link)

        symlink_path = os.path.join(l_dir, new_link)
        target_path = os.path.join("..", new_cache, "diff")
        os.symlink(target_path, symlink_path)

        lower_file = os.path.join(new_dir, "lower")
        if os.path.isfile(lower_file):
            with open(lower_file, "r") as f:
                lower_content = f.read().strip()
            parts = lower_content.split(":")
            new_parts = []
            for p in parts:
                if p.startswith("l/"):
                    old_l = p[2:]
                    new_l = old_link_to_new_link.get(old_l, old_l)
                    new_parts.append("l/" + new_l)
                else:
                    new_parts.append(p)
            with open(lower_file, "w") as f:
                f.write(":".join(new_parts))
"""


class DockerIngestionHandler(ContainerIngestionHandler):
    """Ingests container archives into Docker engine (/var/lib/docker)."""

    def __init__(self, root: str = "/var/lib/docker"):
        self.root = root

    def generate_normalize_script(self, source_date_epoch: int) -> str:
        return DOCKER_NORMALIZE_PYTHON.format(root=self.root)

    def generate_start_script(self, source_date_epoch: int) -> str:
        return f"""\
set -e
mkdir -p /sys/fs/cgroup /sysroot{self.root}
mount -t cgroup2 cgroup2 /sys/fs/cgroup 2>/dev/null || true
mount -t tmpfs tmpfs /run 2>/dev/null || true
mkdir -p /run/containerd /run/docker /run/containers
modprobe overlay 2>/dev/null || true
modprobe 9pnet_virtio 2>/dev/null || true
modprobe 9p 2>/dev/null || true
if ! mountpoint -q /run/containers 2>/dev/null; then
  mount -t 9p -o trans=virtio,ro,version=9p2000.L seine_containers /run/containers 2>/dev/null || \\
  mount -t 9p -o trans=virtio,ro seine_containers /run/containers 2>/dev/null || true
fi

containerd --address /run/containerd/containerd.sock >/tmp/containerd.log 2>&1 &
CD_PID=$!
echo $CD_PID > /run/containerd.pid
for i in $(seq 1 30); do
  [ -S /run/containerd/containerd.sock ] && break
  sleep 0.1
done

/usr/sbin/dockerd \\
  --data-root=/sysroot{self.root} \\
  --exec-root=/run/docker \\
  --host=unix:///run/docker/docker.sock \\
  --iptables=false --bridge=none --ip-masq=false --userland-proxy=false >/tmp/dockerd.log 2>&1 &
D_PID=$!
echo $D_PID > /run/dockerd.pid

READY=0
for i in $(seq 1 50); do
  if docker -H unix:///run/docker/docker.sock info >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.1
done
if [ "$READY" -ne 1 ]; then
  cat /tmp/dockerd.log || true
  cat /tmp/containerd.log || true
  exit 1
fi
"""

    def generate_import_script(self, dev: str, source_date_epoch: int) -> str:
        return f"""\
set -e
docker -H unix:///run/docker/docker.sock load < "{dev}"
"""

    def generate_stop_script(self, source_date_epoch: int) -> str:
        normalize_py = self.generate_normalize_script(source_date_epoch)
        return f"""\
set -e
D_PID=$(cat /run/dockerd.pid 2>/dev/null || true)
CD_PID=$(cat /run/containerd.pid 2>/dev/null || true)

stop_pid() {{
  pid="$1"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    for i in $(seq 1 100); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -9 "$pid" 2>/dev/null || true
  fi
}}

stop_pid "$D_PID"
stop_pid "$CD_PID"

python3 - << 'PY_NORMALIZE'
{normalize_py}
PY_NORMALIZE

find /sysroot{self.root} -exec touch -h -d @{source_date_epoch} {{}} +
"""
