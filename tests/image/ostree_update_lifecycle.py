#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import http.server
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import boto3

import avocado

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)
sys.path.append(os.path.dirname(path_to_self))

from update_guest import AGENT, ESP, PORT, UNHEALTHY, UpdateGuest

# Evolution packages across 9 snapshot versions
EXTRA_V2 = """
playbook:
    - name: v2 packages
      wave: packages
      after: [base]
      tasks:
          - name: install htop and strace
            apt:
                name: [htop, strace]
                state: present
"""

EXTRA_V3 = """
playbook:
    - name: v3 snapshot churn
      wave: packages
      after: [base]
      tasks:
          - name: install htop and strace
            apt:
                name: [htop, strace]
                state: present
"""

EXTRA_V4 = """
playbook:
    - name: v4 daemon
      wave: packages
      after: [base]
      tasks:
          - name: install htop, strace and chrony
            apt:
                name: [htop, strace, chrony]
                state: present
"""

EXTRA_V5 = """
playbook:
    - name: v5 prune htop
      wave: packages
      after: [base]
      tasks:
          - name: install strace and chrony
            apt:
                name: [strace, chrony]
                state: present
"""

EXTRA_V6 = """
playbook:
    - name: v6 python
      wave: packages
      after: [base]
      tasks:
          - name: install strace, chrony and python3-minimal
            apt:
                name: [strace, chrony, python3-minimal]
                state: present
"""

EXTRA_V7 = """
playbook:
    - name: v7 replace chrony with timesyncd drop-in
      wave: packages
      after: [base]
      tasks:
          - name: install strace and python3-minimal
            apt:
                name: [strace, python3-minimal]
                state: present
          - name: create timesyncd drop-in directory
            file:
                path: /etc/systemd/timesyncd.conf.d
                state: directory
          - name: configure timesyncd drop-in
            copy:
                dest: /etc/systemd/timesyncd.conf.d/test.conf
                content: "[Time]\\nNTP=pool.ntp.org\\n"
"""

EXTRA_V8 = """
playbook:
    - name: v8 snapshot churn
      wave: packages
      after: [base]
      tasks:
          - name: install strace and python3-minimal
            apt:
                name: [strace, python3-minimal]
                state: present
          - name: create timesyncd drop-in directory
            file:
                path: /etc/systemd/timesyncd.conf.d
                state: directory
          - name: configure timesyncd drop-in
            copy:
                dest: /etc/systemd/timesyncd.conf.d/test.conf
                content: "[Time]\\nNTP=pool.ntp.org\\n"
"""

EXTRA_V9 = """
playbook:
    - name: v9 app and iperf3
      wave: packages
      after: [base]
      tasks:
          - name: install strace, python3-minimal and iperf3
            apt:
                name: [strace, python3-minimal, iperf3]
                state: present
          - name: create timesyncd drop-in directory
            file:
                path: /etc/systemd/timesyncd.conf.d
                state: directory
          - name: configure timesyncd drop-in
            copy:
                dest: /etc/systemd/timesyncd.conf.d/test.conf
                content: "[Time]\\nNTP=pool.ntp.org\\n"
          - name: install custom app binary
            copy:
                dest: /usr/bin/myapp
                content: "#!/bin/sh\\necho 'myapp v9 running'\\n"
                mode: "0755"
"""


