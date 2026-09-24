#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import json
import os
import sys

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import sbom as sbom_module
from seine.bugs import sources_from_sbom
from seine.containers.spec import ContainerImage
from seine.sbom import SBOM, scan_container, splice_container_sbom


class MockContainerEngine:
    def __init__(self, testcase, syft_output=None):
        self.testcase = testcase
        self.syft_output = syft_output or {
            "SPDXID": "SPDXRef-DOCUMENT",
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-apk-busybox-1234",
                    "name": "busybox",
                    "versionInfo": "1.36.1",
                }
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relatedSpdxElement": "SPDXRef-DocumentRoot-Image-alpine",
                    "relationshipType": "DESCRIBES",
                },
                {
                    "spdxElementId": "SPDXRef-DocumentRoot-Image-alpine",
                    "relatedSpdxElement": "SPDXRef-Package-apk-busybox-1234",
                    "relationshipType": "CONTAINS",
                },
            ],
        }
        self.commands = []

    def run(self, cmd, check=False):
        self.commands.append(cmd)
        for arg in cmd:
            if isinstance(arg, str) and "spdx-json=" in arg:
                out_path = arg.split("spdx-json=", 1)[1]
                with open(out_path, "w") as f:
                    json.dump(self.syft_output, f)

    def scratch(self):
        return self.testcase.workdir

    def __enter__(self):
        self.saved = sbom_module.ContainerEngine
        sbom_module.ContainerEngine = self
        return self

    def __exit__(self, *args):
        sbom_module.ContainerEngine = self.saved


def sample_root_sbom(workdir, path=None):
    if path is None:
        path = os.path.join(workdir, "pc-image-sbom.spdx.json")
    data = {
        "SPDXID": "SPDXRef-DOCUMENT",
        "spdxVersion": "SPDX-2.3",
        "packages": [
            {
                "SPDXID": "SPDXRef-Debian",
                "name": "Debian",
            },
            {
                "SPDXID": "SPDXRef-bash-amd64",
                "name": "bash",
                "versionInfo": "5.2",
            },
        ],
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relatedSpdxElement": "SPDXRef-Debian",
                "relationshipType": "DESCRIBES",
            },
            {
                "spdxElementId": "SPDXRef-bash-amd64",
                "relatedSpdxElement": "SPDXRef-Debian",
                "relationshipType": "PACKAGE_OF",
            },
        ],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


class ContainerSBOMSplice(avocado.Test):
    def test_splicing_single_container(self):
        sbom_path = sample_root_sbom(self.workdir)
        container = ContainerImage(
            {
                "image": "docker.io/library/alpine:3.19",
                "digest": "sha256:6baf43584bcb78f2e5847d1de515f23499913ac9f12bdf834811a3145eb11ca1",
            },
            1,
        )

        syft_data = {
            "SPDXID": "SPDXRef-DOCUMENT",
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-apk-busybox-1234",
                    "name": "busybox",
                    "versionInfo": "1.36.1",
                },
                {
                    "SPDXID": "SPDXRef-Package-apk-ssl-client-5678",
                    "name": "ssl-client",
                    "versionInfo": "1.36.1",
                },
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relatedSpdxElement": "SPDXRef-DocumentRoot-Image-alpine",
                    "relationshipType": "DESCRIBES",
                },
                {
                    "spdxElementId": "SPDXRef-DocumentRoot-Image-alpine",
                    "relatedSpdxElement": "SPDXRef-Package-apk-busybox-1234",
                    "relationshipType": "CONTAINS",
                },
            ],
        }

        splice_container_sbom(sbom_path, [(container, syft_data)], distro_arch="amd64")

        with open(sbom_path) as f:
            result = json.load(f)

        pkg_ids = [p["SPDXID"] for p in result["packages"]]
        self.assertIn("SPDXRef-Container-alpine-3.19", pkg_ids)
        self.assertIn("SPDXRef-Container-alpine-3.19-Package-apk-busybox-1234", pkg_ids)
        self.assertIn("SPDXRef-Container-alpine-3.19-Package-apk-ssl-client-5678", pkg_ids)

        container_pkg = next(p for p in result["packages"] if p["SPDXID"] == "SPDXRef-Container-alpine-3.19")
        self.assertEqual(container_pkg["versionInfo"], "sha256:6baf43584bcb78f2e5847d1de515f23499913ac9f12bdf834811a3145eb11ca1")

        # Rootfs CONTAINS container
        contains_container = any(
            r["spdxElementId"] == "SPDXRef-Debian"
            and r["relatedSpdxElement"] == "SPDXRef-Container-alpine-3.19"
            and r["relationshipType"] == "CONTAINS"
            for r in result["relationships"]
        )
        self.assertTrue(contains_container)

        # Container CONTAINS busybox & ssl-client
        contains_busybox = any(
            r["spdxElementId"] == "SPDXRef-Container-alpine-3.19"
            and r["relatedSpdxElement"] == "SPDXRef-Container-alpine-3.19-Package-apk-busybox-1234"
            and r["relationshipType"] == "CONTAINS"
            for r in result["relationships"]
        )
        self.assertTrue(contains_busybox)

        contains_ssl = any(
            r["spdxElementId"] == "SPDXRef-Container-alpine-3.19"
            and r["relatedSpdxElement"] == "SPDXRef-Container-alpine-3.19-Package-apk-ssl-client-5678"
            and r["relationshipType"] == "CONTAINS"
            for r in result["relationships"]
        )
        self.assertTrue(contains_ssl)

    def test_splicing_multiple_containers(self):
        sbom_path = sample_root_sbom(self.workdir)
        c1 = ContainerImage({"image": "docker.io/library/alpine:3.19"}, 1)
        c2 = ContainerImage({"image": "docker.io/library/redis:7.2"}, 2)

        syft1 = {
            "SPDXID": "SPDXRef-DOCUMENT",
            "packages": [{"SPDXID": "SPDXRef-Package-apk-busybox-1", "name": "busybox"}],
            "relationships": [],
        }
        syft2 = {
            "SPDXID": "SPDXRef-DOCUMENT",
            "packages": [{"SPDXID": "SPDXRef-Package-deb-redis-server-1", "name": "redis-server"}],
            "relationships": [],
        }

        splice_container_sbom(sbom_path, [(c1, syft1), (c2, syft2)], distro_arch="amd64")

        with open(sbom_path) as f:
            result = json.load(f)

        pkg_ids = [p["SPDXID"] for p in result["packages"]]
        self.assertIn("SPDXRef-Container-alpine-3.19", pkg_ids)
        self.assertIn("SPDXRef-Container-redis-7.2", pkg_ids)
        self.assertIn("SPDXRef-Container-alpine-3.19-Package-apk-busybox-1", pkg_ids)
        self.assertIn("SPDXRef-Container-redis-7.2-Package-deb-redis-server-1", pkg_ids)

    def test_sources_from_sbom_ignores_container_components(self):
        sbom_path = sample_root_sbom(self.workdir)
        container = ContainerImage({"image": "docker.io/library/alpine:3.19"}, 1)
        syft_data = {
            "SPDXID": "SPDXRef-DOCUMENT",
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-apk-busybox-1234",
                    "name": "busybox",
                    "versionInfo": "1.36.1",
                }
            ],
            "relationships": [],
        }
        splice_container_sbom(sbom_path, [(container, syft_data)], distro_arch="amd64")

        sources = sources_from_sbom(sbom_path)
        # Should only contain host packages ('Debian', 'bash'), not container 'busybox' or 'alpine:3.19'
        self.assertEqual(sources, ["Debian", "bash"])


