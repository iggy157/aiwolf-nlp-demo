#!/usr/bin/env bash
# 閉店。トンネルを落とし、起動したプロセスを全部止める（GPU も解放される）。
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/native/run"

"$ROOT/scripts/close-public.sh" || true

if [ -f "$RUN/pids" ]; then
  # 子プロセスごと落とす（エージェントは別プロセスグループで動いている）
  while read -r pid; do
    [ -n "$pid" ] || continue
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done < "$RUN/pids"
  sleep 2
  while read -r pid; do
    [ -n "$pid" ] || continue
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  done < "$RUN/pids"
  rm -f "$RUN/pids"
fi

echo "閉店しました。受付ページは「準備中」に戻ります。"
