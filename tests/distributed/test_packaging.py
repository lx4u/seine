# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import re
import shutil
import subprocess

from avocado import Test

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(*path):
    with open(os.path.join(ROOT, *path), encoding="utf-8") as f:
        return f.read()


def _directives(unit):
    """Return the unit's lines as a dict of key -> list of values."""
    out = {}
    for line in unit.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")) and "=" in line:
            key, _, value = line.partition("=")
            out.setdefault(key, []).append(value)
    return out


def _active_lines(text):
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


class PackagingTest(Test):
    """Systemd units, defaults and control data of the distributed packages."""

    def test_no_dev_token_left(self):
        for top in ("debian", "docs"):
            for base, _dirs, files in os.walk(os.path.join(ROOT, top)):
                if "_build" in base:
                    continue
                for name in files:
                    path = os.path.join(base, name)
                    try:
                        with open(path, encoding="utf-8") as f:
                            text = f.read()
                    except (UnicodeDecodeError, OSError):
                        continue
                    self.assertNotIn("seine-dev-enrollment-token", text, path)

    def test_units_run_as_dedicated_users(self):
        agent = _directives(_read("debian", "seine-agent.service"))
        server = _directives(_read("debian", "seine-server.service"))
        self.assertEqual(agent["User"], ["seine-agent"])
        self.assertEqual(agent["Group"], ["seine-agent"])
        self.assertEqual(server["User"], ["seine"])
        self.assertEqual(server["Group"], ["seine"])
        for unit in (agent, server):
            self.assertNotIn("root", unit["User"])

    def test_server_unit_is_hardened(self):
        server = _directives(_read("debian", "seine-server.service"))
        self.assertEqual(server["NoNewPrivileges"], ["yes"])
        self.assertEqual(server["ProtectSystem"], ["strict"])
        self.assertEqual(server["ReadWritePaths"], ["/var/lib/seine"])
        self.assertEqual(server["StateDirectory"], ["seine"])
        self.assertEqual(server["CapabilityBoundingSet"], [""])
        self.assertIn("--config /etc/seine/server.yaml", server["ExecStart"][0])

    def test_agent_unit_keeps_rootless_podman_working(self):
        agent = _directives(_read("debian", "seine-agent.service"))
        for name in ("NoNewPrivileges", "RestrictSUIDSGID"):
            self.assertNotIn(name, agent)
        self.assertNotEqual(agent.get("ProtectSystem"), ["strict"])
        self.assertEqual(agent["StateDirectory"], ["seine-agent"])
        self.assertIn("--work-dir /var/lib/seine-agent", agent["ExecStart"][0])
        self.assertEqual(agent["Environment"], ["XDG_RUNTIME_DIR=/run/seine-agent"])
        self.assertEqual(agent["KillMode"], ["mixed"])
        self.assertGreaterEqual(int(agent["TimeoutStopSec"][0]), 60)

    def test_defaults_have_no_token_and_no_public_bind(self):
        for name in ("seine-server.default", "seine-agent.default"):
            active = _active_lines(_read("debian", name))
            self.assertFalse([ln for ln in active if ln.startswith("SEINE_ENROLLMENT_TOKEN")], name)
        server = _active_lines(_read("debian", "seine-server.default"))
        self.assertIn("SEINE_HOST=127.0.0.1", server)
        self.assertFalse([ln for ln in server if "0.0.0.0" in ln])
        yaml_active = _active_lines(_read("debian", "seine-server.yaml"))
        self.assertFalse([ln for ln in yaml_active if ln.startswith("enrollment_token")])

    def test_users_and_groups(self):
        server = _read("debian", "seine-server.sysusers")
        agent = _read("debian", "seine-agent.sysusers")
        self.assertRegex(server, r"(?m)^u seine - .* /var/lib/seine /usr/sbin/nologin$")
        self.assertRegex(agent, r"(?m)^u seine-agent - .* /var/lib/seine-agent /usr/sbin/nologin$")
        self.assertRegex(agent, r"(?m)^m seine-agent kvm$")
        self.assertIn("dh_installsysusers", _read("debian", "rules"))
        self.assertIn("root seine -", _read("debian", "seine-server.tmpfiles"))
        self.assertIn("root seine-agent -", _read("debian", "seine-agent.tmpfiles"))

    def test_agent_postinst_allocates_subids_idempotently(self):
        postinst = _read("debian", "seine-agent.postinst")
        self.assertIn("#DEBHELPER#", postinst)
        for kind in ("subuid", "subgid"):
            self.assertRegex(postinst, rf"grep -q '\^seine-agent:' /etc/{kind} \|\| usermod --add-{kind}s ")

    def test_control_depends(self):
        control = _read("debian", "control")
        stanzas = {}
        for block in control.split("\n\n"):
            match = re.search(r"(?m)^Package: (\S+)", block)
            if match:
                stanzas[match.group(1)] = block
        self.assertRegex(stanzas["seine-agent"], r"(?m)^\s+uidmap\b")
        self.assertIn("seine (= ${binary:Version})", stanzas["seine-agent"])
        self.assertIn("Architecture: all", stanzas["seine-server"])

    def test_remote_client_and_agent_install_their_models_dependency(self):
        setup = _read("setup.py")
        for extra in ("server", "agent", "remote"):
            line = re.search(rf"'{extra}': \[([^\]]*)\]", setup).group(1)
            self.assertIn("'pydantic'", line, f"extra '{extra}' lacks pydantic")
        control = _read("debian", "control")
        recommends = re.search(r"(?ms)^Package: seine\n.*?^Recommends:(.*?)^Suggests:", control).group(1)
        for package in ("python3-pydantic", "python3-websockets"):
            self.assertIn(package, recommends)

    def test_systemd_analyze_verify(self):
        if not shutil.which("systemd-analyze"):
            self.cancel("systemd-analyze not available")
        for name in ("seine-server.service", "seine-agent.service"):
            res = subprocess.run(
                ["systemd-analyze", "verify", os.path.join(ROOT, "debian", name)],
                capture_output=True, text=True,
            )
            # The binaries are not installed on a development machine.
            problems = [ln for ln in res.stderr.splitlines() if ln and "is not executable" not in ln]
            self.assertEqual(problems, [], name)
