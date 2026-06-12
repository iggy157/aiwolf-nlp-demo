package model

type Packet struct {
	Request        *Request    `json:"request"`
	Info           *Info       `json:"info,omitempty"`
	Setting        *Setting    `json:"setting,omitempty"`
	TalkHistory    *[]Talk     `json:"talk_history,omitempty"`
	WhisperHistory *[]Talk     `json:"whisper_history,omitempty"`
	NewTalk        *Talk       `json:"new_talk,omitempty"`
	NewWhisper     *Talk       `json:"new_whisper,omitempty"`
	// Turn はプレイヤー視点UI(/demo)向けのターン開始/終了マーカー。
	// 「いま誰の番か」だけを神視点情報なしで伝える。R_TALK_BROADCAST と併用する。
	// AIクライアント(aiwolf-nlp-common)は未知フィールドとして無視するため後方互換。
	Turn *TurnMarker `json:"turn,omitempty"`
}

// TurnMarker はプレイヤー逐次push用のターンマーカー。
type TurnMarker struct {
	Type  string `json:"type"`            // "start" | "end"
	Agent string `json:"agent"`           // 対象エージェントのゲーム内名(GameName)
	Idx   int    `json:"idx"`             // 対象エージェントのインデックス
	Phase string `json:"phase,omitempty"` // "talk" | "whisper"
}
