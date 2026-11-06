"""game-lobby: 共通ロビー HTTP server。

- GET  /            -> web/index.html (Cache-Control: no-store)
- GET  /health      -> ロビー自身の status (Docker healthcheck 用)
- GET  /api/servers -> ゲームサーバー一覧 (巡回結果のキャッシュを返す)
- GET  /api/chat    -> メモリ内チャットの履歴 (after より大きい id だけ)
- POST /api/chat    -> メモリ内チャットに1件追加
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chat import CHAT_BODY_MAX, ChatError, ChatStore
from lobby import LobbyPoller, load_servers, lobby_version

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
HTML_PATH = os.path.join(WEB_DIR, "index.html")
STATIC_DIR = os.path.join(WEB_DIR, "static")
STATIC_TYPES = {
    "favicon.svg": "image/svg+xml",
    "favicon.ico": "image/x-icon",
    "favicon-16x16.png": "image/png",
    "favicon-32x32.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}


class LobbyHandler(BaseHTTPRequestHandler):
    poller = None
    chat = ChatStore()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_html()
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path == "/health":
            self._send_json(200, {"title": "ゲームロビー", "version": lobby_version(), "status": "idle"})
        elif path == "/api/servers":
            updated, entries = self.poller.snapshot()
            self._send_json(200, {"updated": updated, "servers": entries})
        elif path == "/api/chat":
            self._get_chat(parsed)
        else:
            self._send_json(404, {"status": "error", "message": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/chat":
            self._post_chat()
        else:
            self._send_json(404, {"status": "error", "message": "not_found"})

    def _get_chat(self, parsed):
        query = parse_qs(parsed.query)
        raw_after = query.get("after", [None])[0]
        after = None
        if raw_after is not None:
            try:
                after = int(raw_after)
            except (TypeError, ValueError):
                after = None
        self._send_json(200, self.chat.snapshot(after))

    def _post_chat(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length < 0:
            length = 0
        if length > CHAT_BODY_MAX:
            # 413 を返した後にクライアントが応答を読めるよう、小さなボディだけ読み捨てる。
            if length <= 65536:
                try:
                    self.rfile.read(length)
                except Exception:
                    pass
            self._send_json(413, {"status": "error", "message": "too_large"})
            return
        body = self.rfile.read(length) if length else b""
        try:
            message = self.chat.post_json(body)
        except ChatError as e:
            self._send_json(e.status, {"status": "error", "message": e.message})
            return
        self._send_json(200, message)

    def _serve_html(self):
        try:
            with open(HTML_PATH, "rb") as f:
                body = f.read()
            ctype = "text/html; charset=utf-8"
        except Exception:
            body = "<html><body><h1>ゲームロビー</h1><p>web/index.html が見つかりません。</p></body></html>".encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self._send_bytes(200, ctype, body)

    def _serve_static(self, name):
        if name not in STATIC_TYPES or "/" in name or "\\" in name:
            self.send_response(404)
            self.end_headers()
            return
        path = os.path.join(STATIC_DIR, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            self.send_response(404)
            self.end_headers()
            return
        ctype = STATIC_TYPES[name]
        self._send_bytes(200, ctype, body)

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_bytes(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    import argparse
    p = argparse.ArgumentParser(description="共通ロビー (game-lobby)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=None)
    args = p.parse_args()
    port = args.port if args.port is not None else int(os.environ.get("LOBBY_PORT", "80"))
    servers = load_servers(os.environ.get("LOBBY_SERVERS", "./servers.json"))
    poller = LobbyPoller(servers)
    LobbyHandler.poller = poller
    httpd = ThreadingHTTPServer((args.host, port), LobbyHandler)
    httpd.daemon_threads = True
    print("[lobby] listening on http://%s:%d" % (args.host, port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
