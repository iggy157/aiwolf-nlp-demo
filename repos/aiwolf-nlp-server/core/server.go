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
	s.waitingRoom.AddConnection(conn.TeamName, *conn)

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
			return
		}
		game = logic.NewGameWithRole(&s.config, s.gameSetting, roleMapConns)
	} else {
		connections, err := s.waitingRoom.GetConnections()
		if err != nil {
			slog.Error("待機部屋からの接続の取得に失敗しました", "error", err)
			return
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
