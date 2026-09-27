package core

import (
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/aiwolfdial/aiwolf-nlp-server/logic"
	"github.com/aiwolfdial/aiwolf-nlp-server/model"
	"github.com/aiwolfdial/aiwolf-nlp-server/service"
	"github.com/aiwolfdial/aiwolf-nlp-server/util"
	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

type Server struct {
	config              model.Config
	upgrader            websocket.Upgrader
	waitingRoom         *WaitingRoom
	matchOptimizer      *MatchOptimizer
	gameSetting         *model.Setting
	games               sync.Map
	mu                  sync.RWMutex
	signaled            bool
	jsonLogger          *service.JSONLogger
	gameLogger          *service.GameLogger
	realtimeBroadcaster *service.RealtimeBroadcaster
	ttsBroadcaster      *service.TTSBroadcaster
}

func NewServer(config model.Config) (*Server, error) {
	server := &Server{
		config: config,
		upgrader: websocket.Upgrader{
			CheckOrigin: func(r *http.Request) bool {
				return true
			},
		},
		waitingRoom: NewWaitingRoom(config),
		games:       sync.Map{},
		mu:          sync.RWMutex{},
		signaled:    false,
	}
	gameSettings, err := model.NewSetting(config)
	if err != nil {
		return nil, errors.New("ゲーム設定の作成に失敗しました")
	}
	server.gameSetting = gameSettings
	if config.JSONLogger.Enable {
		server.jsonLogger = service.NewJSONLogger(config)
	}
	if config.GameLogger.Enable {
		server.gameLogger = service.NewGameLogger(config)
	}
	if config.TTSBroadcaster.Enable {
		server.ttsBroadcaster = service.NewTTSBroadcaster(config)
	}
	if config.RealtimeBroadcaster.Enable {
		server.realtimeBroadcaster = service.NewRealtimeBroadcaster(config)
	}
	if config.Matching.IsOptimize {
		matchOptimizer, err := NewMatchOptimizer(config)
		if err != nil {
			return nil, errors.New("マッチオプティマイザの作成に失敗しました")
		}
		server.matchOptimizer = matchOptimizer
	}
	return server, nil
}

func (s *Server) Run() {
	router := gin.Default()
	router.Use(func(c *gin.Context) {
		c.Header("Server", "aiwolf-nlp-server/"+Version.Version+" "+runtime.Version()+" ("+runtime.GOOS+"; "+runtime.GOARCH+")")

		c.Writer.Header().Set("Access-Control-Allow-Origin", "*")
		c.Writer.Header().Set("Access-Control-Allow-Credentials", "true")
		c.Writer.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, Ngrok-Skip-Browser-Warning")
		c.Writer.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS, GET, PUT, DELETE")

		if c.Request.Method == "OPTIONS" {
			c.AbortWithStatus(204)
			return
		}
		c.Next()
	})

	router.GET("/ws", func(c *gin.Context) {
		s.handleConnections(c.Writer, c.Request)
	})

	if s.config.Matching.RoomMatch {
		s.registerControlRoutes(router)
		// 待合室で待っている間は誰も読まないので、定期的に ping を書いて切断済みを掃除する。
		go func() {
			for {
				time.Sleep(10 * time.Second)
				s.waitingRoom.Sweep()
			}
		}()
	}

	if s.config.RealtimeBroadcaster.Enable {
		realtimeGroup := router.Group("/realtime")
		if s.config.Server.Authentication.Enable {
			realtimeGroup.Use(s.verifyMiddleware())
		}
		realtimeGroup.Static("/", s.config.RealtimeBroadcaster.OutputDir)
	}

	if s.config.TTSBroadcaster.Enable {
		router.Static("/tts", s.config.TTSBroadcaster.SegmentDir)
		go s.ttsBroadcaster.Start()
	}

	go func() {
		trap := make(chan os.Signal, 1)
		signal.Notify(trap, syscall.SIGTERM, syscall.SIGHUP, syscall.SIGINT)
		sig := <-trap
		slog.Info("シグナルを受信しました", "signal", sig)
		s.signaled = true
		s.gracefullyShutdown()
		os.Exit(0)
	}()

	slog.Info("サーバを起動しました", "host", s.config.Server.WebSocket.Host, "port", s.config.Server.WebSocket.Port)
	err := router.Run(s.config.Server.WebSocket.Host + ":" + strconv.Itoa(s.config.Server.WebSocket.Port))
	if err != nil {
		slog.Error("サーバの起動に失敗しました", "error", err)
		return
	}
}

func (s *Server) gracefullyShutdown() {
	for {
		isFinished := true
		s.games.Range(func(key, value any) bool {
			game, ok := value.(*logic.Game)
			if !ok || !game.IsFinished() {
				isFinished = false
				return false
			}
			return true
		})
		if isFinished {
			break
		}
		time.Sleep(15 * time.Second)
	}
	slog.Info("全てのゲームが終了しました")
}

// handleTakeover は ?takeover=<original_name> の接続を、進行中ゲームの該当席へ引き渡す。
// 渡せた席（応答待ち中の人間が切断していた席）があれば、その席のAI引き継ぎが始まる。
func (s *Server) handleTakeover(ws *websocket.Conn, room, name string) {
	delivered := false
	s.games.Range(func(_, value any) bool {
		if g, ok := value.(*logic.Game); ok && g.TryTakeover(name, ws) {
			delivered = true
			return false
		}
		return true
	})
	if delivered {
		slog.Info("引き継ぎ接続を席へ渡しました", "room", room, "name", name)
	} else {
		slog.Warn("引き継ぎ対象の席が見つかりませんでした（既に終了/別席）", "room", room, "name", name)
		_ = ws.Close()
	}
}

func (s *Server) handleConnections(w http.ResponseWriter, r *http.Request) {
	if s.signaled {
		slog.Warn("シグナルを受信したため、新しい接続を受け付けません")
		return
	}
	header := r.Header.Clone()
	ws, err := s.upgrader.Upgrade(w, r, nil)
	if err != nil {
		slog.Error("クライアントのアップグレードに失敗しました", "error", err)
		return
	}
	// 引き継ぎ接続(?takeover=<original_name>)は、待機部屋でなく進行中ゲームの該当席へ渡す。
	// NAME ハンドシェイクは行わない（席は既に確定しており、サーバが INITIALIZE を再送する）。
	if takeoverName := r.URL.Query().Get("takeover"); takeoverName != "" {
		s.handleTakeover(ws, r.URL.Query().Get("room"), takeoverName)
		return
	}
	conn, err := model.NewConnection(ws, &header)
	if err != nil {
		slog.Error("クライアントの接続に失敗しました", "error", err)
		return
	}
	if s.config.Server.Authentication.Enable {
		token := r.URL.Query().Get("token")
		if token != "" {
			if !util.IsValidPlayerToken(os.Getenv("SECRET_KEY"), token, conn.TeamName) {
				slog.Warn("トークンが無効です", "team_name", conn.TeamName)
				conn.Conn.Close()
				slog.Info("クライアントの接続を切断しました", "team_name", conn.TeamName)
				return
			}
		} else {
			token = strings.ReplaceAll(conn.Header.Get("Authorization"), "Bearer ", "")
			if !util.IsValidPlayerToken(os.Getenv("SECRET_KEY"), token, conn.TeamName) {
				slog.Warn("トークンが無効です", "team_name", conn.TeamName)
				conn.Conn.Close()
				slog.Info("クライアントの接続を切断しました", "team_name", conn.TeamName)
				return
			}
		}
	}
	// room_match マッチング時は接続クエリ ?room=<id> を卓IDとしてグループキーに使う。
	// TeamName はそのまま識別子として保持する（複数チームが同一卓に入っても区別可能）。
	groupKey := conn.TeamName
	if s.config.Matching.RoomMatch {
		room := r.URL.Query().Get("room")
		if room == "" {
			slog.Warn("room_match が有効ですが ?room= が指定されていません。接続を切断します", "team_name", conn.TeamName)
			conn.Conn.Close()
			return
		}
		conn.Room = room
		groupKey = room
	}
	// 役職・キャラの希望（任意）。?role=<ROLE> / ?character=<idx>。
	// 指定が無ければ従来どおりランダム割り当て。人間プレイヤー(/demo)だけが付けてくる想定。
	if role := r.URL.Query().Get("role"); role != "" {
		conn.DesiredRole = role
	}
	if cs := r.URL.Query().Get("character"); cs != "" {
		if ci, err := strconv.Atoi(cs); err == nil {
			conn.DesiredCharacter = ci
		}
	}
	// 同じ卓に同じ名前が既に待っていたら断る（room_match のときだけ。卓が違えば同名でも構わない）。
	if s.config.Matching.RoomMatch && s.waitingRoom.HasName(groupKey, conn.OriginalName) {
		slog.Warn("同じ名前の接続が既に同じ卓で待っているため切断します", "room", groupKey, "name", conn.OriginalName)
		_ = conn.Conn.WriteMessage(websocket.TextMessage, []byte(`{"error":"duplicate name in room: `+conn.OriginalName+`"}`))
		_ = conn.Conn.Close()
		return
	}
	s.waitingRoom.AddConnection(groupKey, *conn)
	s.tryFormGame()
}

// tryFormGame は待合室に揃った卓があれば1つ立てて走らせる。
// 接続が増えたとき（handleConnections）と、保留が外れたとき（/control/release）の両方から呼ばれる。
// 立てたら true。
func (s *Server) tryFormGame() bool {
	var game *logic.Game
	if s.config.Matching.IsOptimize {
		s.waitingRoom.connections.Range(func(key, value any) bool {
			team := key.(string)
			s.matchOptimizer.updateTeam(team)
			return true
		})
		matches := s.matchOptimizer.getMatches()
		roleMapConns, err := s.waitingRoom.GetConnectionsWithMatchOptimizer(matches)
		if err != nil {
			slog.Error("待機部屋からの接続の取得に失敗しました", "error", err)
			return false
		}
		game = logic.NewGameWithRole(&s.config, s.gameSetting, roleMapConns)
	} else {
		connections, err := s.waitingRoom.GetConnections()
		if err != nil {
			slog.Error("待機部屋からの接続の取得に失敗しました", "error", err)
			return false
		}
		game = logic.NewGame(&s.config, s.gameSetting, connections)
	}
	if s.jsonLogger != nil {
		game.SetJSONLogger(s.jsonLogger)
	}
	if s.gameLogger != nil {
		game.SetGameLogger(s.gameLogger)
	}
	if s.realtimeBroadcaster != nil {
		game.SetRealtimeBroadcaster(s.realtimeBroadcaster)
	}
	if s.ttsBroadcaster != nil {
		game.SetTTSBroadcaster(s.ttsBroadcaster)
	}
	s.games.Store(game.GetID(), game)

	go func() {
		winSide := game.Start()
		if s.config.Matching.IsOptimize {
			if winSide != model.T_NONE {
				s.matchOptimizer.setMatchEnd(game.GetRoleTeamNamesMap())
			} else {
				s.matchOptimizer.setMatchWeight(game.GetRoleTeamNamesMap(), 0)
			}
		}
	}()
	return true
}

// controlAuth は /control/* をロビー専用にする。共有鍵 CONTROL_KEY（環境変数）を
// X-Control-Key ヘッダで照合。未設定なら機能ごと無効（503）にして、外から触れないようにする。
func (s *Server) controlAuth() gin.HandlerFunc {
	return func(c *gin.Context) {
		key := os.Getenv("CONTROL_KEY")
		if key == "" {
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"error": "CONTROL_KEY が未設定です"})
			return
		}
		if c.GetHeader("X-Control-Key") != key {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		c.Next()
	}
}

