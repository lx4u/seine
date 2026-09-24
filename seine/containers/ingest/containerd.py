# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import datetime
from typing import List
from seine.containers.ingest.base import ContainerIngestionHandler


class ContainerdIngestionHandler(ContainerIngestionHandler):
    """Ingests container archives into containerd (/var/lib/containerd or custom root)."""

    def __init__(self, root: str = "/var/lib/containerd", namespace: str = "default"):
        self.root = root
        self.namespace = namespace

    def generate_normalize_script(self, source_date_epoch: int) -> str:
        return f"""\
for d in tmp io.containerd.runtime.v1.linux io.containerd.runtime.v2.task \\
         tmpmounts io.containerd.snapshotter.v1.btrfs io.containerd.snapshotter.v1.native \\
         io.containerd.grpc.v1.cri; do
  rm -rf "/sysroot{self.root}/$d"
done
rm -rf "/sysroot{self.root}/io.containerd.content.v1.content/ingest"

if [ ! -x /usr/bin/bbolt-normalize ]; then
  echo "ERROR: /usr/bin/bbolt-normalize binary not found in appliance!" >&2
  exit 1
fi
find "/sysroot{self.root}" -name "*.db" ! -name "metadata.db" -exec /usr/bin/bbolt-normalize {{}} "{source_date_epoch}" \\;
find "/sysroot{self.root}" -name "metadata.db" -exec /usr/bin/bbolt-normalize --zero-keys=size,inodes {{}} "{source_date_epoch}" \\;

find "/sysroot{self.root}" -exec touch -h -d "@{source_date_epoch}" {{}} +
"""

    def generate_start_script(self, source_date_epoch: int) -> str:
        when = datetime.datetime.fromtimestamp(
            source_date_epoch, datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        return f"""\
set -e
date -s "@{source_date_epoch}" 2>/dev/null || true
mkdir -p /sys/fs/cgroup /sysroot{self.root}
mount -t cgroup2 cgroup2 /sys/fs/cgroup 2>/dev/null || true
mount -t tmpfs tmpfs /run 2>/dev/null || true
mkdir -p /run/containerd /run/containers
modprobe overlay 2>/dev/null || true
modprobe 9pnet_virtio 2>/dev/null || true
modprobe 9p 2>/dev/null || true
if ! mountpoint -q /run/containers 2>/dev/null; then
  mount -t 9p -o trans=virtio,ro,version=9p2000.L seine_containers /run/containers 2>/dev/null || \\
  mount -t 9p -o trans=virtio,ro seine_containers /run/containers 2>/dev/null || true
fi

FAKETIME_LIB=$(find /usr/lib -name "libfaketime.so*" 2>/dev/null | head -n 1)

LD_PRELOAD="$FAKETIME_LIB" FAKETIME="@{when}" /usr/bin/containerd \\
  --root=/sysroot{self.root} \\
  --state=/run/containerd \\
  --address=/run/containerd/containerd.sock >/tmp/containerd.log 2>&1 &
CD_PID=$!
echo $CD_PID > /run/containerd.pid

READY=0
for i in $(seq 1 50); do
  if [ -S /run/containerd/containerd.sock ]; then
    READY=1
    break
  fi
  sleep 0.1
done
if [ "$READY" -ne 1 ]; then
  cat /tmp/containerd.log || true
  exit 1
fi
"""

    def generate_import_script(self, dev: str, source_date_epoch: int) -> str:
        when = datetime.datetime.fromtimestamp(
            source_date_epoch, datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        return f"""\
set -e
FAKETIME_LIB=$(find /usr/lib -name "libfaketime.so*" 2>/dev/null | head -n 1)
LD_PRELOAD="$FAKETIME_LIB" FAKETIME="@{when}" \\
/usr/bin/ctr -a /run/containerd/containerd.sock -n "{self.namespace}" images import \\
  --all-platforms "{dev}"
"""

    def generate_stop_script(self, source_date_epoch: int) -> str:
        normalize_sh = self.generate_normalize_script(source_date_epoch)
        return f"""\
set -e
CD_PID=$(cat /run/containerd.pid 2>/dev/null || true)
if [ -n "$CD_PID" ] && kill -0 "$CD_PID" 2>/dev/null; then
  kill -TERM "$CD_PID" 2>/dev/null || true
  for i in $(seq 1 100); do
    kill -0 "$CD_PID" 2>/dev/null || break
    sleep 0.1
  done
  kill -9 "$CD_PID" 2>/dev/null || true
fi

{normalize_sh}
"""
