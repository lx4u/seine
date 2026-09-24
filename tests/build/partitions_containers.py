#!/usr/bin/env python3

import avocado
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.partition import PartitionHandler

def _create_container_archive(path, layers_content):
    layers = []
    layer_tar_paths = []
    for idx, members in enumerate(layers_content):
        layer_name = f"layer{idx}.tar"
        layers.append(layer_name)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as lt:
            for name, size in members:
                ti = tarfile.TarInfo(name=name)
                ti.size = size
                content = b"x" * size if size > 0 else b""
                lt.addfile(ti, io.BytesIO(content))
        layer_tar_paths.append((layer_name, buf.getvalue()))

    manifest = [{"Config": "config.json", "RepoTags": ["test:latest"], "Layers": layers}]
    manifest_bytes = json.dumps(manifest).encode("utf-8")

    with tarfile.open(path, "w") as tar:
        ti_m = tarfile.TarInfo(name="manifest.json")
        ti_m.size = len(manifest_bytes)
        tar.addfile(ti_m, io.BytesIO(manifest_bytes))
        for layer_name, data in layer_tar_paths:
            ti_l = tarfile.TarInfo(name=layer_name)
            ti_l.size = len(data)
            tar.addfile(ti_l, io.BytesIO(data))


class ContainerPartitionSizing(avocado.Test):

    def test_uncompressed_layer_math_and_dedicated_partition(self):
        # 3 files: 100B (-> 4096B), 5000B (-> 8192B), 0B (-> 4096B).
        # Payload: 16384B. Inodes: 3 * 256 = 768B. Slack: int(16384 * 0.15) = 2457B. Total: 19609B.
        archive = os.path.join(self.workdir, "app.tar")
        _create_container_archive(archive, [
            [("file1.txt", 100), ("file2.txt", 5000), ("empty.txt", 0)]
        ])

        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "disk.img",
                "partitions": [
                    {"label": "rootfs", "where": "/"},
                    {"label": "docker", "where": "/var/lib/docker"},
                ],
            },
        })

        root_mount = next(m for m in ph.mounts if m["label"] == "rootfs")
        docker_mount = next(m for m in ph.mounts if m["label"] == "docker")
        root_before = root_mount["_size"]
        docker_before = docker_mount["_size"]

        matched = ph.distribute_container_archives([archive])
        self.assertIs(matched, docker_mount)
        self.assertEqual(root_mount["_size"], root_before)
        self.assertEqual(docker_mount["_size"], docker_before + 19609)

    def test_fallback_to_root_partition(self):
        archive = os.path.join(self.workdir, "app.tar")
        _create_container_archive(archive, [
            [("file1.txt", 100), ("file2.txt", 5000), ("empty.txt", 0)]
        ])

        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "disk.img",
                "partitions": [
                    {"label": "rootfs", "where": "/"},
                ],
            },
        })

        root_mount = next(m for m in ph.mounts if m["label"] == "rootfs")
        root_before = root_mount["_size"]

        matched = ph.distribute_container_archives([archive])
        self.assertIs(matched, root_mount)
        self.assertEqual(root_mount["_size"], root_before + 19609)

    def test_intermediate_var_partition(self):
        archive = os.path.join(self.workdir, "app.tar")
        _create_container_archive(archive, [
            [("file1.txt", 100), ("file2.txt", 5000), ("empty.txt", 0)]
        ])

        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "disk.img",
                "partitions": [
                    {"label": "rootfs", "where": "/"},
                    {"label": "var", "where": "/var"},
                ],
            },
        })

        root_mount = next(m for m in ph.mounts if m["label"] == "rootfs")
        var_mount = next(m for m in ph.mounts if m["label"] == "var")
        root_before = root_mount["_size"]
        var_before = var_mount["_size"]

        matched = ph.distribute_container_archives([archive])
        self.assertIs(matched, var_mount)
        self.assertEqual(root_mount["_size"], root_before)
        self.assertEqual(var_mount["_size"], var_before + 19609)

    def test_multi_layer_archive(self):
        # Layer 1: 1000B (-> 4096B). Layer 2: 2000B (-> 4096B).
        # Total payload: 8192B. Inodes: 2 * 256 = 512B. Slack: int(8192 * 0.15) = 1228B. Total: 9932B.
        archive = os.path.join(self.workdir, "multi.tar")
        _create_container_archive(archive, [
            [("app/bin", 1000)],
            [("app/data", 2000)],
        ])

        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "disk.img",
                "partitions": [
                    {"label": "rootfs", "where": "/"},
                ],
            },
        })

        root_mount = next(m for m in ph.mounts if m["label"] == "rootfs")
        root_before = root_mount["_size"]

        ph.distribute_container_archives([archive])
        self.assertEqual(root_mount["_size"], root_before + 9932)

    def test_distribute_scoped_by_source(self):
        archive = os.path.join(self.workdir, "app.tar")
        _create_container_archive(archive, [[("file.txt", 100)]])

        ph = PartitionHandler()
        ph.parse({
            "multiconfig": {"main": ["main.yaml"], "recovery": ["recovery.yaml"]},
            "image": {
                "filename": "disk.img",
                "partitions": [
                    {"label": "main-root", "source": "main", "where": "/"},
                    {"label": "recovery-root", "source": "recovery", "where": "/"},
                ],
            },
        })

        main_root = next(m for m in ph.mounts if m["label"] == "main-root")
        recovery_root = next(m for m in ph.mounts if m["label"] == "recovery-root")
        recovery_before = recovery_root["_size"]
        main_before = main_root["_size"]

        ph.distribute_container_archives([archive], source="main")
        self.assertGreater(main_root["_size"], main_before)
        self.assertEqual(recovery_root["_size"], recovery_before)

    def test_image_size_partitions_with_containers(self):
        archive = os.path.join(self.workdir, "app.tar")
        _create_container_archive(archive, [[("file1.txt", 100)]])

        rootfs_tar = os.path.join(self.workdir, "rootfs.tar")
        with tarfile.open(rootfs_tar, "w") as tar:
            content = b"myhost\n"
            ti = tarfile.TarInfo(name="etc/hostname")
            ti.size = len(content)
            tar.addfile(ti, io.BytesIO(content))

        build = BuildCmd()
        build.loads(f"""
            distribution:
                release: bookworm
                architecture: amd64
            image:
                filename: test.img
                partitions:
                    - label: rootfs
                      where: /
                    - label: docker
                      where: /var/lib/docker
            containers:
                - file: {archive}
                  image: local/test:latest
        """)
        build.parse()
        build.image._tarball = rootfs_tar
        docker_mount = next(m for m in build.image.partitionHandler.mounts if m["label"] == "docker")
        docker_before = docker_mount["_size"]

        build.image._size_partitions()
        self.assertGreater(docker_mount["_size"], docker_before)
