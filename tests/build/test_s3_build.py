#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import io
import os
import sys
from unittest import mock
import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.build import BuildCmd
from seine.packages import Builder
from seine.sbuild import SbuildChroot
from seine.storage.s3 import S3StorageProvider
from seine.storage.local import LocalStorageProvider


class BuildCmdS3OptionsTest(avocado.Test):
    def test_build_cmd_s3_options_parsing(self):
        cmd = BuildCmd()
        argv = [
            "--s3-cache",
            "--s3-endpoint=http://192.168.1.100:3900",
            "--s3-bucket=my-cache",
            "--s3-region=garage",
            "--s3-offline-mode=strict",
            "--cache-rootfs",
            "dummy.yaml",
        ]
        with mock.patch.object(cmd, "load_all"), \
             mock.patch.object(cmd, "parse", return_value={}), \
             mock.patch.object(cmd, "build", return_value=0), \
             mock.patch("seine.build.collect_credentials"), \
             mock.patch("seine.build.locked"), \
             mock.patch("seine.build.remember"):
            with self.assertRaises(SystemExit) as cm:
                cmd.main(argv)
            self.assertEqual(cm.exception.code, 0)
            self.assertTrue(cmd.options["s3_cache"])
            self.assertEqual(cmd.options["s3_endpoint"], "http://192.168.1.100:3900")
            self.assertEqual(cmd.options["s3_bucket"], "my-cache")
            self.assertEqual(cmd.options["s3_region"], "garage")
            self.assertEqual(cmd.options["s3_offline_mode"], "strict")
            self.assertTrue(cmd.options["cache_rootfs"])

    def test_build_cmd_invalid_offline_mode_exits(self):
        cmd = BuildCmd()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                cmd.main(["--s3-offline-mode=invalid", "dummy.yaml"])
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("error: --s3-offline-mode must be 'fallback' or 'strict'", mock_err.getvalue())


class S3BuildIntegrationTest(avocado.Test):
    def test_builder_package_pull_hit(self):
        mock_provider = mock.MagicMock()
        mock_package = mock.MagicMock()
        mock_package.name = "mypkg"
        mock_package.depends = []

        distro = {"release": "trixie", "architecture": "amd64", "source": "debian"}
        options = {"s3_cache": True}

        builder = Builder(distro, options, None, storage_provider=mock_provider)
        with mock.patch.object(builder, "architectures", return_value=["amd64"]), \
             mock.patch.object(builder, "stamp") as mock_stamp, \
             mock.patch("os.path.isfile") as mock_isfile:
            # First check returns False (local miss), pull happens, second check returns True (hit)
            mock_stamp.return_value = "/tmp/fake/mypkg_amd64_deadbeef12345678"
            mock_isfile.side_effect = [False, True]

            stamps = builder.stamps([mock_package])
            self.assertEqual(len(stamps), 1)
            mock_provider.pull.assert_called_once()
            args, _ = mock_provider.pull.call_args
            self.assertEqual(args[0], "packages")
            self.assertIn("trixie/amd64/mypkg/deadbeef12345678", args[1])

    def test_sbuild_chroot_pull_hit(self):
        mock_provider = mock.MagicMock()
        distro = {"release": "trixie", "architecture": "amd64", "uri": "http://deb.debian.org/debian"}
        options = {"s3_cache": True}

        chroot = SbuildChroot(distro, options, "amd64")
        chroot.storage_provider = mock_provider

        builder_image = mock.MagicMock()

        with mock.patch.object(chroot, "current", side_effect=[False, True]), \
             mock.patch.object(SbuildChroot, "path", new_callable=mock.PropertyMock, return_value="/tmp/fake/trixie-amd64.tar.zst"), \
             mock.patch.object(SbuildChroot, "inputs", new_callable=mock.PropertyMock, return_value="/tmp/fake/trixie-amd64.inputs"), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("builtins.open", mock.mock_open()):
            res = chroot._create(builder_image)
            self.assertEqual(res, chroot)
            mock_provider.pull.assert_called_once_with("chroots", "trixie-amd64", "/tmp/fake/trixie-amd64.tar.zst")
            builder_image.exec.assert_not_called()

    def test_image_rootfs_pull_hit(self):
        from seine.image import Image
        mock_provider = mock.MagicMock()
        mock_provider.pull.return_value = "/tmp/fake/rootfs.tar"

        options = {"cache_rootfs": True, "s3_cache": True, "verbose": False}
        spec = {"distribution": {"release": "trixie", "architecture": "amd64"}}

        img = Image(None, options)
        img.spec = spec
        img.storage_provider = mock_provider
        img._rootfs = "/tmp/fake/rootfs.tar"

        with mock.patch("seine.utils.digest_file_current", side_effect=[False, True]), \
             mock.patch("seine.utils.write_digest_file"):
            hit = img._rootfs_current("digest123")
            self.assertTrue(hit)
            mock_provider.pull.assert_called_once_with("rootfs", "trixie-amd64/digest123", "/tmp/fake/rootfs.tar")

    def test_image_rootfs_export_pushes_when_cache_rootfs(self):
        from seine.image import Image
        mock_provider = mock.MagicMock()

        options = {"cache_rootfs": True, "s3_cache": True, "verbose": False}
        spec = {"distribution": {"release": "trixie", "architecture": "amd64"}}

        img = Image(None, options)
        img.spec = spec
        img.storage_provider = mock_provider
        img._rootfs = os.path.join(self.workdir, "rootfs.tar")

        with mock.patch("seine.container.ContainerEngine.run"), \
             mock.patch.object(img, "_exported"), \
             mock.patch.object(img, "_normalize_timestamps"), \
             mock.patch("seine.utils.invalidate_digest_file"), \
             mock.patch("os.replace"), \
             mock.patch("seine.utils.write_digest_file"):
            img._export("digest456", recipe=[("distro", "trixie")])
            self.assertEqual(mock_provider.push.call_count, 2)
            args, kwargs = mock_provider.push.call_args_list[0]
            self.assertEqual(args[0], "rootfs")
            self.assertEqual(args[1], "trixie-amd64/digest456")
            self.assertEqual(kwargs["recipe"], [("distro", "trixie")])

    def test_image_rootfs_export_skips_unversioned_on_push_failure(self):
        from seine.image import Image
        mock_provider = mock.MagicMock()
        mock_provider.push.return_value = False

        options = {"cache_rootfs": True, "s3_cache": True, "verbose": False}
        spec = {"distribution": {"release": "trixie", "architecture": "amd64"}}

        img = Image(None, options)
        img.spec = spec
        img.storage_provider = mock_provider
        img._rootfs = os.path.join(self.workdir, "rootfs_fail.tar")

        with mock.patch("seine.container.ContainerEngine.run"), \
             mock.patch.object(img, "_exported"), \
             mock.patch.object(img, "_normalize_timestamps"), \
             mock.patch("seine.utils.invalidate_digest_file"), \
             mock.patch("os.replace"), \
             mock.patch("seine.utils.write_digest_file"):
            img._export("digest789", recipe=[("distro", "trixie")])
            self.assertEqual(mock_provider.push.call_count, 1)
            args, _ = mock_provider.push.call_args
            self.assertEqual(args[1], "trixie-amd64/digest789")
