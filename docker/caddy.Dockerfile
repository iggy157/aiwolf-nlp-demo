# Caddy（TLS終端＋静的ビューア配信＋wss/api リバプロ）。build context = 作業ルート
# 多段ビルド: 1) ビューアを BASE_PATH="" でルート配信用にビルド → 2) caddy イメージへ
FROM node:22-slim AS build
RUN corepack enable
WORKDIR /viewer
COPY repos/aiwolf-nlp-viewer/ ./
RUN pnpm install --no-frozen-lockfile
# 会場はルート配信（/demo 等）なので base を空に
ENV BASE_PATH=""
RUN pnpm run build

FROM caddy:2
COPY --from=build /viewer/build /srv
COPY Caddyfile /etc/caddy/Caddyfile
# 言語別サーバへの WebSocket ルート（scripts/gen_i18n.py が生成）。Caddyfile が import する。
COPY caddy/langs.caddy /etc/caddy/langs.caddy
