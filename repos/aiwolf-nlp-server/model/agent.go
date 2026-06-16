package model

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

type AgentMessage struct {
	Data []byte
	Err  error
}

type Agent struct {
	Idx                int
	TeamName           string
	OriginalName       string
	GameName           string
	Profile            *Profile
	ProfileDescription *string
	Role               Role
	Connection         *websocket.Conn
	HasError           bool
	msgChan            chan AgentMessage
	closed             chan struct{}
	// takeoverCh: 人間が切断した席に、代替（AI）の接続を引き渡すためのチャネル。
	// handleConnections が ?takeover= で来た接続を OfferTakeover で投入し、
	// 応答待ち中の WaitTakeover がそれを受け取って Connection を差し替える。
	// Connection の読みは reader goroutine がローカルに捕捉した conn を使うため、
	// a.Connection 自体への並行アクセスは無く（ゲームループ単一 goroutine のみ）、ロック不要。
	takeoverCh chan *websocket.Conn
}

func NewAgent(idx int, role Role, conn Connection) *Agent {
	agent := &Agent{
		Idx:                idx,
		TeamName:           conn.TeamName,
		OriginalName:       conn.OriginalName,
		GameName:           "Agent[" + fmt.Sprintf("%02d", idx) + "]",
		Profile:            nil,
		ProfileDescription: nil,
		Role:               role,
		Connection:         conn.Conn,
		HasError:           false,
		takeoverCh:         make(chan *websocket.Conn, 1),
	}
	agent.startReader()
	slog.Info("エージェントを作成しました", "idx", agent.Idx, "agent", agent.String(), "role", agent.Role, "connection", agent.Connection.RemoteAddr())
	return agent
}

func NewAgentWithProfile(idx int, role Role, conn Connection, profile Profile, encoding map[string]string) *Agent {
	var builder strings.Builder
	for key, value := range encoding {
		if val, ok := profile.Arguments[key]; ok {
			builder.WriteString(fmt.Sprintf("%s: %s\n", value, val))
		}
	}
	description := strings.TrimRight(builder.String(), "\n")

	agent := &Agent{
		Idx:                idx,
		TeamName:           conn.TeamName,
		OriginalName:       conn.OriginalName,
		GameName:           profile.Name,
		Profile:            &profile,
		ProfileDescription: &description,
		Role:               role,
		Connection:         conn.Conn,
		HasError:           false,
		takeoverCh:         make(chan *websocket.Conn, 1),
	}
	agent.startReader()
	slog.Info("エージェントを作成しました", "idx", agent.Idx, "agent", agent.String(), "profile", agent.ProfileDescription, "role", agent.Role, "connection", agent.Connection.RemoteAddr())
	return agent
}

// keepAlive 関連: 一時停止中など無通信が続くと、中継(cloudflaredトンネル/Caddy/NAT)が
// アイドル接続を勝手に切ってしまう。定期的に ping フレームを送って接続を生かし続ける。
const (
	keepAliveInterval  = 20 * time.Second // cloudflared等のアイドル上限(~100s)より十分短く
	keepAliveWriteWait = 10 * time.Second
)

func (a *Agent) startReader() {
	// conn/ch/closed をローカルに確定してから goroutine を起動する。
	// こうすることで Connection を差し替え（takeover）て startReader を再実行しても、
	// 旧 reader は旧 conn / 旧 ch を読み続け（やがてエラーで終了）、新 reader は新 conn / 新 ch を
	// 読むため、両者が混線しない（フィールド参照だと旧 goroutine が新 conn を読んでしまう）。
	conn := a.Connection
	ch := make(chan AgentMessage, 100)
	closed := make(chan struct{})
	a.msgChan = ch
	a.closed = closed
	go func() {
		defer close(closed)
		for {
			_, data, err := conn.ReadMessage()
			ch <- AgentMessage{Data: data, Err: err}
			if err != nil {
				return
			}
		}
	}()
	a.startKeepAlive(conn, closed)
}

