#!/usr/bin/env python3

import avocado
import os
import sys
from unittest.mock import MagicMock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager.appliance import ImagerAppliance


class DummyTargetBootstrap:
    name = "target-bootstrap:latest"


class DummySource:
    def __init__(self, spec, containers=None):
        self.spec = spec
        self.containers = containers or []
        self.options = {"keep": False}
        self.targetBootstrap = DummyTargetBootstrap()


CONTAINER_TOOLING = [
    "docker.io", "containerd", "runc", "iptables",
    "/usr/bin/docker", "/usr/sbin/dockerd", "/usr/bin/containerd",
    "/usr/bin/containerd-shim-runc-v2", "/usr/bin/runc", "/usr/bin/ctr",
]


def build_dockerfile(appliance):
    dockerfile = None

    def fake_build(script, base, options=None, **kwargs):
        nonlocal dockerfile
        dockerfile = script
        return "appliance-image-id"

    appliance.build = fake_build
    appliance.create()
    return dockerfile


class ApplianceDockerTest(avocado.Test):
    # Appliance content must not depend on an image's 'containers:'
    # section, only on source/release/architecture/kernel.
    def test_appliance_bakes_container_tooling_unconditionally(self):
        specs = [
            ("no containers", {}, []),
            ("docker target", {
                "containers": [{"image": "docker.io/library/alpine:3.19"}]
            }, [MagicMock()]),
            ("containerd target", {
                "containers": [{"image": "docker.io/library/alpine:3.19",
                                "target": "containerd"}]
            }, [MagicMock(target="containerd")]),
            ("mixed targets", {
                "containers": [
                    {"image": "docker.io/library/alpine:3.19", "target": "containerd"},
                    {"image": "docker.io/library/redis:alpine", "target": "docker"},
                ]
            }, [MagicMock(target="containerd"), MagicMock(target="docker")]),
        ]
        for label, extra, containers in specs:
            spec = dict({
                "distribution": {
                    "source": "debian",
                    "uri": "http://example.com/debian",
                    "release": "bookworm",
                    "architecture": "amd64",
                },
            }, **extra)
            appliance = ImagerAppliance(DummySource(spec, containers=containers))
            dockerfile = build_dockerfile(appliance)
            for needle in CONTAINER_TOOLING:
                self.assertIn(needle, dockerfile,
                              "%s missing for '%s' spec" % (needle, label))
            self.assertEqual(appliance.defaultName(),
                             "imager-appliance/debian/bookworm/amd64/amd64")

    def test_appliance_docker_cli_gated_by_release(self):
        base_spec = {
            "distribution": {
                "source": "debian",
                "uri": "http://example.com/debian",
                "architecture": "amd64",
            },
        }
        bookworm = dict(base_spec, distribution=dict(base_spec["distribution"], release="bookworm"))
        trixie = dict(base_spec, distribution=dict(base_spec["distribution"], release="trixie"))

        dockerfile = build_dockerfile(ImagerAppliance(DummySource(bookworm)))
        self.assertNotIn("docker-cli", dockerfile)

        dockerfile = build_dockerfile(ImagerAppliance(DummySource(trixie)))
        self.assertIn("docker-cli", dockerfile)

    def test_appliance_memsize_still_tracks_this_images_containers(self):
        spec = {
            "distribution": {
                "source": "debian",
                "uri": "http://example.com/debian",
                "release": "bookworm",
                "architecture": "amd64",
            },
        }
        without = ImagerAppliance(DummySource(spec))
        self.assertFalse(without.has_containers())
        self.assertIsNone(without.memsize())

        with_containers = ImagerAppliance(DummySource(
            dict(spec, containers=[{"image": "docker.io/library/alpine:3.19"}]),
            containers=[MagicMock()]))
        self.assertTrue(with_containers.has_containers())
        self.assertEqual(with_containers.memsize(), 2048)

    def test_appliance_kernel_override_changes_tag_not_default(self):
        spec = {
            "distribution": {
                "source": "debian",
                "uri": "http://example.com/debian",
                "release": "bookworm",
                "architecture": "amd64",
            },
        }
        default = ImagerAppliance(DummySource(spec))
        self.assertEqual(default.defaultName(),
                         "imager-appliance/debian/bookworm/amd64/amd64")

        overridden = ImagerAppliance(DummySource(
            dict(spec, imager={"kernel": "linux-image-cloud-amd64"})))
        self.assertEqual(overridden.defaultName(),
                         "imager-appliance/debian/bookworm/amd64/cloud-amd64")

        dockerfile = build_dockerfile(overridden)
        self.assertIn("linux-image-cloud-amd64", dockerfile)

    def test_appliance_runtime_stage_includes_uki_and_signing_tools(self):
        spec = {
            "distribution": {
                "source": "debian",
                "uri": "http://example.com/debian",
                "release": "trixie",
                "architecture": "amd64",
            },
        }
        dockerfile = build_dockerfile(ImagerAppliance(DummySource(spec)))
        self.assertIn("FROM target-bootstrap:latest AS builder", dockerfile)
        self.assertIn("FROM target-bootstrap:latest AS base", dockerfile)
        runtime_stage = dockerfile.split("FROM target-bootstrap:latest AS base")[1]
        for tool in ["binutils", "sbsigntool", "libfaketime", "systemd-ukify"]:
            self.assertIn(tool, runtime_stage)
        self.assertIn("COPY --from=builder /appliance /appliance", runtime_stage)
        self.assertIn("COPY --from=builder /extra-tools /extra-tools", runtime_stage)

