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

from prompt_provider import MAX_PERSONA_CHARS, FilePromptProvider, preview_persona

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

# 言語別サーバ（scripts/gen_i18n.py が生成）を運用しているか。
#   I18N_SERVER_LANGS="all"          → ja以外の全言語に専用サーバがある前提（make public 既定）
#   I18N_SERVER_LANGS="en,zh,..."    → 列挙した言語だけ専用サーバがある
#   未設定                            → 言語別サーバなし（全卓を既定=ja サーバへ）
_i18n_raw = _env("I18N_SERVER_LANGS", "")
I18N_SERVER_ALL = _i18n_raw.strip().lower() == "all"
I18N_SERVER_LANGS = {x.strip() for x in _i18n_raw.split(",") if x.strip() and x.strip().lower() != "all"}


def _has_lang_server(language: str) -> bool:
    # 既定言語(ja)はベースのサーバ(game-server/game-server-9)を使う。
    if not language or language == DEFAULT_LANGUAGE:
        return False
    return I18N_SERVER_ALL or language in I18N_SERVER_LANGS


def _lang_internal(base: str, size: int, language: str) -> str:
    # 内部URLのサービス名に -<lang> を付ける（compose のサービス名規約に一致）。
    # 例: ws://game-server:8080/ws -> ws://game-server-en:8080/ws
    #     ws://game-server-9:8080/ws -> ws://game-server-9-en:8080/ws
    token = "game-server-9" if size == 9 else "game-server"
    return base.replace(token, f"{token}-{language}", 1)


def _lang_public(base: str, size: int, language: str) -> str:
    # 公開URLのパス末尾に -<lang> を付ける（Caddy の言語別ルートに一致）。
    # 例: wss://host/ws -> wss://host/ws-en ／ wss://host/ws9 -> wss://host/ws9-en
    suffix = "/ws9" if size == 9 else "/ws"
    if base.endswith(suffix):
        return base[: -len(suffix)] + f"{suffix}-{language}"
    return base


def internal_url_for(size: int, language: str = DEFAULT_LANGUAGE) -> str:
    base = GAME_WS_INTERNAL_URL_9 if size == 9 else GAME_WS_INTERNAL_URL
    return _lang_internal(base, size, language) if _has_lang_server(language) else base


def public_url_for(size: int, language: str = DEFAULT_LANGUAGE) -> str:
    base = GAME_WS_PUBLIC_URL_9 if size == 9 else GAME_WS_PUBLIC_URL
    return _lang_public(base, size, language) if _has_lang_server(language) else base


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
# マルチ卓の待機部屋（開始前）のハートビート猶予。ホスト/参加者の誰もポーリングしなくなったら回収。
WAITING_ROOM_TTL = int(_env("WAITING_ROOM_TTL", "60"))  # 秒
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
class Participant:
    """マルチプレイ卓に入った人間1人。

    token は端末ごとの匿名識別子（ブラウザの localStorage 保持）。アカウント機能は無いが、
    これを「誰が入っているか」の識別に使う。将来アカウントを足すときは、この匿名 token を
    アカウントに紐付け（claim）するだけで移行できる（[[role-character-choice]] と同じ前方互換方針）。
    """
    token: str          # 端末の匿名トークン
    display_name: str   # 表示名（userNN）
    team: str           # この人が接続に使う一意の human team（you-...）
    # この人が離脱したとき、席を引き継ぐ takeover AI に使う自作プロンプト（任意）。
    # 空なら既定プロンプトで引き継ぐ。
    agent_prompt: str = ""
    last_seen: float = field(default_factory=time.time)