// startKeepAlive は接続が閉じるまで一定間隔で ping を送り続ける。
// WriteControl は他の書き込み(WriteMessage)と並行に呼んでも安全（gorilla/websocket仕様）なので
// 送信用の排他ロックは不要。ブラウザは ping に自動で pong を返すため上り下り双方に通信が流れ、
// 一時停止中でも中継のアイドルタイムアウトで切断されなくなる。
func (a *Agent) startKeepAlive(conn *websocket.Conn, closed chan struct{}) {
	go func() {
		ticker := time.NewTicker(keepAliveInterval)
		defer ticker.Stop()
		for {
			select {
			case <-closed:
				return
			case <-ticker.C:
				if err := conn.WriteControl(
					websocket.PingMessage, nil, time.Now().Add(keepAliveWriteWait),
				); err != nil {
					return
				}
			}
		}
	}()
}

// OfferTakeover は切断した席へ代替接続を渡す（handleConnections から呼ぶ）。
// 既に1件待っている等で渡せなければ false（呼び出し側が接続を閉じる）。
func (a *Agent) OfferTakeover(conn *websocket.Conn) bool {
	select {
	case a.takeoverCh <- conn:
		return true
	default:
		return false
	}
}

// WaitTakeover は切断検知後、timeout まで代替接続を待つ。来たら Connection を差し替えて
// reader を貼り直し true を返す（呼び出し側は INITIALIZE 再送→元リクエスト再送する）。
// 時間切れなら false（呼び出し側は HasError 扱いにする）。
func (a *Agent) WaitTakeover(timeout time.Duration) bool {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case conn := <-a.takeoverCh:
		old := a.Connection
		a.Connection = conn
		a.HasError = false
		a.startReader() // 新 conn 用の reader/keepAlive を起動（旧 reader は旧 conn でやがて終了）
		if old != nil {
			_ = old.Close() // 旧接続を閉じる（旧 reader はこれで確実に終了）
		}
		slog.Info("席の接続を引き継ぎました", "agent", a.String())
		return true
	case <-timer.C:
		return false
	}
}

func (a *Agent) ReadChannel() <-chan AgentMessage {
	// freeformモードなどで直接selectするためのチャネルを返す
	return a.msgChan
}

// maxPauseBudget は1回の応答待ちにおける一時停止の上限時間。
// クライアントが C_PAUSE のまま放置(タブ閉じ等で C_RESUME が来ない)しても
// この時間で自動再開し、卓が無限にハングするのを防ぐ。
// 接続断時は reader が msgChan にエラーを流すため、それより先に解消される。
// 累積上限はロビーのセッション回収(MAX_SESSION_SECONDS)が別途担保する。
const maxPauseBudget = 10 * time.Minute

func (a *Agent) receive(timeout time.Duration) ([]byte, error) {
	// チャネルからタイムアウト付きでメッセージを受信する。
	// C_PAUSE/C_RESUME 制御トークンを受け取った場合はタイマーを止め/再開し、
	// 一時停止中はタイムアウトを進めない（/demo のサーバ側一時停止対応）。
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	deadline := time.Now().Add(timeout)
	remaining := timeout
	paused := false

	// 一時停止の上限タイマー。停止中のみ作動させる。
	pauseTimer := time.NewTimer(maxPauseBudget)
	pauseTimer.Stop()
	defer pauseTimer.Stop()

	resume := func() {
		paused = false
		pauseTimer.Stop()
		timer.Reset(remaining)
		deadline = time.Now().Add(remaining)
	}

	for {
		select {
		case msg := <-a.msgChan:
			if msg.Err != nil {
				return nil, msg.Err
			}
			switch strings.TrimSpace(string(msg.Data)) {
			case C_PAUSE:
				if !paused {
					paused = true
					remaining = time.Until(deadline)
					if remaining < 0 {
						remaining = 0
					}
					if !timer.Stop() {
						select {
						case <-timer.C:
						default:
						}
					}
					pauseTimer.Reset(maxPauseBudget)
					slog.Info("応答待ちを一時停止しました", "agent", a.String(), "remaining", remaining.String())
				}
				continue
			case C_RESUME:
				if paused {
					resume()
					slog.Info("応答待ちを再開しました", "agent", a.String(), "remaining", remaining.String())
				}
				continue
			default:
				// 通常の応答
				return msg.Data, nil
			}
		case <-timer.C:
			if paused {
				// 停止中はタイマーをStop済みのため通常ここには来ないが、念のため無視
				continue
			}
			return nil, errors.New("レスポンスの受信がタイムアウトしました")
		case <-pauseTimer.C:
			if paused {
				slog.Warn("一時停止が上限に達したため自動再開します", "agent", a.String(), "budget", maxPauseBudget.String())
				resume()
			}
			continue
		}
	}
}

