package core

import (
	"errors"
	"log/slog"
	"math/rand/v2"
	"sync"
	"time"

	"github.com/aiwolfdial/aiwolf-nlp-server/model"
	"github.com/gorilla/websocket"
)

type WaitingRoom struct {
	agentCount  int
	selfMatch   bool
	roomMatch   bool
	connections sync.Map
	// held は「人数が揃っても卓を立てない」保留中のキー（room）。
	// ロビーが卓を作ったときに /control/hold で入れ、全員の準備完了で /control/release する。
	// 保留が無ければ従来どおり揃った瞬間に卓が立つ（待合室ゲートを使わない構成との互換）。
	held sync.Map
	// mu は connections のスライス差し替え（追加・取り出し・掃除）を直列化する。
	mu sync.Mutex
}

// Seat は待合室に着席中の1接続（ロビー/画面に「誰が来ているか」を見せる用）。
type Seat struct {
	Team string `json:"team"`
	Name string `json:"name"`
}

func NewWaitingRoom(config model.Config) *WaitingRoom {
	return &WaitingRoom{
		agentCount: config.Game.AgentCount,
		selfMatch:  config.Matching.SelfMatch,
		roomMatch:  config.Matching.RoomMatch,
	}
}

func (wr *WaitingRoom) AddConnection(team string, connection model.Connection) {
	wr.mu.Lock()
	defer wr.mu.Unlock()
	value, _ := wr.connections.LoadOrStore(team, []model.Connection{})
	connections := value.([]model.Connection)

	updatedConnections := append(connections, connection)
	wr.connections.Store(team, updatedConnections)

	slog.Info("新しいクライアントが待機部屋に追加されました", "team", team, "remote_addr", connection.Conn.RemoteAddr().String())
}

func (wr *WaitingRoom) GetConnectionsWithMatchOptimizer(matches []map[model.Role][]string) (map[model.Role][]model.Connection, error) {
	var roleMapConns = make(map[model.Role][]model.Connection)

	if len(matches) == 0 {
		return nil, errors.New("スケジュールされたマッチがありません")
	}

	readyMatch := map[model.Role][]string{}
	for _, match := range matches {
		isMatchReady := true
		for _, teams := range match {
			for _, team := range teams {
				value, exists := wr.connections.Load(team)
				if !exists {
					isMatchReady = false
					break
				}
				connections := value.([]model.Connection)
				if len(connections) == 0 {
					isMatchReady = false
					break
				}
			}
			if !isMatchReady {
				break
			}
		}

		if isMatchReady {
			readyMatch = match
			break
		}
	}

	if len(readyMatch) == 0 {
		return nil, errors.New("スケジュールされたマッチ内に不足しているチームがあります")
	}
	slog.Info("スケジュールされたマッチの接続を取得しました")

	for role, teams := range readyMatch {
		for _, team := range teams {
			value, exists := wr.connections.Load(team)
			if !exists {
				continue
			}
			connections := value.([]model.Connection)

			roleMapConns[role] = append(roleMapConns[role], connections[0])

			if len(connections) > 1 {
				wr.connections.Store(team, connections[1:])
			} else {
				wr.connections.Delete(team)
			}
		}
	}
	return roleMapConns, nil
}

// Hold はキー（room）を保留にする。保留中は人数が揃っても GetConnections が卓を立てない。
func (wr *WaitingRoom) Hold(key string) { wr.held.Store(key, struct{}{}) }

// Release は保留を外す。以後は揃い次第（既に揃っていれば呼び出し側の tryFormGame で即）卓が立つ。
func (wr *WaitingRoom) Release(key string) { wr.held.Delete(key) }

func (wr *WaitingRoom) IsHeld(key string) bool {
	_, ok := wr.held.Load(key)
	return ok
}

// Seats はキー（room）に着席中の接続一覧を返す（卓が立つとキーごと消えるので空になる）。
func (wr *WaitingRoom) Seats(key string) []Seat {
	wr.mu.Lock()
	defer wr.mu.Unlock()
	seats := []Seat{}
	if value, ok := wr.connections.Load(key); ok {
		for _, c := range value.([]model.Connection) {
			seats = append(seats, Seat{Team: c.TeamName, Name: c.OriginalName})
		}
	}
	return seats
}

