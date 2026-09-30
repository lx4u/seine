#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import base64
import http.server
import os
import sys
import threading

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.credentials import CredentialError, probe


# ---------------------------------------------------------------------------
# A tiny local server, routed by 'state["routes"]' -- set *after* it is
# started (its port is only known then), so requests only ever see the
# final state; no lock needed for that ordering.
# ---------------------------------------------------------------------------

def _start_server():
    state = {"routes": {}, "expect_auth": None, "record": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keep test output quiet

        def do_GET(self):
            if state["record"] is not None:
                state["record"].append(dict(self.headers.items()))
            expect_auth = state["expect_auth"]
            if expect_auth is not None:
                want = "Basic " + base64.b64encode(
                    ("%s:%s" % expect_auth).encode()).decode()
                if self.headers.get("Authorization") != want:
                    self.send_response(401)
                    self.end_headers()
                    return
            route = state["routes"].get(self.path)
            if route is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(route[0])
            if len(route) > 1:
                self.send_header("Location", route[1])
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state


def _stop_server(server, thread):
    server.shutdown()
    thread.join()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class OkOnTheFirstCandidate(avocado.Test):
    def test(self):
        server, thread, state = _start_server()
        try:
            state["routes"] = {"/debian/dists/bookworm/InRelease": (200,)}
            self.assertTrue(probe(
                "http://127.0.0.1:%d/debian" % server.server_port,
                "bookworm", "alice", "s3cr3t"))
        finally:
            _stop_server(server, thread)


class PreemptiveBasicAuthIsSent(avocado.Test):
    def test(self):
        server, thread, state = _start_server()
        try:
            state["routes"] = {"/debian/dists/bookworm/InRelease": (200,)}
            state["expect_auth"] = ("alice", "s3cr3t")
            self.assertTrue(probe(
                "http://127.0.0.1:%d/debian" % server.server_port,
                "bookworm", "alice", "s3cr3t"))
        finally:
            _stop_server(server, thread)

    def test_wrong_credential_is_rejected_not_erred(self):
        server, thread, state = _start_server()
        try:
            state["routes"] = {"/debian/dists/bookworm/InRelease": (200,)}
            state["expect_auth"] = ("alice", "s3cr3t")
            self.assertFalse(probe(
                "http://127.0.0.1:%d/debian" % server.server_port,
                "bookworm", "alice", "wrong"))
        finally:
            _stop_server(server, thread)


class Rejected401(avocado.Test):
    def test(self):
        server, thread, state = _start_server()
        try:
            state["routes"] = {"/debian/dists/bookworm/InRelease": (401,)}
            self.assertFalse(probe(
                "http://127.0.0.1:%d/debian" % server.server_port,
                "bookworm", "alice", "s3cr3t"))
        finally:
            _stop_server(server, thread)


class Rejected403(avocado.Test):
    def test(self):
        server, thread, state = _start_server()
        try:
            state["routes"] = {"/debian/dists/bookworm/InRelease": (403,)}
            self.assertFalse(probe(
                "http://127.0.0.1:%d/debian" % server.server_port,
                "bookworm", "alice", "s3cr3t"))
        finally:
            _stop_server(server, thread)


class ServerErrorSurfacesImmediately(avocado.Test):
    def test(self):
        server, thread, state = _start_server()
        try:
            state["routes"] = {"/debian/dists/bookworm/InRelease": (500,)}
            state["record"] = []
            with self.assertRaises(CredentialError):
                probe("http://127.0.0.1:%d/debian" % server.server_port,
                     "bookworm", "alice", "s3cr3t")
            # never tried the Release/flat fallbacks -- a 5xx is not "wrong path".
            self.assertEqual(len(state["record"]), 1)
        finally:
            _stop_server(server, thread)


class InReleaseFallsBackToRelease(avocado.Test):
    def test(self):
        server, thread, state = _start_server()
        try:
            state["routes"] = {
                "/debian/dists/bookworm/InRelease": (404,),
                "/debian/dists/bookworm/Release": (200,),
            }
            self.assertTrue(probe(
                "http://127.0.0.1:%d/debian" % server.server_port,
                "bookworm", "alice", "s3cr3t"))
        finally:
            _stop_server(server, thread)


class FlatRepositoryFallback(avocado.Test):
    def test(self):
        server, thread, state = _start_server()
        try:
            state["routes"] = {
                "/debian/dists/bookworm/InRelease": (404,),
                "/debian/dists/bookworm/Release": (404,),
                "/debian/InRelease": (200,),
            }
            self.assertTrue(probe(
                "http://127.0.0.1:%d/debian" % server.server_port,
                "bookworm", "alice", "s3cr3t"))
        finally:
            _stop_server(server, thread)


class NotFoundEverywhereRaises(avocado.Test):
    def test(self):
        server, thread, state = _start_server()
        try:
            state["routes"] = {}  # every candidate 404s
            with self.assertRaises(CredentialError) as ctx:
                probe("http://127.0.0.1:%d/debian" % server.server_port,
                     "bookworm", "alice", "s3cr3t")
            self.assertIn("404", str(ctx.exception))
        finally:
            _stop_server(server, thread)


class CrossHostRedirectDropsAuthorization(avocado.Test):
    def test(self):
        server, thread, state = _start_server()
        try:
            port = server.server_port
            state["routes"] = {
                "/debian/dists/bookworm/InRelease":
                    (302, "http://localhost:%d/debian/InRelease" % port),
                "/debian/InRelease": (200,),
            }
            state["record"] = []
            self.assertTrue(probe(
                "http://127.0.0.1:%d/debian" % port,
                "bookworm", "alice", "s3cr3t"))
            # first request (127.0.0.1) carried it, the redirected one
            # (localhost -- a different host string) must not.
            self.assertEqual(len(state["record"]), 2)
            self.assertIn("Authorization", state["record"][0])
            self.assertNotIn("Authorization", state["record"][1])
        finally:
            _stop_server(server, thread)


class UnreachableHostRaisesCredentialError(avocado.Test):
    def test(self):
        with self.assertRaises(CredentialError):
            probe("http://127.0.0.1:1/debian", "bookworm", "a", "b", timeout=2)


if __name__ == "__main__":
    avocado.main()