@dataclass
class Session:
    """1卓（Room）。ソロ=人間1＋AI、マルチ=人間N＋AI。

    Phase 1（DB/アカウント）への布石として、卓は RoomStore（今は InMemoryRoomStore）越しに
    保持する。code は人間が共有して同卓に入るための短い合言葉（マルチのみ）。
    """
    id: str
    display_name: str  # 採番された表示名（user01 等。代表＝ホスト）
    team: str          # 埋めのサンプルAIが使うチーム名（末尾は非数字）
    # room: room_match マッチングの卓ID。?room=<room> でこの卓に来た接続だけが1卓に集まる。
    # チーム名ではなく room で束ねるため、人間・サンプルAI・持ち込みエージェントが
    # それぞれ別チーム名のまま同一卓に入れる（卓は room で他卓と分離）。
    room: str = ""
    # human_team: 人間プレイヤーが使う「人間と分かる」チーム名（例 you-user01）。
    # ソロでは代表参加者の team。マルチでは participants 各自の team を使う。
    human_team: str = ""
    status: str = "queued"  # waiting | queued | running | finished | error
    size: int = 5           # 村の人数（= サーバの agent_count。5 or 9）
    language: str = "ja"    # ゲーム言語（AIの発話/プロンプト言語。卓作成時に固定）
    ai_count: int = 0       # この卓で起動するサンプルAI数（= size - 参加人間数）
    external_slots: int = 1 # 外部接続数（人間 + 持ち込みエージェント）
    # --- Room（ソロ/マルチ）---
    mode: str = "solo"      # solo | multi
    code: str = ""          # マルチの合言葉（共有して同卓に入る）。ソロは空。
    # agent_prompt: この卓のサンプルAIに使うユーザ自作の「プレイスタイル/性格」文（任意）。
    # provider が initialize に安全注入する。空なら既定プロンプト。
    agent_prompt: str = ""
    # ai_specs: AI席をグループに分けて別プロンプトで起動する（観戦の「自作AI×N＋既定×M」等）。
    # 各要素は (persona, count)。空なら _spawn が単一グループ((agent_prompt, ai_count))にフォールバック。
    ai_specs: list[Any] = field(default_factory=list)
    human_slots: int = 1    # 人間の席数（マルチでホストが指定。残りをAIが埋める）
    host_token: str = ""    # ホスト（部屋作成者）の匿名トークン
    participants: list[Participant] = field(default_factory=list)
    process: Any = None     # subprocess.Popen | None
    # 人間離脱時にその席を引き継ぐために追加 spawn したAIプロセス（(Popen, config_path) の組）。
    takeover_processes: list[Any] = field(default_factory=list)
    # AIが引き継いだ離脱者の表示名（時系列）。プレイ中の各クライアントが room ポーリングで拾い、
    # 「○○が退出。AIが代わりに参加」をフィードに出すのに使う。
    takeover_events: list[str] = field(default_factory=list)
    config_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    last_seen: float = field(default_factory=time.time)  # 最終ポーリング時刻（ハートビート）
    error: str | None = None

    def alive_seen(self) -> float:
        """卓の最終アクティブ時刻（誰かのポーリング/参加者の last_seen の最大）。"""
        if self.participants:
            return max(self.last_seen, max(p.last_seen for p in self.participants))
        return self.last_seen


