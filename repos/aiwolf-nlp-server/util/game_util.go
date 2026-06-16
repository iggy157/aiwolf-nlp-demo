package util

import (
	"maps"
	"math/rand/v2"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/aiwolfdial/aiwolf-nlp-server/model"
)

func CountAliveTeams(statusMap map[model.Agent]model.Status) (int, int) {
	var humans, werewolfs int
	for agent, status := range statusMap {
		if status == model.S_ALIVE {
			switch agent.Role.Species {
			case model.S_HUMAN:
				humans++
			case model.S_WEREWOLF:
				werewolfs++
			}
		}
	}
	return humans, werewolfs
}

func CalcWinSideTeam(statusMap map[model.Agent]model.Status) model.Team {
	humans, werewolfs := CountAliveTeams(statusMap)
	if humans <= werewolfs {
		return model.T_WEREWOLF
	}
	if werewolfs == 0 {
		return model.T_VILLAGER
	}
	return model.T_NONE
}

func CalcHasErrorAgents(agents []*model.Agent) int {
	var count int
	for _, a := range agents {
		if a.HasError {
			count++
		}
	}
	return count
}

func GetRoleMap(agents []*model.Agent) map[model.Agent]model.Role {
	roleMap := make(map[model.Agent]model.Role)
	for _, a := range agents {
		roleMap[*a] = a.Role
	}
	return roleMap
}

func CreateAgents(conns []model.Connection, roles map[model.Role]int) []*model.Agent {
	rolesCopy := make(map[model.Role]int)
	maps.Copy(rolesCopy, roles)
	agents := make([]*model.Agent, 0)
	for i, conn := range conns {
		role := assignRole(rolesCopy)
		agent := model.NewAgent(i+1, role, conn)
		agents = append(agents, agent)
	}
	return agents
}

// CreateAgentsWithProfiles は接続に役職とプロフィールを割り当てる。
// 接続が希望（DesiredRole / DesiredCharacter）を持つ場合はそれを優先し、
// 残りの接続には残りの役職・プロフィールをランダムに配る（希望なしは従来どおり完全ランダム）。
// 人間プレイヤー(/demo)だけが希望を付けてくる想定で、AIは希望なし。
func CreateAgentsWithProfiles(conns []model.Connection, roles map[model.Role]int, profiles []model.Profile, encoding map[string]string) []*model.Agent {
	rolesCopy := make(map[model.Role]int)
	maps.Copy(rolesCopy, roles)

	// caller の profiles スライスを破壊しないようコピーして扱う。
	profilesCopy := make([]model.Profile, len(profiles))
	copy(profilesCopy, profiles)
	usedProfile := make([]bool, len(profilesCopy))

	// --- 役職割り当て ---
	assignedRoles := make([]model.Role, len(conns))
	roleDone := make([]bool, len(conns))
	// 1) 希望役職を先に確保（capacity がある場合のみ）
	for i, conn := range conns {
		if conn.DesiredRole == "" {
			continue
		}
		if r := model.RoleFromString(conn.DesiredRole); r != model.R_NONE && rolesCopy[r] > 0 {
			rolesCopy[r]--
			assignedRoles[i] = r
			roleDone[i] = true
		}
	}
	// 2) 残りの接続に残りの役職
	for i := range conns {
		if !roleDone[i] {
			assignedRoles[i] = assignRole(rolesCopy)
		}
	}

	// --- プロフィール（キャラ）割り当て ---
	assignedProfile := make([]int, len(conns))
	profDone := make([]bool, len(conns))
	// 1) 希望キャラを先に確保（範囲内かつ未使用の場合のみ）
	for i, conn := range conns {
		c := conn.DesiredCharacter
		if c >= 0 && c < len(profilesCopy) && !usedProfile[c] {
			usedProfile[c] = true
			assignedProfile[i] = c
			profDone[i] = true
		}
	}
	// 2) 残りのプロフィールをシャッフルして、希望なしの接続に配る
	remaining := make([]int, 0, len(profilesCopy))
	for idx := range profilesCopy {
		if !usedProfile[idx] {
			remaining = append(remaining, idx)
		}
	}
	rand.Shuffle(len(remaining), func(a, b int) { remaining[a], remaining[b] = remaining[b], remaining[a] })
	rp := 0
	for i := range conns {
		if !profDone[i] {
			assignedProfile[i] = remaining[rp]
			rp++
		}
	}

	// --- エージェント生成 ---
	agents := make([]*model.Agent, 0, len(conns))
	for i, conn := range conns {
		agent := model.NewAgentWithProfile(i+1, assignedRoles[i], conn, profilesCopy[assignedProfile[i]], encoding)
		agents = append(agents, agent)
	}
	return agents
}

