"""game-lobby: 共通ロビー HTTP server。

- GET /            -> web/index.html (Cache-Control: no-store)
- GET /health      -> ロビー自身の status (Docker healthcheck 用)
- GET /api/servers -> ゲームサーバー一覧 (巡回結果のキャッシュを返す)
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lobby import LobbyPoller, load_servers, lobby_version

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
HTML_PATH = os.path.join(WEB_DIR, "index.html")


class LobbyHandler(BaseHTTPRequestHandler):
    poller = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_html()
        elif path == "/health":
            self._send_json(200, {"title": "ゲームロビー", "version": lobby_version(), "status": "idle"})
        elif path == "/api/servers":
            updated, entries = self.poller.snapshot()
            self._send_json(200, {"updated": updated, "servers": entries})
        else:
            self._send_json(404, {"status": "error", "message": "not_found"})

    def _serve_html(self):
        try:
            with open(HTML_PATH, "rb") as f:
                body = f.read()
            ctype = "text/html; charset=utf-8"
        except Exception:
            body = "<html><body><h1>ゲームロビー</h1><p>web/index.html が見つかりません。</p></body></html>".encode("utf-8")
            ctype = "text/html; charset=utf-8"
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
    port = args.port if args.port is not None else int(os.environ.get("LOBBY_PORT", "8080"))
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
