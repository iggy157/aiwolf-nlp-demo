#!/usr/bin/env bash
# 公開デモを最短で立ち上げる。
#   トンネル(cloudflared/ngrok)で公開HTTPS URLを取得 → .env を自動設定 → docker compose 起動
#   → QR用の /demo URL を表示。
#
# 事前準備（あなたがやること）:
#   1) .env に LLM 設定（LLM_PROVIDER / LLM_MODEL / OPENAI_API_KEY 等）を記入
#   2) cloudflared か ngrok を導入
#        - cloudflared: アカウント不要。`cloudflared tunnel --url` のクイックトンネルを使用（推奨）
#        - ngrok      : 無料アカウントの authtoken が必要（`ngrok config add-authtoken <token>` を一度）
#   3) docker を使える権限（`docker ps` が通ること）
#
# 使い方:  make public   （= bash scripts/serve-public.sh）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ENV_FILE="$ROOT/.env"
PORT="${HTTP_PORT:-80}"

# プロジェクト同梱のトンネルバイナリ(./bin/cloudflared 等)を優先的に使えるように
export PATH="$ROOT/bin:$PATH"

# ---------- preflight ----------
[ -f "$ENV_FILE" ] || { echo "ERROR: .env がありません（$ENV_FILE）"; exit 1; }
if ! grep -qE '^(OPENAI_API_KEY|GOOGLE_API_KEY|VLLM_API_KEY)=.+' "$ENV_FILE"; then
  echo "WARNING: .env に APIキー(OPENAI_API_KEY 等)が未設定のようです。商用APIなら必須です。"
fi
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker が見つかりません"; exit 1; }

# ---------- choose tunnel ----------
TUNNEL=""
if command -v cloudflared >/dev/null 2>&1; then TUNNEL=cloudflared
elif command -v ngrok >/dev/null 2>&1; then TUNNEL=ngrok
else
  # どちらも無ければ cloudflared を ./bin に自動取得（新規クローン環境=WSL等でもそのまま動く）
  echo "==> cloudflared / ngrok が無いので cloudflared を自動取得します（./bin, アカウント不要）"
  case "$(uname -m)" in
    x86_64|amd64) CF_ARCH=amd64 ;;
    aarch64|arm64) CF_ARCH=arm64 ;;
    *) echo "ERROR: 未対応アーキテクチャ $(uname -m)。手動で cloudflared/ngrok を導入してください。"; exit 1 ;;
  esac
  mkdir -p "$ROOT/bin"
  if curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -o "$ROOT/bin/cloudflared"; then
    chmod +x "$ROOT/bin/cloudflared"
    TUNNEL=cloudflared
    echo "==> cloudflared を取得しました: $ROOT/bin/cloudflared"
  else
    echo "ERROR: cloudflared の自動取得に失敗しました。ネットワークを確認するか手動導入してください。"
    exit 1
  fi
fi
echo "==> tunnel: $TUNNEL  (forward -> http://localhost:$PORT)"

TUNNEL_LOG="$ROOT/.tunnel.log"
TUNNEL_PID_FILE="$ROOT/.tunnel.pid"
PUBLIC_URL=""

# 起動途中に中断(Ctrl+C)したら起ち上げかけのトンネルを片付ける。
# ※成功時は正常終了(EXITはトラップ対象外)なのでトンネルは残る。
cleanup_on_abort() { [ -n "${TUNNEL_PID:-}" ] && kill "$TUNNEL_PID" 2>/dev/null || true; }
trap cleanup_on_abort INT TERM

# nohup + disown で、ターミナルを閉じても(SIGHUP)生き続けるよう常駐させる。
if [ "$TUNNEL" = cloudflared ]; then
  nohup cloudflared tunnel --url "http://localhost:$PORT" >"$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!
  disown "$TUNNEL_PID" 2>/dev/null || true
  for _ in $(seq 1 40); do
    PUBLIC_URL="$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
    [ -n "$PUBLIC_URL" ] && break
    sleep 1
  done
else
  # ngrok: ローカル管理API(4040)から公開URLを取得
  nohup ngrok http "$PORT" --log=stdout >"$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!
  disown "$TUNNEL_PID" 2>/dev/null || true
  for _ in $(seq 1 40); do
    PUBLIC_URL="$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
      | grep -oE 'https://[a-zA-Z0-9.-]+\.ngrok[a-zA-Z0-9.-]*' | head -1 || true)"
    [ -n "$PUBLIC_URL" ] && break
    sleep 1
  done
