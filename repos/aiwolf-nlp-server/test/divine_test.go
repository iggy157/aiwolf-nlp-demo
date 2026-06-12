package test

import (
	"sync"
	"testing"

	"github.com/aiwolfdial/aiwolf-nlp-server/model"
	"github.com/stretchr/testify/assert"
)

func TestDivinePhase1(t *testing.T) {
	t.Log("占いフェーズ: 占い師が人狼を占う")
	config, err := model.LoadFromPath("./config/divine.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}

	executeDivinePhase(t, model.R_WEREWOLF, model.S_WEREWOLF, config)
}

func TestDivinePhase2(t *testing.T) {
	t.Log("占いフェーズ: 占い師が狂人を占う")
	config, err := model.LoadFromPath("./config/divine.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}

	executeDivinePhase(t, model.R_POSSESSED, model.S_HUMAN, config)
}

func TestDivinePhase3(t *testing.T) {
	t.Log("占いフェーズ: 占い師が村人を占う")
	config, err := model.LoadFromPath("./config/divine.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}

	executeDivinePhase(t, model.R_VILLAGER, model.S_HUMAN, config)
}

func executeDivinePhase(t *testing.T, targetRole model.Role, expectSpecies model.Species, config *model.Config) {
	roleMapping := make(map[model.Role][]string)
	var mu sync.Mutex

	handlers := map[model.Request]func(tc TestClient) (string, error){
		model.R_INITIALIZE: func(tc TestClient) (string, error) {
			if roleMap, exists := tc.info["role_map"].(map[string]any); exists {
				mu.Lock()
				for agent, role := range roleMap {
					r := model.RoleFromString(role.(string))
					roleMapping[r] = append(roleMapping[r], agent)
				}
				mu.Unlock()
			}
			return "", nil
		},
		model.R_DIVINE: func(tc TestClient) (string, error) {
			mu.Lock()
			defer mu.Unlock()
			if gameNames, exists := roleMapping[targetRole]; exists {
				return gameNames[0], nil
			}
			tc.t.Errorf("占い対象が見つかりません: %s", targetRole)
			return "", nil
		},
		model.R_FINISH: func(tc TestClient) (string, error) {
			if tc.role != model.R_SEER {
				return "", nil
			}
			if divineResult, exists := tc.info["divine_result"].(map[string]any); exists {
				assert.Equal(t, 0, int(divineResult["day"].(float64)))
				assert.Equal(t, tc.gameName, divineResult["agent"].(string))
				if gameNames, exists := roleMapping[targetRole]; exists {
					assert.Equal(t, gameNames[0], divineResult["target"].(string))
				}
				assert.Equal(t, string(expectSpecies), divineResult["result"].(string))
			} else {
				tc.t.Error("divine_resultが見つかりません")
			}
			return "", nil
		},
	}
	executeGame(t, []string{"WEREWOLF", "POSSESSED", "SEER", "VILLAGER-A", "VILLAGER-B"}, config, handlers)
}
