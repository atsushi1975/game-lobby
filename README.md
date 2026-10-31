# game-lobby(共通ロビー)

複数のゲームサーバーの状態を一覧表示する「共通ロビー」。
カタン(catan)と同じ流儀で、http.server ベースの単一プロセス・Python 3.12 標準ライブラリのみ。

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
      "status": "idle",
      "version": "20261029-2251-b7bc7eb",
      "stale": false
    }
  ]
}
```

- `status`: `idle` / `waiting` / `playing` / `off`(4値。`off` は応答なし・異常を意味する)
- `url` / `port`: `servers.json` の値をそのまま出す(無い方は `null`)。内部URL(`health`)は含めない。
- `version`: health の version 文字列(文字列でなければ null)
- `stale`: 同じ game の中で最大 version より小さい(=古い)サーバーへの `true`
- 巡回が一度も終わっていない間は `updated: null` / `servers: []`

## 共通 /health 仕様

各ゲームサーバーは共通仕様の `GET /health` を持つ(認証なし)。

```json
{"title": "...", "version": "20261029-2251-b7bc7eb", "status": "idle|waiting|playing"}
```

- `idle`: 起動中・アイドル / `waiting`: 起動中・開始待ち(参加可・観戦可) / `playing`: 起動中・プレイ中
- 「停止中」はサーバーが返す値ではない。ロビー側が「応答なし・異常」を `off` として扱う。
- version は「コミット日時-コミットハッシュ」形式。ファイル `VERSION` から読む(無ければ `"dev"`)。

## テスト

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```
