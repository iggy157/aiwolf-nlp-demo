#!/usr/bin/env bash
# デモを公開する（開店）。
#
#   デモ本体（このマシン）の LOCAL_PORT を、公開サーバの 8013 番へ SSH の逆トンネルで届ける。
#   公開サーバ側では常駐の中継（aiwolf-relay）が待っていて、
#   トンネルが無いあいだは「いまは開いていません」を返す。
#
#   ngrok も cloudflared もドメインも要らない。費用ゼロ。
set -euo pipefail

REMOTE="${REMOTE:-aiwolf}"          # ~/.ssh/config の Host 名
LOCAL_PORT="${LOCAL_PORT:-8088}"    # このマシンでデモが見えているポート（Caddy の LOCAL_PORT）
BRIDGE_PORT="${BRIDGE_PORT:-18013}" # 公開サーバ側の内向きポート（中継がここを見ている）
PUBLIC_URL="${PUBLIC_URL:-https://133.167.32.100/game}"
PIDFILE="${PIDFILE:-$HOME/.aiwolf-public.pid}"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "すでに開いています（pid $(cat "$PIDFILE")）"
  exit 0
fi

# 張り続ける係（tunnel-loop.sh）を切り離して起動する。切れても自分で張り直す。
LOG_DIR="${LOG_DIR:-$(dirname "$0")/../native/run/logs}"
mkdir -p "$LOG_DIR"
REMOTE="$REMOTE" LOCAL_PORT="$LOCAL_PORT" BRIDGE_PORT="$BRIDGE_PORT" \
  setsid nohup "$(dirname "$0")/tunnel-loop.sh" > "$LOG_DIR/tunnel.log" 2>&1 < /dev/null &
echo $! > "$PIDFILE"

# 中継が「開いている」と言うまで最大 20 秒待つ
for _ in $(seq 1 10); do
  sleep 2
  curl -fsS --max-time 8 "${PUBLIC_URL}/__status" 2>/dev/null | grep -q '"open": true' && break
done
if curl -fsS --max-time 8 "${PUBLIC_URL}/__status" | grep -q '"open": true'; then
  echo "開店しました"
  echo "  ゲーム : ${PUBLIC_URL}/demo"
else
  echo "トンネルは張れましたが、まだ中身が応答しません。" >&2
  echo "  このマシンの ${LOCAL_PORT} 番でデモが動いているか確認してください。" >&2
  exit 1
fi
