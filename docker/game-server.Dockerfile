# ゲームサーバ（Go）。build context = 作業ルート(aiwolf-nlp-demo/)
FROM golang:1.24 AS build
WORKDIR /src
COPY repos/aiwolf-nlp-server/ ./
RUN CGO_ENABLED=0 go build -o /out/server .

FROM alpine:3.20
WORKDIR /app
# main.go が起動時に ./config/.env を任意ロードするため空ファイルを用意（無くても致命的ではない）
RUN mkdir -p config log cache && touch config/.env
COPY --from=build /out/server /app/server
EXPOSE 8080
ENTRYPOINT ["/app/server"]
# configs/ は compose で /app/configs にマウントする
CMD ["-c", "/app/configs/server.yml"]
