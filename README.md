# game-lobby(共通ロビー)

複数のゲームサーバーの状態を一覧表示する「共通ロビー」。
カタン(catan)と同じ流儀で、http.server ベースの単一プロセス・Python 3.12 標準ライブラリのみ。
呼び名の登録と、メモリ内だけで動く簡易チャット(最後の投稿から10分で消える)を備える。

## 起動方法

```bash
python3 server.py
```

- 待ち受け: 0.0.0.0:80(環境変数 `LOBBY_PORT` で変更可)
- ゲームサーバー一覧: `GET /api/servers`

## Docker

```bash
docker compose up -d --build
```

- ポート: 80(`LOBBY_PORT` で変更可)
- `servers.json` はホスト側 `./servers.json` を `/app/servers.json` に mount する想定(`docker-compose.yml`)。無くても空の一覧で起動する。

## 環境変数

| 変数 | 既定 | 説明 |
| --- | --- | --- |
| `LOBBY_PORT` | `80` | ロビーが待ち受けるポート |
| `LOBBY_SERVERS` | `./servers.json` | 巡回対象のゲームサーバー一覧ファイル |
| `LOBBY_POLL_SEC` | `5` | 状態を再取得する間隔(秒) |

## servers.json の書式

```json
[
  {
    "name": "カタン 部屋1",
    "game": "catan",
    "health": "http://catan_001:8000/health",
    "port": 6100
  }
]
```

- `health`: ロビーが巡回する内部URL(GET /health)。
- `url`: ブラウザに見せるリンク先(別物)。`http://` / `https://` / `/` で始まる文字列のみ有効。
- `port`: ブラウザに見せるリンク先のポート番号(整数 1〜65535)。`url` が無いときに使う。
- `url` と `port` の両方があるときは `url` を優先する(画面側で `port` は使わない)。
- `url` を省略した場合は、ロビーを開いているアドレスのホスト名と `port` からリンクを組み立てる
  (`location.protocol + "//" + location.hostname + ":" + port + "/"`)。
- `name`: 省略可(省略時は health の title → health URL のホスト名の順で代用)。
- `game` / `health` は必須。`url` / `port` はどちらか1つ以上必要。
- 必須項目の欠落、`url` / `port` の不正(`port` が範囲外・文字列・真偽値・小数、`url` が `javascript:` など)がある要素は
  警告を出して読み飛ばす。起動は継続する。
- ファイルが無い/壊れていても空の一覧で起動し、警告を出す。

## GET /api/servers

```json
{
  "updated": "2026-10-31T12:34:56.789012+00:00",
  "servers": [
    {
      "name": "カタン 部屋1",
      "game": "catan",
      "url": "/catan1/",
      "port": null,
      "playing_rooms": null,
      "status": "idle",
      "version": "20261029-2251-b7bc7eb",
      "stale": false
    }
  ]
}
```

- `status`: `idle` / `waiting` / `playing` / `off`(4値。`off` は応答なし・異常を意味する)
- `url` / `port`: `servers.json` の値をそのまま出す(無い方は `null`)。内部URL(`health`)は含めない。
- `playing_rooms`: 複数ルーム型(例: 犯人は踊る)だけが整数。`/health` の `playing_rooms` が
  整数(bool を除く)で 0 以上のときだけ採用し、無い・文字列・負数・小数・bool のときは `null`。
  停止中(`off`)も `null`。カタンのような単一ルーム型は常に `null`。
- `version`: health の version 文字列(文字列でなければ null)
- `stale`: 同じ game の中で最大 version より小さい(=古い)サーバーへの `true`
- 巡回が一度も終わっていない間は `updated: null` / `servers: []`

## チャット API

履歴は Python プロセスのメモリ上にだけ保持する(ファイル・DB・Docker volume には保存しない)。
認証はしない(家族・友達用)。メッセージ本文は `docker logs` に出さない。

- `GET /api/chat?after=<id>`:
  ```json
  {"epoch": 0, "messages": [{"id": 1, "name": "たろう", "text": "こんにちは", "at": "2026-11-06T12:34:56+00:00"}]}
  ```
  `after` より大きい id のメッセージだけを返す。省略時は保持している全件。
- `POST /api/chat`: JSON `{"name": "...", "text": "..."}` を追加し、追加したメッセージを返す。
  `name` は前後の空白を除いて1〜16文字、`text` は前後の空白を除いて1〜200文字。
  空・長すぎる値・不正JSONは `400`、ボディが2KB超は `413`。
- 保持は最新100件。最後の投稿から `CHAT_TTL_SEC`(600秒)経過すると、
  次の GET/POST 時に全件削除する(GETでは延長されない)。
- `id` は単調増加で、全削除後も巻き戻らない。`epoch` は全削除のたびに +1 する。
- `at` は ISO8601(UTC)。

## 共通 /health 仕様

各ゲームサーバーは共通仕様の `GET /health` を持つ(認証なし)。

```json
{"title": "...", "version": "20261029-2251-b7bc7eb", "status": "idle|waiting|playing", "playing_rooms": 1}
```

- `idle`: 起動中・アイドル(誰もいない=ここから入る) / `waiting`: 起動中・開始待ち(参加可・観戦可) / `playing`: 起動中・プレイ中
- `playing_rooms`: 省略可。複数ルーム型(犯人は踊る)だけが 0 以上の整数を返す。
- ロビー画面の「入れる部屋」は、複数ルーム型は `off` 以外なら 1 室として数え、それ以外は `waiting + idle` の合計。
- 複数ルーム型は `off` 以外ならボタンが常に「入る」、チップは `playing_rooms` が 1 以上なら「プレイ中(N室)」。
- 「停止中」はサーバーが返す値ではない。ロビー側が「応答なし・異常」を `off` として扱う。
- version は「コミット日時-コミットハッシュ」形式。ファイル `VERSION` から読む(無ければ `"dev"`)。

## テスト

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```
