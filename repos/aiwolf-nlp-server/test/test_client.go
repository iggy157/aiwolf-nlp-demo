package test

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"testing"
	"time"

	"github.com/aiwolfdial/aiwolf-nlp-server/model"
	"github.com/gorilla/websocket"
)

type TestClient struct {
	t              *testing.T
	conn           *websocket.Conn
	done           chan struct{}
	originalName   string
	gameName       string
	request        model.Request
	info           map[string]any
	setting        map[string]any
	talkHistory    []any
	whisperHistory []any
	role           model.Role
	handlers       map[model.Request]func(tc TestClient) (string, error)
}

func NewTestClient(t *testing.T, u url.URL, name string, handlers map[model.Request]func(tc TestClient) (string, error)) (*TestClient, error) {
	c, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("dial: %v", err)
	}
	client := &TestClient{
		t:            t,
		conn:         c,
		done:         make(chan struct{}),
		originalName: name,
		handlers:     handlers,
	}
	go client.listen()
	return client, nil
}

func (tc *TestClient) listen() {
	defer close(tc.done)
	for {
		_, message, err := tc.conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err) || websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				tc.t.Logf("connection closed: %v", err)
				return
			}
			tc.t.Logf("read: %v", err)
			return
		}
		tc.t.Logf("recv: %s", message)

		var recv map[string]any
		if err := json.Unmarshal(message, &recv); err != nil {
			tc.t.Logf("unmarshal: %v", err)
			continue
		}

		req := model.RequestFromString(recv["request"].(string))
		resp, err := tc.handleRequest(req, recv)
		if err != nil {
			tc.t.Error(err)
		}
		tc.request = req

		if req.RequireResponse {
			err = tc.conn.WriteMessage(websocket.TextMessage, []byte(resp))
			if err != nil {
				if websocket.IsUnexpectedCloseError(err) || websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
					tc.t.Logf("connection closed: %v", err)
					return
				}
				tc.t.Logf("write: %v", err)
				continue
			}
			tc.t.Logf("send: %s", resp)
		}

		if req == model.R_FINISH {
			tc.t.Logf("close")
			tc.conn.Close()
			return
		}
	}
}

func (tc *TestClient) setInfo(recv map[string]any) error {
	if info, exists := recv["info"].(map[string]any); exists {
		tc.info = info
		if tc.gameName == "" {
			if agent, exists := info["agent"].(string); exists {
				tc.gameName = agent
			} else {
				return errors.New("agentが見つかりません")
			}
			if roleMap, exists := info["role_map"].(map[string]any); exists {
				if role, exists := roleMap[tc.gameName].(string); exists {
					tc.role = model.RoleFromString(role)
				} else {
					return fmt.Errorf("エージェントの役職が見つかりません: %s", tc.gameName)
				}
			} else {
				return errors.New("role_mapが見つかりません")
			}
		}
	} else {
		return errors.New("infoが見つかりません")
	}
	return nil
}

func (tc *TestClient) setSetting(recv map[string]any) error {
	if setting, exists := recv["setting"].(map[string]any); exists {
		tc.setting = setting
	} else {
		return errors.New("settingが見つかりません")
	}
	return nil
}

func (tc *TestClient) handleRequest(request model.Request, recv map[string]any) (string, error) {
	switch request {
	case model.R_NAME:
		return tc.originalName, nil
	case model.R_INITIALIZE, model.R_DAILY_INITIALIZE:
		err := tc.setInfo(recv)
		if err != nil {
			return "", err
		}
		err = tc.setSetting(recv)
		if err != nil {
			return "", err
		}
	case model.R_VOTE, model.R_DIVINE, model.R_GUARD:
		err := tc.setInfo(recv)
		if err != nil {
			return "", err
		}
	case model.R_DAILY_FINISH, model.R_TALK, model.R_WHISPER, model.R_ATTACK:
		err := tc.setInfo(recv)
		if err != nil {
			return "", err
		}
		if request == model.R_TALK || request == model.R_DAILY_FINISH {
			if talkHistory, exists := recv["talk_history"].([]any); exists {
				tc.talkHistory = append(tc.talkHistory, talkHistory...)
			} else {
				return "", errors.New("talk_historyが見つかりません")
			}
		}
		if request == model.R_WHISPER || request == model.R_ATTACK || (request == model.R_DAILY_FINISH && tc.role == model.R_WEREWOLF) {
			if whisperHistory, exists := recv["whisper_history"].([]any); exists {
				tc.whisperHistory = append(tc.whisperHistory, whisperHistory...)
			} else {
				return "", errors.New("whisper_historyが見つかりません")
			}
		}
	case model.R_FINISH:
		err := tc.setInfo(recv)
		if err != nil {
			return "", err
		}
	}
	if handler, exists := tc.handlers[request]; exists {
		resp, err := handler(*tc)
		if err != nil {
			return "", fmt.Errorf("handle %s: %v", request.String(), err)
		}
		return resp, nil
	} else {
		return "", nil
	}
}

func (tc *TestClient) close() {
	tc.conn.Close()
	select {
	case <-tc.done:
	case <-time.After(time.Second):
	}
}

func (tc *TestClient) validateStatusPattern(expectStatuses []map[string]model.Status, nameMap map[string]string) (string, error) {
	if statusMap, exists := tc.info["status_map"].(map[string]any); exists {
		for _, expectStatus := range expectStatuses {
			matchesPattern := true
			for k, expectedStatus := range expectStatus {
				if v, ok := statusMap[nameMap[k]]; ok {
					if v != expectedStatus.String() {
						matchesPattern = false
						break
					}
				} else {
					matchesPattern = false
					break
				}
			}
			if matchesPattern {
				tc.t.Logf("期待されるステータスパターンと一致しました")
				for k, v := range statusMap {
					tc.t.Logf("%s: %s", k, v)
				}
				return "", nil
			}
		}
		tc.t.Errorf("期待されるステータスパターンと一致しません")
		for k, v := range statusMap {
			tc.t.Logf("%s: %s", k, v)
		}
	} else {
		tc.t.Error("status_mapが見つかりません")
	}
	return "", nil
}
