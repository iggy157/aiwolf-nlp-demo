package model

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"

	"github.com/gorilla/websocket"
)

type Connection struct {
	TeamName     string
	OriginalName string
	// Room は接続クエリ ?room=<id> で渡される卓ID。room_match マッチング時の
	// グループ化キー。空文字なら未指定（room_match 無効時は使われない）。
	Room string
	// DesiredRole は接続クエリ ?role=<ROLE> で渡される希望役職（例 "SEER"）。
	// 空文字なら希望なし＝従来どおりランダム。割り当て時に capacity があれば優先される。
	DesiredRole string
	// DesiredCharacter は接続クエリ ?character=<idx> で渡される希望キャラの番号（custom_profile の0始まり）。
	// -1 なら希望なし＝従来どおりランダム。範囲内かつ未使用なら優先される。
	DesiredCharacter int
	Conn             *websocket.Conn
	Header           *http.Header
}

func NewConnection(conn *websocket.Conn, header *http.Header) (*Connection, error) {
	req, err := json.Marshal(Packet{
		Request: &R_NAME,
	})
	if err != nil {
		slog.Error("NAMEパケットの作成に失敗しました", "error", err)
		return nil, err
	}
	err = conn.WriteMessage(websocket.TextMessage, req)
	if err != nil {
		slog.Error("NAMEパケットの送信に失敗しました", "error", err)
		return nil, err
	}
	slog.Info("NAMEパケットを送信しました", "remote_addr", conn.RemoteAddr().String())
	_, res, err := conn.ReadMessage()
	if err != nil {
		slog.Error("NAMEリクエストの受信に失敗しました", "error", err)
		return nil, err
	}
	originalName := strings.TrimRight(string(res), "\n")
	teamName := strings.TrimRight(originalName, "1234567890")
	connection := Connection{
		TeamName:         teamName,
		OriginalName:     originalName,
		DesiredCharacter: -1, // 既定: 希望なし（ハンドラが ?character= を見て上書きする）
		Conn:             conn,
		Header:           header,
	}
	slog.Info("クライアントが接続しました", "team_name", connection.TeamName, "original_name", connection.OriginalName, "remote_addr", conn.RemoteAddr().String())
	return &connection, nil
}
