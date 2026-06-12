# aiwolf-nlp-demo: 起動を束ねる（HANDOFF §9-7）
# 事前準備: .env に LLM 設定（LLM_PROVIDER / LLM_MODEL / APIキー or vLLM）を記入。

# docker グループ未所属なら自動で sudo 経由（実行時にパスワードを聞かれます）
DOCKER := $(shell docker ps >/dev/null 2>&1 && echo docker || echo sudo docker)

.PHONY: demo up up-d build down logs ps restart health public

# 公開デモをワンコマンドで（トンネル取得→.env設定→compose→QR表示）
public:
	bash scripts/serve-public.sh

# 会場ワンショット起動（ビルド込み）
demo: up

up:
	$(DOCKER) compose up --build

# バックグラウンド起動
up-d:
	$(DOCKER) compose up --build -d

build:
	$(DOCKER) compose build

# コンテナ停止 + 常駐トンネルの停止
down:
	-@[ -f .tunnel.pid ] && kill $$(cat .tunnel.pid) 2>/dev/null && echo "tunnel stopped" || true
	-@rm -f .tunnel.pid
	$(DOCKER) compose down

logs:
	$(DOCKER) compose logs -f

ps:
	$(DOCKER) compose ps

restart:
	$(DOCKER) compose restart

# ロビーの稼働状況確認
health:
	curl -s http://localhost/api/health | (python3 -m json.tool 2>/dev/null || cat)
