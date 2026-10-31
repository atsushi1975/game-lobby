#!/usr/bin/env python3
"""game-lobby のテスト: 新旧判定 / servers.json 読み込み / 巡回結果 / /api/servers の形。

Run: python3 -m unittest discover -s tests -p 'test_*.py' -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lobby
from lobby import LobbyPoller, compute_stale, load_servers, parse_version
from server import LobbyHandler


class FakeHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if getattr(self.server, "delay", 0):
            time.sleep(self.server.delay)
        if getattr(self.server, "raw_body", None) is not None:
            body = self.server.raw_body
        else:
            body = json.dumps(self.server.payload).encode("utf-8")
        self.send_response(self.server.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeHealthServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, payload=None, code=200, raw_body=None, delay=0.0):
        self.payload = payload if payload is not None else {
            "title": "カタン 部屋1",
            "version": "20261029-2251-b7bc7eb",
            "status": "idle",
        }
        self.code = code
        self.raw_body = raw_body
        self.delay = delay
        ThreadingHTTPServer.__init__(self, ("127.0.0.1", 0), FakeHealthHandler)


def start_http_server(handler_cls, **kwargs):
    if handler_cls is FakeHealthServer:
        httpd = FakeHealthServer(**kwargs)
    else:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, thread, "http://127.0.0.1:%d" % port


def http_get(url):
    with urlrequest.urlopen(url, timeout=10) as r:
        body = r.read()
        return r.status, json.loads(body.decode("utf-8"))


class LobbyTests(unittest.TestCase):
    def setUp(self):
        self.httpd = None
        self.thread = None
        self.health_url = None

    def tearDown(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    # --- (1) 新旧判定の関数 ---

    def test_parse_version(self):
        self.assertEqual(lobby.parse_version("20261029-2251-b7bc7eb"), (20261029, 2251, "b7bc7eb"))
        self.assertIsNone(lobby.parse_version("dev"))
        self.assertIsNone(lobby.parse_version("2026-10-29"))
        self.assertIsNone(lobby.parse_version(None))
        self.assertIsNone(lobby.parse_version(123))

    def test_compute_stale(self):
        entries = [
            {"game": "catan", "version": "20261029-2251-b7bc7eb", "status": "idle"},
            {"game": "catan", "version": "20261030-0912-e41d07a", "status": "playing"},
            {"game": "catan", "version": "dev", "status": "waiting"},
            {"game": "hannin", "version": "20261030-0912-e41d07a", "status": "idle"},
        ]
        self.assertEqual(lobby.compute_stale(entries), [True, False, False, False])

        entries2 = [
            {"game": "catan", "version": "20261029-2251-b7bc7eb", "status": "off"},
            {"game": "catan", "version": "20261029-2251-b7bc7eb", "status": "idle"},
        ]
        self.assertEqual(lobby.compute_stale(entries2), [False, False])

    # --- (2) servers.json の読み込み ---

    def test_load_servers_missing_file(self):
        self.assertEqual(load_servers("/nonexistent/path/servers.json"), [])

    def test_load_servers_corrupt_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "servers.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{ broken json !!")
            self.assertEqual(load_servers(p), [])

    def _load_servers_json(self, entries):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "servers.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(entries, f)
            return load_servers(p)

    def test_load_servers_url_only(self):
        servers = self._load_servers_json([
            {"name": "url-only", "game": "catan", "health": "http://catan_001:8000/health", "url": "/catan1/"},
        ])
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["url"], "/catan1/")
        self.assertIsNone(servers[0]["port"])

    def test_load_servers_port_only(self):
        servers = self._load_servers_json([
            {"name": "port-only", "game": "catan", "health": "http://catan_001:8000/health", "port": 6100},
        ])
        self.assertEqual(len(servers), 1)
        self.assertIsNone(servers[0]["url"])
        self.assertEqual(servers[0]["port"], 6100)

    def test_load_servers_url_and_port(self):
        servers = self._load_servers_json([
            {"name": "both", "game": "catan", "health": "http://catan_001:8000/health",
             "url": "https://example.com/catan1/", "port": 6100},
        ])
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["url"], "https://example.com/catan1/")
        self.assertEqual(servers[0]["port"], 6100)

    def test_load_servers_skips_without_url_and_port(self):
        servers = self._load_servers_json([
            {"name": "no-link", "game": "catan", "health": "http://catan_002:8000/health"},
        ])
        self.assertEqual(servers, [])

    def test_load_servers_skips_missing_game_or_health(self):
        servers = self._load_servers_json([
            {"name": "ok", "game": "catan", "health": "http://catan_001:8000/health", "url": "/catan1/"},
            {"name": "no-health", "game": "catan", "url": "/catan2/"},
            {"name": "no-game", "health": "http://catan_003:8000/health", "url": "/catan3/"},
        ])
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "ok")

    def test_load_servers_invalid_ports(self):
        for port in (0, 70000, "6100", True, False, 1.5, -1):
            with self.subTest(port=port):
                servers = self._load_servers_json([
                    {"name": "bad", "game": "catan", "health": "http://catan_001:8000/health", "port": port},
                ])
                self.assertEqual(servers, [])

    def test_load_servers_valid_port_bounds(self):
        servers = self._load_servers_json([
            {"name": "low", "game": "catan", "health": "http://catan_001:8000/health", "port": 1},
            {"name": "high", "game": "catan", "health": "http://catan_002:8000/health", "port": 65535},
        ])
        self.assertEqual([s["port"] for s in servers], [1, 65535])

    def test_load_servers_rejects_unsafe_url(self):
        for url in ("javascript:alert(1)", "ftp://example.com/", "data:text/html,x", ""):
            with self.subTest(url=url):
                servers = self._load_servers_json([
                    {"name": "bad", "game": "catan", "health": "http://catan_001:8000/health", "url": url},
                ])
                self.assertEqual(servers, [])

    def test_load_servers_accepts_allowed_url_schemes(self):
        servers = self._load_servers_json([
            {"name": "http", "game": "catan", "health": "http://catan_001:8000/health", "url": "http://example.com/a"},
            {"name": "https", "game": "catan", "health": "http://catan_002:8000/health", "url": "https://example.com/b"},
            {"name": "path", "game": "catan", "health": "http://catan_003:8000/health", "url": "/cantan3/"},
        ])
        self.assertEqual([s["url"] for s in servers], ["http://example.com/a", "https://example.com/b", "/cantan3/"])

    # --- (3) 巡回結果 (実HTTPサーバー立てて) ---

    def test_poller_ok(self):
        self.httpd, self.thread, base = start_http_server(FakeHealthServer)
        self.health_url = base + "/health"
        poller = LobbyPoller([
            {"name": "カタン 部屋1", "game": "catan", "health": self.health_url, "url": "/catan1/"},
        ], poll_interval=0.05)
        try:
            entries = None
            end = time.monotonic() + 10
            while time.monotonic() < end:
                updated, ents = poller.snapshot()
                if updated is not None:
                    entries = ents
                    break
                time.sleep(0.02)
            self.assertIsNotNone(entries)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["status"], "idle")
            self.assertEqual(entries[0]["version"], "20261029-2251-b7bc7eb")
            self.assertEqual(entries[0]["stale"], False)
            self.assertEqual(entries[0]["name"], "カタン 部屋1")
        finally:
            poller.stop()

    def test_poller_off_on_bad_json(self):
        self.httpd, self.thread, base = start_http_server(FakeHealthServer, raw_body=b"not json at all")
        self.health_url = base + "/health"
        poller = LobbyPoller([
            {"name": "bad", "game": "catan", "health": self.health_url, "url": "/catan1/"},
        ], poll_interval=0.05)
        try:
            entries = self.wait_poller_updated(poller)
            self.assertEqual(entries[0]["status"], "off")
            self.assertIsNone(entries[0]["version"])
        finally:
            poller.stop()

    def test_poller_off_on_500(self):
        self.httpd, self.thread, base = start_http_server(FakeHealthServer, code=500)
        self.health_url = base + "/health"
        poller = LobbyPoller([
            {"name": "err500", "game": "catan", "health": self.health_url, "url": "/catan1/"},
        ], poll_interval=0.05)
        try:
            entries = self.wait_poller_updated(poller)
            self.assertEqual(entries[0]["status"], "off")
        finally:
            poller.stop()

    def test_poller_off_on_unexpected_status(self):
        self.httpd, self.thread, base = start_http_server(
            FakeHealthServer, payload={"title": "x", "version": "20261029-2251-b7bc7eb", "status": "unknown"})
        self.health_url = base + "/health"
        poller = LobbyPoller([
            {"name": "badstatus", "game": "catan", "health": self.health_url, "url": "/catan1/"},
        ], poll_interval=0.05)
        try:
            entries = self.wait_poller_updated(poller)
            self.assertEqual(entries[0]["status"], "off")
        finally:
            poller.stop()

    def test_poller_off_on_timeout(self):
        self.httpd, self.thread, base = start_http_server(FakeHealthServer, delay=10.0)
        self.health_url = base + "/health"
        poller = LobbyPoller([
            {"name": "slow", "game": "catan", "health": self.health_url, "url": "/catan1/"},
        ], poll_interval=0.05, timeout=1.0)
        try:
            entries = self.wait_poller_updated(poller)
            self.assertEqual(entries[0]["status"], "off")
        finally:
            poller.stop()

    # --- (4) /api/servers のレスポンスの形 ---

    def test_api_servers_shape(self):
        self.httpd, self.thread, base = start_http_server(FakeHealthServer)
        self.health_url = base + "/health"
        poller = LobbyPoller([
            {"name": "url指定", "game": "catan", "health": self.health_url, "url": "/catan1/", "port": None},
            {"name": "port指定", "game": "catan", "health": self.health_url, "url": None, "port": 6100},
        ], poll_interval=0.05)
        LobbyHandler.poller = poller
        api_httpd = None
        api_thread = None
        try:
            api_httpd, api_thread, api_base = start_http_server(LobbyHandler)
            entries = self.wait_poller_updated(poller)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["status"], "idle")
            status, data = http_get(api_base + "/api/servers")
            self.assertEqual(status, 200)
            self.assertEqual(set(data.keys()), {"updated", "servers"})
            self.assertIsNotNone(data["updated"])
            self.assertEqual(len(data["servers"]), 2)
            by_name = {e["name"]: e for e in data["servers"]}
            url_entry = by_name["url指定"]
            port_entry = by_name["port指定"]
            self.assertEqual(set(url_entry.keys()), {"name", "game", "url", "port", "status", "version", "stale"})
            self.assertNotIn("health", url_entry)
            self.assertNotIn("health", port_entry)
            self.assertEqual(url_entry["url"], "/catan1/")
            self.assertIsNone(url_entry["port"])
            self.assertIsNone(port_entry["url"])
            self.assertEqual(port_entry["port"], 6100)
            for entry in (url_entry, port_entry):
                self.assertEqual(entry["status"], "idle")
                self.assertEqual(entry["version"], "20261029-2251-b7bc7eb")
                self.assertIs(entry["stale"], False)

            status2, health = http_get(api_base + "/health")
            self.assertEqual(status2, 200)
            self.assertEqual(set(health.keys()), {"title", "version", "status"})
            self.assertEqual(health["status"], "idle")
            self.assertIsInstance(health["version"], str)
        finally:
            LobbyHandler.poller = None
            if api_httpd is not None:
                api_httpd.shutdown()
                api_httpd.server_close()
                api_httpd = None
            if api_thread is not None:
                api_thread.join(timeout=5)
                api_thread = None
            poller.stop()

    # --- helpers ---

    def wait_poller_updated(self, poller, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            updated, entries = poller.snapshot()
            if updated is not None:
                return entries
            time.sleep(0.02)
        self.fail("poller did not finish within %ss" % timeout)


if __name__ == "__main__":
    unittest.main()