class Lobby:
    def __init__(self) -> None:
        # sessions / _codes は「インメモリの RoomStore」。Phase 1 で DB 実装に差し替える際は
        # ここのアクセサ（room_by_code / sessions 参照）を Store 越しにするだけでロジックは不変。
        self.sessions: dict[str, Session] = {}
        self._codes: dict[str, str] = {}    # code(大文字) -> session_id（マルチの合言葉索引）
        self.queue: list[str] = []          # session_id の待機列（FIFO）
        self._user_counter = 0
        self._lock = asyncio.Lock()

    # --- 採番・チーム名 ---
    def _next_display_name(self) -> str:
        self._user_counter += 1
        return f"user{self._user_counter:02d}"

    # 合言葉に紛らわしい文字(0/O,1/I)を避けた英数字。
    _CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def _gen_code(self, length: int = 5) -> str:
        for _ in range(20):
            code = "".join(secrets.choice(self._CODE_ALPHABET) for _ in range(length))
            if code not in self._codes:
                return code
        # 衝突が続くなら桁を増やす
        return self._gen_code(length + 1)

    def room_by_code(self, code: str) -> Session | None:
        sid = self._codes.get((code or "").upper())
        return self.sessions.get(sid) if sid else None

    def _make_participant(self, token: str, agent_prompt: str = "") -> Participant:
        display = self._next_display_name()
        return Participant(
            token=token, display_name=display, team=f"you-{display}",
            agent_prompt=(agent_prompt or "").strip(),
        )

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
        # /demo の人間1枠（外部=人間1人、残りをAIが埋める）。後方互換用。
        return await self.create_session(external_slots=1, size=size, language=language)

    # --- Room（ソロ/マルチ）---
    async def create_room(
        self,
        mode: str,
        size: int = 5,
        language: str = DEFAULT_LANGUAGE,
        human_slots: int = 1,
        token: str = "",
        agent_prompt: str = "",
        my_ai_count: int = -1,
    ) -> Session:
        """卓を作る。ソロは即開始（AI spawn）、マルチは待機（コード発行・ホスト参加）。
        agent_prompt があれば、この卓のサンプルAIにユーザ自作のプレイスタイルを使う。
        my_ai_count>=0 なら、AI席のうち my_ai_count 体だけ自作プロンプト・残りは既定（観戦の構成用）。"""
        if size not in VALID_SIZES:
            size = 5
        language = PROMPT_PROVIDER.resolve_language(language)
        mode = "multi" if mode == "multi" else "solo"
        # 人間席数: ソロは1、マルチは 1..size
        human_slots = 1 if mode == "solo" else max(1, min(human_slots, size))
        token = token or secrets.token_urlsafe(9)
        agent_prompt = (agent_prompt or "").strip()
        async with self._lock:
            sid = secrets.token_urlsafe(9)
            host = self._make_participant(token, agent_prompt)
            session = Session(
                id=sid,
                display_name=host.display_name,
                team=self._new_team(host.display_name),
                room=sid,
                human_team=host.team,
                size=size,
                language=language,
                mode=mode,
                # solo: サンプルAIに persona を使う(①)。multi: サンプルAIは既定とし、persona は
                # host 参加者に持たせて「離脱時の takeover」に使う(②)。
                agent_prompt=agent_prompt if mode == "solo" else "",
                human_slots=human_slots,
                host_token=token,
                participants=[host],
            )
            self.sessions[sid] = session
            if mode == "solo":
                # ソロは即「順番待ち→spawn」。AIで全席（size-1）埋める。
                session.external_slots = 1
                session.ai_count = max(0, size - 1)
                # 自作プロンプトの構成: my_ai_count>=0 なら「自作×k＋既定×残り」、
                # それ以外(=-1)で persona があれば全AIに適用（①の挙動）。
                if agent_prompt and my_ai_count >= 0:
                    k = max(0, min(my_ai_count, session.ai_count))
                    session.ai_specs = [(agent_prompt, k), ("", session.ai_count - k)]
                elif agent_prompt:
                    session.ai_specs = [(agent_prompt, session.ai_count)]
                session.status = "queued"
                self.queue.append(sid)
            else:
                # マルチは合言葉を発行して待機。開始はホストの start_room で。
                session.code = self._gen_code()
                self._codes[session.code] = sid
                session.status = "waiting"
            return session

    async def join_room(self, code: str, token: str, agent_prompt: str = "") -> tuple[Session, Participant]:
        """マルチ卓に合言葉で参加する。空席が無ければ例外。既参加(同token)なら既存席を返す。
        agent_prompt はこの人の離脱時 takeover に使う自作プロンプト。"""
        token = token or secrets.token_urlsafe(9)
        agent_prompt = (agent_prompt or "").strip()
        async with self._lock:
            session = self.room_by_code(code)
            if session is None or session.mode != "multi":
                raise KeyError("room not found")
            if session.status != "waiting":
                raise ValueError("room already started")
            for p in session.participants:
                if p.token == token:  # 再入（リロード等）は既存席をそのまま（personaは最新に更新）
                    p.last_seen = time.time()
                    p.agent_prompt = agent_prompt
                    return session, p
            if len(session.participants) >= session.human_slots:
                raise ValueError("room full")
            p = self._make_participant(token, agent_prompt)
            session.participants.append(p)
            return session, p

    async def start_room(self, code: str, host_token: str) -> Session:
        """ホストがマルチ卓を開始する。参加人間以外の席をAIで埋めて spawn。"""
        async with self._lock:
            session = self.room_by_code(code)
            if session is None or session.mode != "multi":
                raise KeyError("room not found")
            if session.host_token != host_token:
                raise PermissionError("only the host can start")
            if session.status != "waiting":
                return session  # 二重開始は無視
            humans = len(session.participants)
            session.external_slots = humans
            session.ai_count = max(0, session.size - humans)
            session.status = "queued"
            self.queue.append(session.id)
        await self._schedule()  # noqa: SLF001
        return session

    def participant_of(self, session: Session, token: str) -> Participant | None:
        for p in session.participants:
            if p.token == token:
                return p
        return None

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
        # AI席のグループ (persona, count)。ai_specs があれば異種AI（自作AI＋既定 等）、
        # 無ければ単一グループ（session.agent_prompt を ai_count 体）。
        specs = [
            (p, c)
            for (p, c) in (session.ai_specs or [(session.agent_prompt, session.ai_count)])
            if c > 0
        ]
        if not specs:
            # 外部接続のみの卓（サンプルAIなし）。spawnしない＝プロセスは持たない。
            session.process = None
            return
        import subprocess

        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        # 各グループは ?room=<session.room> で同じ卓に集まる。グループごとに別チーム名。
        ai_url = with_room(internal_url_for(session.size, session.language), session.room)
        session.process = None
        for i, (persona, count) in enumerate(specs):
            team = session.team if i == 0 else self._new_team(f"{session.display_name}-g{i}")
            cfg = self._build_agent_config(team, count, ai_url, session.language, persona)
            cfg_path = GENERATED_DIR / (f"{session.id}.yml" if i == 0 else f"{session.id}-g{i}.yml")
            with cfg_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            # start_new_session=True で別プロセスグループにし、終了時に一括 kill 可能にする（M8）
            proc = subprocess.Popen(  # noqa: S603
                [AGENT_LLM_PYTHON, "src/main.py", "-c", str(cfg_path)],
                cwd=str(AGENT_LLM_DIR),
                env=self._child_env(),
                start_new_session=True,
            )
            if i == 0:
                # 先頭グループを主プロセスにする（リーパーはこれの終了でゲーム終了を検知）。
                session.process = proc
                session.config_path = cfg_path
            else:
                # 追加グループは cleanup 対象リストに乗せる（kill/掃除は takeover と共通）。
                session.takeover_processes.append((proc, cfg_path))

    @staticmethod
    def _child_env() -> dict[str, str]:
        # APIキー等は os.environ.copy() で子に引き継がれる（agent.py は os.environ を参照）。
        # OPENAI_BASE_URL は vLLM のときだけ渡し、それ以外では取り除く（[[openai-base-url-footgun]]）。
        env = os.environ.copy()
        if LLM_PROVIDER == "vllm" and OPENAI_BASE_URL:
            env["OPENAI_BASE_URL"] = OPENAI_BASE_URL
        else:
            env.pop("OPENAI_BASE_URL", None)
            env.pop("OPENAI_API_BASE", None)
        return env

    def _spawn_takeover_ai(self, session: Session, original_name: str, persona: str = "") -> bool:
        """人間が離脱した席(original_name)を引き継ぐAIを1体 spawn する。
        サーバが ?takeover=<original_name> を解釈し、進行中ゲームの該当席へ接続を渡す。
        persona があれば、その人の自作プロンプトで引き継ぐ。"""
        import subprocess
        from urllib.parse import quote

        if session.status != "running":
            return False
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        base_url = with_room(internal_url_for(session.size, session.language), session.room)
        ai_url = base_url + ("&" if "?" in base_url else "?") + "takeover=" + quote(original_name, safe="")
        cfg = self._build_agent_config("takeover", 1, ai_url, session.language, persona)
        cfg_path = GENERATED_DIR / f"{session.id}-takeover-{secrets.token_hex(3)}.yml"
        with cfg_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        proc = subprocess.Popen(  # noqa: S603
            [AGENT_LLM_PYTHON, "src/main.py", "-c", str(cfg_path)],
            cwd=str(AGENT_LLM_DIR),
            env=self._child_env(),
            start_new_session=True,
        )
        session.takeover_processes.append((proc, cfg_path))
        return True

    async def handle_leave(self, code: str, token: str) -> str:
        """部屋からの離脱処理。マルチ進行中の離脱は卓を殺さず、その席をAIに引き継がせる。"""
        to_kill: Session | None = None
        async with self._lock:
            session = self.room_by_code(code)
            if session is None:
                return "left"
            if session.status == "waiting":
                if token == session.host_token:
                    to_kill = session  # ホストが待機をやめる → 解散
                else:
                    session.participants = [p for p in session.participants if p.token != token]
                    return "left"
            elif session.status == "running" and session.mode == "multi":
                p = self.participant_of(session, token)
                if p is None:
                    return "left"
                remaining = [x for x in session.participants if x.token != token]
                if not remaining:
                    # 最後の人間が抜けた → 卓を終了する。
                    # 人間が誰も居ないのに takeover してAI-vs-AIで無駄に走らせない（LLMコスト防止）。
                    to_kill = session
                else:
                    # まだ人間が残っている → 抜けた席だけAIが引き継ぎ、卓は続行。
                    # その人の自作プロンプト(あれば)で引き継ぐ。
                    spawned = self._spawn_takeover_ai(session, p.team, p.agent_prompt)
                    if spawned:
                        session.takeover_events.append(p.display_name)
                    session.participants = remaining
                    return "takeover" if spawned else "left"
            else:
                to_kill = session  # ソロ進行中など → 卓終了
        if to_kill is not None:
            self.kill_session(to_kill)
        return "left"

    def _build_agent_config(
        self,
        team: str,
        ai_count: int,
        internal_url: str,
        language: str = DEFAULT_LANGUAGE,
        persona: str = "",
    ) -> dict[str, Any]:
        # provider が base.yml + prompts/<lang>.yml をマージした config を返す。
        # persona があればユーザ自作のプレイスタイルを initialize に注入する。
        # それに接続/チーム/LLM を上書きして最終 config にする。
        cfg: dict[str, Any] = PROMPT_PROVIDER.config_for(language, persona or None)

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
                if (now - session.alive_seen()) > QUEUE_HEARTBEAT_TTL:
                    self.queue.remove(sid)
                    session.status = "finished"
                    session.finished_at = now

            # 放棄された待機部屋（マルチ・開始前）を回収（誰もポーリングしなくなった）
            for session in self.sessions.values():
                if session.status == "waiting" and (now - session.alive_seen()) > WAITING_ROOM_TTL:
                    session.status = "finished"
                    session.finished_at = now

            # 終了済みセッションの掃除（辞書の肥大化防止）。合言葉索引も外す。
            stale = [
                sid
                for sid, s in self.sessions.items()
                if s.status in ("finished", "error")
                and s.finished_at is not None
                and (now - s.finished_at) > FINISHED_RETENTION_SECONDS
            ]
            for sid in stale:
                s = self.sessions.pop(sid, None)
                if s and s.code:
                    self._codes.pop(s.code, None)

    @staticmethod
    def _kill_proc(proc: Any) -> None:
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                # start_new_session=True で作ったプロセスグループごと停止
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

    @classmethod
    def _terminate_process(cls, session: Session) -> None:
        cls._kill_proc(session.process)
        for proc, _ in session.takeover_processes:
            cls._kill_proc(proc)

    @classmethod
    def _cleanup_config(cls, session: Session) -> None:
        if session.config_path is not None:
            with contextlib.suppress(FileNotFoundError, OSError):
                session.config_path.unlink()
            session.config_path = None
        # takeover AI プロセスも確実に停止＋設定ファイル削除（リーパーの自然終了経路の保険）。
        for proc, cfg_path in session.takeover_processes:
            cls._kill_proc(proc)
            with contextlib.suppress(FileNotFoundError, OSError):
                cfg_path.unlink()
        session.takeover_processes = []

    def kill_session(self, session: Session) -> None:
        self._terminate_process(session)
        if session.status in ("waiting", "queued", "running"):
            session.status = "finished"
            session.finished_at = time.time()
        if session.id in self.queue:
            self.queue.remove(session.id)
        if session.code:
            self._codes.pop(session.code, None)
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
        ws_url=with_room(public_url_for(session.size, session.language), session.room),
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
    pub_room = with_room(public_url_for(session.size, session.language), session.room)
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
        ws_url=with_room(public_url_for(session.size, session.language), session.room),
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


