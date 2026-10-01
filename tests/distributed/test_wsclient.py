# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import asyncio
import datetime
import ipaddress
import logging
import os
import shutil
import ssl
import tempfile
import threading
import time

from avocado import Test
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.asyncio.server import serve

from seine.distributed.common.wsclient import WsClient, WsClosed


async def handler(ws):
    async for message in ws:
        if message == "bye":
            await ws.close(4401, "go away")
        elif message == "push":
            await ws.send("pushed")
        else:
            await ws.send(f"echo {message}")


class Server:
    """A websockets asyncio server running on its own thread."""

    def __init__(self, ssl_context=None):
        self.ssl_context = ssl_context
        self.loop = asyncio.new_event_loop()
        self.started = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    async def main(self):
        self.stop = asyncio.Event()
        async with serve(handler, "127.0.0.1", 0, ssl=self.ssl_context) as server:
            self.port = server.sockets[0].getsockname()[1]
            self.started.set()
            await self.stop.wait()

    def run(self):
        self.loop.run_until_complete(self.main())

    def __enter__(self):
        self.thread.start()
        self.started.wait(10)
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(10)
        self.loop.close()

    def url(self, scheme="ws"):
        return f"{scheme}://127.0.0.1:{self.port}/"


def self_signed(directory):
    """Write a self-signed certificate for 127.0.0.1 and return (cert, key) paths."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    return cert_path, key_path


class WsClientTest(Test):
    """Tests for the blocking WebSocket client."""

    def setUp(self):
        # Avocado records websockets' per-frame debug logs, which slows every handshake.
        logger = logging.getLogger("websockets")
        self.log_level = logger.level
        logger.setLevel(logging.WARNING)

    def tearDown(self):
        logging.getLogger("websockets").setLevel(self.log_level)
        shutil.rmtree(getattr(self, "tmp", ""), ignore_errors=True)

    def test_send_and_recv(self):
        with Server() as server:
            ws = WsClient()
            ws.connect(server.url())
            ws.send("hello")
            self.assertEqual(ws.recv(timeout=5), "echo hello")
            ws.close()

    def test_recv_times_out_and_stays_usable(self):
        with Server() as server:
            ws = WsClient()
            ws.connect(server.url())
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                ws.recv(timeout=0.3)
            self.assertGreaterEqual(time.monotonic() - started, 0.25)
            ws.send("push")
            self.assertEqual(ws.recv(timeout=5), "pushed")
            ws.close()

    def test_recv_raises_the_close_code_of_the_peer(self):
        with Server() as server:
            ws = WsClient()
            ws.connect(server.url())
            ws.send("bye")
            with self.assertRaises(WsClosed) as ctx:
                ws.recv(timeout=5)
            self.assertEqual(ctx.exception.code, 4401)
            self.assertEqual(ctx.exception.reason, "go away")
            with self.assertRaises(WsClosed):
                ws.send("late")
            ws.close()

    def test_connect_failure_stops_the_thread(self):
        before = threading.active_count()
        ws = WsClient()
        with self.assertRaises(OSError):
            ws.connect("ws://127.0.0.1:1/", open_timeout=2)
        self.assertEqual(threading.active_count(), before)

    def test_close_is_idempotent_and_stops_the_thread(self):
        before = threading.active_count()
        with Server() as server:
            for _ in range(10):
                ws = WsClient()
                ws.connect(server.url())
                ws.close()
                ws.close()
            self.assertEqual(threading.active_count(), before + 1)

    def test_close_with_unread_messages_is_quick(self):
        with Server() as server:
            ws = WsClient()
            ws.connect(server.url())
            for _ in range(200):
                ws.send("push")
            started = time.monotonic()
            ws.close()
            self.assertLess(time.monotonic() - started, 5)

    def test_tls_handshakes_never_stall_with_the_default_context(self):
        """Guards the websockets.sync client stalling about 30% of TLS 1.3 handshakes."""
        self.tmp = tempfile.mkdtemp(prefix="seine-test-wsclient-")
        cert, key = self_signed(self.tmp)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        with Server(server_ctx) as server:
            for i in range(40):
                client_ctx = ssl.create_default_context(cafile=cert)
                ws = WsClient()
                ws.connect(server.url("wss"), client_ctx, open_timeout=5)
                ws.send(f"n{i}")
                self.assertEqual(ws.recv(timeout=5), f"echo n{i}")
                ws.close()
