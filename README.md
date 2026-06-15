# aiwolf-nlp-demo

「スマホで誰でもAIと人狼対戦」学会デモシステム。QRを読んだ参加者が `/demo` で4体のLLMエージェントと
人狼対戦できる。設計の詳細は [HANDOFF.md](./HANDOFF.md) を参照。

> ⚠️ このリポジトリ群は **commit/push しない** 運用。`repos/*` の origin は削除済み（ローカル専用）。

## 構成
```
aiwolf-nlp-demo/
├── HANDOFF.md          # 設計仕様
├── README.md           # 本ファイル
├── .env                # LLM/ネットワーク設定（要編集）
├── docker-compose.yml  # caddy / game-server / lobby
├── Caddyfile           # TLS終端・静的配信・wss/api リバプロ
├── Makefile            # make public / demo / down 等
├── docker/             # 各サービスの Dockerfile
├── scripts/
│   └── serve-public.sh # 公開デモをワンコマンドで（make public の実体）
├── bin/
│   └── cloudflared     # 同梱トンネルバイナリ（make public が自動検出）
├── configs/
│   ├── server.yml      # ゲームサーバ設定（room_match, agent_count:5, turn-based, TTS無効）
│   └── agent.yml       # AI設定テンプレ（lobby が動的生成のベース）
├── lobby/              # 採番・キュー・AI spawn（FastAPI, agent-llm 同梱）
└── repos/
    ├── aiwolf-nlp-server/     # ゲーム本体（Go）★逐次push+ターンマーカー追加
    ├── aiwolf-nlp-viewer/     # ビューア（SvelteKit）★新ルート /demo 追加
    └── aiwolf-nlp-agent-llm/  # LLMエージェント（Python）★vLLM/商用API両対応
```

## 実装した変更点（パッチ概要）
- **server**: `runTurnBased` にプレイヤー向け逐次push（既存 `R_TALK_BROADCAST` + `new_talk`）と
  ターン開始マーカー（`Packet.Turn`）を追加。公開トークのみ配信（囁きは漏洩防止で対象外）。
- **viewer**: 新ルート `/demo`（既存 `/agent` は不変）。`demo-socket.ts`（`agent-socket.ts` のコピー＋拡張）で
  LINE風逐次表示・ターンUI・誤送信防止の入力ロック・ロビー連携（開始ボタン/順番待ち）。
- **agent-llm**: `openai` 型に `base_url` を追加し `vllm` エイリアス対応（商用OpenAI/vLLMをenv切替）。
  プレイヤー向け配信通知（`TALK_BROADCAST`/`WHISPER_BROADCAST`）はAIが行動せず無視するよう修正。
- **lobby**: 採番・一意セッションチーム発行・AI spawn・同時数キュー・無人運転（ハング卓回収/放棄掃除）。

## 起動手順 ※起動は運営が実施

### 0. LLM準備（どちらか）
- **商用API**: `.env` の `LLM_PROVIDER=openai`（or `google`）, `LLM_MODEL`, `OPENAI_API_KEY`（or `GOOGLE_API_KEY`）。
  ※ `OPENAI_BASE_URL` は商用openaiでは**無視**される（vLLM時のみ有効）。残っていても問題なし。
- **vLLM**: `.env` の `LLM_PROVIDER=vllm`, `LLM_MODEL`=起動モデル名, `OPENAI_BASE_URL=http://<host>:8000/v1`。
  vLLM の実起動（GPU）は別途。

> **docker権限**: 実行ユーザーが docker グループ未所属でも、`make` は自動で `sudo docker` に切替えます
> （実行時にパスワードを聞かれます）。毎回省くなら管理者に `sudo usermod -aG docker <user>` をしてもらい再ログイン。

### A.（最短）公開して試す ─ `make public`
ドメインも GitHub も不要。トンネル(cloudflared 同梱)で公開HTTPS URLを即発行し、QRに使えます。
```bash
# .env に OPENAI_API_KEY を入れておく（それだけ）
make public
#  → cloudflared 自動起動で公開URL取得 → .env の GAME_WS_PUBLIC_URL 自動設定
#  → (sudo) docker compose up --build → 「https://xxxx.trycloudflare.com/demo」を表示
#  → そのURLをQRに。スマホで対戦可能。
```
- **初回はビルドで数分**（Go/lobby/viewer の3イメージ。2回目以降はキャッシュで速い）。
- トンネルは ngrok でも可（`ngrok config add-authtoken <token>` 済みなら自動検出）。cloudflared は
  アカウント不要・初回警告ページ無しで多人数配布向き。
