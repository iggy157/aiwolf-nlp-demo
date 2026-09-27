#!/usr/bin/env bash
# デモを閉じる（閉店）。トンネルを落とすだけ。受付ページは出たまま「準備中」に戻る。
set -euo pipefail

BRIDGE_PORT="${BRIDGE_PORT:-18013}"
PIDFILE="${PIDFILE:-$HOME/.aiwolf-public.pid}"

# 張り続ける係（tunnel-loop.sh）を止める。TERM を受けると子の ssh も落とす。
if [ -f "$PIDFILE" ]; then
  kill -TERM "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
fi
sleep 1
# 取りこぼした ssh があれば落とす（自分自身と親は除外）
for pid in $(pgrep -f "ssh.*-R ${BRIDGE_PORT}:localhost:" 2>/dev/null); do
  [ "$pid" != "$$" ] && [ "$pid" != "$PPID" ] && kill "$pid" 2>/dev/null || true
done

sleep 1
if curl -fsS --max-time 8 "${PUBLIC_BASE:-https://133.167.32.100/game}/__status" | grep -q '"open": false'; then
  echo "閉店しました（受付ページは出たままです）"
else
  echo "まだ開いているように見えます。手で確認してください。" >&2
fi
