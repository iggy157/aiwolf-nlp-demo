#!/usr/bin/env bash
# 開店。Docker を使わずに、デモ一式（＋必要なら vLLM）を起こして公開する。
#
#   ゲームサーバ(5人村/9人村) → ロビー → Caddy → SSH 逆トンネル の順に上げる。
#   すべてこのユーザの権限で動く。root も docker も要らない。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
RUN="$ROOT/native/run"; mkdir -p "$RUN"
LOG="$RUN/logs"; mkdir -p "$LOG"

[ -f native/env ] && set -a && . native/env && set +a
: "${LOCAL_PORT:=8088}" "${LOBBY_PORT:=8002}" "${GAME_PORT:=8080}" "${GAME_PORT_9:=8081}"
: "${PUBLIC_BASE:=https://133.167.32.100/game}" "${START_VLLM:=0}" "${VLLM_PORT:=8000}"
: "${DEFAULT_LANGUAGE:=ja}" "${AI_COUNT:=4}" "${MAX_CONCURRENT_GAMES:=4}"

GAME_BIN="$RUN/game-server"
CADDY="${CADDY_BIN:-$HOME/bin/caddy}"
VIEWER_ROOT="$ROOT/repos/aiwolf-nlp-viewer/build"

say() { printf '  %s\n' "$*"; }
die() { printf 'エラー: %s\n' "$*" >&2; exit 1; }

# 起動したものは全部ここに記録して、close.sh がまとめて落とす
: > "$RUN/pids"
track() { echo "$1" >> "$RUN/pids"; }

wait_port() {  # wait_port <port> <名前> <秒>
  for _ in $(seq 1 "${3:-40}"); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3<&-; return 0; }
    sleep 1
  done
  return 1
}

echo "== 準備 =="
[ -x "$CADDY" ] || die "caddy が無い（$CADDY）。scripts/setup-native.sh を先に実行"
[ -d "$VIEWER_ROOT" ] || die "ビューアが未ビルド。scripts/setup-native.sh を先に実行"
if [ ! -x "$GAME_BIN" ]; then
  say "ゲームサーバをビルドしています…"
  (cd repos/aiwolf-nlp-server && go build -o "$GAME_BIN" .) || die "ビルド失敗"
fi

# ---- vLLM（GPU を使う日だけ）----
if [ "$START_VLLM" = "1" ]; then
  echo "== vLLM =="
  if wait_port "$VLLM_PORT" vllm 1; then
    say "すでに $VLLM_PORT で動いています"
  else
    [ -n "${VLLM_MODEL:-}" ] || die "VLLM_MODEL が未設定"
    say "起動中: $VLLM_MODEL (GPU=${VLLM_GPUS:-0})"
    CUDA_VISIBLE_DEVICES="${VLLM_GPUS:-0}" nohup \
      "${VLLM_PYTHON:-python3}" -m vllm.entrypoints.openai.api_server \
      --model "$VLLM_MODEL" --port "$VLLM_PORT" ${VLLM_EXTRA:-} \
      > "$LOG/vllm.log" 2>&1 &
    track $!
    wait_port "$VLLM_PORT" vllm 600 || die "vLLM が上がりません（$LOG/vllm.log）"
    say "準備できました"
  fi
fi

# ---- ゲームサーバ ----
echo "== ゲームサーバ =="
nohup "$GAME_BIN" -c configs/server.yml   > "$LOG/game5.log" 2>&1 & track $!
nohup "$GAME_BIN" -c native/server9.yml   > "$LOG/game9.log" 2>&1 & track $!
wait_port "$GAME_PORT" game5 30 || die "5人村サーバが上がりません（$LOG/game5.log）"
wait_port "$GAME_PORT_9" game9 30 || die "9人村サーバが上がりません（$LOG/game9.log）"
say "5人村 :$GAME_PORT / 9人村 :$GAME_PORT_9"

