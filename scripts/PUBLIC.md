# 公開のしかた（Docker なし・ngrok なし・費用ゼロ）

```
        いつでも: https://133.167.32.100/game/demo    これ1本（443番・ポート番号なし）
        予備:     https://133.167.32.100:8013/demo    ポート付き。443が塞がれる環境向け

[ 公開サーバ 133.167.32.100 ]  nginx が TLS を終端し /game を外して中継へ渡す
        ↓ 127.0.0.1:8014（中継）→ 127.0.0.1:18013（SSH逆トンネル）
[ freccia ]  ゲームサーバ / ロビー / ビューア / Caddy（＋必要なら vLLM）
```

トンネルが無いあいだ、`/game/` は「いまは開いていません」を返す。

トンネルが無いあいだ、8013 は「いまは開いていません」を返す。受付ページはそれを見て
「開催中 / 準備中」を出し分ける（10秒ごと）。

## 使い方

```bash
./scripts/setup-native.sh   # 最初の一度だけ（Caddy 取得・依存導入・ビルド）
./scripts/open.sh           # 開店（vLLM → ゲームサーバ → ロビー → Caddy → トンネル）
./scripts/close.sh          # 閉店（全部落とす。GPU も解放される）
```

設定は `native/env`（`native/env.example` をコピー。git に入らない）。

| | |
|---|---|
| `START_VLLM` | 1 なら open.sh が vLLM も起こす。GPU を使わない日は 0 |
| `LLM_PROVIDER` / `LLM_MODEL` | サーバ側の既定。`vllm` なら `OPENAI_BASE_URL` も |
| `LOCAL_PORT` | Caddy が待つポート（既定 8088）。トンネルはここを公開する |

## Docker を使わない構成

| プロセス | 待つ場所 | 実体 |
|---|---|---|
| ゲームサーバ（5人村） | `127.0.0.1:8080` | Go バイナリ `native/run/game-server` |
| ゲームサーバ（9人村） | `127.0.0.1:8081` | 同上（`native/server9.yml`。素で動かすとポートが重なるので分けてある） |
| ロビー | `127.0.0.1:8002` | `.venv-lobby` の uvicorn |
| Caddy | `:8088` | `~/bin/caddy`（単体バイナリ・root不要）+ `native/Caddyfile` |
| ビューア | 静的 | `repos/aiwolf-nlp-viewer/build` |

## LLM の用意と、遊ぶ人の持ち込みキー

サーバ側に用意があれば手ぶらで遊べる。無ければ遊ぶ人が自分のキーを入れる。

- `/api/health` の `server_llm_ready` が用意の有無を返す
- 画面はそれを見て「そのまま遊べます」/「いまは自分のAPIキーが要ります」を出し分ける
- キーは**その卓のエージェントの子プロセスの環境変数にだけ**入る。親の環境も他の卓も汚さない
- ログにも生成 config にも API のレスポンスにも出さない。卓が消えれば一緒に消える
- 受け付けるのは `openai` と `google` のみ。`vllm` の `base_url` は持ち込みキーには付けない


## 公開サーバ側（設置済み・sudo 不要）

| | |
|---|---|
| `~/aiwolf-relay/relay.py` | 8013 → 127.0.0.1:18013 の中継。落ちていれば閉店ページ、`/__status` で状態 |
| `~/.config/systemd/user/aiwolf-relay.service` | 常駐。サーバ再起動後も自動で戻る |

やめるとき: `ssh aiwolf 'systemctl --user disable --now aiwolf-relay'`
