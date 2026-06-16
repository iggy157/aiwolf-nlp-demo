package logic

import (
	"errors"
	"log/slog"
	"strings"
	"time"

	"github.com/aiwolfdial/aiwolf-nlp-server/model"
	"github.com/aiwolfdial/aiwolf-nlp-server/util"
)

func (g *Game) findTargetByRequest(agent *model.Agent, request model.Request) (*model.Agent, error) {
	name, err := g.requestToAgent(agent, request)
	if err != nil {
		return nil, err
	}
	target := util.FindAgentByName(g.agents, name)
	if target == nil {
		return nil, errors.New("対象エージェントが見つかりません")
	}
	slog.Info("対象エージェントを受信しました", "id", g.id, "agent", agent.String(), "target", target.String())
	return target, nil
}
func (g *Game) closeAllAgents() {
	for _, agent := range g.agents {
		agent.Close()
	}
}

func (g *Game) requestToEveryone(request model.Request) {
	for _, agent := range g.agents {
		g.requestToAgent(agent, request)
	}
}

func (g *Game) buildInfo(agent *model.Agent) model.Info {
	info := model.Info{
		GameID: g.id,
		Day:    g.currentDay,
		Agent:  agent,
	}
	gameStatus := g.getCurrentGameStatus()
	lastGameStatus := g.gameStatuses[g.currentDay-1]
	if lastGameStatus != nil {
		if lastGameStatus.MediumResult != nil && agent.Role == model.R_MEDIUM {
			info.MediumResult = lastGameStatus.MediumResult
		}
		if lastGameStatus.DivineResult != nil && agent.Role == model.R_SEER {
			info.DivineResult = lastGameStatus.DivineResult
		}
		if lastGameStatus.ExecutedAgent != nil {
			info.ExecutedAgent = lastGameStatus.ExecutedAgent
		}
		if lastGameStatus.AttackedAgent != nil {
			info.AttackedAgent = lastGameStatus.AttackedAgent
		}
		if g.setting.VoteVisibility {
			info.VoteList = lastGameStatus.Votes
		}
		if g.setting.VoteVisibility && agent.Role == model.R_WEREWOLF {
			info.AttackVoteList = lastGameStatus.AttackVotes
		}
	}
	info.TalkList = gameStatus.Talks
	if agent.Role == model.R_WEREWOLF {
		info.WhisperList = gameStatus.Whispers
	}
	info.StatusMap = gameStatus.StatusMap
	roleMap := make(map[model.Agent]model.Role)
	roleMap[*agent] = agent.Role
	if agent.Role == model.R_WEREWOLF {
		for a := range gameStatus.StatusMap {
			if a.Role == model.R_WEREWOLF {
				roleMap[a] = a.Role
			}
		}
	}
	info.RoleMap = roleMap
	if gameStatus.RemainCountMap != nil {
		count := (*gameStatus.RemainCountMap)[*agent]
		info.RemainCount = &count
	}
	if gameStatus.RemainLengthMap != nil {
		if value, exists := (*gameStatus.RemainLengthMap)[*agent]; exists {
			info.RemainLength = &value
		}
	}
	if gameStatus.RemainSkipMap != nil {
		count := (*gameStatus.RemainSkipMap)[*agent]
		info.RemainSkip = &count
	}
	return info
}

// requestToAgent は通常のリクエスト送信に「人間切断→AI引き継ぎ」を被せたもの。
// 切断（HasError）を検知し、対象が人間席かつ takeover 有効なら、代替接続を待って引き継ぐ。
func (g *Game) requestToAgent(agent *model.Agent, request model.Request) (string, error) {
	resp, err := g.requestOnce(agent, request)
	if err == nil || !agent.HasError || !g.takeoverEligible(agent) {
		return resp, err
	}
	timeout := g.config.Takeover.Timeout
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	slog.Warn("人間席が切断されました。AIの引き継ぎを待ちます", "agent", agent.String(), "name", agent.OriginalName, "timeout", timeout.String())
	if !agent.WaitTakeover(timeout) {
		agent.HasError = true // 引き継ぎが来なかった → 従来どおりエラー席（以降スキップ）
		return resp, err
	}
	// 引き継ぎ成功。新接続に INITIALIZE を再送して LLM/役職を初期化する。
	// requestOnce(R_INITIALIZE) は resetLastIdxMaps も呼ぶため、次リクエストで全履歴が再送され、
	// 引き継いだAIは過去ログを引き継いだ状態で続行できる（agent-llm は idx で重複排除）。
	if _, e := g.requestOnce(agent, model.R_INITIALIZE); e != nil || agent.HasError {
		agent.HasError = true
		return resp, err
	}
	slog.Info("AIが席を引き継ぎました。元のリクエストを再送します", "agent", agent.String(), "request", request.Type)
	return g.requestOnce(agent, request)
}

// takeoverEligible: その席が引き継ぎ対象か（takeover 有効 & 人間席=OriginalName が Prefix で始まる）。
func (g *Game) takeoverEligible(agent *model.Agent) bool {
	return g.config.Takeover.Enable &&
		g.config.Takeover.Prefix != "" &&
		strings.HasPrefix(agent.OriginalName, g.config.Takeover.Prefix)
}