func (a *Agent) DrainMessages() {
	for {
		select {
		case <-a.msgChan:
		default:
			return
		}
	}
}

func (a *Agent) SendPacket(packet Packet, actionTimeout, responseTimeout, acceptableTimeout time.Duration) (string, error) {
	if a.HasError {
		slog.Error("エージェントにエラーが発生しているため、リクエストを送信できません", "agent", a.String())
		return "", errors.New("エージェントにエラーが発生しているため、リクエストを送信できません")
	}
	req, err := json.Marshal(packet)
	if err != nil {
		slog.Error("パケットの作成に失敗しました", "error", err)
		a.HasError = true
		return "", err
	}
	err = a.Connection.WriteMessage(websocket.TextMessage, req)
	if err != nil {
		slog.Error("パケットの送信に失敗しました", "error", err)
		a.HasError = true
		return "", err
	}
	slog.Info("パケットを送信しました", "agent", a.String(), "packet", packet)
	if packet.Request.RequireResponse {
		data, err := a.receive(actionTimeout + acceptableTimeout)
		if err == nil {
			response := strings.ReplaceAll(string(data), "\n", "")
			slog.Info("レスポンスを受信しました", "agent", a.String(), "response", response)
			return response, nil
		}
		if websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
			slog.Error("接続が閉じられました", "error", err)
			a.HasError = true
			return "", err
		}
		slog.Warn("レスポンスの受信に失敗したため、NAMEリクエストを送信します", "agent", a.String(), "error", err)
		nameReq, err := json.Marshal(Packet{Request: &R_NAME})
		if err != nil {
			slog.Error("NAMEパケットの作成に失敗しました", "error", err)
			a.HasError = true
			return "", err
		}
		err = a.Connection.WriteMessage(websocket.TextMessage, nameReq)
		if err != nil {
			slog.Error("NAMEパケットの送信に失敗しました", "error", err)
			a.HasError = true
			return "", err
		}
		slog.Info("NAMEパケットを送信しました", "agent", a.String())
		data, err = a.receive(responseTimeout)
		if err != nil {
			slog.Error("NAMEリクエストのレスポンス受信に失敗しました", "agent", a.String(), "error", err)
			a.HasError = true
			return "", err
		}
		if strings.TrimRight(string(data), "\n") == a.OriginalName {
			slog.Info("NAMEリクエストのレスポンスを受信しました", "agent", a.String(), "response", string(data))
			return "", errors.New("リクエストのレスポンス受信がタイムアウトしました")
		}
		slog.Error("不正なNAMEリクエストのレスポンスを受信しました", "agent", a.String(), "response", string(data))
		a.HasError = true
		return "", errors.New("不正なNAMEリクエストのレスポンスを受信しました")
	}
	return "", nil
}

func (a *Agent) ReceiveWithTimeout(timeout time.Duration) (string, error) {
	if a.HasError {
		return "", errors.New("エージェントにエラーが発生しています")
	}

	data, err := a.receive(timeout)
	if err != nil {
		return "", err
	}

	return strings.TrimSpace(string(data)), nil
}

func (a Agent) Close() {
	a.Connection.Close()
	slog.Info("エージェントをクローズしました", "agent", a.String())
}

func (a Agent) String() string {
	return a.GameName
}

func (a Agent) MarshalJSON() ([]byte, error) {
	return json.Marshal(a.String())
}
