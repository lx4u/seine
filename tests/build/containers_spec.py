#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import containers
from seine.build import BuildCmd


class ContainersSpec(avocado.Test):
    def test_valid_containers_declaration(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: test.img
                partitions:
                    - label: root
                      where: /
            containers:
                - image: docker://docker.io/library/alpine:3.19
                  digest: sha256:51b67269f350ca123512e022f77833076ff2a7e781ecc4518386f78810eb70a1
                - image: ghcr.io/my-org/backend:v1.0
                  digest: sha256:88ab10ff3245cba8971203498172340918230918230918230918230918230918
                  auth:
                      vault: secret/data/ci/registry-token
                - file: files/custom-app.tar
                  image: custom-app:latest
        """)
        spec = build.parse()
        self.assertIn("containers", spec)
        parsed = build.image.containers
        self.assertEqual(len(parsed), 3)

        self.assertEqual(parsed[0].image, "docker.io/library/alpine:3.19")
        self.assertEqual(parsed[0].digest, "sha256:51b67269f350ca123512e022f77833076ff2a7e781ecc4518386f78810eb70a1")
        self.assertEqual(parsed[0].identity, "docker.io/library/alpine:3.19@sha256:51b67269f350ca123512e022f77833076ff2a7e781ecc4518386f78810eb70a1")

        self.assertEqual(parsed[1].image, "ghcr.io/my-org/backend:v1.0")
        self.assertEqual(parsed[1].auth, {"vault": "secret/data/ci/registry-token"})

        self.assertEqual(parsed[2].file, "files/custom-app.tar")
        self.assertEqual(parsed[2].image, "custom-app:latest")
        self.assertTrue(parsed[2].resolved_file.endswith("files/custom-app.tar"))

    def test_per_arch_digests_declaration(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: test.img
                partitions:
                    - label: root
                      where: /
            containers:
                - image: alpine:3.19
                  digests:
                      amd64: sha256:cf4fd9eaf3086aeef25c678e7359a3418f4b9af203469243b48ac5d22e052dff
                      arm64: sha256:4b49b6b7f36979ff96a1a47738f6575971a8f948f9e612b7a9aa3bfa182f7c0a
                - image: nginx:alpine
                  architectures:
                      amd64:
                          digest: sha256:45543926cb9b4f00e05547ecd63a2cb1acb4f6492d44b32571bdd383c2618644
                      arm64:
                          file: files/nginx-arm64.tar
        """)
        build.parse()
        parsed = build.image.containers
        self.assertEqual(len(parsed), 2)

        self.assertEqual(parsed[0].digest_for("amd64"), "sha256:cf4fd9eaf3086aeef25c678e7359a3418f4b9af203469243b48ac5d22e052dff")
        self.assertEqual(parsed[0].digest_for("arm64"), "sha256:4b49b6b7f36979ff96a1a47738f6575971a8f948f9e612b7a9aa3bfa182f7c0a")
        self.assertIsNone(parsed[0].digest_for("riscv64"))

        self.assertEqual(parsed[1].digest_for("amd64"), "sha256:45543926cb9b4f00e05547ecd63a2cb1acb4f6492d44b32571bdd383c2618644")
        self.assertIsNone(parsed[1].digest_for("arm64"))
        self.assertEqual(parsed[1].archive_for("arm64"), "files/nginx-arm64.tar")

    def test_invalid_types_raise_value_error(self):
        cases = [
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers: not-a-list
            """, "'containers:' shall be a list of container definitions!"),
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers:
                    - not-a-dict
            """, "container #1 is not a dictionary!"),
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers:
                    - {}
            """, "container #1 must declare either 'image:' or 'file:'"),
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers:
                    - image: ''
            """, "container #1: 'image' must be a non-empty string"),
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers:
                    - image: alpine
                      digest: not-a-sha256
            """, "invalid digest format"),
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers:
                    - image: alpine
                      digests: not-a-dict
            """, "container #1: 'digests' must be a dictionary"),
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers:
                    - image: alpine
                      digests:
                          amd64: bad-digest
            """, "invalid digest format for architecture 'amd64'"),
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers:
                    - image: alpine
                      auth: not-a-dict
            """, "container #1: 'auth' must be a dictionary"),
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers:
                    - image: alpine
                      auth:
                          vault: 123
            """, "container #1: 'auth: vault:' must be a string path"),
            ("""
                image:
                    filename: test.img
                    partitions:
                        - label: root
                          where: /
                containers:
                    - file: 123
            """, "container #1: 'file' must be a string path"),
        ]
        for yml, expected_err in cases:
            build = BuildCmd()
            build.loads(yml)
            with self.assertRaises(ValueError) as ctx:
                build.parse()
            self.assertIn(expected_err, str(ctx.exception))

    def test_require_hashes_enforcement(self):
        build_unhashed = BuildCmd()
        build_unhashed.options["require_hashes"] = True
        build_unhashed.loads("""
            image:
                filename: test.img
                partitions:
                    - label: root
                      where: /
            containers:
                - image: docker.io/library/alpine:3.19
        """)
        with self.assertRaises(ValueError) as ctx:
            build_unhashed.parse()
        self.assertIn("--require-hashes was given and 1 container(s) missing digest:", str(ctx.exception))
        self.assertIn("container #1: image 'docker.io/library/alpine:3.19'", str(ctx.exception))

        build_hashed = BuildCmd()
        build_hashed.options["require_hashes"] = True
        build_hashed.loads("""
            image:
                filename: test.img
                partitions:
                    - label: root
                      where: /
            containers:
                - image: docker.io/library/alpine:3.19
                  digest: sha256:51b67269f350ca123512e022f77833076ff2a7e781ecc4518386f78810eb70a1
                - image: nginx:alpine
                  digests:
                      amd64: sha256:45543926cb9b4f00e05547ecd63a2cb1acb4f6492d44b32571bdd383c2618644
                - file: files/local.tar
                  image: local:latest
        """)
        build_hashed.parse()
        self.assertEqual(len(build_hashed.image.containers), 3)

    def test_multi_spec_merging(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: test.img
                partitions:
                    - label: root
                      where: /
            containers:
                - image: alpine:3.19
                  digests:
                      amd64: sha256:51b67269f350ca123512e022f77833076ff2a7e781ecc4518386f78810eb70a1
                - image: nginx:alpine
                  digest: sha256:45543926cb9b4f00e05547ecd63a2cb1acb4f6492d44b32571bdd383c2618644
        """)
        build.loads("""
            containers:
                - image: alpine:3.19
                  digests:
                      arm64: sha256:611293e622b7a69b7b993361e2714a6007e997e337de8faea072d73351eb8980
                - image: redis:7-alpine
                  digest: sha256:611293e622b7a69b7b993361e2714a6007e997e337de8faea072d73351eb8980
        """)
        spec = build.parse()
        self.assertEqual(len(spec["containers"]), 3)
        images = [c["image"] for c in spec["containers"]]
        self.assertEqual(images, ["alpine:3.19", "nginx:alpine", "redis:7-alpine"])
        alpine_c = build.image.containers[0]
        self.assertEqual(alpine_c.digest_for("amd64"), "sha256:51b67269f350ca123512e022f77833076ff2a7e781ecc4518386f78810eb70a1")
        self.assertEqual(alpine_c.digest_for("arm64"), "sha256:611293e622b7a69b7b993361e2714a6007e997e337de8faea072d73351eb8980")

    def test_architecture_mapping(self):
        self.assertEqual(containers.to_container_arch("amd64"), ("amd64", None))
        self.assertEqual(containers.to_container_arch("arm64"), ("arm64", "v8"))
        self.assertEqual(containers.to_container_arch("armhf"), ("arm", "v7"))
        self.assertEqual(containers.to_container_arch("armel"), ("arm", "v6"))
        self.assertEqual(containers.to_container_arch("i386"), ("386", None))
        self.assertEqual(containers.to_container_arch("ppc64el"), ("ppc64le", None))
        self.assertEqual(containers.to_container_arch("riscv64"), ("riscv64", None))
        self.assertEqual(containers.to_container_arch("s390x"), ("s390x", None))

        self.assertEqual(containers.to_debian_arch("amd64"), "amd64")
        self.assertEqual(containers.to_debian_arch("arm64"), "arm64")
        self.assertEqual(containers.to_debian_arch("arm64", "v8"), "arm64")
        self.assertEqual(containers.to_debian_arch("arm", "v7"), "armhf")
        self.assertEqual(containers.to_debian_arch("arm", "v6"), "armel")
        self.assertEqual(containers.to_debian_arch("386"), "i386")
        self.assertEqual(containers.to_debian_arch("ppc64le"), "ppc64el")


