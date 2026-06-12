package test

import (
	"errors"
	"testing"

	"github.com/aiwolfdial/aiwolf-nlp-server/model"
)

func TestFull5Game(t *testing.T) {
	config, err := model.LoadFromPath("./config/full5.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}

	handlers := map[model.Request]func(tc TestClient) (string, error){
		model.R_VOTE:   handleTarget,
		model.R_DIVINE: handleTarget,
		model.R_GUARD:  handleTarget,
		model.R_TALK: func(tc TestClient) (string, error) {
			return "Hello World!", nil
		},
		model.R_WHISPER: func(tc TestClient) (string, error) {
			return "Hello World!", nil
		},
		model.R_ATTACK: handleTarget,
	}
	executeSelfMatchGame(t, config, handlers)
}

func TestFull13Game(t *testing.T) {
	config, err := model.LoadFromPath("./config/full13.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}

	handlers := map[model.Request]func(tc TestClient) (string, error){
		model.R_VOTE:   handleTarget,
		model.R_DIVINE: handleTarget,
		model.R_GUARD:  handleTarget,
		model.R_TALK: func(tc TestClient) (string, error) {
			return "Hello World!", nil
		},
		model.R_WHISPER: func(tc TestClient) (string, error) {
			return "Hello World!", nil
		},
		model.R_ATTACK: handleTarget,
	}
	executeSelfMatchGame(t, config, handlers)
}

func handleTarget(tc TestClient) (string, error) {
	if statusMap, exists := tc.info["status_map"].(map[string]any); exists {
		for k, v := range statusMap {
			if k == tc.info["agent"].(string) {
				continue
			}
			if v == model.S_ALIVE.String() {
				return k, nil
			}
		}
		return "", errors.New("投票対象が見つかりません")
	}
	return "", errors.New("status_mapが見つかりません")
}
