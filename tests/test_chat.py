#!/usr/bin/env python3
"""game-lobby のチャット API / ChatStore のテスト。

Run: python3 -m unittest discover -s tests -p 'test_*.py' -v
"""
import json
import os
import sys
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat import CHAT_MAX_MESSAGES, CHAT_TTL_SEC, ChatStore
from server import LobbyHandler


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def http_json(url, payload=None, raw=None, method=None):
    data = raw
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
    except HTTPError as e:
        try:
            body = e.read()
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:
                parsed = None
            return e.code, parsed
        finally:
            e.close()


class ChatApiTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = ChatStore(now=self.clock)
        self._old_chat = LobbyHandler.chat
        LobbyHandler.chat = self.store
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), LobbyHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        LobbyHandler.chat = self._old_chat

    def get_chat(self, after=None):
        url = self.base + "/api/chat"
        if after is not None:
            url += "?after=" + str(after)
        return http_json(url)

    def post_chat(self, payload=None, raw=None):
        return http_json(self.base + "/api/chat", payload=payload, raw=raw, method="POST")

    # --- 基本 ---

    def test_empty(self):
        status, data = self.get_chat()
        self.assertEqual(status, 200)
        self.assertEqual(data, {"epoch": 0, "messages": []})

    def test_post_and_get(self):
        status, msg = self.post_chat({"name": " たろう ", "text": " こんにちは "})
        self.assertEqual(status, 200)
        self.assertEqual(set(msg.keys()), {"id", "name", "text", "at"})
        self.assertEqual(msg["id"], 1)
        self.assertEqual(msg["name"], "たろう")
        self.assertEqual(msg["text"], "こんにちは")
        datetime.fromisoformat(msg["at"])  # ISO8601 として読めること

        status, data = self.get_chat()
        self.assertEqual(status, 200)
        self.assertEqual(data["epoch"], 0)
        self.assertEqual(data["messages"], [msg])

        status, data = self.get_chat(after=0)
        self.assertEqual(data["messages"], [msg])
        status, data = self.get_chat(after=1)
        self.assertEqual(data["messages"], [])

    def test_ids_are_monotonic(self):
        for i in range(1, 4):
            status, msg = self.post_chat({"name": "a", "text": "m%d" % i})
            self.assertEqual(status, 200)
            self.assertEqual(msg["id"], i)

    # --- 入力検証 ---

    def test_validation_errors(self):
        cases = [
            {"name": "", "text": "x"},
            {"name": "   ", "text": "x"},
            {"name": "x" * 17, "text": "x"},
            {"name": "a", "text": ""},
            {"name": "a", "text": "   "},
            {"name": "a", "text": "x" * 201},
            {"name": 1, "text": "x"},
            {"name": "a", "text": 1},
            {"name": "a"},
            {"text": "x"},
            [],
            "not an object",
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                status, data = self.post_chat(payload)
                self.assertEqual(status, 400)
                self.assertEqual(data["status"], "error")

        for raw in (b"{broken", b'{"name":"a","text":"x"} trailing'):
            with self.subTest(raw=raw):
                status, data = self.post_chat(raw=raw)
                self.assertEqual(status, 400)

        status, data = self.get_chat()
        self.assertEqual(data["messages"], [])

    def test_name_and_text_limits_are_inclusive(self):
        status, msg = self.post_chat({"name": "x" * 16, "text": "y" * 200})
        self.assertEqual(status, 200)
        self.assertEqual(msg["name"], "x" * 16)
        self.assertEqual(msg["text"], "y" * 200)

    def test_body_too_large(self):
        raw = json.dumps({"name": "a", "text": "x" * 2100}).encode("utf-8")
        self.assertGreater(len(raw), 2048)
        status, data = self.post_chat(raw=raw)
        self.assertEqual(status, 413)
        self.assertEqual(data["status"], "error")
        status, data = self.get_chat()
        self.assertEqual(data["messages"], [])

    # --- 100件上限 ---

    def test_keeps_latest_100(self):
        for i in range(1, 106):
            status, msg = self.post_chat({"name": "a", "text": "m%d" % i})
            self.assertEqual(status, 200)
            self.assertEqual(msg["id"], i)

        status, data = self.get_chat()
        self.assertEqual(status, 200)
        self.assertEqual(data["epoch"], 0)
        self.assertEqual(len(data["messages"]), CHAT_MAX_MESSAGES)
        self.assertEqual([m["id"] for m in data["messages"]], list(range(6, 106)))

        status, data = self.get_chat(after=104)
        self.assertEqual([m["id"] for m in data["messages"]], [105])
        status, data = self.get_chat(after=105)
        self.assertEqual(data["messages"], [])

    # --- TTL と epoch ---

    def test_ttl_clears_all_and_increments_epoch(self):
        self.post_chat({"name": "a", "text": "1"})

        self.clock.advance(CHAT_TTL_SEC - 1)
        status, data = self.get_chat()
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["epoch"], 0)

        self.clock.advance(1)
        status, data = self.get_chat()
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["epoch"], 1)

        # 全削除後も id は巻き戻らない。
        status, msg = self.post_chat({"name": "a", "text": "2"})
        self.assertEqual(status, 200)
        self.assertEqual(msg["id"], 2)
        self.assertEqual(self.get_chat()[1]["epoch"], 1)

        self.clock.advance(CHAT_TTL_SEC - 1)
        self.assertEqual(len(self.get_chat()[1]["messages"]), 1)
        self.clock.advance(1)
        status, data = self.get_chat()
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["epoch"], 2)

        status, msg = self.post_chat({"name": "a", "text": "3"})
        self.assertEqual(msg["id"], 3)

    def test_get_does_not_extend_ttl(self):
        self.post_chat({"name": "a", "text": "1"})
        self.clock.advance(300)
        self.assertEqual(len(self.get_chat()[1]["messages"]), 1)
        self.clock.advance(299)
        self.assertEqual(len(self.get_chat()[1]["messages"]), 1)
        self.clock.advance(1)
        status, data = self.get_chat()
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["epoch"], 1)

    def test_limit_does_not_increment_epoch(self):
        for i in range(101):
            self.post_chat({"name": "a", "text": "m%d" % i})
        data = self.get_chat()[1]
        self.assertEqual(len(data["messages"]), CHAT_MAX_MESSAGES)
        self.assertEqual(data["epoch"], 0)

    def test_invalid_post_also_triggers_expiry(self):
        self.post_chat({"name": "a", "text": "1"})
        self.clock.advance(CHAT_TTL_SEC)
        status, _ = self.post_chat({"name": "", "text": "x"})
        self.assertEqual(status, 400)
        status, data = self.get_chat()
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["epoch"], 1)


if __name__ == "__main__":
    unittest.main()