func CreateAgentsWithRole(roleMapConns map[model.Role][]model.Connection) []*model.Agent {
	agents := make([]*model.Agent, 0)
	i := 0
	for role, conns := range roleMapConns {
		for _, conn := range conns {
			agent := model.NewAgent(i+1, role, conn)
			i++
			agents = append(agents, agent)
		}
	}
	return agents
}

func CreateAgentsWithRoleAndProfile(roleMapConns map[model.Role][]model.Connection, profiles []model.Profile, encoding map[string]string) []*model.Agent {
	agents := make([]*model.Agent, 0)

	rand.Shuffle(len(profiles), func(i, j int) { profiles[i], profiles[j] = profiles[j], profiles[i] })

	i := 0
	for role, conns := range roleMapConns {
		for _, conn := range conns {
			profile := profiles[i]
			agent := model.NewAgentWithProfile(i+1, role, conn, profile, encoding)
			i++
			agents = append(agents, agent)
		}
	}
	return agents
}

func assignRole(roles map[model.Role]int) model.Role {
	for r, n := range roles {
		if n > 0 {
			roles[r]--
			return r
		}
	}
	return model.R_VILLAGER
}

func GetCandidates(votes []model.Vote, condition func(model.Vote) bool) []model.Agent {
	counter := make(map[model.Agent]int)
	for _, vote := range votes {
		if condition(vote) {
			counter[vote.Target]++
		}
	}
	return getMaxCountCandidates(counter)
}

func getMaxCountCandidates(counter map[model.Agent]int) []model.Agent {
	var max int
	for _, count := range counter {
		if count > max {
			max = count
		}
	}
	candidates := make([]model.Agent, 0)
	for agent, count := range counter {
		if count == max {
			candidates = append(candidates, agent)
		}
	}
	return candidates
}

func GetRoleTeamNamesMap(agents []*model.Agent) map[model.Role][]string {
	roleTeamNamesMap := make(map[model.Role][]string)
	for _, a := range agents {
		roleTeamNamesMap[a.Role] = append(roleTeamNamesMap[a.Role], a.TeamName)
	}
	return roleTeamNamesMap
}

func CountLength(text string, inWord bool, countSpaces bool) int {
	if inWord {
		return len(strings.Fields(text))
	}
	if countSpaces {
		return utf8.RuneCountInString(text)
	}

	words := strings.Fields(text)
	return utf8.RuneCountInString(strings.Join(words, ""))
}

func TrimLength(text string, length int, inWord bool, countSpaces bool) string {
	if inWord {
		return trimByWords(text, length)
	}
	
	currentLength := CountLength(text, inWord, countSpaces)
	if currentLength <= length {
		return text
	}
	
	if countSpaces {
		return trimByRunes(text, length)
	}
	
	return trimByNonSpaceCount(text, length)
}

func trimByWords(text string, maxWords int) string {
	words := strings.Fields(text)
	if len(words) <= maxWords {
		return text
	}
	return strings.Join(words[:maxWords], " ")
}

func trimByRunes(text string, maxRunes int) string {
	runes := []rune(text)
	if len(runes) <= maxRunes {
		return text
	}
	return string(runes[:maxRunes])
}

func trimByNonSpaceCount(text string, maxNonSpaceChars int) string {
	runes := []rune(text)
	nonSpaceCount := 0
	
	for i, r := range runes {
		if !unicode.IsSpace(r) {
			nonSpaceCount++
			if nonSpaceCount == maxNonSpaceChars {
				return string(runes[:i+1])
			}
		}
	}
	
	return text
}