// HasName はキー（room）の待合室に同じ接続名（OriginalName）が既に居るか。
// 同名が同じ卓に2体入るとログで区別できず、人間離脱時の引き継ぎ（名前で席を探す）も誤るため、
// 接続時にこれで弾く。
func (wr *WaitingRoom) HasName(key, name string) bool {
	wr.mu.Lock()
	defer wr.mu.Unlock()
	if value, ok := wr.connections.Load(key); ok {
		for _, c := range value.([]model.Connection) {
			if c.OriginalName == name {
				return true
			}
		}
	}
	return false
}

// Drop はキー（room）の待機接続をすべて切断して待合室から消し、保留も外す。
// ロビーが待合室のまま放置された卓を片付けるときに使う。
func (wr *WaitingRoom) Drop(key string) int {
	wr.held.Delete(key)
	wr.mu.Lock()
	defer wr.mu.Unlock()
	n := 0
	if value, ok := wr.connections.LoadAndDelete(key); ok {
		for _, c := range value.([]model.Connection) {
			_ = c.Conn.Close()
			n++
		}
	}
	return n
}

// Sweep は待機中の全接続に ping を書き、書けなかった接続（相手が切った）を待合室から外す。
// 待機中は誰も読まないので close フレームでは気付けず、書き込みの失敗で検出する。
// 相手の FIN 後の最初の書き込みは通ることがあるため、検出には掃除2回ぶんかかりうる。
func (wr *WaitingRoom) Sweep() {
	wr.mu.Lock()
	defer wr.mu.Unlock()
	wr.connections.Range(func(key, value any) bool {
		conns := value.([]model.Connection)
		alive := conns[:0:0]
		for _, c := range conns {
			err := c.Conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(3*time.Second))
			if err != nil {
				slog.Info("待機中の接続が切れていたため待合室から外します", "key", key, "team_name", c.TeamName, "error", err)
				_ = c.Conn.Close()
				continue
			}
			alive = append(alive, c)
		}
		if len(alive) == 0 {
			wr.connections.Delete(key)
		} else if len(alive) != len(conns) {
			wr.connections.Store(key, alive)
		}
		return true
	})
}

func (wr *WaitingRoom) GetConnections() ([]model.Connection, error) {
	wr.mu.Lock()
	defer wr.mu.Unlock()
	connections := []model.Connection{}
	ready := false

	// roomMatch / selfMatch はどちらも「同一キーで agentCount 接続が揃ったら1卓成立」。
	// キーは AddConnection 時に決まる（roomMatch=room, selfMatch=team）。
	// roomMatch では team が異なる接続を同じ room キーに束ねるため、
	// 「1卓に複数チーム＋人間」を卓ごとに分離して構成できる。
	if wr.selfMatch || wr.roomMatch {
		wr.connections.Range(func(key, value any) bool {
			team := key.(string)
			conns := value.([]model.Connection)

			// 保留中の卓（待合室ゲート）は揃っていても立てない。
			if wr.IsHeld(team) {
				return true
			}
			if len(conns) >= wr.agentCount {
				connections = append(connections, conns[:wr.agentCount]...)

				if len(conns) > wr.agentCount {
					wr.connections.Store(team, conns[wr.agentCount:])
				} else {
					wr.connections.Delete(team)
				}
				ready = true
				return false
			}
			return true
		})
	} else {
		var teams []string
		wr.connections.Range(func(key, value any) bool {
			team := key.(string)
			conns := value.([]model.Connection)
			if len(conns) > 0 {
				teams = append(teams, team)
			}
			return true
		})

		if len(teams) >= wr.agentCount {
			rand.Shuffle(len(teams), func(i, j int) {
				teams[i], teams[j] = teams[j], teams[i]
			})

			for _, team := range teams[:wr.agentCount] {
				value, exists := wr.connections.Load(team)
				if !exists {
					continue
				}
				conns := value.([]model.Connection)
				if len(conns) == 0 {
					continue
				}

				connections = append(connections, conns[0])

				if len(conns) > 1 {
					wr.connections.Store(team, conns[1:])
				} else {
					wr.connections.Delete(team)
				}
			}
			ready = true
		}
	}

	if !ready {
		return nil, errors.New("待機部屋内の接続が不足しています")
	}
	slog.Info("マッチの接続を取得しました")
	return connections, nil
}
