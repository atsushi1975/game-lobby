"""game-lobby: 複数ゲームサーバーの状態を巡回してキャッシュする共通ロビー側ロジック。

- servers.json の読み込み(欠損/必須項目欠落/壊れは警告を出して続行)
- バックグラウンドスレッドによる health 巡回(ThreadPoolExecutor で並列・1台2秒タイムアウト)
- 結果のメモリキャッシュ(ブラウザ要求はキャッシュを返すだけ)
- 新旧(stale)判定(バージョン文字列が "8桁数字-4桁数字-..." のときだけ比較)

HTTP には依存しない純粋な関数と Poller に分けている。
"""
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from urllib import request as urlrequest
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler
from concurrent.futures import ThreadPoolExecutor

OFF = "off"
STATUSES = ("idle", "waiting", "playing")
STATUS_RANK = {"waiting": 0, "idle": 1, "playing": 2, "off": 3}
DEFAULT_POLL_SEC = 5.0
DEFAULT_TIMEOUT_SEC = 2.0
MAX_BODY_BYTES = 4096
REQUIRED_FIELDS = ("game", "health")
_VERSION_RE = re.compile(r"^(\d{8})-(\d{4})-([0-9a-fA-F]+)$")


def _warn(msg):
    print("[lobby] " + msg, file=sys.stderr, flush=True)