fi
echo "$TUNNEL_PID" > "$TUNNEL_PID_FILE"

if [ -z "$PUBLIC_URL" ]; then
  echo "ERROR: 公開URLの取得に失敗しました。ログ: $TUNNEL_LOG"
  sed -n '1,20p' "$TUNNEL_LOG" || true
  cleanup
  exit 1
fi
HOST="${PUBLIC_URL#https://}"
echo "==> public URL: $PUBLIC_URL"

# ---------- .env 自動更新 ----------
set_env() { # key value
  local k="$1" v="$2"
  if grep -qE "^${k}=" "$ENV_FILE"; then
    sed -i "s|^${k}=.*|${k}=${v}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$k" "$v" >>"$ENV_FILE"
  fi
}
set_env DEMO_SITE_ADDRESS ":$PORT"
set_env GAME_WS_PUBLIC_URL "wss://$HOST/ws"
# 全言語サーバを立てる（lobby は未起動言語を ja にフォールバック）
set_env I18N_SERVER_LANGS "all"
echo "==> .env updated: GAME_WS_PUBLIC_URL=wss://$HOST/ws, I18N_SERVER_LANGS=all"

# ---------- 言語別サーバ設定の生成 ----------
# configs/generated/server.<lang>.yml, docker-compose.langs.yml, caddy/langs.caddy を生成。
echo "==> generating per-language server configs (scripts/gen_i18n.py)"
python3 "$ROOT/scripts/gen_i18n.py"

# ---------- compose 起動 ----------
# docker グループ未所属なら sudo 経由に自動切替（パスワードを対話入力）
if docker ps >/dev/null 2>&1; then
  DOCKER="docker"
else
  echo "==> docker 実行に sudo が必要です。パスワードを求められたら入力してください。"
  DOCKER="sudo docker"
fi
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.langs.yml"
echo "==> ${DOCKER} compose ${COMPOSE_FILES} up --build -d  （言語別サーバ含む全卓）"
$DOCKER compose $COMPOSE_FILES up --build -d

DEMO_URL="$PUBLIC_URL/demo"
QR_PNG="$ROOT/demo-qr.png"
echo
echo "============================================================"
echo "  公開デモURL（QRにこれを埋める）:"
echo "  $DEMO_URL"
echo "============================================================"

# --- QR を PNG ファイルに出力（ダウンロードするだけで使える）---
#   1) qrencode があればローカル生成
#   2) 無ければ lobby の /api/qr エンドポイント経由でPNG取得（起動待ちでリトライ）
if command -v qrencode >/dev/null 2>&1; then
  qrencode -o "$QR_PNG" -s 10 -m 2 "$DEMO_URL" && echo "  QRコード(PNG): $QR_PNG"
  qrencode -t ANSIUTF8 "$DEMO_URL" 2>/dev/null || true
else
  echo "  QR(PNG)を lobby 経由で生成中…"
  for _ in $(seq 1 30); do
    if curl -fsS -G --data-urlencode "data=$DEMO_URL" \
        "http://localhost:${PORT}/api/qr" -o "$QR_PNG" 2>/dev/null; then
      echo "  QRコード(PNG): $QR_PNG"
      break
    fi
    sleep 1
  done
fi
echo "  ブラウザでDLも可: ${PUBLIC_URL}/api/qr?data=$(printf %s "$DEMO_URL" | sed 's/:/%3A/g; s#/#%2F#g')"
echo
echo "コンテナもトンネルも【バックグラウンド常駐】です。"
echo "  → このターミナルは閉じてOK。明示的に止めるまで動き続けます。"
echo "  停止: make down   （コンテナ停止 + トンネル停止をまとめて実行）"
echo "  トンネルPID: $TUNNEL_PID  (ログ: $TUNNEL_LOG)"
if [ "$TUNNEL" = ngrok ]; then
  echo "注意: ngrok 無料枠は初回アクセスに警告ページ(クリック)が挟まります。"
  echo "      多人数に配るなら cloudflared（警告なし・アカウント不要）を推奨。"
fi
# wait はしない。トンネルは nohup+disown で常駐済みなので、スクリプトは正常終了する。
