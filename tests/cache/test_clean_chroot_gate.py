#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import io
import os
import sys
import tarfile

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.insert(0, path_to_sources)

from seine.cache import CleanChrootViolation, check_clean_chroot


class CleanChrootGateTest(avocado.Test):
    def _create_tar(self, filename, members_dict):
        tar_path = os.path.join(self.workdir, filename)
        with tarfile.open(tar_path, "w") as tar:
            for name, content in members_dict.items():
                ti = tarfile.TarInfo(name)
                ti.size = len(content)
                tar.addfile(ti, io.BytesIO(content))
        return tar_path

    def test_clean_tar_passes(self):
        tar_path = self._create_tar("clean.tar", {
            "bin/sh": b"#!/bin/sh",
            "etc/hostname": b"my-host\n",
            "etc/ssl/certs/ca-certificates.crt": b"BEGIN CERTIFICATE",
            "etc/ssh/ssh_host_rsa_key.pub": b"ssh-rsa AAAAB3NzaC1yc2E...",
        })
        self.assertTrue(check_clean_chroot(tar_path))

    def test_ssh_private_key_rejected(self):
        tar_path = self._create_tar("bad_ssh.tar", {
            "etc/ssh/ssh_host_ed25519_key": b"-----BEGIN OPENSSH PRIVATE KEY-----",
        })
        with self.assertRaises(CleanChrootViolation) as caught:
            check_clean_chroot(tar_path)
        self.assertIn("ssh_host_ed25519_key", str(caught.exception))

    def test_user_ssh_private_key_rejected(self):
        tar_path = self._create_tar("bad_id.tar", {
            "root/.ssh/id_rsa": b"-----BEGIN RSA PRIVATE KEY-----",
        })
        with self.assertRaises(CleanChrootViolation) as caught:
            check_clean_chroot(tar_path)
        self.assertIn("id_rsa", str(caught.exception))

    def test_ssl_private_key_rejected(self):
        tar_path = self._create_tar("bad_ssl.tar", {
            "etc/ssl/private/server.key": b"PRIVATE KEY DATA",
        })
        with self.assertRaises(CleanChrootViolation) as caught:
            check_clean_chroot(tar_path)
        self.assertIn("etc/ssl/private/server.key", str(caught.exception))

    def test_ssl_private_directory_allowed(self):
        tar_path = self._create_tar("ssl_dir.tar", {
            "etc/ssl/private/": b"",
            "usr/share/doc/package": b"doc content",
        })
        self.assertTrue(check_clean_chroot(tar_path))

    def test_credential_files_rejected(self):
        tar_path = self._create_tar("bad_netrc.tar", {
            "etc/default/grub": b"GRUB_TIMEOUT=5",
            ".netrc": b"machine repo.example.com login alice password s3cr3t",
        })
        with self.assertRaises(CleanChrootViolation) as caught:
            check_clean_chroot(tar_path)
        self.assertIn(".netrc", str(caught.exception))

    def test_spec_redacted_path_rejected(self):
        tar_path = self._create_tar("bad_custom.tar", {
            "etc/custom_secret.conf": b"token=12345",
        })
        spec = {"redact": [r"custom_secret\.conf"]}
        with self.assertRaises(CleanChrootViolation) as caught:
            check_clean_chroot(tar_path, spec=spec)
        self.assertIn("custom_secret.conf", str(caught.exception))

    def test_spec_secret_content_rejected(self):
        tar_path = self._create_tar("bad_content.tar", {
            "etc/app/config.ini": b"api_token = SUPER_CONFIDENTIAL_KEY_999\n",
        })
        spec = {"redact": [r"SUPER_CONFIDENTIAL_KEY_999"]}
        with self.assertRaises(CleanChrootViolation) as caught:
            check_clean_chroot(tar_path, spec=spec)
        self.assertIn("secret detected", str(caught.exception))

    def test_clean_directory_passes(self):
        clean_dir = os.path.join(self.workdir, "clean_rootfs")
        os.makedirs(os.path.join(clean_dir, "etc", "ssl", "certs"), exist_ok=True)
        with open(os.path.join(clean_dir, "etc", "hostname"), "w") as f:
            f.write("box\n")
        self.assertTrue(check_clean_chroot(clean_dir))

    def test_dirty_directory_rejected(self):
        dirty_dir = os.path.join(self.workdir, "dirty_rootfs")
        os.makedirs(os.path.join(dirty_dir, "etc", "ssl", "private"), exist_ok=True)
        with open(os.path.join(dirty_dir, "etc", "ssl", "private", "key.pem"), "w") as f:
            f.write("SECRET\n")
        with self.assertRaises(CleanChrootViolation):
            check_clean_chroot(dirty_dir)
