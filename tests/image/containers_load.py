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
        imager._output_dir = self.workdir
        return imager

    def test_ingest_containers_script_generation(self):
        imager = self._create_imager(epoch=1712345678)
        mock_g = mock.Mock()

        imager._ingest_containers(mock_g, ["/dev/sdc", "/dev/sdd"])

        mock_g.debug.assert_called_once()
        args = mock_g.debug.call_args[0]
        self.assertEqual(args[0], "sh")
        script = args[1][0]

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
        self.assertIn("kill -TERM $D_PID $CD_PID", script)

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