# ---- 公開トンネル（TUNNEL=quick のとき）----
# cloudflared のクイックトンネルで https の一時URLを取る。無料・アカウント不要・ドメイン不要。
# 参加者の持ち込みAPIキーを平文HTTPで流さないための推奨経路。
# URLはここで初めて決まるので、ロビー起動（GAME_WS_PUBLIC_URL の算出）より先に張る。
if [ "${TUNNEL:-}" = "quick" ]; then
  echo "== トンネル（cloudflared）=="
  CFD="$(command -v cloudflared || true)"
  [ -z "$CFD" ] && [ -x "$ROOT/bin/cloudflared" ] && CFD="$ROOT/bin/cloudflared"
  if [ -z "$CFD" ]; then
    say "cloudflared が無いので取得します（./bin へ。アカウント不要）"
    case "$(uname -m)" in
      x86_64|amd64) CF_ARCH=amd64 ;;
      aarch64|arm64) CF_ARCH=arm64 ;;
      *) die "未対応アーキテクチャ $(uname -m)。cloudflared を手動導入してください" ;;
    esac
    mkdir -p "$ROOT/bin"
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
      -o "$ROOT/bin/cloudflared" || die "cloudflared の取得に失敗"
    chmod +x "$ROOT/bin/cloudflared"
    CFD="$ROOT/bin/cloudflared"
  fi
  : > "$LOG/tunnel.log"
  # 既定は http2（TCP）。QUIC(UDP) はこのネットワークだと握手後に切られる。
  # UDPが素直に通る環境なら TUNNEL_PROTOCOL=quic にすると少し速い。
  nohup "$CFD" tunnel --no-autoupdate --protocol "${TUNNEL_PROTOCOL:-http2}" \
    --url "http://127.0.0.1:${LOCAL_PORT}" > "$LOG/tunnel.log" 2>&1 & track $!
  TUNNEL_URL=""
  for _ in $(seq 1 30); do
    TUNNEL_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG/tunnel.log" | head -1)"
    [ -n "$TUNNEL_URL" ] && break
    sleep 1
  done
  [ -n "$TUNNEL_URL" ] || die "トンネルのURLが取れません（$LOG/tunnel.log）"
  PUBLIC_BASE="$TUNNEL_URL"
  say "公開URL: $PUBLIC_BASE（クイックトンネル。起動のたびに変わる）"
fi

# ---- ロビー ----
echo "== ロビー =="
export GAME_WS_INTERNAL_URL="ws://127.0.0.1:${GAME_PORT}/ws"
export GAME_WS_INTERNAL_URL_9="ws://127.0.0.1:${GAME_PORT_9}/ws"
export GAME_WS_PUBLIC_URL="${PUBLIC_BASE/http/ws}/ws"
export GAME_WS_PUBLIC_URL_9="${PUBLIC_BASE/http/ws}/ws9"
export AGENT_LLM_PYTHON="$ROOT/repos/aiwolf-nlp-agent-llm/.venv/bin/python"
export AI_COUNT MAX_CONCURRENT_GAMES DEFAULT_LANGUAGE
nohup "$ROOT/.venv-lobby/bin/uvicorn" main:app --host 127.0.0.1 --port "$LOBBY_PORT" \
  --app-dir "$ROOT/lobby" > "$LOG/lobby.log" 2>&1 & track $!
wait_port "$LOBBY_PORT" lobby 40 || die "ロビーが上がりません（$LOG/lobby.log）"
say "公開WS: $GAME_WS_PUBLIC_URL"

# ---- Caddy ----
echo "== 束ね役 =="
DEMO_SITE_ADDRESS=":${LOCAL_PORT}" LOBBY_PORT="$LOBBY_PORT" \
GAME_PORT="$GAME_PORT" GAME_PORT_9="$GAME_PORT_9" VIEWER_ROOT="$VIEWER_ROOT" \
XDG_DATA_HOME="$RUN/caddy" XDG_CONFIG_HOME="$RUN/caddy" \
nohup "$CADDY" run --config native/Caddyfile > "$LOG/caddy.log" 2>&1 & track $!
wait_port "$LOCAL_PORT" caddy 30 || die "Caddy が上がりません（$LOG/caddy.log）"
say "ローカル: http://127.0.0.1:${LOCAL_PORT}/demo"

# ---- 公開 ----
echo "== 公開 =="
if [ "${TUNNEL:-}" = "quick" ]; then
  say "クイックトンネルで公開中（SSH中継は使いません）"
else
  LOCAL_PORT="$LOCAL_PORT" "$ROOT/scripts/open-public.sh" || die "トンネルが張れません"
fi

echo
echo "開店しました。"
if [ "${TUNNEL:-}" = "quick" ]; then
  echo "  ゲーム : ${PUBLIC_BASE}/demo   ← このURLをQRに（起動のたびに変わります）"
  echo "  ※ https なので、参加者が自分のAPIキーを持ち込めます"
else
  echo "  ゲーム : ${PUBLIC_BASE}/demo"
  echo "  ※ https なので、参加者が自分のAPIキーを持ち込めます"
fi
echo "  閉じる : ./scripts/close.sh"