// registerControlRoutes は待合室ゲートの操作口。room_match のときだけ生える。
//   POST /control/hold?room=     揃っても卓を立てない（卓作成時にロビーが呼ぶ）
//   POST /control/release?room=  保留を外す。揃っていれば即立てる → {formed, count}
//   POST /control/drop?room=     待機接続を全部切って片付ける（放置卓の回収）
//   GET  /control/room?room=     {held, count, seats:[{team,name}]}
func (s *Server) registerControlRoutes(router *gin.Engine) {
	ctrl := router.Group("/control", s.controlAuth())
	roomOf := func(c *gin.Context) (string, bool) {
		room := c.Query("room")
		if room == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "room が必要です"})
			return "", false
		}
		return room, true
	}
	ctrl.POST("/hold", func(c *gin.Context) {
		room, ok := roomOf(c)
		if !ok {
			return
		}
		s.waitingRoom.Hold(room)
		slog.Info("卓を保留にしました", "room", room)
		c.JSON(http.StatusOK, gin.H{"held": true, "count": len(s.waitingRoom.Seats(room))})
	})
	ctrl.POST("/release", func(c *gin.Context) {
		room, ok := roomOf(c)
		if !ok {
			return
		}
		s.waitingRoom.Release(room)
		s.waitingRoom.Sweep()
		formed := false
		if len(s.waitingRoom.Seats(room)) >= s.config.Game.AgentCount {
			formed = s.tryFormGame()
		}
		slog.Info("卓の保留を外しました", "room", room, "formed", formed)
		c.JSON(http.StatusOK, gin.H{"held": false, "formed": formed, "count": len(s.waitingRoom.Seats(room))})
	})
	ctrl.POST("/drop", func(c *gin.Context) {
		room, ok := roomOf(c)
		if !ok {
			return
		}
		n := s.waitingRoom.Drop(room)
		slog.Info("待機中の卓を片付けました", "room", room, "dropped", n)
		c.JSON(http.StatusOK, gin.H{"dropped": n})
	})
	ctrl.GET("/room", func(c *gin.Context) {
		room, ok := roomOf(c)
		if !ok {
			return
		}
		seats := s.waitingRoom.Seats(room)
		c.JSON(http.StatusOK, gin.H{
			"held":  s.waitingRoom.IsHeld(room),
			"count": len(seats),
			"size":  s.config.Game.AgentCount,
			"seats": seats,
		})
	})
}

func (s *Server) verifyMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		token := c.Query("token")
		if token == "" {
			token = strings.ReplaceAll(c.GetHeader("Authorization"), "Bearer ", "")
		}
		if token == "" {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		if !util.IsValidReceiver(os.Getenv("SECRET_KEY"), token) {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		c.Next()
	}
}