class ContainerFetchArchive(avocado.Test):
    def _make_image(self, image, digest=None, digests=None):
        spec = {"image": image}
        if digest:
            spec["digest"] = digest
        if digests:
            spec["digests"] = digests
        return containers.ContainerImage(spec, index=1)

    def _fake_fetch(self, dest_dir):
        """Return a fetch_container side_effect that writes a minimal tar."""
        import io, tarfile as tf

        def side_effect(image, arch, dest_path, manifest_digest=None, **kw):
            os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
            buf = io.BytesIO()
            with tf.open(fileobj=buf, mode="w"):
                pass
            with open(dest_path, "wb") as f:
                f.write(buf.getvalue())

        return side_effect

    def test_fetch_archive_places_content_addressed_tar(self):
        """fetch_archive() names the archive by digest and is idempotent."""
        from unittest.mock import patch
        import tempfile

        index_digest = "sha256:" + "a" * 64
        arch_digest = "sha256:" + "b" * 64
        img = self._make_image("docker.io/library/alpine:3.19", digest=index_digest)
        with tempfile.TemporaryDirectory() as d:
            with patch("seine.containers.fetch.resolve_container",
                       return_value=(index_digest, {"amd64": arch_digest})), \
                 patch("seine.containers.fetch.fetch_container",
                       side_effect=self._fake_fetch(d)):
                path = img.fetch_archive("amd64", d)
            self.assertTrue(path.endswith(".tar"))
            self.assertIn("amd64", path)
            self.assertIn("a" * 12, path)
            self.assertTrue(os.path.isfile(path))
            # Second call must reuse the cached file without fetching again.
            with patch("seine.containers.fetch.resolve_container",
                       return_value=(index_digest, {"amd64": arch_digest})) as mock_r, \
                 patch("seine.containers.fetch.fetch_container") as mock_f:
                path2 = img.fetch_archive("amd64", d)
            self.assertEqual(path, path2)
            mock_f.assert_not_called()

    def test_fetch_archive_accepts_per_arch_digest_as_pinned(self):
        """fetch_archive() accepts the per-arch manifest digest as the pinned value."""
        from unittest.mock import patch
        import tempfile

        index_digest = "sha256:" + "a" * 64
        arch_digest = "sha256:" + "b" * 64
        # User pins the per-arch digest in digests: mapping
        img = self._make_image("docker.io/library/alpine:3.19", digests={"amd64": arch_digest})
        with tempfile.TemporaryDirectory() as d:
            with patch("seine.containers.fetch.resolve_container",
                       return_value=(index_digest, {"amd64": arch_digest})), \
                 patch("seine.containers.fetch.fetch_container",
                       side_effect=self._fake_fetch(d)):
                path = img.fetch_archive("amd64", d)
            self.assertTrue(os.path.isfile(path))

    def test_fetch_archive_rejects_digest_mismatch(self):
        """fetch_archive() raises ValueError when neither resolved digest matches the pin."""
        from unittest.mock import patch
        import tempfile

        pinned = "sha256:" + "b" * 64
        index_digest = "sha256:" + "c" * 64
        arch_digest = "sha256:" + "d" * 64
        img = self._make_image("docker.io/library/alpine:3.19", digest=pinned)
        with tempfile.TemporaryDirectory() as d:
            with patch("seine.containers.fetch.resolve_container",
                       return_value=(index_digest, {"amd64": arch_digest})):
                with self.assertRaises(ValueError) as ctx:
                    img.fetch_archive("amd64", d)
        self.assertIn("does not match pinned digest", str(ctx.exception))


if __name__ == "__main__":
    avocado.main()
