"""game-lobby: チャットのメモリ内ロジック。

- 保存はプロセスのメモリ上のみ(ファイル・DB・volume には書かない)
- 保持は最新 CHAT_MAX_MESSAGES 件
- 最後の投稿から CHAT_TTL_SEC 秒経過したら、GET/POST 時に全削除する
  (バックグラウンドのタイマーは使わない)
- id は単調増加。全削除しても巻き戻さない
- epoch は全削除のたびに +1
- 時刻は now 関数で差し替えられる(テスト用)
"""
import json
import threading
import time
from datetime import datetime, timezone

CHAT_TTL_SEC = 600
CHAT_MAX_MESSAGES = 100
CHAT_NAME_MAX = 16
CHAT_TEXT_MAX = 200
CHAT_BODY_MAX = 2048


class ChatError(Exception):
    """チャット API の入力エラー。status は HTTP ステータス。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _normalize(value, max_len, field):
    if not isinstance(value, str):
        raise ChatError("invalid_" + field, 400)
    value = value.strip()
    if not value or len(value) > max_len:
        raise ChatError("invalid_" + field, 400)
    return value


def validate_name(value):
    return _normalize(value, CHAT_NAME_MAX, "name")


def validate_text(value):
    return _normalize(value, CHAT_TEXT_MAX, "text")


class ChatStore:
    """スレッドセーフなメモリ内チャット。"""

    def __init__(self, now=None):
        self._now = now if now is not None else time.time
        self._lock = threading.Lock()
        self._messages = []
        self._next_id = 1
        self._epoch = 0
        self._last_post = None

    def _expire_locked(self):
        """lock を保持したまま呼ぶ。期限切れなら全削除して epoch を進める。"""
        if not self._messages or self._last_post is None:
            return
        if self._now() - self._last_post >= CHAT_TTL_SEC:
            self._messages = []
            self._last_post = None
            self._epoch += 1

    def snapshot(self, after=None):
        """{"epoch": int, "messages": [...]} を返す。after より大きい id だけ。"""
        if after is None or not isinstance(after, int) or isinstance(after, bool):
            after = 0
        with self._lock:
            self._expire_locked()
            messages = [dict(m) for m in self._messages if m["id"] > after]
            return {"epoch": self._epoch, "messages": messages}

    def post(self, name, text):
        """検証して1件追加し、追加したメッセージ(dict)を返す。"""
        with self._lock:
            # 不正な投稿でも、POST が来た時点で期限切れを処理する。
            self._expire_locked()
            name = validate_name(name)
            text = validate_text(text)
            now = self._now()
            msg = {
                "id": self._next_id,
                "name": name,
                "text": text,
                "at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            }
            self._next_id += 1
            self._messages.append(msg)
            if len(self._messages) > CHAT_MAX_MESSAGES:
                del self._messages[:len(self._messages) - CHAT_MAX_MESSAGES]
            self._last_post = now
            return dict(msg)

    def post_json(self, body):
        """POST の生ボディを検証して追加する。失敗時は ChatError。"""
        if not isinstance(body, (bytes, bytearray)):
            raise ChatError("invalid_json", 400)
        if len(body) > CHAT_BODY_MAX:
            raise ChatError("too_large", 413)
        try:
            data = json.loads(bytes(body).decode("utf-8"))
        except Exception:
            raise ChatError("invalid_json", 400)
        if not isinstance(data, dict):
            raise ChatError("invalid_json", 400)
        return self.post(data.get("name"), data.get("text"))
