#!/usr/bin/env bash
# Docker を使わずに動かすための一度きりの準備。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== Caddy（単体バイナリ・root不要）=="
mkdir -p "$HOME/bin"
if [ ! -x "$HOME/bin/caddy" ]; then
  url=$(curl -fsSL https://api.github.com/repos/caddyserver/caddy/releases/latest \
        | python3 -c "import json,sys;[print(a['browser_download_url']) for a in json.load(sys.stdin)['assets'] if a['name'].endswith('linux_amd64.tar.gz')]" | head -1)
  curl -fsSL "$url" | tar xz -C "$HOME/bin" caddy && chmod +x "$HOME/bin/caddy"
fi
"$HOME/bin/caddy" version

echo "== ロビーの依存 =="
[ -d .venv-lobby ] || uv venv .venv-lobby --python 3.11
uv pip install --python .venv-lobby/bin/python -r lobby/requirements.txt -q

echo "== エージェントの依存 =="
[ -d repos/aiwolf-nlp-agent-llm/.venv ] || uv venv repos/aiwolf-nlp-agent-llm/.venv --python 3.11
uv pip install --python repos/aiwolf-nlp-agent-llm/.venv/bin/python -e repos/aiwolf-nlp-agent-llm -q

echo "== ビューアのビルド =="
[ -f native/env ] && . native/env
(cd repos/aiwolf-nlp-viewer && pnpm install --no-frozen-lockfile && BASE_PATH="${VIEWER_BASE:-}" pnpm run build)

echo "== ゲームサーバのビルド =="
mkdir -p native/run
(cd repos/aiwolf-nlp-server && go build -o "$ROOT/native/run/game-server" .)

[ -f native/env ] || { cp native/env.example native/env; echo "native/env を作りました。中を確認してください"; }
echo
echo "準備できました。 ./scripts/open.sh で開店します。"