# ---------------------------------------------------------------------------
# Room API（ソロ/マルチ）。/demo の前段。匿名トークンで席を識別（アカウント不要）。
# ---------------------------------------------------------------------------
class CreateRoomRequest(BaseModel):
    mode: str = "solo"               # solo | multi
    size: int = 5                    # 村の人数（5 or 9）
    language: str = DEFAULT_LANGUAGE
    human_slots: int = 1             # マルチの人間席数（1..size、残りをAIが埋める）
    token: str = ""                  # 端末の匿名トークン（localStorage）
    agent_prompt: str = ""           # 自作AIの「プレイスタイル/性格」文（任意）。AI席に注入。
    my_ai_count: int = -1            # AI席のうち自作AIにする数（観戦の構成用。-1=persona適用なら全AI）


class JoinRoomRequest(BaseModel):
    token: str = ""                  # 端末の匿名トークン
    agent_prompt: str = ""           # 離脱時の takeover に使う自作プロンプト（任意）


class ParticipantInfo(BaseModel):
    display_name: str
    team: str
    is_host: bool = False


class RoomResponse(BaseModel):
    room_id: str
    code: str
    mode: str
    status: str                      # waiting | queued | running | finished | error
    size: int
    language: str
    human_slots: int
    ai_count: int
    participants: list[ParticipantInfo]
    you: ParticipantInfo | None = None   # 呼び出し元(token)の席
    host_token: str = ""             # 自分がホストのときだけ返す（start 認可用）
    ws_url: str | None = None        # running のとき、あなたの接続先 URL（team は you.team）
    takeover_events: list[str] = []  # AIが引き継いだ離脱者の表示名（時系列）
    position: int = 0
    error: str | None = None


