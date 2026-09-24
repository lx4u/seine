#!/usr/bin/env python3
# :avocado: tags=container

import atexit
import avocado
import json
import os
import shutil
import sys
import tempfile
from unittest.mock import MagicMock, patch

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.vendor import VendorCmd, load_lock_containers, save_lock
from seine.containers import resolve_container
from seine import containers



class ContainersVendorTest(avocado.Test):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="seine-test-containers-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_vendor_identifies_containers(self):
        spec_path = os.path.join(self.tmpdir, "main.yaml")
        with open(spec_path, "w") as f:
            f.write("""
distribution:
  source: debian
  release: bookworm
  architecture: amd64
containers:
  - image: docker.io/library/alpine:3.19
""")

        mock_resolve = MagicMock(return_value=(
            "sha256:topdigest00000000000000000000000000000000000000000000000000000000",
            {"amd64": "sha256:archdigest11111111111111111111111111111111111111111111111111111111"}
        ))
        mock_fetch = MagicMock(return_value={
            "manifest_digest": "sha256:archdigest11111111111111111111111111111111111111111111111111111111",
            "size": 2048,
            "sha256": "2222222222222222222222222222222222222222222222222222222222222222"
        })

        cmd = VendorCmd()
        with patch("seine.vendor.cli.resolve_container", mock_resolve), \
             patch("seine.vendor.cli.fetch_container", mock_fetch):
            with self.assertRaises(SystemExit) as ctx:
                cmd.main([spec_path])

        self.assertEqual(ctx.exception.code, 0)
        self.assertTrue(mock_resolve.called)
        self.assertEqual(mock_resolve.call_args[0][0], "docker.io/library/alpine:3.19")
        self.assertIn("amd64", mock_resolve.call_args[0][1])
        self.assertTrue(mock_fetch.called)

        lock_path = os.path.join(self.tmpdir, "main.lock.yaml")
        self.assertTrue(os.path.isfile(lock_path))
        locked = load_lock_containers(lock_path)
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0]["image"], "docker.io/library/alpine:3.19")

    def test_multi_arch_manifest_resolution(self):
        index_data = {
            "manifests": [
                {
                    "digest": "sha256:amd64manifestdigest000000000000000000000000000000000000000000000000",
                    "platform": {"architecture": "amd64", "os": "linux"}
                },
                {
                    "digest": "sha256:arm64manifestdigest111111111111111111111111111111111111111111111111",
                    "platform": {"architecture": "arm64", "os": "linux"}
                },
                {
                    "digest": "sha256:s390xmanifestdigest222222222222222222222222222222222222222222222222",
                    "platform": {"architecture": "s390x", "os": "linux"}
                }
            ]
        }
        with patch("seine.containers.fetch.run_skopeo", return_value=json.dumps(index_data).encode("utf-8")):
            top_digest, arch_digests = resolve_container("alpine:3.19", ["amd64", "arm64"])


        self.assertTrue(top_digest.startswith("sha256:"))
        self.assertEqual(
            arch_digests["amd64"],
            "sha256:amd64manifestdigest000000000000000000000000000000000000000000000000"
        )
        self.assertEqual(
            arch_digests["arm64"],
            "sha256:arm64manifestdigest111111111111111111111111111111111111111111111111"
        )
        self.assertNotIn("s390x", arch_digests)

    def test_multi_arch_variant_manifest_resolution(self):
        index_data = {
            "manifests": [
                {
                    "digest": "sha256:armhfmanifestdigest333333333333333333333333333333333333333333333333",
                    "platform": {"architecture": "arm", "variant": "v7", "os": "linux"}
                },
                {
                    "digest": "sha256:i386manifestdigest444444444444444444444444444444444444444444444444",
                    "platform": {"architecture": "386", "os": "linux"}
                },
                {
                    "digest": "sha256:ppc64elmanifestdigest5555555555555555555555555555555555555555555555",
                    "platform": {"architecture": "ppc64le", "os": "linux"}
                }
            ]
        }
        with patch("seine.containers.fetch.run_skopeo", return_value=json.dumps(index_data).encode("utf-8")):
            top_digest, arch_digests = resolve_container("alpine:3.19", ["armhf", "i386", "ppc64el"])

        self.assertEqual(
            arch_digests["armhf"],
            "sha256:armhfmanifestdigest333333333333333333333333333333333333333333333333"
        )
        self.assertEqual(
            arch_digests["i386"],
            "sha256:i386manifestdigest444444444444444444444444444444444444444444444444"
        )
        self.assertEqual(
            arch_digests["ppc64el"],
            "sha256:ppc64elmanifestdigest5555555555555555555555555555555555555555555555"
        )


    def test_lockfile_serialization(self):
        lock_path = os.path.join(self.tmpdir, "main.lock.yaml")
        containers_data = [
            {
                "image": "docker.io/library/alpine:3.19",
                "digest": "sha256:51b67269f350ca123512e022f77833076ff2a7e781ecc4518386f78810eb70a1",
                "architectures": {
                    "amd64": {
                        "manifest_digest": "sha256:6457d531112ff203f07943061d76b0fce77294b139fa7be45eb110ac964937ae",
                        "file": "vendor/containers/docker.io_library_alpine_3.19_amd64_6457d531112f.tar",
                        "size": 7720960,
                        "sha256": "d83b7cf70e4544d658b4f1773bfbfeb42f8c5b96788bc5f949fbbe870d069002",
                    },
                    "arm64": {
                        "manifest_digest": "sha256:c5b1261d6d3e43071626931fc004f70149baeba2c8ec672bd49f277b221f16f5",
                        "file": "vendor/containers/docker.io_library_alpine_3.19_arm64_c5b1261d6d3e.tar",
                        "size": 7120384,
                        "sha256": "f19348e69fa894c48972eec91e2b5ba476100140d39e31d418cb2a014a4d6345",
                    },
                },
            }
        ]

        save_lock(lock_path, {}, containers=containers_data)
        self.assertTrue(os.path.isfile(lock_path))

        loaded = load_lock_containers(lock_path)
        self.assertEqual(len(loaded), 1)
        entry = loaded[0]
        self.assertEqual(entry["image"], "docker.io/library/alpine:3.19")
        self.assertEqual(entry["digest"], "sha256:51b67269f350ca123512e022f77833076ff2a7e781ecc4518386f78810eb70a1")
        self.assertIn("amd64", entry["architectures"])
        self.assertIn("arm64", entry["architectures"])
        amd64 = entry["architectures"]["amd64"]
        self.assertEqual(amd64["size"], 7720960)
        self.assertEqual(amd64["sha256"], "d83b7cf70e4544d658b4f1773bfbfeb42f8c5b96788bc5f949fbbe870d069002")

    def test_offline_build_guarantee(self):
        spec_path = os.path.join(self.tmpdir, "main.yaml")
        with open(spec_path, "w") as f:
            f.write("""
distribution:
  source: debian
  release: bookworm
  architecture: amd64
containers:
  - image: docker.io/library/alpine:3.19
image:
  filename: test.img
  partitions:
    - label: root
      where: /
""")

        build = BuildCmd()
        build.options["offline"] = True
        build.load(spec_path)
        with self.assertRaises(ValueError) as ctx:
            build.parse()
        self.assertIn("offline build requires all containers to be vendored locally", str(ctx.exception))

        vendor_dir = os.path.join(self.tmpdir, "vendor", "containers")
        os.makedirs(vendor_dir, exist_ok=True)
        archive_name = "docker.io_library_alpine_3.19_amd64_6457d531112f.tar"
        archive_path = os.path.join(vendor_dir, archive_name)
        with open(archive_path, "wb") as f:
            f.write(b"dummy container archive content")

        lock_path = os.path.join(self.tmpdir, "main.lock.yaml")
        lock_containers = [
            {
                "image": "docker.io/library/alpine:3.19",
                "digest": "sha256:51b67269f350ca123512e022f77833076ff2a7e781ecc4518386f78810eb70a1",
                "architectures": {
                    "amd64": {
                        "manifest_digest": "sha256:6457d531112ff203f07943061d76b0fce77294b139fa7be45eb110ac964937ae",
                        "file": f"vendor/containers/{archive_name}",
                        "size": len(b"dummy container archive content"),
                        "sha256": "abc12345",
                    }
                }
            }
        ]
        save_lock(lock_path, {}, containers=lock_containers)

        build_offline = BuildCmd()
        build_offline.options["offline"] = True
        build_offline.load_all([spec_path])
        build_offline.parse()

        self.assertEqual(len(build_offline.image.containers), 1)
        c = build_offline.image.containers[0]
        resolved = c.archive_for("amd64", self.tmpdir)
        self.assertEqual(resolved, os.path.normpath(archive_path))
        self.assertTrue(os.path.isfile(resolved))
