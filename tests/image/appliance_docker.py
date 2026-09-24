#!/usr/bin/env python3

import avocado
import os
import sys
from unittest.mock import MagicMock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager_appliance import ImagerAppliance


class DummyTargetBootstrap:
    name = "target-bootstrap:latest"


class DummySource:
    def __init__(self, spec, containers=None):
        self.spec = spec
        self.containers = containers or []
        self.options = {"keep": False}
        self.targetBootstrap = DummyTargetBootstrap()


class ApplianceDockerTest(avocado.Test):
    def test_appliance_with_containers(self):
        spec = {
            "distribution": {
                "source": "debian",
                "release": "bookworm",
                "architecture": "amd64",
            },
            "containers": [
                {"image": "docker.io/library/alpine:3.19"}
            ]
        }
        source = DummySource(spec, containers=[MagicMock()])
        appliance = ImagerAppliance(source)
        self.assertTrue(appliance.has_containers())
        self.assertEqual(appliance.memsize(), 2048)
        self.assertIn("imager-appliance-containers", appliance.defaultName())

        dockerfile = None

        def fake_build(script, base, options=None):
            nonlocal dockerfile
            dockerfile = script
            return "appliance-image-id"

        appliance.build = fake_build
        appliance.create()

        self.assertIsNotNone(dockerfile)
        self.assertIn("docker.io", dockerfile)
        self.assertIn("containerd", dockerfile)
        self.assertIn("runc", dockerfile)
        self.assertNotIn("docker-cli", dockerfile)
        self.assertIn("iptables", dockerfile)
        self.assertIn("/usr/bin/docker", dockerfile)
        self.assertIn("/usr/sbin/dockerd", dockerfile)
        self.assertIn("/usr/bin/containerd", dockerfile)
        self.assertIn("/usr/bin/containerd-shim-runc-v2", dockerfile)
        self.assertIn("/usr/bin/runc", dockerfile)

    def test_appliance_with_containers_trixie(self):
        spec = {
            "distribution": {
                "source": "debian",
                "release": "trixie",
                "architecture": "amd64",
            },
            "containers": [
                {"image": "docker.io/library/alpine:3.19"}
            ]
        }
        source = DummySource(spec, containers=[MagicMock()])
        appliance = ImagerAppliance(source)
        self.assertTrue(appliance.has_containers())

        dockerfile = None

        def fake_build(script, base, options=None):
            nonlocal dockerfile
            dockerfile = script
            return "appliance-image-id"

        appliance.build = fake_build
        appliance.create()

        self.assertIsNotNone(dockerfile)
        self.assertIn("docker.io", dockerfile)
        self.assertIn("docker-cli", dockerfile)
        self.assertIn("containerd", dockerfile)
        self.assertIn("runc", dockerfile)

    def test_appliance_without_containers(self):
        spec = {
            "distribution": {
                "source": "debian",
                "release": "bookworm",
                "architecture": "amd64",
            }
        }
        source = DummySource(spec)
        appliance = ImagerAppliance(source)
        self.assertFalse(appliance.has_containers())
        self.assertIsNone(appliance.memsize())
        self.assertEqual(appliance.defaultName(), "imager-appliance/debian/bookworm/amd64")

        dockerfile = None

        def fake_build(script, base, options=None):
            nonlocal dockerfile
            dockerfile = script
            return "appliance-image-id"

        appliance.build = fake_build
        appliance.create()

        self.assertIsNotNone(dockerfile)
        self.assertNotIn("docker.io", dockerfile)
        self.assertNotIn("containerd", dockerfile)
        self.assertNotIn("runc", dockerfile)
        self.assertNotIn("docker-cli", dockerfile)
        self.assertNotIn("/usr/sbin/dockerd", dockerfile)

    def test_appliance_with_containerd_only(self):
        spec = {
            "distribution": {
                "source": "debian",
                "release": "bookworm",
                "architecture": "amd64",
            },
            "containers": [
                {"image": "docker.io/library/alpine:3.19", "target": "containerd"}
            ]
        }
        mock_c = MagicMock()
        mock_c.target = "containerd"
        source = DummySource(spec, containers=[mock_c])
        appliance = ImagerAppliance(source)
        self.assertTrue(appliance.has_containers())
        self.assertFalse(appliance.has_docker_target())
        self.assertEqual(appliance.memsize(), 2048)

        dockerfile = None

        def fake_build(script, base, options=None):
            nonlocal dockerfile
            dockerfile = script
            return "appliance-image-id"

        appliance.build = fake_build
        appliance.create()

        self.assertIsNotNone(dockerfile)
        self.assertIn("containerd", dockerfile)
        self.assertIn("runc", dockerfile)
        self.assertIn("/usr/bin/ctr", dockerfile)
        self.assertIn("/usr/bin/containerd", dockerfile)
        self.assertNotIn("docker.io", dockerfile)
        self.assertNotIn("docker-cli", dockerfile)
        self.assertNotIn("iptables", dockerfile)
        self.assertNotIn("/usr/bin/docker", dockerfile)
        self.assertNotIn("/usr/sbin/dockerd", dockerfile)

    def test_appliance_with_mixed_targets(self):
        spec = {
            "distribution": {
                "source": "debian",
                "release": "bookworm",
                "architecture": "amd64",
            },
            "containers": [
                {"image": "docker.io/library/alpine:3.19", "target": "containerd"},
                {"image": "docker.io/library/redis:alpine", "target": "docker"},
            ]
        }
        mock_c1 = MagicMock()
        mock_c1.target = "containerd"
        mock_c2 = MagicMock()
        mock_c2.target = "docker"
        source = DummySource(spec, containers=[mock_c1, mock_c2])
        appliance = ImagerAppliance(source)
        self.assertTrue(appliance.has_containers())
        self.assertTrue(appliance.has_docker_target())

        dockerfile = None

        def fake_build(script, base, options=None):
            nonlocal dockerfile
            dockerfile = script
            return "appliance-image-id"

        appliance.build = fake_build
        appliance.create()

        self.assertIsNotNone(dockerfile)
        self.assertIn("docker.io", dockerfile)
        self.assertIn("containerd", dockerfile)
        self.assertIn("runc", dockerfile)
        self.assertIn("iptables", dockerfile)
        self.assertIn("/usr/sbin/dockerd", dockerfile)
        self.assertIn("/usr/bin/ctr", dockerfile)