func (g *Game) requestOnce(agent *model.Agent, request model.Request) (string, error) {
	info := g.buildInfo(agent)
	var packet model.Packet
	switch request {
	case model.R_NAME:
		packet = model.Packet{Request: &request}
	case model.R_INITIALIZE, model.R_DAILY_INITIALIZE:
		g.resetLastIdxMaps()
		packet = model.Packet{Request: &request, Info: &info, Setting: g.setting}
		if request == model.R_INITIALIZE {
			packet.Info.Profile = agent.ProfileDescription
		}
	case model.R_VOTE, model.R_DIVINE, model.R_GUARD:
		packet = model.Packet{Request: &request, Info: &info}
	case model.R_DAILY_FINISH, model.R_TALK, model.R_WHISPER, model.R_ATTACK:
		packet = model.Packet{Request: &request, Info: &info}
		talks, whispers := g.minimize(agent, info.TalkList, info.WhisperList)
		if request == model.R_TALK || request == model.R_DAILY_FINISH {
			packet.TalkHistory = &talks
		}
		if request == model.R_WHISPER || request == model.R_ATTACK || (request == model.R_DAILY_FINISH && agent.Role == model.R_WEREWOLF) {
			packet.WhisperHistory = &whispers
		}
	case model.R_FINISH:
		info.RoleMap = util.GetRoleMap(g.agents)
		packet = model.Packet{Request: &request, Info: &info}
	default:
		return "", errors.New("一致するリクエストがありません")
	}
	if g.jsonLogger != nil {
		g.jsonLogger.TrackStartRequest(g.id, *agent, packet)
	}
	resp, err := agent.SendPacket(packet, g.config.Server.Timeout.Action, g.config.Server.Timeout.Response, g.config.Server.Timeout.Acceptable)
	if g.jsonLogger != nil {
		g.jsonLogger.TrackEndRequest(g.id, *agent, resp, err)
	}
	return resp, err
}

func (g *Game) resetLastIdxMaps() {
	g.lastTalkIdxMap = make(map[*model.Agent]int)
	g.lastWhisperIdxMap = make(map[*model.Agent]int)
}

func (g *Game) minimize(agent *model.Agent, talks []model.Talk, whispers []model.Talk) ([]model.Talk, []model.Talk) {
	lastTalkIdx := g.lastTalkIdxMap[agent]
	lastWhisperIdx := g.lastWhisperIdxMap[agent]
	g.lastTalkIdxMap[agent] = len(talks)
	g.lastWhisperIdxMap[agent] = len(whispers)
	return talks[lastTalkIdx:], whispers[lastWhisperIdx:]
}

func (g *Game) getCurrentGameStatus() *model.GameStatus {
	return g.gameStatuses[g.currentDay]
}

func (g *Game) getAliveAgents() []*model.Agent {
	return util.FilterAgents(g.agents, func(agent *model.Agent) bool {
		return g.isAlive(agent)
	})
}

func (g *Game) getAliveWerewolves() []*model.Agent {
	return util.FilterAgents(g.agents, func(agent *model.Agent) bool {
		return g.isAlive(agent) && agent.Role.Species == model.S_WEREWOLF
	})
}

func (g *Game) isAlive(agent *model.Agent) bool {
	return g.getCurrentGameStatus().StatusMap[*agent] == model.S_ALIVE
}

func (g *Game) getRealtimeBroadcastPacket() model.BroadcastPacket {
	g.realtimeBroadcasterPacketIdx++
	packet := model.BroadcastPacket{
		Id:        g.id,
		Idx:       g.realtimeBroadcasterPacketIdx,
		Day:       g.currentDay,
		IsDay:     g.isDaytime,
		Event:     "なし",
		Message:   nil,
		FromIdx:   nil,
		ToIdx:     nil,
		BubbleIdx: nil,
	}
	packet.Timestamp = time.Now().Unix()
	for _, a := range g.agents {
		agent := struct {
			Idx     int     `json:"idx"`
			Team    string  `json:"team"`
			Name    string  `json:"name"`
			Profile *string `json:"profile,omitempty"`
			Avatar  *string `json:"avatar,omitempty"`
			Role    string  `json:"role"`
			IsAlive bool    `json:"is_alive"`
		}{
			Idx:     a.Idx,
			Team:    a.TeamName,
			Name:    a.GameName,
			Profile: a.ProfileDescription,
			Role:    a.Role.Name,
			IsAlive: g.isAlive(a),
		}
		if a.Profile != nil {
			agent.Avatar = &a.Profile.AvatarURL
		}
		packet.Agents = append(packet.Agents, agent)
	}
	return packet
}

func (g *Game) GetRoleTeamNamesMap() map[model.Role][]string {
	return util.GetRoleTeamNamesMap(g.agents)
}

func (g *Game) IsFinished() bool {
	return g.isFinished
}
