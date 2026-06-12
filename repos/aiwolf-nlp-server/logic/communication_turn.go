package logic

import (
	"log/slog"
	"math/rand"
	"time"

	"github.com/aiwolfdial/aiwolf-nlp-server/model"
)

func (s *CommunicationSession) runTurnBased() {
	rand.Shuffle(len(s.agents), func(i, j int) {
		s.agents[i], s.agents[j] = s.agents[j], s.agents[i]
	})

	for i := range s.talkSetting.MaxCount.PerDay {
		cnt := false
		for _, agent := range s.agents {
			if !s.canAgentTalk(agent) {
				continue
			}
			// プレイヤー視点UI(/demo)向け逐次push: 「今から agent の番」を他プレイヤーへ通知
			s.broadcastTurnStart(agent)

			text := s.game.getTalkWhisperText(agent, s.request)

			talk := s.buildTalk(agent, text, i, time.Now())
			s.appendTalk(talk)
			if talk.Text != model.T_OVER {
				cnt = true
			}
			s.logTalk(talk)
			// プレイヤー視点UI(/demo)向け逐次push: 確定した発話を全プレイヤーへ（発話者本人にも即時エコー）
			s.broadcastNewTalk(talk)
			slog.Info("発言を受信しました", "id", s.game.id, "agent", agent.String(), "text", talk.Text, "count", s.remainCountMap[*agent], "length", s.remainLengthMap[*agent], "skip", s.remainSkipMap[*agent])
		}
		if !cnt {
			break
		}
	}
}

// broadcastTurnStart は「今から speaker の番」マーカーを発話者以外のプレイヤーへ送る。
// 公開トーク(R_TALK)のみ対象。囁き(R_WHISPER)は情報漏洩防止のため配信しない。
func (s *CommunicationSession) broadcastTurnStart(speaker *model.Agent) {
	if s.request != model.R_TALK {
		return
	}
	req := model.R_TALK_BROADCAST
	marker := model.TurnMarker{
		Type:  "start",
		Agent: speaker.String(),
		Idx:   speaker.Idx,
		Phase: "talk",
	}
	s.broadcastToPlayers(model.Packet{Request: &req, Turn: &marker}, speaker)
}

// broadcastNewTalk は確定した発話を全プレイヤーへ逐次配信する（発話者本人にも即時表示用にエコー）。
// 公開トーク(R_TALK)のみ対象。
func (s *CommunicationSession) broadcastNewTalk(talk model.Talk) {
	if s.request != model.R_TALK {
		return
	}
	req := model.R_TALK_BROADCAST
	t := talk
	s.broadcastToPlayers(model.Packet{Request: &req, NewTalk: &t}, nil)
}

// broadcastToPlayers は応答不要パケット(R_TALK_BROADCAST)を各エージェント接続へ送信する。
// except が非nilのとき、その接続には送らない。R_TALK_BROADCAST.RequireResponse=false のため
// 送信のみで完結し、AIクライアントは未知リクエストとして無視する(starter.py 側で continue)。
// 送信失敗はゲーム進行をブロックしない（SendPacket 内で HasError が記録される）。
func (s *CommunicationSession) broadcastToPlayers(packet model.Packet, except *model.Agent) {
	for _, a := range s.agents {
		if a == except || a.HasError {
			continue
		}
		_, _ = a.SendPacket(packet, 0, 0, 0)
	}
}

func (g *Game) getTalkWhisperText(agent *model.Agent, request model.Request) string {
	text, err := g.requestToAgent(agent, request)
	if text == model.T_FORCE_SKIP {
		text = model.T_SKIP
		slog.Warn("クライアントから強制スキップが指定されたため、発言をスキップに置換しました", "id", g.id, "agent", agent.String())
	}
	if err != nil {
		text = model.T_FORCE_SKIP
		slog.Warn("リクエストの送受信に失敗したため、発言をスキップに置換しました", "id", g.id, "agent", agent.String())
	}
	return text
}
