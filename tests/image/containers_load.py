#!/usr/bin/env python3

import avocado
import os
import re
import shutil
import subprocess
import sys
from unittest import mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.containers.ingest import (
    ContainerIngestionHandler,
    ContainerdIngestionHandler,
    DockerIngestionHandler,
)
from seine.imager import Imager
from seine.utils import HOST_ARCH
from tests.testutils import prune_on_pass

EXAMPLES_DOCKER = os.path.join(path_to_sources, "examples", "docker-image")
PLAN = os.environ.get("SEINE_TEST_PLAN", "")


class ContainersIngestUnitTests(avocado.Test):
    def _create_imager(self, epoch=1700000000):
        imager = Imager.__new__(Imager)
        imager.source = mock.Mock()
        imager.source._epoch = mock.Mock(return_value=epoch)
        imager.source.options = {"files": [], "keep": False, "verbose": False}
        imager.verbose = False
        imager.reproducible = False
        imager._output_dir = self.workdir
        return imager

    def test_ingest_containers_script_generation(self):
        imager = self._create_imager(epoch=1712345678)
        mock_g = mock.Mock()

        imager._ingest_containers(mock_g, ["/dev/sdc", "/dev/sdd"])

        self.assertEqual(mock_g.debug.call_count, 4)
        script = "\n".join(call[0][1][0] for call in mock_g.debug.call_args_list)

        # Verify containment and mountpoints
        self.assertIn("/sys/fs/cgroup", script)
        self.assertIn("mount -t cgroup2 cgroup2 /sys/fs/cgroup", script)
        self.assertIn("mount -t tmpfs tmpfs /run", script)
        self.assertIn("mount -t 9p", script)
        self.assertIn("seine_containers", script)

        # Verify containerd and dockerd data-root
        self.assertIn("containerd --address /run/containerd/containerd.sock", script)
        self.assertIn("--data-root=/sysroot/var/lib/docker", script)
        self.assertIn("--exec-root=/run/docker", script)

        # Verify device ingestion
        self.assertIn("/dev/sdc", script)
        self.assertIn("/dev/sdd", script)
        self.assertIn("docker -H unix:///run/docker/docker.sock load", script)

        # Verify clean shutdown
        self.assertIn('kill -TERM "$pid"', script)

        # Verify deterministic normalization
        self.assertIn("PY_NORMALIZE", script)
        self.assertIn("volumes", script)
        self.assertIn("network", script)
        self.assertIn("containerd", script)
        self.assertIn("engine-id", script)
        self.assertIn("runc", script)
        self.assertIn("tmp", script)
        self.assertIn("layerdb", script)
        self.assertIn("overlay2", script)

        # Verify timestamp clamping to build epoch
        self.assertIn("find /sysroot/var/lib/docker -exec touch -h -d @1712345678 {} +", script)

    def test_ingest_containerd_script_generation(self):
        imager = self._create_imager(epoch=1712345678)
        mock_g = mock.Mock()

        devices = [
            ("/dev/sdc", "containerd", "/var/lib/rancher/k3s/agent/containerd", "k8s.io"),
            ("/dev/sdd", "containerd", "/var/lib/rancher/k3s/agent/containerd", "k8s.io"),
        ]
        imager._ingest_containers(mock_g, devices)

        self.assertEqual(mock_g.debug.call_count, 4)
        script = "\n".join(call[0][1][0] for call in mock_g.debug.call_args_list)

        # Verify containerd flags and socket
        self.assertIn("--root=/sysroot/var/lib/rancher/k3s/agent/containerd", script)
        self.assertIn("--state=/run/containerd", script)
        self.assertIn("--address=/run/containerd/containerd.sock", script)

        # Verify ctr import commands with namespace and all-platforms
        self.assertIn('ctr -a /run/containerd/containerd.sock -n "k8s.io" images import', script)
        self.assertIn("--all-platforms", script)
        self.assertIn("/dev/sdc", script)
        self.assertIn("/dev/sdd", script)

        # Verify normalization and timestamp clamping
        self.assertIn("bbolt-normalize", script)
        self.assertIn("io.containerd.content.v1.content", script)
        self.assertIn('find "/sysroot/var/lib/rancher/k3s/agent/containerd" -exec touch -h -d "@1712345678" {} +', script)

    def test_ingest_containers_mixed_dispatch(self):
        imager = self._create_imager(epoch=1712345678)
        mock_g = mock.Mock()

        devices = [
            ("/dev/sdc", "docker", "/var/lib/docker", None),
            ("/dev/sdd", "containerd", "/var/lib/containerd", "default"),
        ]
        imager._ingest_containers(mock_g, devices)

        self.assertEqual(mock_g.debug.call_count, 6)
        scripts = [call[0][1][0] for call in mock_g.debug.call_args_list]

        # One docker start script and one containerd start script
        docker_scripts = [s for s in scripts if "/usr/sbin/dockerd" in s]
        containerd_scripts = [s for s in scripts if "/usr/bin/ctr" in s]
        self.assertEqual(len(docker_scripts), 1)
        self.assertEqual(len(containerd_scripts), 1)

    def test_populate_source_wires_container_devices(self):
        imager = self._create_imager()
        imager._restore_xattrs = mock.Mock()
        imager._write_fstab = mock.Mock()
        imager._label_selinux = mock.Mock()
        imager._ingest_containers = mock.Mock()

        mock_g = mock.Mock()
        ph = mock.Mock()
        ph.bootlets = []

        mount = {"_prefix": "/", "type": "ext4"}
        mounts = [mount]
        part_devices = {id(mount): "/dev/sda1"}
        vol_devices = {}
        part_index = {id(mount): 1}

        imager._populate_source(
            mock_g, ph, None, mounts, part_devices, vol_devices, part_index,
            container_devices=["/dev/sdc"]
        )

        imager._ingest_containers.assert_called_once_with(mock_g, ["/dev/sdc"])

    def test_populate_source_skips_ingest_when_no_containers(self):
        imager = self._create_imager()
        imager._restore_xattrs = mock.Mock()
        imager._write_fstab = mock.Mock()
        imager._label_selinux = mock.Mock()
        imager._ingest_containers = mock.Mock()

        mock_g = mock.Mock()
        ph = mock.Mock()
        ph.bootlets = []

        mount = {"_prefix": "/", "type": "ext4"}
        mounts = [mount]
        part_devices = {id(mount): "/dev/sda1"}
        vol_devices = {}
        part_index = {id(mount): 1}

        imager._populate_source(
            mock_g, ph, None, mounts, part_devices, vol_devices, part_index,
            container_devices=[]
        )

        imager._ingest_containers.assert_not_called()

    def test_docker_ingestion_handler_direct(self):
        handler = DockerIngestionHandler(root="/var/lib/docker")
        self.assertIsInstance(handler, ContainerIngestionHandler)

        norm_sh = handler.generate_normalize_script(1712345678)
        self.assertIn("/sysroot/var/lib/docker", norm_sh)
        self.assertIn("layerdb", norm_sh)
        self.assertIn("overlay2", norm_sh)

        script = handler.generate_ingest_script(["/dev/sdb1"], 1712345678)
        self.assertIn("/usr/sbin/dockerd", script)
        self.assertIn("/dev/sdb1", script)
        self.assertIn("find /sysroot/var/lib/docker -exec touch -h -d @1712345678 {} +", script)

    def test_containerd_ingestion_handler_direct(self):
        handler = ContainerdIngestionHandler(
            root="/var/lib/rancher/k3s/agent/containerd", namespace="k8s.io"
        )
        self.assertIsInstance(handler, ContainerIngestionHandler)

        norm_sh = handler.generate_normalize_script(1712345678)
        self.assertIn("/sysroot/var/lib/rancher/k3s/agent/containerd", norm_sh)
        self.assertIn("bbolt-normalize", norm_sh)
        self.assertIn("1712345678", norm_sh)

        script = handler.generate_ingest_script(["/dev/sdc1"], 1712345678)
        self.assertIn("/usr/bin/containerd", script)
        self.assertIn("--root=/sysroot/var/lib/rancher/k3s/agent/containerd", script)
        self.assertIn('ctr -a /run/containerd/containerd.sock -n "k8s.io" images import', script)
        self.assertIn("/dev/sdc1", script)
        self.assertIn('find "/sysroot/var/lib/rancher/k3s/agent/containerd" -exec touch -h -d "@1712345678" {} +', script)


class DockerImageBuildAndBoot(avocado.Test):
    """
    :avocado: tags=full,container,kvm
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("x86_64 container image tested here")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        try:
            import guestfs
        except ImportError as e:
            self.cancel("python3-guestfs is missing: %s" % e)

    def tearDown(self):
        prune_on_pass(self)

    def test_builds_and_loads_containers(self):
        disk = os.path.join(self.workdir, "docker-test.raw")
        filename_yml = os.path.join(self.workdir, "override-filename.yml")
        with open(filename_yml, "w") as f:
            f.write(f"image:\n    filename: {disk}\n")

        environment = dict(os.environ)
        environment["PATH"] = f"{os.path.dirname(sys.executable)}:{environment.get('PATH', '')}"

        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v",
                 os.path.join(EXAMPLES_DOCKER, "main.yaml"), filename_yml],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT
            )
        self.assertEqual(built.returncode, 0, f"building docker image failed, see {log}")
        self.assertTrue(os.path.isfile(disk), f"no disk image at {disk}")


if __name__ == "__main__":
    avocado.main()