class GarageGatewayHandler(http.server.BaseHTTPRequestHandler):
    STRIP_HEADERS = {
        "connection",
        "upgrade",
        "http2-settings",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailers",
        "host",
        "if-modified-since",
        "if-none-match",
    }

    def log_message(self, *args):
        pass

    def do_GET(self):
        # Fault injection: simulate network drop mid-transfer
        if getattr(self.server, "fault_network_drop", False):
            self.send_response(200)
            self.send_header("Content-Length", "10485760")
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"PARTIAL")
            self.wfile.flush()
            return

        # Fault injection: payload tampering on deltas
        if getattr(self.server, "fault_tamper_delta", False) and ".delta" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"CORRUPTED_DELTA_DATA")
            return

        target_url = f"http://127.0.0.1:{self.server.garage_web_port}{self.path}"
        headers = {
            "Host": "updates.web.garage",
            **{
                k: v
                for k, v in self.headers.items()
                if k.lower() not in self.STRIP_HEADERS
            },
        }
        req = urllib.request.Request(target_url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                shutil.copyfileobj(resp, self.wfile)
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code != 404:
                sys.stderr.write(f"\n[GATEWAY ERROR] {e.code} for {self.path}: {body.decode(errors='replace')}\n")
                sys.stderr.flush()
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(body)


class UpdateLifecycle(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    def setUp(self):
        self.token = os.urandom(4).hex()
        self.testbed = f"/var/tmp/seine-test-run-{self.token}"
        self.podman_storage = os.path.join(self.testbed, "containers", "storage")
        self.podman_run = os.path.join(self.testbed, "containers", "run")
        self.garage_meta = os.path.join(self.testbed, "garage", "meta")
        self.garage_data = os.path.join(self.testbed, "garage", "data")
        self.garage_conf = os.path.join(self.testbed, "garage.toml")

        self.cid = None
        self.s3_client = None
        self.proxy_server = None

        super().setUp()

    def tearDown(self):
        if self.proxy_server:
            self.proxy_server.shutdown()
            self.proxy_server.server_close()
            self.proxy_server = None

        if self.cid:
            # Kill and remove the ephemeral Garage container
            subprocess.run([
                "podman", "--root", self.podman_storage, "--runroot", self.podman_run,
                "kill", self.cid
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([
                "podman", "--root", self.podman_storage, "--runroot", self.podman_run,
                "rm", "-f", self.cid
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.cid = None

        if hasattr(self, "testbed") and os.path.exists(self.testbed):
            shutil.rmtree(self.testbed, ignore_errors=True)

        super().tearDown()

    def serve(self):
        os.makedirs(self.podman_storage, exist_ok=True)
        os.makedirs(self.podman_run, exist_ok=True)
        os.makedirs(self.garage_meta, exist_ok=True)
        os.makedirs(self.garage_data, exist_ok=True)
        # Configure and launch isolated Garage container in Podman
        rpc_secret = os.urandom(32).hex()
        toml_content = f"""
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1

rpc_bind_addr = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "{rpc_secret}"

[s3_api]
api_bind_addr = "[::]:3900"
s3_region = "garage"

[s3_web]
bind_addr = "[::]:3902"
root_domain = ".web.garage"
index = "summary"
"""
        with open(self.garage_conf, "w") as f:
            f.write(toml_content)

        # Load cached image into isolated storage
        p_save = subprocess.Popen(["podman", "save", "docker.io/dxflrs/garage:v1.1.0"],
                                  stdout=subprocess.PIPE)
        p_load = subprocess.Popen([
            "podman", "--root", self.podman_storage, "--runroot", self.podman_run, "load"
        ], stdin=p_save.stdout, stdout=subprocess.PIPE)
        p_save.stdout.close()
        p_load.communicate()

        # Run container on ephemeral host ports
        run_cmd = [
            "podman", "--root", self.podman_storage, "--runroot", self.podman_run,
            "run", "-d", "--name", f"seine-garage-{self.token}",
            "-p", "127.0.0.1::3900",
            "-p", "127.0.0.1::3902",
            "-v", f"{self.garage_conf}:/etc/garage.toml:ro",
            "-v", f"{self.garage_meta}:/var/lib/garage/meta:U",
            "-v", f"{self.garage_data}:/var/lib/garage/data:U",
            "docker.io/dxflrs/garage:v1.1.0",
            "/garage", "server"
        ]
        self.cid = subprocess.check_output(run_cmd).decode().strip()
        time.sleep(2)

        def exec_garage(*args):
            return subprocess.check_output([
                "podman", "--root", self.podman_storage, "--runroot", self.podman_run,
                "exec", f"seine-garage-{self.token}", "/garage"
            ] + list(args)).decode()

        # Initialize node layout and access keys
        status = exec_garage("status")
        node_id = [line.split()[0] for line in status.splitlines() if "127.0.0.1:3901" in line][0]
        exec_garage("layout", "assign", node_id, "-z", "dc1", "-c", "10G")
        exec_garage("layout", "apply", "--version", "1")

        key_out = exec_garage("key", "create", "test-key")
        lines = key_out.splitlines()
        ak = [l.split(":")[1].strip() for l in lines if "Key ID:" in l][0]
        sk = [l.split(":")[1].strip() for l in lines if "Secret key:" in l][0]

        exec_garage("bucket", "create", "updates")
        exec_garage("bucket", "allow", "updates", "--read", "--write", "--owner", f"--key={ak}")
        exec_garage("bucket", "website", "--allow", "updates")

        # Discover dynamic host ports
        port_lines = subprocess.check_output([
            "podman", "--root", self.podman_storage, "--runroot", self.podman_run,
            "port", f"seine-garage-{self.token}"
        ]).decode().splitlines()

        s3_port = int([p.split(":")[-1] for p in port_lines if "3900/tcp" in p][0])
        web_port = int([p.split(":")[-1] for p in port_lines if "3902/tcp" in p][0])

        self.s3_client = boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{s3_port}",
            region_name="garage",
            aws_access_key_id=ak,
            aws_secret_access_key=sk
        )

        # Start reverse proxy forwarding to s3_web
        try:
            self.server = http.server.ThreadingHTTPServer(("0.0.2.2", PORT), GarageGatewayHandler)
        except OSError:
            try:
                self.server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), GarageGatewayHandler)
            except OSError:
                self.cancel(f"port {PORT} is in use")

        self.server.garage_web_port = web_port
        self.server.fault_network_drop = False
        self.server.fault_tamper_delta = False
        self.proxy_server = self.server
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def sync_payload(self):
        # Sync all local payload files into the Garage updates bucket
        for root, _, files in os.walk(self.payload):
            for name in files:
                full_path = os.path.join(root, name)
                rel_path = os.path.relpath(full_path, self.payload)
                with open(full_path, "rb") as fp:
                    self.s3_client.put_object(Bucket="updates", Key=rel_path, Body=fp.read())

    def apply_user_edits(self):
        # 1. Modified existing config: /etc/systemd/journald.conf.d/99-user.conf
        self.sh("mkdir -p /etc/systemd/journald.conf.d && "
                "printf '[Journal]\\nStorage=persistent\\nSystemMaxUse=100M\\n' "
                "> /etc/systemd/journald.conf.d/99-user.conf")
        # 2. Modified service config: /etc/ssh/sshd_config.d/99-user.conf
        self.sh("mkdir -p /etc/ssh/sshd_config.d && "
                "printf 'Port 2222\\nPasswordAuthentication no\\n' "
                "> /etc/ssh/sshd_config.d/99-user.conf")
        # 3. Custom untracked file: /etc/sysctl.d/99-device.conf
        self.sh("mkdir -p /etc/sysctl.d && "
                "printf 'net.ipv4.ip_forward=1\\n' "
                "> /etc/sysctl.d/99-device.conf")
        # 4. Deleted file: /etc/motd
        self.sh("rm -f /etc/motd")

    def assert_user_edits_intact(self):
        self.assertEqual(
            self.sh("cat /etc/systemd/journald.conf.d/99-user.conf").strip(),
            "[Journal]\nStorage=persistent\nSystemMaxUse=100M"
        )
        self.assertEqual(
            self.sh("cat /etc/ssh/sshd_config.d/99-user.conf").strip(),
            "Port 2222\nPasswordAuthentication no"
        )
        self.assertEqual(
            self.sh("cat /etc/sysctl.d/99-device.conf").strip(),
            "net.ipv4.ip_forward=1"
        )
        self.sh("test ! -e /etc/motd")
        diff = self.sh("ostree admin config-diff")
        self.assertNotIn(".ostree-merge-conflict", diff)

    def assert_package_state(self, version):
        if version == "1":
            self.sh("test ! -e /usr/bin/htop")
            self.sh("test ! -e /usr/bin/strace")
        elif version in ("2", "3", "4"):
            self.sh("test -x /usr/bin/htop")
            self.sh("test -x /usr/bin/strace")
            if version == "4":
                self.sh("test -x /usr/sbin/chronyd || test -x /usr/bin/chronyd")
        elif version in ("5", "5.1"):
            self.sh("test ! -e /usr/bin/htop")
            self.sh("test -x /usr/bin/strace")
        elif version == "6":
            self.sh("test ! -e /usr/bin/htop")
            self.sh("test -x /usr/bin/strace")
            self.sh("test -x /usr/bin/python3")
        elif version in ("7", "8"):
            self.sh("test ! -e /usr/bin/htop")
            self.sh("test -x /usr/bin/strace")
            self.sh("test -x /usr/bin/python3")
            self.sh("test -f /etc/systemd/timesyncd.conf.d/test.conf")
        elif version == "9":
            self.sh("test ! -e /usr/bin/htop")
            self.sh("test -x /usr/bin/strace")
            self.sh("test -x /usr/bin/python3")
            self.sh("test -x /usr/bin/iperf3")
            self.sh("test -f /etc/systemd/timesyncd.conf.d/test.conf")
            self.assertEqual(self.sh("/usr/bin/myapp").strip(), "myapp v9 running")

    def settle(self):
        self.sh(f"{AGENT} reconcile")
        self.assert_invariants()

    def test_multi_month_snapshot_lifecycle(self):
        # Build factory baseline (v1) and sync to Garage
        factory_disk = self.build("1")
        self.sync_payload()

        # Start guest from factory image and apply user edits
        self.start(factory_disk)
        self.settle()
        self.assertEqual(self.booted_version(), "1")
        self.assert_package_state("1")
        self.apply_user_edits()
        self.assert_user_edits_intact()

        # v2: Add htop, strace with static delta from v1
        os.remove(self.build("2", extra=EXTRA_V2, deltas_from=["1"]))
        self.sync_payload()
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "2"))
        self.reboot()
        self.assertEqual(self.booted_version(), "2")
        self.wait_until(f"test -e {ESP}/debian-2.efi")
        self.settle()
        self.assert_package_state("2")
        self.assert_user_edits_intact()

        # v3: Snapshot churn update with delta from v2
        os.remove(self.build("3", extra=EXTRA_V3, deltas_from=["2"]))
        self.sync_payload()
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "3"))
        self.reboot()
        self.assertEqual(self.booted_version(), "3")
        self.wait_until(f"test -e {ESP}/debian-3.efi")
        self.settle()
        self.assert_package_state("3")
        self.assert_user_edits_intact()

        # v4: Add daemon chrony with delta from v3
        os.remove(self.build("4", extra=EXTRA_V4, deltas_from=["3"]))
        self.sync_payload()
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "4"))
        self.reboot()
        self.assertEqual(self.booted_version(), "4")
        self.wait_until(f"test -e {ESP}/debian-4.efi")
        self.settle()
        self.assert_package_state("4")
        self.assert_user_edits_intact()

        # Catastrophic failure test: poisoned v5 release rolls back to v4
        poisoned_v5 = EXTRA_V4 + UNHEALTHY
        os.remove(self.build("5", extra=poisoned_v5, deltas_from=["4"]))
        self.sync_payload()
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "5"))
        for _ in range(3):
            self.reboot()
            self.assertEqual(self.booted_version(), "5")
        # Watchdog / try counter trips and falls back to v4
        self.reboot()
        self.assertEqual(self.booted_version(), "4")
        self.wait_until(f"test ! -e {ESP}/debian-5+0-3.efi")
        self.settle()
        status = self.sh("cat /var/lib/seine-update/status")
        self.assertIn("result=failed-update", status)
        self.assertIn("version=5", status)

        # Denied retry for failed version 5
        res = self.update()
        self.assertEqual(res["result"], "nothing")
        self.assertIn("denied", res["message"])

        # Hotfix v5.1: prune htop and clean up failed state
        os.remove(self.build("5.1", extra=EXTRA_V5, deltas_from=["4"]))
        self.sync_payload()
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "5.1"))
        self.reboot()
        self.assertEqual(self.booted_version(), "5.1")
        self.wait_until(f"test -e {ESP}/debian-5.1.efi")
        self.settle()
        self.assert_package_state("5.1")
        self.assert_user_edits_intact()

        # Interrupted network test during v6 delta fetch
        os.remove(self.build("6", extra=EXTRA_V6, deltas_from=["5.1"]))
        self.sync_payload()

        self.server.fault_network_drop = True
        status, _ = self.guest.run(AGENT, 300)
        self.assertNotEqual(status, 0)
        self.server.fault_network_drop = False
        # Running system remains unharmed on v5.1
        self.assertEqual(self.booted_version(), "5.1")
        self.settle()

        # Network restored: update resumes and completes cleanly
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "6"))
        self.reboot()
        self.assertEqual(self.booted_version(), "6")
        self.wait_until(f"test -e {ESP}/debian-6.efi")
        self.settle()
        self.assert_package_state("6")
        self.assert_user_edits_intact()

        # Artificial ESP exhaustion test before v7
        self.sh("dd if=/dev/zero of=/efi/fill.dat bs=1M count=480 2>/dev/null || true; sync")
        os.remove(self.build("7", extra=EXTRA_V7, deltas_from=["6"]))
        self.sync_payload()
        status, output = self.guest.run(AGENT, 300)
        self.assertNotEqual(status, 0)
        self.assertIn("not enough room on the ESP", output)
        self.sh("rm -f /efi/fill.dat && sync")
        self.settle()

        # v7 proceeds after free space restored
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "7"))
        self.reboot()
        self.assertEqual(self.booted_version(), "7")
        self.wait_until(f"test -e {ESP}/debian-7.efi")
        self.settle()
        self.assert_package_state("7")
        self.assert_user_edits_intact()

        # v8: Snapshot churn update
        os.remove(self.build("8", extra=EXTRA_V8, deltas_from=["7"]))
        self.sync_payload()
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "8"))
        self.reboot()
        self.assertEqual(self.booted_version(), "8")
        self.wait_until(f"test -e {ESP}/debian-8.efi")
        self.settle()
        self.assert_package_state("8")
        self.assert_user_edits_intact()

        # v9: Final release with static deltas from both v8 (stepwise) and v1 (long-jump)
        os.remove(self.build("9", extra=EXTRA_V9, deltas_from=["1", "8"]))
        self.sync_payload()

        # Stepwise upgrade from v8 -> v9
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "9"))
        self.reboot()
        self.assertEqual(self.booted_version(), "9")
        self.wait_until(f"test -e {ESP}/debian-9.efi")
        self.settle()
        self.assert_package_state("9")
        self.assert_user_edits_intact()

        # Long-jump test: boot fresh guest from v1 baseline and jump directly to v9
        self.guest.close()
        self.guest = None
        if os.path.exists(self.disk):
            os.remove(self.disk)

        self.start(factory_disk)
        self.settle()
        self.assertEqual(self.booted_version(), "1")
        self.apply_user_edits()
        self.assert_user_edits_intact()

        # Direct upgrade v1 -> v9 using static delta against LKG baseline
        res = self.update()
        self.assertEqual((res["result"], res["version"]), ("ok", "9"))
        self.reboot()
        self.assertEqual(self.booted_version(), "9")
        self.wait_until(f"test -e {ESP}/debian-9.efi")
        self.settle()
        self.assert_package_state("9")
        self.assert_user_edits_intact()