def _room_response(session: Session, token: str) -> RoomResponse:
    you = lobby.participant_of(session, token)
    ws = None
    if session.status == "running" and you is not None:
        ws = with_room(public_url_for(session.size, session.language), session.room)
    parts = [
        ParticipantInfo(display_name=p.display_name, team=p.team, is_host=(p.token == session.host_token))
        for p in session.participants
    ]
    return RoomResponse(
        room_id=session.id,
        code=session.code,
        mode=session.mode,
        status=session.status,
        size=session.size,
        language=session.language,
        human_slots=session.human_slots,
        ai_count=session.ai_count,
        participants=parts,
        you=(
            ParticipantInfo(display_name=you.display_name, team=you.team, is_host=(you.token == session.host_token))
            if you else None
        ),
        host_token=session.host_token if token and token == session.host_token else "",
        ws_url=ws,
        takeover_events=list(session.takeover_events),
        position=lobby.position_of(session.id),
        error=session.error,
    )


class PromptPreviewRequest(BaseModel):
    language: str = DEFAULT_LANGUAGE
    persona: str = ""


@app.post("/api/prompt/preview")
async def prompt_preview(req: PromptPreviewRequest) -> dict[str, Any]:
    """自作AIエディタ用: 変数をサンプル値に解決した persona のプレビューを返す。"""
    return {"preview": preview_persona(req.persona), "max_chars": MAX_PERSONA_CHARS}