- **QRコードを PNG 出力**: `make public` 後にプロジェクト直下へ `demo-qr.png` を自動生成（DLして配布に使える）。
  qrencode があればローカル生成、無ければ lobby の `/api/qr` 経由で生成。
  ブラウザからは `<公開URL>/api/qr?data=<デモURL>` でも取得可。

### B. ローカル / 会場LANで試す ─ `make demo`
```bash
make demo            # = (sudo) docker compose up --build
# ブラウザ: http://localhost/demo
make health          # ロビー稼働確認
```
- 同一Wi-Fiのスマホで試すなら `.env` の `GAME_WS_PUBLIC_URL=ws://<LAN-IP>/ws` にして `http://<LAN-IP>/demo`。

### C. 本番（自前ドメイン＋TLS）
`.env` で `DEMO_SITE_ADDRESS=demo.example.com`, `GAME_WS_PUBLIC_URL=wss://demo.example.com/ws` を設定し
`make demo`。Caddy が自動で Let's Encrypt TLS を取得（独自ドメイン・80/443開放は会場側で用意）。

### D. コア検証（dockerなし・マイルストン2）
[configs/README.md](./configs/README.md) 参照。server→agent-llm(team=test,num=4)→ビューア `/agent` で自動マッチ確認。

## 村の人数（5人村 / 9人村）
`/demo` の最初のページで村の人数を選べる（既定/最小は **5人村**、**9人村**も可）。
- 5人村: 村人2・占い師・人狼・狂人（あなた＋AI4体）
- 9人村: 村人3・占い師・騎士・霊媒師・人狼2・狂人（あなた＋AI8体）

サーバの `agent_count` はプロセス固定値のため、**5人村サーバ(`game-server`, configs/server.yml)と
9人村サーバ(`game-server-9`, configs/server9.yml)を別サービスで起動**し、ロビーが選択に応じて
接続先を振り分ける（Caddy: 5人村=`/ws`、9人村=`/ws9`）。`/byo` でも人数を選択可。

## 持ち込みエージェント（/byo）
参加者が自作エージェントを接続して対戦できる。`https://<host>/byo` を開き「卓を作成」すると、
**チーム名・接続URL(wss)・設定スニペット**が表示される。参加者はそれを自分のエージェント設定
（`web_socket.url` と `agent.team`）に入れて起動すれば、その卓に参加できる。
- **fill-to-5**: 1卓5名。外部接続（持ち込みエージェント＋任意で人間1名）の残りをサンプルAIが自動で埋める。
  例) 持ち込み2体＋人間1名 → サンプルAI2体／持ち込み5体 → AI0体。
- エージェント側は「接続URL」と「末尾数字を除いた名前＝そのチーム名」になるよう設定すればよい（aiwolf-nlp-agent-llm 以外のクライアントでも可）。
- 「人間も参加」を選ぶと、その卓に人間が入る `/demo?url=…&team=…` の直リンクも表示される。

## 運用（起動・停止・ターミナル）
- **ターミナルは閉じてOK**: コンテナ(`-d`＋`restart: unless-stopped`)もトンネル(`nohup`常駐)も
  バックグラウンドで動き続ける。`make public` はURL表示後に正常終了する。
- **停止**: `make down` ─ トンネル停止(`.tunnel.pid`)＋ `docker compose down` をまとめて実行。
- **ログ**: `make logs`（全サービス）/ トンネルは `.tunnel.log`。
- **再起動後**: コンテナは自動復帰するが**トンネルは復帰しない**ので `make public` を打ち直す。
  cloudflared クイックトンネルの**URLは起動ごとに変わる**（＝QRも作り直し）。固定したい場合は独自ドメイン(C)へ。

## 検証状況（このセッションで実施）
- server: `go build ./...` 成功（パッチ込み）。
- viewer: `pnpm run check`（新規ファイルにエラーなし）/ `pnpm run build` 成功、`/demo` 出力確認。ルート配信ビルド(`BASE_PATH=""`)も確認。
- agent-llm / lobby: `py_compile` 成功。lobby の採番・キュー・config生成・リーパー（終了/ハング/放棄）はロジックテストで全項目PASS。
- docker: `docker compose config` 検証OK（イメージbuild/起動は運営側。当環境は docker socket 権限なしのため未実行）。

## 運営が用意するもの（自動化できない外部依存）
- LLM 実体（vLLM の GPU 起動 **または** 商用APIキー）
- 公開用の独自ドメイン・TLS証明書（`wss` 用。ローカルは `ws://localhost` で可）
- 会場ネットワークのインバウンド可否・ポート開放
