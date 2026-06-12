package test

import (
	"sync"
	"testing"

	"github.com/aiwolfdial/aiwolf-nlp-server/model"
)

func TestAttackPhase1(t *testing.T) {
	t.Log("襲撃フェーズ: 人狼が狂人を襲撃する")
	config, err := model.LoadFromPath("./config/attack.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}

	targetMap := map[string]string{
		"WEREWOLF": "POSSESSED",
	}
	expectStatuses := []map[string]model.Status{
		{
			"WEREWOLF":   model.S_ALIVE,
			"POSSESSED":  model.S_DEAD,
			"SEER":       model.S_ALIVE,
			"VILLAGER-A": model.S_ALIVE,
			"VILLAGER-B": model.S_ALIVE,
		},
	}
	executeAttackPhase(t, targetMap, expectStatuses, config)
}

func TestAttackPhase2(t *testing.T) {
	t.Log("襲撃フェーズ: 人狼が占い師を襲撃する")
	config, err := model.LoadFromPath("./config/attack.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}

	targetMap := map[string]string{
		"WEREWOLF": "SEER",
	}
	expectStatuses := []map[string]model.Status{
		{
			"WEREWOLF":   model.S_ALIVE,
			"POSSESSED":  model.S_ALIVE,
			"SEER":       model.S_DEAD,
			"VILLAGER-A": model.S_ALIVE,
			"VILLAGER-B": model.S_ALIVE,
		},
	}
	executeAttackPhase(t, targetMap, expectStatuses, config)
}

func TestAttackPhase3(t *testing.T) {
	t.Log("襲撃フェーズ: 人狼が村人を襲撃する")
	config, err := model.LoadFromPath("./config/attack.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}

	targetMap := map[string]string{
		"WEREWOLF": "VILLAGER-A",
	}
	expectStatuses := []map[string]model.Status{
		{
			"WEREWOLF":   model.S_ALIVE,
			"POSSESSED":  model.S_ALIVE,
			"SEER":       model.S_ALIVE,
			"VILLAGER-A": model.S_DEAD,
			"VILLAGER-B": model.S_ALIVE,
		},
	}
	executeAttackPhase(t, targetMap, expectStatuses, config)
}

func TestAttackPhase4(t *testing.T) {
	t.Log("襲撃フェーズ: 自己投票が許可されている場合、人狼が人狼を襲撃できない")
	config, err := model.LoadFromPath("./config/attack.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}

	targetMap := map[string]string{
		"WEREWOLF": "WEREWOLF",
	}
	expectStatuses := []map[string]model.Status{
		{
			"WEREWOLF":   model.S_ALIVE,
			"POSSESSED":  model.S_ALIVE,
			"SEER":       model.S_ALIVE,
			"VILLAGER-A": model.S_ALIVE,
			"VILLAGER-B": model.S_ALIVE,
		},
	}
	executeAttackPhase(t, targetMap, expectStatuses, config)
}

func TestAttackPhase5(t *testing.T) {
	t.Log("襲撃フェーズ: 自己投票が許可されていない場合、人狼が人狼を襲撃できない")
	config, err := model.LoadFromPath("./config/attack.yml")
	if err != nil {
		t.Fatalf("設定ファイルの読み込みに失敗しました: %v", err)
	}
	config.Game.AttackVote.AllowSelfVote = false

	targetMap := map[string]string{
		"WEREWOLF": "WEREWOLF",
	}
	expectStatuses := []map[string]model.Status{
		{
			"WEREWOLF":   model.S_ALIVE,
			"POSSESSED":  model.S_ALIVE,
			"SEER":       model.S_ALIVE,
			"VILLAGER-A": model.S_ALIVE,
			"VILLAGER-B": model.S_ALIVE,
		},
	}
	executeAttackPhase(t, targetMap, expectStatuses, config)
}

func executeAttackPhase(t *testing.T, targetMap map[string]string, expectStatuses []map[string]model.Status, config *model.Config) {
	nameMap := make(map[string]string)
	var mu sync.Mutex

	handlers := map[model.Request]func(tc TestClient) (string, error){
		model.R_INITIALIZE: func(tc TestClient) (string, error) {
			mu.Lock()
			nameMap[tc.originalName] = tc.gameName
			mu.Unlock()
			return "", nil
		},
		model.R_ATTACK: func(tc TestClient) (string, error) {
			mu.Lock()
			target := nameMap[targetMap[tc.originalName]]
			mu.Unlock()
			tc.t.Logf("襲撃投票: %s -> %s", tc.gameName, target)
			return target, nil
		},
		model.R_FINISH: func(tc TestClient) (string, error) {
			return tc.validateStatusPattern(expectStatuses, nameMap)
		},
	}
	executeGame(t, []string{"WEREWOLF", "POSSESSED", "SEER", "VILLAGER-A", "VILLAGER-B"}, config, handlers)
}
