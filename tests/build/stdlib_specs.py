#!/usr/bin/env python3

import glob
import os
import subprocess
import sys
import yaml

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import stdlib
from seine.build import BuildCmd


class TestStdlibSpecs(avocado.Test):
    def setUp(self):
        super().setUp()
        self.repo_root = os.path.dirname(os.path.dirname(os.path.dirname(path_to_self)))
        self.stdlib_dir = os.path.join(self.repo_root, "stdlib")

    def stdlib_spec_files(self):
        pattern = os.path.join(self.stdlib_dir, "**", "*.yml")
        files = glob.glob(pattern, recursive=True)
        self.assertGreater(len(files), 0, "No stdlib specifications found")
        return sorted(files)

    def test_all_stdlib_specs_are_valid_yaml(self):
        for path in self.stdlib_spec_files():
            with open(path, "r", encoding="utf-8") as f:
                content = yaml.safe_load(f)
            self.assertIsInstance(content, dict, f"{path} did not parse as a YAML mapping")

    def test_all_stdlib_specs_resolve_and_load_individually(self):
        for path in self.stdlib_spec_files():
            rel = os.path.relpath(path, self.stdlib_dir)
            ref = f"stdlib:{rel}"
            resolved = stdlib.resolve(ref)
            self.assertEqual(os.path.realpath(resolved), os.path.realpath(path))

            build = BuildCmd()
            spec = build.load(ref)
            self.assertIsInstance(spec, dict, f"Failed to load spec {ref}")

    def test_debian_architecture_specs(self):
        build = BuildCmd()
        amd64 = build.load("stdlib:debian/amd64.yml")
        self.assertEqual(amd64.get("distribution", {}).get("architecture"), "amd64")
        self.assertNotIn("architectures", amd64.get("distribution", {}))
        self.assertEqual(amd64.get("imager", {}).get("kernel"), "linux-image-amd64")

        build = BuildCmd()
        arm64 = build.load("stdlib:debian/arm64.yml")
        self.assertEqual(arm64.get("distribution", {}).get("architecture"), "arm64")
        self.assertNotIn("architectures", arm64.get("distribution", {}))
        self.assertEqual(arm64.get("imager", {}).get("kernel"), "linux-image-arm64")

    def test_debian_base_and_feeds_specs(self):
        build = BuildCmd()
        base = build.load("stdlib:debian/base.yml")
        self.assertFalse(base.get("defaults", {}).get("apt", {}).get("install_recommends", True))

        build = BuildCmd()
        feeds_spec = build.load("stdlib:debian/feeds.yml")
        suites = [feed.get("suite") for feed in feeds_spec.get("distribution", {}).get("feeds", [])]
        self.assertIn("bookworm", suites)
        self.assertIn("bookworm-updates", suites)
        self.assertIn("bookworm-security", suites)
        self.assertIn("trixie", suites)
        self.assertIn("trixie-updates", suites)
        self.assertIn("trixie-security", suites)

    def test_debian_release_specs_include_base_and_feeds(self):
        build = BuildCmd()
        bookworm = build.load("stdlib:debian/bookworm.yml")
        self.assertEqual(bookworm.get("distribution", {}).get("release"), "bookworm")
        suites = [feed.get("suite") for feed in bookworm.get("distribution", {}).get("feeds", [])]
        self.assertIn("bookworm-backports", suites)
        self.assertIn("bookworm", suites)
        self.assertFalse(bookworm.get("defaults", {}).get("apt", {}).get("install_recommends", True))

        build = BuildCmd()
        trixie = build.load("stdlib:debian/trixie.yml")
        self.assertEqual(trixie.get("distribution", {}).get("release"), "trixie")
        suites = [feed.get("suite") for feed in trixie.get("distribution", {}).get("feeds", [])]
        self.assertIn("trixie", suites)
        self.assertFalse(trixie.get("defaults", {}).get("apt", {}).get("install_recommends", True))

    def test_configuration_and_service_specs(self):
        build = BuildCmd()
        locales = build.load("stdlib:debian/locales.yml")
        self.assertEqual(locales.get("overrides", {}).get("locales"), ["en"])

        build = BuildCmd()
        timezone = build.load("stdlib:debian/timezone.yml")
        self.assertTrue(any(play.get("name") == "set the system timezone" for play in timezone.get("playbook", [])))

        build = BuildCmd()
        sysctl = build.load("stdlib:debian/sysctl.yml")
        self.assertTrue(any(play.get("name") == "tune kernel parameters" for play in sysctl.get("playbook", [])))

        build = BuildCmd()
        networkd = build.load("stdlib:services/networkd.yml")
        play_names = [play.get("name") for play in networkd.get("playbook", [])]
        self.assertIn("install network packages", play_names)
        self.assertIn("configure network via systemd-networkd and systemd-resolved", play_names)

        build = BuildCmd()
        sshd = build.load("stdlib:services/sshd.yml")
        self.assertTrue(any(play.get("name") == "install and harden sshd" for play in sshd.get("playbook", [])))
        libdir = os.path.realpath(os.path.join(self.stdlib_dir, "services", "library"))
        self.assertIn(libdir, build.options["ansible_library"])

    def test_extends_golang_spec(self):
        build = BuildCmd()
        golang = build.load("stdlib:extends/golang.yml")
        go_cfg = golang.get("defaults", {}).get("extends", {}).get("go", {})
        self.assertTrue(go_cfg.get("license-scan"))
        self.assertIn("amd64", go_cfg.get("toolchain-sha256", {}))
        self.assertIn("arm64", go_cfg.get("toolchain-sha256", {}))

    def test_full_spec_composition(self):
        build = BuildCmd()
        spec = build.load_all([
            "stdlib:debian/amd64.yml",
            "stdlib:debian/bookworm.yml",
            "stdlib:debian/locales.yml",
            "stdlib:debian/timezone.yml",
            "stdlib:debian/sysctl.yml",
            "stdlib:services/networkd.yml",
            "stdlib:services/sshd.yml",
            "stdlib:extends/golang.yml",
        ])
        self.assertEqual(spec["distribution"]["architecture"], "amd64")
        self.assertEqual(spec["distribution"]["release"], "bookworm")
        self.assertIn("grub-efi-amd64", str(spec["playbook"]))
        self.assertIn("openssh-server", str(spec["playbook"]))

    def test_sshd_config_module_unit_test(self):
        script = os.path.join(self.stdlib_dir, "services", "library", "test_sshd_config.py")
        workdir = os.path.dirname(script)
        res = subprocess.run([sys.executable, script], cwd=workdir, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"test_sshd_config.py failed: {res.stderr}")
        self.assertIn("ok", res.stdout)

    def test_no_credentials_in_stdlib(self):
        suspicious = ["BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY", "BEGIN PGP PRIVATE KEY", "password:"]
        for path in self.stdlib_spec_files():
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            for token in suspicious:
                self.assertNotIn(token, content, f"Found credential pattern '{token}' in {path}")


if __name__ == "__main__":
    avocado.main()
