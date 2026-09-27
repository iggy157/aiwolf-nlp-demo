#!/usr/bin/env bash
# SSH 逆トンネルを張り続ける（切れたら5秒後に張り直す）。open-public.sh から setsid で起動される。
#   このマシンの LOCAL_PORT → 公開サーバの BRIDGE_PORT（中継 aiwolf-relay がここを見ている）
# 止めるときはこのプロセスに TERM（close-public.sh）。子の ssh も一緒に落とす。
set -u
REMOTE="${REMOTE:-aiwolf}"
LOCAL_PORT="${LOCAL_PORT:-8088}"
BRIDGE_PORT="${BRIDGE_PORT:-18013}"

child=""
cleanup() { [ -n "$child" ] && kill -TERM "$child" 2>/dev/null; exit 0; }
trap cleanup TERM INT

while :; do
  ssh -o BatchMode=yes -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=15 \
      -N -R "${BRIDGE_PORT}:localhost:${LOCAL_PORT}" "$REMOTE" &
  child=$!
  wait "$child"
  echo "$(date '+%F %T') tunnel exited (rc=$?), reconnecting in 5s" >&2
  child=""
  sleep 5
done