class ScanContainerExecution(avocado.Test):
    def test_scan_container_invokes_engine(self):
        archive = os.path.join(self.workdir, "app.tar")
        with open(archive, "w") as f:
            f.write("")

        with MockContainerEngine(self) as engine:
            data = scan_container(archive)

        self.assertEqual(len(engine.commands), 1)
        cmd = engine.commands[0]
        self.assertIn(sbom_module.SYFT_IMAGE, cmd)
        self.assertIn(f"docker-archive:{archive}", cmd)
        self.assertEqual(data["SPDXID"], "SPDXRef-DOCUMENT")


class SBOMGenerateWithContainers(avocado.Test):
    def test_generate_invokes_scan_and_splicing(self):
        root_tar = os.path.join(self.workdir, "root.tar")
        with open(root_tar, "w") as f:
            f.write("")

        container_archive = os.path.join(self.workdir, "alpine.tar")
        with open(container_archive, "w") as f:
            f.write("")

        container = ContainerImage(
            {
                "image": "docker.io/library/alpine:3.19",
                "file": "alpine.tar",
            },
            1,
            spec_dir=self.workdir,
        )

        class MockImage:
            _output = os.path.join(self.workdir, "pc-image.img")
            containers = [container]

        distro = {"release": "bookworm", "architecture": "amd64"}

        with MockContainerEngine(self) as engine:
            def custom_run(cmd, check=False):
                engine.commands.append(cmd)
                if any("debsbom" in str(arg) for arg in cmd):
                    sample_root_sbom(self.workdir, path=os.path.join(self.workdir, "pc-image-sbom.spdx.json"))
                for arg in cmd:
                    if isinstance(arg, str) and "spdx-json=" in arg:
                        out_path = arg.split("spdx-json=", 1)[1]
                        with open(out_path, "w") as f:
                            json.dump(engine.syft_output, f)

            engine.run = custom_run
            sbom = SBOM(distro, {"sbom": True})
            sbom._extract = lambda tarball, root: None
            sbom.generate(root_tar, MockImage._output, image_obj=MockImage)

        sbom_file = os.path.join(self.workdir, "pc-image-sbom.spdx.json")
        self.assertTrue(os.path.exists(sbom_file))
        with open(sbom_file) as f:
            data = json.load(f)

        pkg_ids = [p["SPDXID"] for p in data["packages"]]
        self.assertIn("SPDXRef-Container-alpine-3.19", pkg_ids)


if __name__ == "__main__":
    avocado.main()
