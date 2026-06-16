"""aiwolf-nlp-demo ロビーbackend (FastAPI).

役割（HANDOFF §1, §6, §7, §9-6）:
  - 入室順の採番（user01, user02, ...）
  - セッションごとの一意なチーム名発行（末尾数字除去でも他卓と衝突しない）
  - AIエージェント(agent-llm)の subprocess spawn（.env から config を生成して渡す）
  - 同時実行数のキュー制御（超過分は「順番待ち（あなたは N 番目）」）
  - 終了/エラー卓のスロット解放（ハング卓の強制回収は M8 で timeout を追加）

起動は運営が行う（uvicorn）。本ファイルはローカルでも docker でも動くよう、
パス・URL・モデル設定をすべて環境変数で受ける。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from prompt_provider import FilePromptProvider

# ---------------------------------------------------------------------------
# 設定（環境変数）
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
WORK_ROOT = HERE.parent  # aiwolf-nlp-demo/


def _env(key: str, default: str) -> str:
    v = os.environ.get(key)
    return v if v is not None and v != "" else default


# agent-llm リポジトリと設定テンプレートの場所
AGENT_LLM_DIR = Path(_env("AGENT_LLM_DIR", str(WORK_ROOT / "repos" / "aiwolf-nlp-agent-llm")))
# 言語別エージェント設定の置き場（base.yml + prompts/<lang>.yml）。
# 旧 configs/agent.yml は base.yml + prompts/ja.yml に分割された。
AGENTS_DIR = Path(_env("AGENTS_DIR", str(WORK_ROOT / "configs" / "agents")))
# ゲーム言語の既定値。未対応言語が要求されたときのフォールバックにもなる。
DEFAULT_LANGUAGE = _env("DEFAULT_LANGUAGE", "ja")
GENERATED_DIR = Path(_env("GENERATED_CONFIG_DIR", str(HERE / ".generated")))

# プロンプト供給元。今はファイル実装。将来ユーザ編集プロンプトをDB化するなら
# 同じインタフェースの実装に差し替えるだけで _build_agent_config はそのまま使える。
PROMPT_PROVIDER = FilePromptProvider(AGENTS_DIR, default_language=DEFAULT_LANGUAGE)

# AIが接続する内部URL（dockerでは ws://game-server:8080/ws、ローカルでは ws://127.0.0.1:8080/ws）
GAME_WS_INTERNAL_URL = _env("GAME_WS_INTERNAL_URL", "ws://127.0.0.1:8080/ws")
# 人間(ブラウザ)が接続する公開URL（本番は wss://<host>/ws、ローカルは ws://localhost:8080/ws）
GAME_WS_PUBLIC_URL = _env("GAME_WS_PUBLIC_URL", "ws://localhost:8080/ws")


def _derive9(url: str) -> str:
    # 末尾 /ws を /ws9 に置換（9人村サーバ用URLの導出）
    return url[:-3] + "/ws9" if url.endswith("/ws") else url


# 9人村サーバ用URL（未指定なら 5人村URLから導出）
GAME_WS_INTERNAL_URL_9 = _env("GAME_WS_INTERNAL_URL_9", "") or _derive9(GAME_WS_INTERNAL_URL)
GAME_WS_PUBLIC_URL_9 = _env("GAME_WS_PUBLIC_URL_9", "") or _derive9(GAME_WS_PUBLIC_URL)

# 対応する村サイズ（=サーバの agent_count）。最小/既定は 5。
VALID_SIZES = {5, 9}


def internal_url_for(size: int) -> str:
    return GAME_WS_INTERNAL_URL_9 if size == 9 else GAME_WS_INTERNAL_URL


def public_url_for(size: int) -> str:
    return GAME_WS_PUBLIC_URL_9 if size == 9 else GAME_WS_PUBLIC_URL


def with_room(url: str, room: str) -> str:
    """WebSocket URL に ?room=<room> を付与する（room_match マッチング用の卓ID）。
    人間・サンプルAI・持ち込みエージェントは同じ room を付けて接続することで同一卓に集まる。"""
    if not room:
        return url
    from urllib.parse import quote
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}room={quote(room, safe='')}"

# LLM 設定（.env 由来）。LLM_PROVIDER で openai|google|vllm を切替（HANDOFF §8）
LLM_PROVIDER = _env("LLM_PROVIDER", "openai")
LLM_MODEL = _env("LLM_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")

# 1セッションあたりのAI体数（agent_count:5 のうち人間1枠を除いた数）
AI_COUNT = int(_env("AI_COUNT", "4"))
# 1卓の総人数（サーバの game.agent_count と一致させる）。外部接続＋サンプルAI = この値。
AGENT_TOTAL = int(_env("AGENT_TOTAL", "5"))
# 同時に走れる卓数（vLLMならGPU、商用APIならレート/コストで決める）。
# room_match により各卓は room で分離され、ゲームサーバは1プロセスで複数卓を並行ホストできる。
# 実上限は LLM スループット（vLLMのGPU同時処理/商用APIレート）と spawn するプロセス数で決まるため、
# 環境に合わせて .env で調整する。
MAX_CONCURRENT_GAMES = int(_env("MAX_CONCURRENT_GAMES", "20"))

# --- 無人運転（HANDOFF §7）---
# ハング卓の上限時間。これを超えて走行中ならAIプロセスを強制回収しスロット解放。
MAX_SESSION_SECONDS = int(_env("MAX_SESSION_SECONDS", "1800"))  # 30分
# 待機列のハートビート猶予。フロントのポーリングが途絶えた待機者は放棄とみなし列から除去。
QUEUE_HEARTBEAT_TTL = int(_env("QUEUE_HEARTBEAT_TTL", "20"))  # 秒
# 終了/エラー済みセッションを辞書から掃除するまでの保持時間。
FINISHED_RETENTION_SECONDS = int(_env("FINISHED_RETENTION_SECONDS", "300"))

# agent-llm を起動する Python 実行体（uv venv があれば優先）
def _resolve_python() -> str:
    explicit = os.environ.get("AGENT_LLM_PYTHON")
    if explicit:
        return explicit
    venv = AGENT_LLM_DIR / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    return "python3"


AGENT_LLM_PYTHON = _resolve_python()


# ---------------------------------------------------------------------------
# セッション管理
# ---------------------------------------------------------------------------
@dataclass
class Session:
    id: str
    display_name: str  # 採番された表示名（user01 等）
    team: str          # 埋めのサンプルAIが使うチーム名（末尾は非数字）
    # room: room_match マッチングの卓ID。?room=<room> でこの卓に来た接続だけが1卓に集まる。
    # チーム名ではなく room で束ねるため、人間・サンプルAI・持ち込みエージェントが
    # それぞれ別チーム名のまま同一卓に入れる（卓は room で他卓と分離）。
    room: str = ""
    # human_team: 人間プレイヤーが使う「人間と分かる」チーム名（例 you-user01）。
    human_team: str = ""
    status: str = "queued"  # queued | running | finished | error
    size: int = 5           # 村の人数（= サーバの agent_count。5 or 9）
    language: str = "ja"    # ゲーム言語（AIの発話/プロンプト言語。卓作成時に固定）
    ai_count: int = 0       # この卓で起動するサンプルAI数（= size - external_slots）
    external_slots: int = 1 # 外部接続数（人間 + 持ち込みエージェント）
    process: Any = None     # subprocess.Popen | None
    config_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    last_seen: float = field(default_factory=time.time)  # 最終ポーリング時刻（ハートビート）
    error: str | None = None


class Lobby:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.queue: list[str] = []          # session_id の待機列（FIFO）
        self._user_counter = 0
        self._lock = asyncio.Lock()

    # --- 採番・チーム名 ---
    def _next_display_name(self) -> str:
        self._user_counter += 1
        return f"user{self._user_counter:02d}"

    @staticmethod
    def _new_team(display_name: str) -> str:
        # サーバは接続名の末尾数字を除去して team を抽出する（connection.go）。
        # AIは team+idx(1..4) を送るので、末尾は必ず非数字にして
        # 「末尾数字除去後のプレフィックス」が他卓と衝突しないようにする。
        token = secrets.token_hex(4) + secrets.choice(string.ascii_lowercase)
        return f"s-{display_name}-{token}"

    async def create_session(
        self, external_slots: int, size: int = 5, language: str = DEFAULT_LANGUAGE
    ) -> Session:
        # size = 村の人数（5 or 9）。external_slots = 外部接続数（人間 + 持ち込みエージェント）。
        # 残り（size - external_slots）をサンプルAIで埋める。
        # language = ゲーム言語。未対応なら provider が既定言語にフォールバックする。
        if size not in VALID_SIZES:
            size = 5
        external_slots = max(1, min(external_slots, size))
        language = PROMPT_PROVIDER.resolve_language(language)
        async with self._lock:
            display = self._next_display_name()
            sid = secrets.token_urlsafe(9)
            session = Session(
                id=sid,
                display_name=display,
                team=self._new_team(display),
                # room はこの卓の一意ID。session.id をそのまま使う（URLセーフ・一意）。
                room=sid,
                # 人間と分かるチーム名。末尾を非数字にして team 抽出の衝突を避ける。
                human_team=f"you-{display}",
                size=size,
                ai_count=max(0, size - external_slots),
                external_slots=external_slots,
                language=language,
            )
            self.sessions[sid] = session
            self.queue.append(sid)
            return session

    async def join(self, size: int = 5, language: str = DEFAULT_LANGUAGE) -> Session:
        # /demo の人間1枠（外部=人間1人、残りをAIが埋める）
        return await self.create_session(external_slots=1, size=size, language=language)

    def running_count(self) -> int:
        return sum(1 for s in self.sessions.values() if s.status == "running")

    def position_of(self, sid: str) -> int:
        # 待機列での順位（1始まり）。走行中/不在は 0。
        try:
            return self.queue.index(sid) + 1
        except ValueError:
            return 0

    # --- スケジューラ: 空きスロットがあれば待機列の先頭を spawn ---
    async def _schedule(self) -> None:
        async with self._lock:
            while self.queue and self.running_count() < MAX_CONCURRENT_GAMES:
                sid = self.queue.pop(0)
                session = self.sessions.get(sid)
                if session is None or session.status != "queued":
                    continue
                try:
                    self._spawn_agents(session)
                    session.status = "running"
                    session.started_at = time.time()
                except Exception as ex:  # noqa: BLE001
                    session.status = "error"
                    session.error = str(ex)

    def _spawn_agents(self, session: Session) -> None:
        if session.ai_count <= 0:
            # 外部接続のみの卓（サンプルAIなし）。spawnしない＝プロセスは持たない。
            session.process = None
            return
        import subprocess

        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        # サンプルAIは ?room=<session.room> を付けて、この卓(room)にだけ参加する。
        ai_url = with_room(internal_url_for(session.size), session.room)
        cfg = self._build_agent_config(session.team, session.ai_count, ai_url, session.language)
        cfg_path = GENERATED_DIR / f"{session.id}.yml"
        with cfg_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        session.config_path = cfg_path

        env = os.environ.copy()
        # APIキー等は子プロセスの環境変数で渡す（agent.py は os.environ を参照）
        for key in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "OPENAI_BASE_URL", "VLLM_API_KEY"):
            if os.environ.get(key):
                env[key] = os.environ[key]

        # start_new_session=True で別プロセスグループにし、終了時に一括 kill 可能にする（M8）
        session.process = subprocess.Popen(  # noqa: S603
            [AGENT_LLM_PYTHON, "src/main.py", "-c", str(cfg_path)],
            cwd=str(AGENT_LLM_DIR),
            env=env,
            start_new_session=True,
        )

    def _build_agent_config(
        self, team: str, ai_count: int, internal_url: str, language: str = DEFAULT_LANGUAGE
    ) -> dict[str, Any]:
        # provider が base.yml + prompts/<lang>.yml をマージした config を返す。
        # それに接続/チーム/LLM を上書きして最終 config にする。
        cfg: dict[str, Any] = PROMPT_PROVIDER.config_for(language)

        cfg.setdefault("web_socket", {})
        cfg["web_socket"]["url"] = internal_url
        cfg["web_socket"]["token"] = cfg["web_socket"].get("token")
        cfg["web_socket"]["auto_reconnect"] = False

        cfg.setdefault("agent", {})
        cfg["agent"]["num"] = ai_count
        cfg["agent"]["team"] = team
        cfg["agent"]["kill_on_timeout"] = True

        cfg.setdefault("llm", {})
        cfg["llm"]["type"] = LLM_PROVIDER  # openai|google|vllm（agent.py が解釈）

        # プロバイダ別にモデル名（と base_url）を反映
        if LLM_PROVIDER in ("openai", "vllm"):
            cfg.setdefault("openai", {})
            cfg["openai"]["model"] = LLM_MODEL
            cfg["openai"].setdefault("temperature", 0.7)
            # base_url は vllm のときだけ設定（商用openai に vLLM用URLが混入しないように）
            if LLM_PROVIDER == "vllm" and OPENAI_BASE_URL:
                cfg["openai"]["base_url"] = OPENAI_BASE_URL
        elif LLM_PROVIDER == "google":
            cfg.setdefault("google", {})
            cfg["google"]["model"] = LLM_MODEL
            cfg["google"].setdefault("temperature", 0.7)
        elif LLM_PROVIDER == "ollama":
            cfg.setdefault("ollama", {})
            cfg["ollama"]["model"] = LLM_MODEL
            cfg["ollama"].setdefault("temperature", 0.7)

        return cfg

    # --- リーパー（無人運転 HANDOFF §7）---
    # 1) 終了/落ちたAIプロセスのスロット解放
    # 2) ハング卓（上限時間超過）の強制回収
    # 3) ポーリングが途絶えた待機者（放棄）の列からの除去
    # 4) 終了済みセッションの掃除
    async def _reap(self) -> None:
        now = time.time()
        async with self._lock:
            for session in self.sessions.values():
                if session.status != "running":
                    continue
                if session.process is None:
                    # 外部接続のみの卓（サンプルAIなし）はプロセスを持たないため、
                    # 時間切れ(MAX_SESSION_SECONDS)でのみスロットを解放する。
                    if session.started_at and (now - session.started_at) > MAX_SESSION_SECONDS:
                        session.status = "finished"
                        session.finished_at = now
                    continue
                ret = session.process.poll()
                if ret is not None:
                    # ゲーム終了でAIプロセスが自然終了 → スロット解放
                    if ret in (0, None):
                        session.status = "finished"
                    else:
                        session.status = "error"
                        session.error = f"agent process exited with code {ret}"
                    session.finished_at = now
                    self._cleanup_config(session)
                elif session.started_at and (now - session.started_at) > MAX_SESSION_SECONDS:
                    # ハング卓: 上限時間を超過 → 強制回収
                    self._terminate_process(session)
                    session.status = "error"
                    session.error = "session exceeded time limit (hung table reclaimed)"
                    session.finished_at = now
                    self._cleanup_config(session)

            # 放棄された待機者を列から除去（フロントのポーリング途絶で検出）
            for sid in list(self.queue):
                session = self.sessions.get(sid)
                if session is None:
                    self.queue.remove(sid)
                    continue
                if (now - session.last_seen) > QUEUE_HEARTBEAT_TTL:
                    self.queue.remove(sid)
                    session.status = "finished"
                    session.finished_at = now

            # 終了済みセッションの掃除（辞書の肥大化防止）
            stale = [
                sid
                for sid, s in self.sessions.items()
                if s.status in ("finished", "error")
                and s.finished_at is not None
                and (now - s.finished_at) > FINISHED_RETENTION_SECONDS
            ]
            for sid in stale:
                self.sessions.pop(sid, None)

    @staticmethod
    def _terminate_process(session: Session) -> None:
        proc = session.process
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                # start_new_session=True で作ったプロセスグループごと停止
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

    @staticmethod
    def _cleanup_config(session: Session) -> None:
        if session.config_path is not None:
            with contextlib.suppress(FileNotFoundError, OSError):
                session.config_path.unlink()
            session.config_path = None

    def kill_session(self, session: Session) -> None:
        self._terminate_process(session)
        if session.status in ("queued", "running"):
            session.status = "finished"
            session.finished_at = time.time()
        if session.id in self.queue:
            self.queue.remove(session.id)
        self._cleanup_config(session)


lobby = Lobby()


# ---------------------------------------------------------------------------
# バックグラウンドループ（スケジューラ + リーパー）
# ---------------------------------------------------------------------------
async def _background_loop() -> None:
    while True:
        await lobby._reap()      # noqa: SLF001
        await lobby._schedule()  # noqa: SLF001
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="aiwolf-nlp-demo lobby")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 会場の静的配信オリジンを許可（本番は Caddy で同一オリジン）
    allow_methods=["*"],
    allow_headers=["*"],
)


class JoinResponse(BaseModel):
    session_id: str
    display_name: str
    team: str
    status: str
    position: int
    ws_url: str
    ai_count: int
    size: int
    language: str


class JoinRequest(BaseModel):
    size: int = 5  # 村の人数（5 or 9）
    language: str = DEFAULT_LANGUAGE  # ゲーム言語（AIの発話/プロンプト言語）


class StatusResponse(BaseModel):
    session_id: str
    display_name: str
    team: str
    status: str
    position: int
    ws_url: str
    size: int
    language: str
    error: str | None = None


_bg_task: asyncio.Task | None = None


@app.on_event("startup")
async def _on_startup() -> None:
    global _bg_task  # noqa: PLW0603
    _bg_task = asyncio.create_task(_background_loop())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    if _bg_task:
        _bg_task.cancel()
    # 走行中のAIプロセスを全て停止
    for session in list(lobby.sessions.values()):
        lobby.kill_session(session)


@app.get("/api/qr")
async def qr(data: str, scale: int = 10) -> Response:
    """任意文字列(URL)を QR コードの PNG にして返す。
    ブラウザで開けばダウンロード、serve-public.sh からは保存に使う。"""
    import io

    import qrcode  # lazy import（qrcode[pil] が必要。Docker で導入）

    qr_obj = qrcode.QRCode(box_size=max(1, min(scale, 30)), border=2)
    qr_obj.add_data(data)
    qr_obj.make(fit=True)
    img = qr_obj.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="demo-qr.png"'},
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "running": lobby.running_count(),
        "queued": len(lobby.queue),
        "max_concurrent": MAX_CONCURRENT_GAMES,
        "provider": LLM_PROVIDER,
        "model": LLM_MODEL,
    }


@app.get("/api/languages")
async def languages() -> dict[str, Any]:
    """ゲーム言語として選べる言語コード一覧と既定値。"""
    return {
        "languages": PROMPT_PROVIDER.supported_languages(),
        "default": PROMPT_PROVIDER.resolve_language(DEFAULT_LANGUAGE),
    }


@app.post("/api/join", response_model=JoinResponse)
async def join(req: JoinRequest | None = None) -> JoinResponse:
    size = req.size if req else 5
    language = req.language if req else DEFAULT_LANGUAGE
    session = await lobby.join(size=size, language=language)
    # すぐ空きがあれば spawn を試みる
    await lobby._schedule()  # noqa: SLF001
    return JoinResponse(
        session_id=session.id,
        display_name=session.display_name,
        # 人間は「人間と分かるチーム名」で接続する（room で卓に束ねるので team は識別用）。
        team=session.human_team,
        status=session.status,
        position=lobby.position_of(session.id),
        # ws_url に ?room= を付与。フロントはこの URL に接続するだけで正しい卓に入る。
        ws_url=with_room(public_url_for(session.size), session.room),
        ai_count=session.ai_count,
        size=session.size,
        language=session.language,
    )


class ByoRequest(BaseModel):
    agents: int = 1          # 持ち込みエージェントの数
    human: bool = False      # 人間プレイヤー(/demo)も1枠入れるか
    size: int = 5            # 村の人数（5 or 9）
    language: str = DEFAULT_LANGUAGE  # ゲーム言語（埋めのサンプルAIの発話言語）


class ByoResponse(BaseModel):
    session_id: str
    team: str                # 持ち込みエージェントが使うチーム名
    ws_url: str              # 接続先 WebSocket URL
    ai_count: int            # 残りを埋めるサンプルAI数
    agent_slots: int         # 持ち込みエージェント枠
    human_slots: int         # 人間枠(0/1)
    agent_total: int         # 1卓の総数
    status: str
    language: str            # ゲーム言語
    human_join_url: str | None = None  # 人間が /demo で参加する直リンク


@app.post("/api/byo", response_model=ByoResponse)
async def create_byo(req: ByoRequest) -> ByoResponse:
    size = req.size if req.size in VALID_SIZES else 5
    agents = max(0, req.agents)
    human = 1 if req.human else 0
    external = agents + human
    if external < 1:
        raise HTTPException(status_code=400, detail="agents+human must be >= 1")
    if external > size:
        raise HTTPException(status_code=400, detail=f"external slots must be <= {size}")

    session = await lobby.create_session(external_slots=external, size=size, language=req.language)
    await lobby._schedule()  # noqa: SLF001

    # 持ち込みエージェントも人間も ?room=<session.room> を付けて同一卓(room)に入る。
    pub_room = with_room(public_url_for(session.size), session.room)
    human_url = None
    if human:
        # 既存 /demo の直接接続モード(?url=&team=)を再利用して人間が同卓に入る。
        # url には room 付き URL を、team には人間と分かるチーム名を渡す。
        from urllib.parse import quote
        # lang も渡し、人間のUIの初期表示言語を卓のゲーム言語に合わせる（UI言語は後から変更可）。
        human_url = (
            f"/demo?url={quote(pub_room, safe='')}"
            f"&team={quote(session.human_team, safe='')}"
            f"&lang={quote(session.language, safe='')}"
        )

    return ByoResponse(
        session_id=session.id,
        team=session.team,
        ws_url=pub_room,
        ai_count=session.ai_count,
        agent_slots=agents,
        human_slots=human,
        agent_total=session.size,
        status=session.status,
        language=session.language,
        human_join_url=human_url,
    )


@app.get("/api/session/{session_id}", response_model=StatusResponse)
async def get_session(session_id: str) -> StatusResponse:
    session = lobby.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    session.last_seen = time.time()  # ハートビート更新（放棄検出用）
    return StatusResponse(
        session_id=session.id,
        display_name=session.display_name,
        team=session.human_team,
        status=session.status,
        position=lobby.position_of(session.id),
        ws_url=with_room(public_url_for(session.size), session.room),
        size=session.size,
        language=session.language,
        error=session.error,
    )


@app.post("/api/session/{session_id}/leave")
async def leave(session_id: str) -> dict[str, str]:
    session = lobby.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    lobby.kill_session(session)
    return {"status": "left"}
