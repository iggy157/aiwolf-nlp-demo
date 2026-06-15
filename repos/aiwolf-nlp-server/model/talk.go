package model

import (
	"encoding/json"
	"time"
)

type Talk struct {
	Idx   int       `json:"idx"`
	Day   int       `json:"day"`
	Turn  int       `json:"turn"`
	Agent Agent     `json:"agent"`
	Text  string    `json:"text"`
	Time  time.Time `json:"-"`
}

func (t Talk) MarshalJSON() ([]byte, error) {
	type Alias Talk
	return json.Marshal(&struct {
		*Alias
		Skip bool  `json:"skip"`
		Over bool  `json:"over"`
		Time int64 `json:"time"`
	}{
		Alias: (*Alias)(&t),
		Skip:  t.Text == T_SKIP || t.Text == T_FORCE_SKIP,
		Over:  t.Text == T_OVER,
		Time:  t.Time.UnixMilli(),
	})
}

const (
	T_OVER       = "Over"
	T_SKIP       = "Skip"
	T_FORCE_SKIP = "ForceSkip"
)

// 制御トークン（/demo の一時停止用）。
// 人間プレイヤーのWebSocket接続から応答待ち中に送られると、receive() の
// タイムアウト計測を一時停止/再開する。通常の応答(発話/役職名/Over/Skip 等)とは
// 衝突しない予約文字列。AIクライアントは送らないため影響なし。
const (
	C_PAUSE  = "__PAUSE__"
	C_RESUME = "__RESUME__"
)