class NoRedirectHandler(HTTPRedirectHandler):
    """リダイレクト(3xx)を追わない。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urlrequest.build_opener(NoRedirectHandler)


def parse_version(version):
    """バージョン文字列 "20261029-2251-b7bc7eb" -> (20261029, 2251, "b7bc7eb")。
    文字列でなく/形式でなければ None(比較対象外)。"""
    if not isinstance(version, str):
        return None
    m = _VERSION_RE.match(version.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), m.group(3))


def compute_stale(entries):
    """新旧判定。entries は {"game","version","status"} のリストで、stale bool の並びを返す。

    - version が "8桁数字-4桁数字-..." 形式でないものは比較対象外(stale=False)
    - "off" は比較対象外(stale=False)
    - 同じ game の中で最大 version より小さいものに stale=True
    """
    latest = {}
    for e in entries:
        if e.get("status") == OFF:
            continue
        key = parse_version(e.get("version"))
        if key is None:
            continue
        game = e.get("game")
        cur = latest.get(game)
        if cur is None or key > cur:
            latest[game] = key
    flags = []
    for e in entries:
        key = parse_version(e.get("version"))
        stale = False
        if e.get("status") != OFF and key is not None:
            best = latest.get(e.get("game"))
            if best is not None and key < best:
                stale = True
        flags.append(stale)
    return flags


def hostname_of(url):
    try:
        return urlsplit(url).hostname or url
    except Exception:
        return url or ""


def display_name(server, title):
    """name -> health の title -> health URL のホスト名の順で表示名を決める。"""
    name = server.get("name") if isinstance(server, dict) else None
    if isinstance(name, str) and name:
        return name
    if isinstance(title, str) and title:
        return title
    return hostname_of(server.get("health") or server.get("url") or "")


def valid_url(value):
    """リンク先として許可する url: "http://" / "https://" / "/" で始まる文字列のみ。"""
    return isinstance(value, str) and (
        value.startswith("http://") or value.startswith("https://") or value.startswith("/")
    )


def valid_port(value):
    """リンク先の port: 整数 1〜65535 のみ(bool は除外)。"""
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def load_servers(path):
    """servers.json を読む。形式: [ {"name":..., "game":..., "health":..., "url":...|"port":...}, ... ]

    - game / health は必須。url / port はどちらか1つ以上必要(両方あれば両方保持し、画面側は url 優先)
    - 必須項目の欠落・url/port の不正がある要素は警告を出して読み飛ばす(起動は継続)
    - ファイルが無い/壊れていれば空の一覧で返す(呼び出し側が警告を出す)
    """
    if not os.path.exists(path):
        _warn("servers file not found: %s -> starting with empty list" % path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        _warn("failed to load servers file %s: %s -> starting with empty list" % (path, e))
        return []
    if not isinstance(data, list):
        _warn("servers file %s is not a JSON array -> starting with empty list" % path)
        return []
    servers = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            _warn("servers file %s: entry %d is not an object -> skipped" % (path, i))
            continue
        problems = [k for k in REQUIRED_FIELDS if not entry.get(k)]
        url = entry.get("url")
        port = entry.get("port")
        has_url = url is not None
        has_port = port is not None
        if not has_url and not has_port:
            problems.append("url or port")
        if has_url and not valid_url(url):
            problems.append("url")
        if has_port and not valid_port(port):
            problems.append("port")
        if problems:
            _warn("servers file %s: entry %d invalid %s -> skipped" % (path, i, ", ".join(problems)))
            continue
        name = entry.get("name")
        servers.append({
            "name": name if isinstance(name, str) and name else None,
            "game": entry["game"],
            "health": entry["health"],
            "url": url if has_url else None,
            "port": port if has_port else None,
        })
    return servers


class LobbyPoller:
    """バックグラウンドで全ゲームサーバーの /health を巡回し、結果をメモリキャッシュする。"""

    def __init__(self, servers, poll_interval=None, timeout=None, max_body_bytes=None):
        self._servers = [dict(s) for s in servers]
        self._poll_interval = DEFAULT_POLL_SEC if poll_interval is None else poll_interval
        self._timeout = DEFAULT_TIMEOUT_SEC if timeout is None else timeout
        self._max_body_bytes = MAX_BODY_BYTES if max_body_bytes is None else max_body_bytes
        self._lock = threading.Lock()
        self._updated = None
        self._entries = []
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lobby-poller", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:
                # 1サイクルの失敗でスレッドを死なせない
                pass
            if self._stop_event.wait(self._poll_interval):
                break

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=5)

    def snapshot(self):
        """(updated ISO8601 or None, entries) を返す。updated は前回の巡回が終わった時刻。"""
        with self._lock:
            return self._updated, [dict(e) for e in self._entries]

    def _poll_once(self):
        servers = self._servers
        fetched = []
        if servers:
            with ThreadPoolExecutor(max_workers=max(1, len(servers))) as ex:
                futures = [ex.submit(self._fetch, s) for s in servers]
                for f in futures:
                    fetched.append(f.result())
        api_entries = []
        for server, res in zip(servers, fetched):
            api_entries.append({
                "name": res["name"],
                "game": server["game"],
                "url": server.get("url"),
                "port": server.get("port"),
                "status": res["status"],
                "version": res["version"],
                # 停止中は常に null。複数ルーム型以外は _fetch が None を返している。
                "playing_rooms": res["playing_rooms"] if res["status"] != OFF else None,
                "stale": False,
            })
        flags = compute_stale([
            {"game": e["game"], "version": e["version"], "status": e["status"]} for e in api_entries
        ])
        for e, stale in zip(api_entries, flags):
            e["stale"] = stale
        # 表示順: 状態グループ(waiting/idle/playing/off)ごとに、同じ状態の中では port 番号順。
        # Python の sort は安定なので、port が同じ場合は servers.json の並びを保つ。
        decorated = []
        for server, e in zip(servers, api_entries):
            try:
                port = urlsplit(server["health"]).port or 0
            except Exception:
                port = 0
            decorated.append((e, port))
        decorated.sort(key=lambda t: (STATUS_RANK.get(t[0]["status"], 99), t[1]))
        api_entries = [e for e, _ in decorated]
        with self._lock:
            self._updated = datetime.now(timezone.utc).isoformat()
            self._entries = api_entries

    def _off(self, server):
        return {"name": display_name(server, None), "status": OFF, "version": None, "title": None, "playing_rooms": None}

    def _fetch(self, server):
        """GET /health を最大2秒で1回。リダイレクトは追わない。最大4KB。"""
        title = None
        try:
            with _OPENER.open(server["health"], timeout=self._timeout) as resp:
                if resp.status != 200:
                    return self._off(server)
                body = resp.read(self._max_body_bytes)
            data = json.loads(body.decode("utf-8", "replace"))
            if not isinstance(data, dict):
                return self._off(server)
            status = data.get("status")
            if status not in STATUSES:
                return self._off(server)
            version = data.get("version")
            version = version if isinstance(version, str) and version else None
            t = data.get("title")
            title = t if isinstance(t, str) else None
            # 複数ルーム型(犯人は踊る)だけが playing_rooms を返す。
            # 整数(bool を除く)で 0 以上のときだけ採用し、それ以外は None。
            rooms = data.get("playing_rooms")
            if not (isinstance(rooms, int) and not isinstance(rooms, bool) and rooms >= 0):
                rooms = None
            return {"name": display_name(server, title), "status": status, "version": version,
                    "title": title, "playing_rooms": rooms}
        except Exception:
            return self._off(server)


def lobby_version():
    """ロビー自身の version。VERSION ファイルから読む(無ければ "dev")。"""
    v = "dev"
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), "r", encoding="utf-8") as f:
            v = (f.read() or "").strip() or "dev"
    except Exception:
        v = "dev"
    return v or "dev"