@app.post("/api/rooms", response_model=RoomResponse)
async def create_room(req: CreateRoomRequest) -> RoomResponse:
    token = req.token or secrets.token_urlsafe(9)
    session = await lobby.create_room(
        mode=req.mode, size=req.size, language=req.language, human_slots=req.human_slots,
        token=token, agent_prompt=req.agent_prompt, my_ai_count=req.my_ai_count,
    )
    if session.mode == "solo":
        await lobby._schedule()  # noqa: SLF001  ソロは即開始
    return _room_response(session, token)


@app.post("/api/rooms/{code}/join", response_model=RoomResponse)
async def join_room(code: str, req: JoinRoomRequest) -> RoomResponse:
    token = req.token or secrets.token_urlsafe(9)
    try:
        session, _ = await lobby.join_room(code, token, req.agent_prompt)
    except KeyError:
        raise HTTPException(status_code=404, detail="room not found")
    except ValueError as ex:
        raise HTTPException(status_code=409, detail=str(ex))
    return _room_response(session, token)


@app.get("/api/rooms/{code}", response_model=RoomResponse)
async def get_room(code: str, token: str = "") -> RoomResponse:
    session = lobby.room_by_code(code)
    if session is None:
        raise HTTPException(status_code=404, detail="room not found")
    # ハートビート更新（放棄検出用）。自分の席があればそれも更新。
    session.last_seen = time.time()
    you = lobby.participant_of(session, token)
    if you is not None:
        you.last_seen = time.time()
    return _room_response(session, token)


@app.post("/api/rooms/{code}/start", response_model=RoomResponse)
async def start_room(code: str, req: JoinRoomRequest) -> RoomResponse:
    try:
        session = await lobby.start_room(code, req.token)
    except KeyError:
        raise HTTPException(status_code=404, detail="room not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="only the host can start")
    return _room_response(session, req.token)


@app.post("/api/rooms/{code}/leave")
async def leave_room(code: str, req: JoinRoomRequest) -> dict[str, str]:
    # マルチ進行中の離脱は卓を殺さず、その席をAIに引き継がせる（handle_leave 内で判定）。
    status = await lobby.handle_leave(code, req.token)
    return {"status": status}
