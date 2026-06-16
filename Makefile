# aiwolf-nlp-demo: 起動を束ねる（HANDOFF §9-7）
# 事前準備: .env に LLM 設定（LLM_PROVIDER / LLM_MODEL / APIキー or vLLM）を記入。

# docker グループ未所属なら自動で sudo 経由（実行時にパスワードを聞かれます）
DOCKER := $(shell docker ps >/dev/null 2>&1 && echo docker || echo sudo docker)

# 言語別サーバを含めた compose（base + 生成 override）。全卓を多言語対応で起動する。
COMPOSE := $(DOCKER) compose -f docker-compose.yml -f docker-compose.langs.yml

.PHONY: demo up up-d build down logs ps restart health public gen

# 公開デモをワンコマンドで（トンネル取得→.env設定→生成→compose→QR表示）
public:
	bash scripts/serve-public.sh

# 言語別サーバ設定の生成（server.<lang>.yml / docker-compose.langs.yml / caddy/langs.caddy）
gen:
	python3 scripts/gen_i18n.py

# 会場ワンショット起動（ビルド込み）
demo: up

up: gen
	$(COMPOSE) up --build

# バックグラウンド起動
up-d: gen
	$(COMPOSE) up --build -d

build: gen
	$(COMPOSE) build

# コンテナ停止 + 常駐トンネルの停止
down: gen
	-@[ -f .tunnel.pid ] && kill $$(cat .tunnel.pid) 2>/dev/null && echo "tunnel stopped" || true
	-@rm -f .tunnel.pid
	$(COMPOSE) down

logs: gen
	$(COMPOSE) logs -f

ps: gen
	$(COMPOSE) ps

restart: gen
	$(COMPOSE) restart

# ロビーの稼働状況確認
health:
	curl -s http://localhost/api/health | (python3 -m json.tool 2>/dev/null || cat)
