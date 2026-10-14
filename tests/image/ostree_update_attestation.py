#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import functools
import http.server
import os
import shutil
import sys
import threading

import avocado

path_to_self = os.path.realpath(__file__)
sys.path.append(os.path.dirname(path_to_self))

from update_guest import AGENT, ESP, PORT, UpdateGuest

TPM_TOOLS = """
playbook:
    - name: install tpm tools and curl
      priority: 850
      tasks:
          - name: install tpm2-tools and curl
            apt:
                name:
                    - tpm2-tools
                    - curl
                state: present
"""

MOCK_DEVICE_ID = "seine-device-secret-token-abcdef123456"
VALID_BEARER_TOKEN = "valid-bearer-token-778899aabbcc"


class AuthenticatedGatewayHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path == "/token":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode(errors="replace")
            if MOCK_DEVICE_ID in body:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(VALID_BEARER_TOKEN.encode())
            else:
                self.send_response(403)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path.startswith("/repo"):
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {VALID_BEARER_TOKEN}":
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer realm="seine-updates"')
                self.end_headers()
                self.wfile.write(b"Unauthorized\n")
                return
        super().do_GET()


class UpdateAttestation(UpdateGuest, avocado.Test):
    """
    :avocado: tags=full,container
    """
    tpm = True

    def serve(self):
        handler = functools.partial(
            AuthenticatedGatewayHandler, directory=self.payload)
        try:
            self.server = http.server.ThreadingHTTPServer(("0.0.2.2", PORT), handler)
        except OSError:
            try:
                self.server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler)
            except OSError:
                self.cancel(f"port {PORT} is in use")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def test_authenticated_update_flow_with_tpm(self):
        self.start(self.build("1", extra=TPM_TOOLS))

        # Check TPM character device is present in guest
        self.sh("test -e /dev/tpmrm0 || test -e /dev/tpm0")

        # Provision primary key in owner hierarchy and seal device secret in TPM
        self.sh("tpm2_createprimary -C o -c /root/primary.ctx")
        self.sh(f'echo -n "{MOCK_DEVICE_ID}" | '
                'tpm2_create -C /root/primary.ctx -u /root/s.pub -r /root/s.priv -i -')
        self.sh("tpm2_load -C /root/primary.ctx -u /root/s.pub -r /root/s.priv -c /root/s.ctx")
        self.sh("tpm2_evictcontrol -C o -c /root/s.ctx 0x81010002")

        # Verify unsealed secret matches
        secret = self.sh("tpm2_unseal -c 0x81010002")
        self.assertEqual(secret, MOCK_DEVICE_ID)

        # Build version 2 into payload
        os.remove(self.build("2", extra=TPM_TOOLS))

        # Unauthenticated pull fails against the gateway
        self.sh("mkdir -p /var/lib/seine-update && rm -f /var/lib/seine-update/token")
        self.wait_for_network()
        status, _ = self.guest.run(AGENT)
        self.assertNotEqual(status, 0)
        lines = self.sh("cat /var/lib/seine-update/status").splitlines()
        res = dict(line.split("=", 1) for line in lines)
        self.assertEqual(res.get("result"), "failed")

        # Token request with invalid identity returns 403 Forbidden
        bad_status = self.sh("curl -s -o /dev/null -w '%{http_code}' -X POST "
                             "http://10.0.2.2:8000/token -d 'bad-device-id'")
        self.assertEqual(bad_status, "403")

        # Request token from backoffice using TPM-unsealed device identity
        self.sh("curl -sf -X POST http://10.0.2.2:8000/token "
                "-d \"$(tpm2_unseal -c 0x81010002)\" > /var/lib/seine-update/token")
        self.assertEqual(self.sh("cat /var/lib/seine-update/token"), VALID_BEARER_TOKEN)

        # Authenticated update succeeds
        update_res = self.update()
        self.assertEqual((update_res["result"], update_res["version"]), ("ok", "2"))
        self.assertEqual(self.esp(), ["debian-1.efi", "debian-2+3-0.efi"])

        # Reboot into version 2 and assert invariants
        self.reboot()
        self.assertEqual(self.booted_version(), "2")
        self.wait_until(f"test -e {ESP}/debian-2.efi")
        self.assert_invariants()

        # Sealed secret in TPM remains accessible after update and reboot
        self.assertEqual(self.sh("tpm2_unseal -c 0x81010002"), MOCK_DEVICE_ID)
