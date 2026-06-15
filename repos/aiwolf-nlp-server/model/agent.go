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
	}
	agent.startReader()
	slog.Info("エージェントを作成しました", "idx", agent.Idx, "agent", agent.String(), "profile", agent.ProfileDescription, "role", agent.Role, "connection", agent.Connection.RemoteAddr())
	return agent
}

func (a *Agent) startReader() {
	a.msgChan = make(chan AgentMessage, 100)
	go func() {
		for {
			_, data, err := a.Connection.ReadMessage()
			a.msgChan <- AgentMessage{Data: data, Err: err}
			if err != nil {
				return
			}
		}
	}()
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
const maxPauseBudget = 5 * time.Minute

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
