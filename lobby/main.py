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
import re
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

from prompt_provider import (
    MAX_PROMPT_CHARS,
    REQUESTS,
    FilePromptProvider,
    preview_prompt,
    variable_catalog,
)

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
# --- 卓ごとの LLM 指定（ユーザ持ち込みキー）------------------------------------
#
# サーバ側に既定（vLLM や運営のAPIキー）があればそれで遊べる。無い／止めている
# ときは、遊ぶ人が自分のキーを入れればその卓だけそのキーで動く。
#
# キーは Session の寿命だけメモリに置く。ログにもレスポンスにも出さない。
BYO_PROVIDERS = ("openai", "google", "anthropic")

# モデル名が空のまま持ち込まれたときの、プロバイダ別の既定。
# サーバ既定 LLM_MODEL は vLLM のモデル名であることが多く、商用APIにそのまま渡すと
# 「そんなモデルは無い」で死ぬ（[[byo-default-model]]）。
BYO_DEFAULT_MODELS = {
    "openai": os.environ.get("BYO_DEFAULT_OPENAI", "gpt-5.6-luna"),
    "google": os.environ.get("BYO_DEFAULT_GOOGLE", "gemini-2.5-flash-lite"),
    "anthropic": os.environ.get("BYO_DEFAULT_ANTHROPIC", "claude-haiku-4-5"),
}

# 画面に出す「おすすめ」。価格（入力/出力 $/100万トークン）はどのプロバイダも
# APIから取れないので手書きし、PRICE_ASOF を添えて出す。
# /api/models が実際にそのキーで使える一覧と突き合わせるので、
# ここが古くなっても「存在しないモデルを勧める」ことにはならない（消えるだけ）。
PRICE_ASOF = "2026-09"
RECOMMENDED_MODELS: dict[str, list[dict[str, str]]] = {
    "openai": [
        {"id": "gpt-5.6-luna", "note": "安くて速い。デモ向き", "price": "$0.20 / $1.20"},
        {"id": "gpt-5.6-terra", "note": "会話の質を上げたいとき", "price": "$2.00 / $12.00"},
    ],
    "google": [
        {"id": "gemini-2.5-flash-lite", "note": "最安。デモ向き", "price": "$0.10 / $0.40"},
        {"id": "gemini-3.5-flash-lite", "note": "新しめで安い", "price": "$0.30 / $2.50"},
    ],
    "anthropic": [
        {"id": "claude-haiku-4-5", "note": "安くて速い。デモ向き", "price": "$1.00 / $5.00"},
        {"id": "claude-sonnet-5", "note": "会話の質を上げたいとき", "price": "$2.00 / $10.00"},
    ],
}
PRICING_PAGES = {
    "openai": "https://developers.openai.com/api/docs/pricing",
    "google": "https://ai.google.dev/pricing",
    "anthropic": "https://docs.anthropic.com/en/docs/about-claude/pricing",
}


def sanitize_llm(raw: Any) -> dict[str, str]:
    """外から来た LLM 指定を検証する。使えない形なら空 dict（＝サーバ既定を使う）。"""
    if not isinstance(raw, dict):
        return {}
    provider = str(raw.get("provider", "")).strip().lower()
    api_key = str(raw.get("api_key", "")).strip()
    model = str(raw.get("model", "")).strip()
    if provider not in BYO_PROVIDERS or not api_key:
        return {}
    if len(api_key) > 512 or len(model) > 128:
        return {}
    return {"provider": provider, "api_key": api_key, "model": model}


# vLLM の生死を毎回調べると重いので、少しのあいだ結果を覚えておく
_VLLM_PROBE: dict[str, float | bool] = {"at": 0.0, "alive": False}
_VLLM_PROBE_TTL = 15.0


async def _vllm_alive() -> bool:
    """vLLM が実際に応答するか。設定してあるだけで落ちている状態を見抜く。"""
    if not OPENAI_BASE_URL:
        return False
    now = time.time()
    if now - float(_VLLM_PROBE["at"]) < _VLLM_PROBE_TTL:
        return bool(_VLLM_PROBE["alive"])

    from urllib.parse import urlparse

    u = urlparse(OPENAI_BASE_URL)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    alive = False
    if host:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.0)
            w.close()
            alive = True
        except Exception:  # noqa: BLE001  落ちている・届かない
            alive = False
    _VLLM_PROBE.update({"at": now, "alive": alive})
    return alive


async def server_llm_ready() -> bool:
    """サーバ側の既定だけで遊べる状態か（＝キー無しで入れるか）。

    vLLM は「設定してあるか」ではなく「本当に応答するか」で見る。
    GPU を止めた時間帯は自動的に false になり、画面が
    「いまは自分のAPIキーが要ります」に切り替わる。
    """
    if LLM_PROVIDER == "vllm":
        return await _vllm_alive()
    if LLM_PROVIDER == "google":
        return bool(os.environ.get("GOOGLE_API_KEY"))
    return bool(os.environ.get("OPENAI_API_KEY"))


AI_COUNT = int(_env("AI_COUNT", "4"))
# 1卓の総人数（サーバの game.agent_count と一致させる）。外部接続＋サンプルAI = この値。
AGENT_TOTAL = int(_env("AGENT_TOTAL", "5"))
# 同時に走れる卓数（vLLMならGPU、商用APIならレート/コストで決める）。
# room_match により各卓は room で分離され、ゲームサーバは1プロセスで複数卓を並行ホストできる。
# 実上限は LLM スループット（vLLMのGPU同時処理/商用APIレート）と spawn するプロセス数で決まるため、
# 環境に合わせて .env で調整する。
MAX_CONCURRENT_GAMES = int(_env("MAX_CONCURRENT_GAMES", "20"))
# サーバ側 LLM（GPU）を使わない卓＝外部エージェントだけ／持ち込みキーの卓の同時数。
# こちらはGPUを食わないので緩め（サーバの CPU/メモリと spawn 数で決める）。
MAX_CONCURRENT_EXTERNAL = int(_env("MAX_CONCURRENT_EXTERNAL", "20"))

# --- 試合ログの公開 ---
# ゲームサーバが書くログの場所（game/*.log が CSV、json/*.json が JSON。サーバの cwd 基準）。
GAME_LOG_DIR = Path(_env("GAME_LOG_DIR", str(WORK_ROOT / "log")))
# 公開先（scp の宛先。例 aiwolf:/var/www/html/aiwolf/2026/demo）。空なら公開しない。
LOG_PUBLISH_DEST = _env("LOG_PUBLISH_DEST", "")
# 画面に出す公開ページのURL（「対戦ログは公開されます（リンク）」の飛び先）。
LOG_PUBLIC_URL = _env("LOG_PUBLIC_URL", "")
# 何秒おきに「終わったログ」を探して送るか
LOG_PUBLISH_INTERVAL = int(_env("LOG_PUBLISH_INTERVAL", "60"))

# --- 無人運転（HANDOFF §7）---
# ハング卓の上限時間。これを超えて走行中ならAIプロセスを強制回収しスロット解放。
MAX_SESSION_SECONDS = int(_env("MAX_SESSION_SECONDS", "1800"))  # 30分
# 待機列のハートビート猶予。フロントのポーリングが途絶えた待機者は放棄とみなし列から除去。
QUEUE_HEARTBEAT_TTL = int(_env("QUEUE_HEARTBEAT_TTL", "20"))  # 秒
# マルチ卓の待機部屋（開始前）のハートビート猶予。ホスト/参加者の誰もポーリングしなくなったら回収。
WAITING_ROOM_TTL = int(_env("WAITING_ROOM_TTL", "60"))  # 秒
# 終了/エラー済みセッションを辞書から掃除するまでの保持時間。
FINISHED_RETENTION_SECONDS = int(_env("FINISHED_RETENTION_SECONDS", "300"))

# --- 待合室ゲート ---
# ゲームサーバの /control/* を叩く共有鍵（サーバ側の CONTROL_KEY と同じ値）。
# 未設定ならゲート無し＝人数が揃った瞬間に卓が立つ（旧来の挙動）。
CONTROL_KEY = _env("CONTROL_KEY", "")
# 待合室のまま誰も準備完了/開始を押さないときの上限。超えたら卓を片付ける。
GATE_WAIT_TTL = int(_env("GATE_WAIT_TTL", "600"))  # 10分


def _control_base(size: int, language: str) -> str:
    # 内部 ws URL（ws://127.0.0.1:8080/ws）から http のベース（http://127.0.0.1:8080）を作る。
    from urllib.parse import urlparse

    u = urlparse(internal_url_for(size, language))
    scheme = "https" if u.scheme == "wss" else "http"
    return f"{scheme}://{u.netloc}"


async def game_control(session: "Session", action: str) -> dict[str, Any] | None:
    """ゲームサーバの待合室ゲート（/control/hold|release|drop|room）を叩く。

    CONTROL_KEY 未設定・サーバが古い・失敗のときは None を返し、呼び出し側はゲート無しで続行する
    （卓が立たなくなるより、旧来どおり揃った瞬間に始まる方がまし）。"""
    if not CONTROL_KEY:
        return None
    import httpx

    url = f"{_control_base(session.size, session.language)}/control/{action}"
    method = "GET" if action == "room" else "POST"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.request(
                method, url, params={"room": session.room}, headers={"X-Control-Key": CONTROL_KEY}
            )
    except Exception as ex:  # noqa: BLE001
        print(f"[gate] {action} failed: {ex}", flush=True)
        return None
    if r.status_code != 200:
        print(f"[gate] {action} -> HTTP {r.status_code}: {r.text[:200]}", flush=True)
        return None
    return r.json()

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
    # {request: text} の辞書。空なら既定プロンプトで引き継ぐ。
    agent_prompts: dict[str, str] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)


@dataclass
class AgentEntry:
    """持ち込み卓（byo）で外部エージェントが取った枠。name はチーム名（末尾数字なし）。
    接続時は name+番号（例 myagent1, myagent2）を NAME に返す。count はその体数。"""
    name: str
    count: int
    token: str          # 枠を取った端末のトークン（同じ端末からの取り直しを許す）
    claimed_at: float = field(default_factory=time.time)


@dataclass
class Session:
    """1卓（Room）。ソロ=人間1＋AI、マルチ=人間N＋AI、byo=外部エージェントN＋人間M＋AI。

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
    # agent_prompts: この卓のサンプルAIに使うユーザ自作の「リクエスト別プロンプト」（任意）。
    # {request: text} の辞書。provider がトークンを実行時 Jinja に解決して上書き。空なら既定。
    agent_prompts: dict[str, str] = field(default_factory=dict)
    # ai_specs: AI席をグループに分けて別プロンプトで起動する（観戦の「自作AI×N＋既定×M」等）。
    # 各要素は (prompts_dict, count)。空なら _spawn が単一グループ((agent_prompts, ai_count))にフォールバック。
    ai_specs: list[Any] = field(default_factory=list)
    human_slots: int = 1    # 人間の席数（マルチでホストが指定。残りをAIが埋める）
    # talk_length: 1回の発言の目安（文字）。0 なら指定しない。
    # サーバ側の上限(base_length)は別にあるので、これはそれ以下でしか効かない。
    talk_length: int = 0
    # llm: この卓だけで使う LLM 指定（遊ぶ人が持ち込んだキー）。空ならサーバ既定。
    # 卓が消えると一緒に消える。ログにもレスポンスにも出さない。
    llm: dict[str, str] = field(default_factory=dict)
    host_token: str = ""    # ホスト（部屋作成者）の匿名トークン
    participants: list[Participant] = field(default_factory=list)
    process: Any = None     # subprocess.Popen | None
    # 人間離脱時にその席を引き継ぐために追加 spawn したAIプロセス（(Popen, config_path) の組）。
    takeover_processes: list[Any] = field(default_factory=list)
    # AIが引き継いだ離脱者の表示名（時系列）。プレイ中の各クライアントが room ポーリングで拾い、
    # 「○○が退出。AIが代わりに参加」をフィードに出すのに使う。
    takeover_events: list[str] = field(default_factory=list)
    config_path: Path | None = None
    # --- 待合室ゲート ---
    # off      = ゲート無し（揃った瞬間に開始。CONTROL_KEY 未設定や旧サーバ）
    # hold     = 保留中。全員の準備完了（またはホストの開始）を待っている
    # released = 保留解除済み。席が揃い次第サーバが卓を立てる
    # started  = 卓が立った（INITIALIZE が飛んだ）
    gate: str = "off"
    ready: set[str] = field(default_factory=set)  # 準備完了を押した人間の team 名
    released_at: float | None = None
    # publish_logs: この卓のログを公開ページへ送るか（既定は公開。卓を作る人が外せる）。
    publish_logs: bool = True
    # --- 持ち込み卓（byo）---
    agent_slots: int = 0                                   # 外部エージェントの席数（合計）
    agent_entries: list[AgentEntry] = field(default_factory=list)  # 取られた枠
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

    def _make_participant(self, token: str, agent_prompts: dict | None = None) -> Participant:
        display = self._next_display_name()
        return Participant(
            token=token, display_name=display, team=f"you-{display}",
            agent_prompts=dict(agent_prompts or {}),
        )

    @staticmethod
    def _new_team(display_name: str) -> str:
        # サーバは接続名の末尾数字を除去して team を抽出する（connection.go）。
        # AIは team+idx(1..4) を送るので、末尾は必ず非数字にして
        # 「末尾数字除去後のプレフィックス」が他卓と衝突しないようにする。
        token = secrets.token_hex(4) + secrets.choice(string.ascii_lowercase)
        return f"s-{display_name}-{token}"

    async def create_session(
        self,
        external_slots: int,
        size: int = 5,
        language: str = DEFAULT_LANGUAGE,
        llm: dict[str, str] | None = None,
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
                llm=llm or {},
            )
            self.sessions[sid] = session
            self.queue.append(sid)
            return session

    async def join(
        self,
        size: int = 5,
        language: str = DEFAULT_LANGUAGE,
        llm: dict[str, str] | None = None,
    ) -> Session:
        # /demo の人間1枠（外部=人間1人、残りをAIが埋める）。後方互換用。
        return await self.create_session(
            external_slots=1, size=size, language=language, llm=llm
        )

    # --- Room（ソロ/マルチ）---
    async def create_room(
        self,
        mode: str,
        size: int = 5,
        language: str = DEFAULT_LANGUAGE,
        human_slots: int = 1,
        token: str = "",
        agent_prompts: dict | None = None,
        my_ai_count: int = -1,
        llm: dict[str, str] | None = None,
        talk_length: int = 0,
    ) -> Session:
        """卓を作る。ソロは即開始（AI spawn）、マルチは待機（コード発行・ホスト参加）。
        agent_prompts があれば、この卓のサンプルAIにユーザ自作のリクエスト別プロンプトを使う。
        my_ai_count>=0 なら、AI席のうち my_ai_count 体だけ自作プロンプト・残りは既定（観戦の構成用）。"""
        if size not in VALID_SIZES:
            size = 5
        language = PROMPT_PROVIDER.resolve_language(language)
        # spectate = AI同士の自己対戦。人間の席を作らず、全席をAIで埋める。
        mode = mode if mode in ("multi", "spectate") else "solo"
        # 人間席数: 観戦は0、ソロは1、マルチは 1..size
        if mode == "spectate":
            human_slots = 0
        elif mode == "solo":
            human_slots = 1
        else:
            human_slots = max(1, min(human_slots, size))
        token = token or secrets.token_urlsafe(9)
        agent_prompts = dict(agent_prompts or {})
        async with self._lock:
            sid = secrets.token_urlsafe(9)
            host = self._make_participant(token, agent_prompts)
            session = Session(
                id=sid,
                display_name=host.display_name,
                team=self._new_team(host.display_name),
                room=sid,
                human_team=host.team,
                size=size,
                language=language,
                mode=mode,
                # solo/spectate: サンプルAIに自作プロンプトを使う(①)。multi: サンプルAIは既定とし、
                # 自作は host 参加者に持たせて「離脱時の takeover」に使う(②)。
                agent_prompts=agent_prompts if mode in ("solo", "spectate") else {},
                llm=llm or {},
                talk_length=max(0, min(int(talk_length or 0), 2000)),
                human_slots=human_slots,
                host_token=token,
                participants=[host],
            )
            self.sessions[sid] = session
            if mode in ("solo", "spectate"):
                # 即「順番待ち→spawn」。
                #   solo     : 人間1席を空けて残り(size-1)をAIが埋める
                #   spectate : 人間の席を作らず size 体すべてAI（自己対戦）
                session.external_slots = 0 if mode == "spectate" else 1
                session.ai_count = size if mode == "spectate" else max(0, size - 1)
                # 自作プロンプトの構成: my_ai_count>=0 なら「自作×k＋既定×残り」、
                # それ以外(=-1)で自作があれば全AIに適用（①の挙動）。
                if agent_prompts and my_ai_count >= 0:
                    k = max(0, min(my_ai_count, session.ai_count))
                    session.ai_specs = [(agent_prompts, k), ({}, session.ai_count - k)]
                elif agent_prompts:
                    session.ai_specs = [(agent_prompts, session.ai_count)]
                session.status = "queued"
                self.queue.append(sid)
            else:
                # マルチは合言葉を発行して待機。開始はホストの start_room で。
                session.code = self._gen_code()
                self._codes[session.code] = sid
                session.status = "waiting"
            return session

    # --- 持ち込み卓（byo）: 外部エージェント N ＋ 人間 M ＋ 残りサンプルAI ---
    async def create_byo_room(
        self,
        size: int,
        agent_slots: int,
        human_slots: int,
        language: str = DEFAULT_LANGUAGE,
        token: str = "",
        llm: dict[str, str] | None = None,
        talk_length: int = 0,
        publish_logs: bool = True,
    ) -> Session:
        """持ち込み卓を作る。合言葉を発行し、席数は固定（外部 agent_slots・人間 human_slots・残りAI）。
        サンプルAIは即起動して待合室で待つ。外部エージェントは枠を取って接続、人間は /demo の合言葉で参加。
        全員そろって人間が準備完了になれば始まる（人間ゼロならホストの開始ボタン）。"""
        if size not in VALID_SIZES:
            size = 5
        language = PROMPT_PROVIDER.resolve_language(language)
        agent_slots = max(0, min(int(agent_slots), size))
        human_slots = max(0, min(int(human_slots), size - agent_slots))
        if agent_slots + human_slots < 1:
            raise ValueError("agent_slots + human_slots must be >= 1")
        token = token or secrets.token_urlsafe(9)
        async with self._lock:
            display = self._next_display_name()
            sid = secrets.token_urlsafe(9)
            session = Session(
                id=sid,
                display_name=display,
                team=self._new_team(display),
                room=sid,
                human_team="",
                size=size,
                language=language,
                mode="byo",
                ai_count=size - agent_slots - human_slots,
                external_slots=agent_slots + human_slots,
                human_slots=human_slots,
                agent_slots=agent_slots,
                llm=llm or {},
                talk_length=max(0, min(int(talk_length or 0), 2000)),
                host_token=token,
                publish_logs=bool(publish_logs),
            )
            session.code = self._gen_code()
            self._codes[session.code] = sid
            self.sessions[sid] = session
            session.status = "queued"
            self.queue.append(sid)
        # 外部エージェントは順番待ちの間にも繋いでくるので、AI起動を待たずに今すぐ保留を入れる
        # （全員外部の卓だと、保留が無いまま人数が揃った瞬間に立ってしまう）。
        session.gate = "hold" if await game_control(session, "hold") else "off"
        return session

    _NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    _RESERVED_PREFIXES = ("you-", "s-", "demo")

    def validate_agent_name(self, name: str) -> str | None:
        """外部エージェントのチーム名の決まり。None なら OK、文字列なら理由。"""
        if not self._NAME_RE.match(name):
            return "英字で始まる英数字・_・-（32文字まで）にしてください"
        if name.rstrip("0123456789") != name:
            return "末尾は数字以外にしてください（接続時に 1,2,… が付きます）"
        if name.lower().startswith(self._RESERVED_PREFIXES):
            return "you- / s- / demo で始まる名前は予約されています"
        return None

    def agent_slots_taken(self, session: Session) -> int:
        return sum(e.count for e in session.agent_entries)

    async def claim_agent_slot(self, code: str, name: str, count: int, token: str) -> tuple[Session, AgentEntry]:
        """持ち込み卓の外部エージェント枠を name で取る。同じ端末(token)からの取り直しは上書き。"""
        token = token or secrets.token_urlsafe(9)
        count = max(1, int(count))
        async with self._lock:
            session = self.room_by_code(code)
            if session is None or session.mode != "byo":
                raise KeyError("room not found")
            if session.status not in ("queued", "running") or session.gate == "started":
                raise ValueError("room already started")
            reason = self.validate_agent_name(name)
            if reason:
                raise ValueError(reason)
            if name == session.team:
                raise ValueError("その名前は使えません")
            mine = next((e for e in session.agent_entries if e.token == token), None)
            for e in session.agent_entries:
                if e.name == name and e is not mine:
                    raise ValueError("その名前は同じ卓で既に使われています")
            taken = self.agent_slots_taken(session) - (mine.count if mine else 0)
            if taken + count > session.agent_slots:
                raise ValueError(f"枠が足りません（残り {session.agent_slots - taken}）")
            if mine:
                mine.name, mine.count, mine.claimed_at = name, count, time.time()
                entry = mine
            else:
                entry = AgentEntry(name=name, count=count, token=token)
                session.agent_entries.append(entry)
            session.last_seen = time.time()
            return session, entry

    async def join_room(self, code: str, token: str, agent_prompts: dict | None = None) -> tuple[Session, Participant]:
        """マルチ卓／持ち込み卓に合言葉で参加する。空席が無ければ例外。既参加(同token)なら既存席を返す。
        agent_prompts はこの人の離脱時 takeover に使う自作プロンプト（リクエスト別辞書）。"""
        token = token or secrets.token_urlsafe(9)
        agent_prompts = dict(agent_prompts or {})
        async with self._lock:
            session = self.room_by_code(code)
            if session is None or session.mode not in ("multi", "byo"):
                raise KeyError("room not found")
            if session.mode == "multi" and session.status != "waiting":
                raise ValueError("room already started")
            if session.mode == "byo" and (session.status not in ("queued", "running") or session.gate == "started"):
                raise ValueError("room already started")
            for p in session.participants:
                if p.token == token:  # 再入（リロード等）は既存席をそのまま（自作は最新に更新）
                    p.last_seen = time.time()
                    p.agent_prompts = agent_prompts
                    return session, p
            if len(session.participants) >= session.human_slots:
                raise ValueError("room full")
            p = self._make_participant(token, agent_prompts)
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

    @staticmethod
    def uses_server_llm(session: Session) -> bool:
        # サンプルAIを1体でも起動し、かつ持ち込みキーが無い＝サーバ側 LLM（GPU）を使う卓
        return session.ai_count > 0 and not session.llm

    def running_count(self, server_llm: bool | None = None) -> int:
        return sum(
            1 for s in self.sessions.values()
            if s.status == "running" and (server_llm is None or self.uses_server_llm(s) == server_llm)
        )

    def position_of(self, sid: str) -> int:
        # 待機列での順位（1始まり）。走行中/不在は 0。
        try:
            return self.queue.index(sid) + 1
        except ValueError:
            return 0

    # --- スケジューラ: 空きスロットがあれば待機列の先頭を spawn ---
    async def _schedule(self) -> None:
        async with self._lock:
            # 2つの枠（GPU卓 / それ以外）を別々に見る。片方が満杯でももう片方の卓は待たせない。
            progressed = True
            while progressed and self.queue:
                progressed = False
                for sid in list(self.queue):
                    session = self.sessions.get(sid)
                    if session is None or session.status != "queued":
                        self.queue.remove(sid)
                        continue
                    gpu = self.uses_server_llm(session)
                    cap = MAX_CONCURRENT_GAMES if gpu else MAX_CONCURRENT_EXTERNAL
                    if self.running_count(gpu) >= cap:
                        continue
                    self.queue.remove(sid)
                    progressed = True
                    if not session.publish_logs:
                        self._mark_private(session)
                    try:
                        # 先に保留を入れてからAIを起動する（起動が速いと揃った瞬間に立ってしまう）。
                        session.gate = "hold" if await game_control(session, "hold") else "off"
                        self._spawn_agents(session)
                        session.status = "running"
                        session.started_at = time.time()
                    except Exception as ex:  # noqa: BLE001
                        session.status = "error"
                        session.error = str(ex)

    # --- 試合ログの公開 ---
    # ログのファイル名は "{timestamp}_{teams}" なので、卓に居たチーム名で「どの卓のログか」が分かる。
    # 公開しない卓のチーム名を1行ずつ残し、送るときにファイル名と突き合わせる（ロビー再起動をまたいでも効く）。
    @staticmethod
    def _private_file() -> Path:
        return GAME_LOG_DIR / "private-teams.txt"

    @staticmethod
    def _published_file() -> Path:
        return GAME_LOG_DIR / "published.txt"

    def _mark_private(self, session: Session) -> None:
        teams = {session.team, session.human_team, *(p.team for p in session.participants)}
        teams = {t for t in teams if t}
        try:
            GAME_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with self._private_file().open("a", encoding="utf-8") as f:
                for t in sorted(teams):
                    f.write(t + "\n")
        except OSError as ex:
            print(f"[publish] private-teams write failed: {ex}", flush=True)

    @staticmethod
    def _read_lines(path: Path) -> set[str]:
        try:
            return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}
        except OSError:
            return set()

    @staticmethod
    def _log_finished(path: Path) -> bool | None:
        """完走したログか。True=完走 / False=中断（result が NONE）/ None=まだ書き終わっていない。"""
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 400))
                tail = f.read().decode("utf-8", "replace").rstrip("\n").splitlines()
        except OSError:
            return None
        if not tail:
            return None
        last = tail[-1].split(",")
        if len(last) >= 5 and last[1] == "result":
            return last[4].strip() != "NONE"
        return None

    async def _scp(self, src: Path, dest: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", str(src), dest,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            print(f"[publish] scp {src.name} failed: {err.decode(errors='replace').strip()[:200]}", flush=True)
            return False
        return True

    async def publish_logs(self) -> None:
        """終わった試合のログを公開先へ1回だけ送る（繰り返しの同期はしない）。

        - 末尾が result のファイルだけ（進行中は送らない）。中断（NONE）は送らずスキップ扱いにする。
        - 公開しない卓（private-teams.txt のチーム名を含む）は送らない。
        - 送った/スキップしたファイル名は published.txt に残し、二度と見ない。"""
        if not LOG_PUBLISH_DEST:
            return
        game_dir = GAME_LOG_DIR / "game"
        json_dir = GAME_LOG_DIR / "json"
        if not game_dir.is_dir():
            return
        published = self._read_lines(self._published_file())
        private = self._read_lines(self._private_file())
        done: list[str] = []
        now = time.time()
        for f in sorted(game_dir.glob("*.log")):
            if f.name in published:
                continue
            try:
                age = now - f.stat().st_mtime
            except OSError:
                continue
            if age < 20:
                continue  # 書き終わりを待つ
            finished = self._log_finished(f)
            if finished is None:
                if age > 6 * 3600:
                    done.append(f.name)  # 結果が書かれないまま古くなった＝放棄。もう見ない
                continue
            if not finished or any(t and t in f.name for t in private):
                done.append(f.name)
                continue
            ok = await self._scp(f, LOG_PUBLISH_DEST.rstrip("/") + "/log/")
            if ok:
                j = json_dir / (f.stem + ".json")
                if j.exists():
                    await self._scp(j, LOG_PUBLISH_DEST.rstrip("/") + "/json/")
                done.append(f.name)
                print(f"[publish] sent {f.name}", flush=True)
        if done:
            try:
                with self._published_file().open("a", encoding="utf-8") as fh:
                    for name in done:
                        fh.write(name + "\n")
            except OSError as ex:
                print(f"[publish] published.txt write failed: {ex}", flush=True)

    # --- 待合室ゲート ---
    def human_teams(self, session: Session) -> list[str]:
        """この卓で「準備完了」を押す必要がある人間の team 名。空なら開始ボタン式（観戦・AIのみ）。"""
        if session.mode == "multi":
            return [p.team for p in session.participants]
        if session.mode == "spectate":
            return []
        if session.mode == "byo":
            return [p.team for p in session.participants]
        return [session.human_team]

    def all_ready(self, session: Session) -> bool:
        if session.mode == "byo" and len(session.participants) < session.human_slots:
            return False  # 人間の席がまだ埋まっていない
        return all(t in session.ready for t in self.human_teams(session))

    async def gate_view(self, session: Session, token: str = "") -> dict[str, Any]:
        """画面用の待合室ビュー。サーバの着席一覧と、ロビーが持つ準備完了を合わせる。"""
        seats: list[dict[str, Any]] = []
        count = 0
        if session.gate in ("hold", "released"):
            info = await game_control(session, "room")
            if info is not None:
                count = int(info.get("count", 0))
                humans = set(self.human_teams(session))
                for st in info.get("seats", []):
                    team = str(st.get("team", ""))
                    seats.append({
                        "team": team,
                        "name": str(st.get("name", "")),
                        "human": team in humans,
                        "ready": team in session.ready or team not in humans,
                    })
                # 解除後に席が消えた＝卓が立った
                if session.gate == "released" and count == 0 and not info.get("held"):
                    session.gate = "started"
        is_host = bool(token) and token == session.host_token
        # 持ち込み卓: 取られた枠ごとに「何体つながったか」（席の team 名で数える）
        agents = []
        if session.mode == "byo":
            for e in session.agent_entries:
                agents.append({
                    "name": e.name, "count": e.count,
                    "connected": sum(1 for st in seats if st["team"] == e.name),
                    "mine": bool(token) and token == e.token,
                })
        return {
            "code": session.code,
            "mode": session.mode,
            "agent_slots": session.agent_slots,
            "agent_slots_taken": self.agent_slots_taken(session),
            "agents": agents,
            "human_slots": session.human_slots,
            "humans_joined": [
                {"name": p.display_name, "team": p.team, "ready": p.team in session.ready}
                for p in session.participants
            ],
            "gate": session.gate,
            "size": session.size,
            "count": count,
            "seats": seats,
            "humans": self.human_teams(session),
            "ready": sorted(session.ready),
            "all_ready": self.all_ready(session),
            "is_host": is_host,
            # 開始ボタンを出す条件: ホストで、人間が全員準備完了（人間なしなら即）
            "can_start": is_host and session.gate == "hold" and self.all_ready(session),
        }

    async def set_ready(self, session: Session, team: str, ready: bool) -> None:
        if team not in self.human_teams(session):
            raise KeyError("not a human seat")
        if ready:
            session.ready.add(team)
        else:
            session.ready.discard(team)
        await self.maybe_release(session)

    async def maybe_release(self, session: Session) -> None:
        """全人間が準備完了で、席も揃っていれば保留を外す（人間なしの卓は開始ボタン待ち）。"""
        if session.gate != "hold" or not self.human_teams(session) or not self.all_ready(session):
            return
        info = await game_control(session, "room")
        if info is not None and int(info.get("count", 0)) >= session.size:
            await self.release(session)

    async def release(self, session: Session) -> None:
        if session.gate != "hold":
            return
        r = await game_control(session, "release")
        session.released_at = time.time()
        if r is None:
            session.gate = "off"
        else:
            session.gate = "started" if r.get("formed") else "released"

    def _drop_gate(self, session: Session) -> None:
        # 待合室に残った接続を切って片付ける（fire-and-forget。失敗しても害はない）。
        if session.gate in ("hold", "released"):
            session.gate = "off"
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(game_control(session, "drop"))

    def _spawn_agents(self, session: Session) -> None:
        # AI席のグループ (prompts, count)。ai_specs があれば異種AI（自作AI＋既定 等）、
        # 無ければ単一グループ（session.agent_prompts を ai_count 体）。
        specs = [
            (p, c)
            for (p, c) in (session.ai_specs or [(session.agent_prompts, session.ai_count)])
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
        for i, (prompts, count) in enumerate(specs):
            team = session.team if i == 0 else self._new_team(f"{session.display_name}-g{i}")
            cfg = self._build_agent_config(
                team, count, ai_url, session.language, prompts, session.llm, session.talk_length
            )
            cfg_path = GENERATED_DIR / (f"{session.id}.yml" if i == 0 else f"{session.id}-g{i}.yml")
            with cfg_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            # start_new_session=True で別プロセスグループにし、終了時に一括 kill 可能にする（M8）
            proc = subprocess.Popen(  # noqa: S603
                [AGENT_LLM_PYTHON, "src/main.py", "-c", str(cfg_path)],
                cwd=str(AGENT_LLM_DIR),
                env=self._child_env(session.llm),
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
    def _child_env(llm: dict[str, str] | None = None) -> dict[str, str]:
        # APIキー等は os.environ.copy() で子に引き継がれる（agent.py は os.environ を参照）。
        # OPENAI_BASE_URL は vLLM のときだけ渡し、それ以外では取り除く（[[openai-base-url-footgun]]）。
        env = os.environ.copy()
        provider = (llm or {}).get("provider") or LLM_PROVIDER

        if provider == "vllm" and OPENAI_BASE_URL:
            env["OPENAI_BASE_URL"] = OPENAI_BASE_URL
        else:
            env.pop("OPENAI_BASE_URL", None)
            env.pop("OPENAI_API_BASE", None)

        # 持ち込みキーはこの子プロセスの環境にだけ入れる。
        # 親の os.environ は触らないので、他の卓には漏れない。
        if llm and llm.get("api_key"):
            for name in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
                env.pop(name, None)
            # Gemini のライブラリは、APIキーより先にサービスアカウント認証(ADC)を
            # 見にいくことがある。運営マシンに ADC が置いてあると、参加者のキーが
            # 無視されて別プロジェクトの認証待ちで固まる（落ちないので検知もされない）。
            # 持ち込みキーのときは ADC 系を子に渡さない。
            for name in (
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GOOGLE_CLOUD_PROJECT",
                "GOOGLE_CLOUD_QUOTA_PROJECT",
                "GCLOUD_PROJECT",
                "GCP_PROJECT",
            ):
                env.pop(name, None)
            key_env = {
                "google": "GOOGLE_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
            }.get(provider, "OPENAI_API_KEY")
            env[key_env] = llm["api_key"]
        return env

    def _spawn_takeover_ai(self, session: Session, original_name: str, custom_prompts: dict | None = None) -> bool:
        """人間が離脱した席(original_name)を引き継ぐAIを1体 spawn する。
        サーバが ?takeover=<original_name> を解釈し、進行中ゲームの該当席へ接続を渡す。
        custom_prompts があれば、その人の自作プロンプトで引き継ぐ。"""
        import subprocess
        from urllib.parse import quote

        if session.status != "running":
            return False
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        base_url = with_room(internal_url_for(session.size, session.language), session.room)
        ai_url = base_url + ("&" if "?" in base_url else "?") + "takeover=" + quote(original_name, safe="")
        cfg = self._build_agent_config(
            "takeover", 1, ai_url, session.language, custom_prompts, session.llm, session.talk_length
        )
        cfg_path = GENERATED_DIR / f"{session.id}-takeover-{secrets.token_hex(3)}.yml"
        with cfg_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        proc = subprocess.Popen(  # noqa: S603
            [AGENT_LLM_PYTHON, "src/main.py", "-c", str(cfg_path)],
            cwd=str(AGENT_LLM_DIR),
            env=self._child_env(session.llm),
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
            elif session.status in ("queued", "running") and session.mode in ("multi", "byo"):
                p = self.participant_of(session, token)
                if p is None:
                    return "left"
                remaining = [x for x in session.participants if x.token != token]
                if session.mode == "byo" and session.gate in ("hold", "released"):
                    # まだ始まっていない → 席を空けるだけ（接続は待合室の掃除で消える）
                    session.participants = remaining
                    session.ready.discard(p.team)
                    return "left"
                if not remaining and session.agent_slots == 0:
                    # 最後の人間が抜けた → 卓を終了する。
                    # 人間が誰も居ないのに takeover してAI-vs-AIで無駄に走らせない（LLMコスト防止）。
                    to_kill = session
                else:
                    # まだ人間が残っている → 抜けた席だけAIが引き継ぎ、卓は続行。
                    # その人の自作プロンプト(あれば)で引き継ぐ。
                    spawned = self._spawn_takeover_ai(session, p.team, p.agent_prompts)
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
        custom_prompts: dict | None = None,
        llm: dict[str, str] | None = None,
        talk_length: int = 0,
    ) -> dict[str, Any]:
        # provider が base.yml + prompts/<lang>.yml をマージした config を返す。
        # custom_prompts があればユーザ自作のリクエスト別プロンプトで上書きする。
        # それに接続/チーム/LLM を上書きして最終 config にする。
        cfg: dict[str, Any] = PROMPT_PROVIDER.config_for(language, custom_prompts or None)

        cfg.setdefault("web_socket", {})
        cfg["web_socket"]["url"] = internal_url
        cfg["web_socket"]["token"] = cfg["web_socket"].get("token")
        cfg["web_socket"]["auto_reconnect"] = False

        # 発言の長さは、プロンプトの最後に一文足して伝える。
        # 自作プロンプトを上書きしないよう、末尾に付け足すだけにする。
        if talk_length > 0:
            base = cfg.setdefault("prompt", {}).get("initialize", "")
            cfg["prompt"]["initialize"] = (
                base.rstrip()
                + f"\n\n1回の発言は{talk_length}文字以内にしてください。長くなりそうなときは要点だけを述べてください。"
            )

        cfg.setdefault("agent", {})
        cfg["agent"]["num"] = ai_count
        cfg["agent"]["team"] = team
        cfg["agent"]["kill_on_timeout"] = True

        # 卓ごとの指定があればそれを、無ければサーバ既定を使う（[[byo-key-fallback]]）。
        provider = (llm.get("provider") or LLM_PROVIDER) if llm else LLM_PROVIDER
        if llm:
            # 持ち込みキーでモデル名が空のときは、そのプロバイダ用の既定へ。
            # LLM_MODEL（サーバ既定）はプロバイダが同じときだけ流用できる。
            model = llm.get("model") or (
                LLM_MODEL if provider == LLM_PROVIDER else BYO_DEFAULT_MODELS.get(provider, "")
            )
        else:
            model = LLM_MODEL
        # base_url は vLLM のときだけ。持ち込みキーは商用APIなので付けない。
        base_url = OPENAI_BASE_URL if (provider == "vllm" and OPENAI_BASE_URL) else ""

        cfg.setdefault("llm", {})
        cfg["llm"]["type"] = provider  # openai|google|vllm（agent.py が解釈）

        if provider in ("openai", "vllm"):
            cfg.setdefault("openai", {})
            cfg["openai"]["model"] = model
            cfg["openai"].setdefault("temperature", 0.7)
            if base_url:
                cfg["openai"]["base_url"] = base_url
        elif provider == "google":
            cfg.setdefault("google", {})
            cfg["google"]["model"] = model
            cfg["google"].setdefault("temperature", 0.7)
        elif provider == "anthropic":
            cfg.setdefault("anthropic", {})
            cfg["anthropic"]["model"] = model
            cfg["anthropic"].setdefault("temperature", 0.7)
        elif provider == "ollama":
            cfg.setdefault("ollama", {})
            cfg["ollama"]["model"] = model
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
                # 待合室のまま放置（誰も準備完了/開始を押さない、または画面が閉じられて誰もポーリングしない）
                if session.gate == "hold" and session.started_at and (
                    (now - session.started_at) > GATE_WAIT_TTL
                    or (now - session.alive_seen()) > WAITING_ROOM_TTL
                ):
                    await game_control(session, "drop")
                    session.gate = "off"
                    self._terminate_process(session)
                    session.status = "error"
                    session.error = "waiting room expired (nobody pressed ready/start)"
                    session.finished_at = now
                    self._cleanup_config(session)
                    continue
                # 上限時間の起点は「実際にゲームが始まった時刻」（待合室の時間は数えない）
                clock_start = session.released_at or session.started_at
                if session.process is None:
                    # 外部接続のみの卓（サンプルAIなし）はプロセスを持たないため、
                    # 時間切れ(MAX_SESSION_SECONDS)でのみスロットを解放する。
                    if clock_start and (now - clock_start) > MAX_SESSION_SECONDS:
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
                elif clock_start and (now - clock_start) > MAX_SESSION_SECONDS:
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
        self._drop_gate(session)
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
    last_publish = 0.0
    while True:
        await lobby._reap()      # noqa: SLF001
        await lobby._schedule()  # noqa: SLF001
        if time.time() - last_publish >= LOG_PUBLISH_INTERVAL:
            last_publish = time.time()
            try:
                await lobby.publish_logs()
            except Exception as ex:  # noqa: BLE001  公開が失敗しても卓の運転は止めない
                print(f"[publish] error: {ex}", flush=True)
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


class LlmRequest(BaseModel):
    """遊ぶ人が持ち込む LLM の指定。省略するとサーバ既定を使う。

    キーはこの卓が生きているあいだメモリに置くだけで、保存もログ出力もしない。
    """

    provider: str = ""   # openai | google
    model: str = ""      # 省略時はサーバ既定のモデル名
    api_key: str = ""


class JoinRequest(BaseModel):
    size: int = 5  # 村の人数（5 or 9）
    language: str = DEFAULT_LANGUAGE  # ゲーム言語（AIの発話/プロンプト言語）
    llm: LlmRequest | None = None  # 持ち込みキー（任意）


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
    gate: str = "off"            # 待合室ゲートの状態（off|hold|released|started）


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
        "running_gpu": lobby.running_count(True),
        "running_external": lobby.running_count(False),
        "max_concurrent_external": MAX_CONCURRENT_EXTERNAL,
        # 試合ログの公開ページ（空なら公開していない）
        "log_public_url": LOG_PUBLIC_URL if LOG_PUBLISH_DEST else "",
        # 接続ガイド用: 外部エージェントが繋ぐ公開 WebSocket（5人村 / 9人村）。実際は ?room= が付く
        "public_ws_url": GAME_WS_PUBLIC_URL,
        "public_ws_url_9": GAME_WS_PUBLIC_URL_9,
        "provider": LLM_PROVIDER,
        "model": LLM_MODEL,
        # サーバ側の既定だけで遊べるか。false なら画面がキーの入力欄を出す。
        "server_llm_ready": await server_llm_ready(),
        # 持ち込みキーで選べるプロバイダ
        "byo_providers": list(BYO_PROVIDERS),
        # モデル名を省略したときにプロバイダ別で使われる既定
        "byo_defaults": dict(BYO_DEFAULT_MODELS),
    }


class ModelsRequest(BaseModel):
    """持ち込みキーで使えるモデル一覧の問い合わせ。キーの有効性チェックを兼ねる。"""

    provider: str = ""
    api_key: str = ""


def _looks_chatty_openai(model_id: str) -> bool:
    """OpenAI の一覧は埋め込みや音声も混ざるので、会話に使える見込みのものだけ通す。"""
    mid = model_id.lower()
    if not mid.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return False
    blocked = (
        "embedding", "whisper", "tts", "dall-e", "audio", "realtime",
        "moderation", "transcribe", "image", "search", "instruct", "codex",
    )
    return not any(b in mid for b in blocked)


async def _fetch_models(provider: str, api_key: str) -> list[str]:
    """そのキーで実際に使えるモデルをプロバイダに聞く。

    3社とも一覧APIにはキーが必要＝ここを通れば「キーが生きている」ことも分かる。
    キーはヘッダにだけ載せ、URLやログには出さない。
    """
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        if provider == "openai":
            r = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            ids = [str(m.get("id", "")) for m in r.json().get("data", [])]
            return sorted(i for i in ids if _looks_chatty_openai(i))
        if provider == "google":
            r = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"pageSize": 1000},
                headers={"x-goog-api-key": api_key},
            )
            r.raise_for_status()
            out: list[str] = []
            for m in r.json().get("models", []):
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue
                out.append(str(m.get("name", "")).removeprefix("models/"))
            return sorted(out)
        if provider == "anthropic":
            r = await client.get(
                "https://api.anthropic.com/v1/models",
                params={"limit": 100},
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
            r.raise_for_status()
            return [str(m.get("id", "")) for m in r.json().get("data", [])]
    return []


@app.post("/api/models")
async def list_models(req: ModelsRequest) -> dict[str, Any]:
    """企業を選んでキーを入れた時点で呼ばれ、そのキーで選べるモデルを返す。

    おすすめ（価格つき・手書き）は、取れた一覧と突き合わせて
    実在するものだけ返す。キーは保存もログ出力もしない。
    """
    provider = req.provider.strip().lower()
    api_key = req.api_key.strip()
    if provider not in BYO_PROVIDERS:
        return {"ok": False, "error": "対応していないプロバイダです"}
    if not api_key or len(api_key) > 512:
        return {"ok": False, "error": "キーを入力してください"}

    import httpx

    try:
        models = await _fetch_models(provider, api_key)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        # Google は無効キーを 400 INVALID_ARGUMENT で返す（一覧GETで400になる他の理由はまず無い）
        if code in (401, 403) or (provider == "google" and code == 400):
            return {"ok": False, "error": "キーが無効です（認証に失敗しました）"}
        if code == 429:
            return {"ok": False, "error": "レート制限に当たりました。少し待ってやり直してください"}
        return {"ok": False, "error": f"プロバイダ側のエラーです（HTTP {code}）"}
    except Exception:  # noqa: BLE001  ネットワーク断・タイムアウトなど
        return {"ok": False, "error": "プロバイダに接続できませんでした"}

    available = set(models)
    recommended = [dict(m) for m in RECOMMENDED_MODELS.get(provider, []) if m["id"] in available]
    return {
        "ok": True,
        "models": models,
        "recommended": recommended,
        "default_model": BYO_DEFAULT_MODELS.get(provider, ""),
        "price_asof": PRICE_ASOF,
        "pricing_page": PRICING_PAGES.get(provider, ""),
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
    llm = sanitize_llm(req.llm.model_dump() if req and req.llm else None)
    if not llm and not await server_llm_ready():
        raise HTTPException(
            status_code=503,
            detail="いまは自分の API キーが要ります",
        )
    session = await lobby.join(size=size, language=language, llm=llm)
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
    token: str = ""          # 作成者（ホスト）の匿名トークン。開始ボタンの認可に使う
    llm: LlmRequest | None = None    # 埋めのサンプルAIに使う持ち込みキー（任意）
    talk_length: int = 0
    publish_logs: bool = True        # 試合ログを公開ページへ送るか（既定: 公開）


class ByoResponse(BaseModel):
    session_id: str
    team: str                # 持ち込みエージェントが使うチーム名
    ws_url: str              # 接続先 WebSocket URL
    ai_count: int            # 残りを埋めるサンプルAI数
    agent_slots: int         # 持ち込みエージェント枠
    host_token: str = ""     # 開始ボタン用（作成者にだけ返す）
    gate: str = "off"
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

    llm = sanitize_llm(req.llm.model_dump() if req.llm else None)
    # サンプルAIが1体でも要るなら、サーバ側LLMか持ち込みキーが必要
    if size - external > 0 and not llm and not await server_llm_ready():
        raise HTTPException(status_code=503, detail="いまは自分の API キーが要ります")
    token = req.token or secrets.token_urlsafe(9)
    session = await lobby.create_session(external_slots=external, size=size, language=req.language, llm=llm)
    session.mode = "byo"
    session.human_slots = human
    session.host_token = token
    session.talk_length = max(0, min(int(req.talk_length or 0), 2000))
    session.publish_logs = bool(req.publish_logs)
    await lobby._schedule()  # noqa: SLF001

    # 持ち込みエージェントも人間も ?room=<session.room> を付けて同一卓(room)に入る。
    pub_room = with_room(public_url_for(session.size, session.language), session.room)
    human_url = None
    if human:
        # 既存 /demo の直接接続モード(?url=&team=)を再利用して人間が同卓に入る。
        # url には room 付き URL を、team には人間と分かるチーム名を渡す。
        from urllib.parse import quote
        # lang も渡し、人間のUIの初期表示言語を卓のゲーム言語に合わせる（UI言語は後から変更可）。
        # sid を渡すと /demo が待合室（着席状況・準備完了ボタン）を出せる。
        human_url = (
            f"/demo?url={quote(pub_room, safe='')}"
            f"&team={quote(session.human_team, safe='')}"
            f"&lang={quote(session.language, safe='')}"
            f"&sid={quote(session.id, safe='')}"
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
        host_token=token,
        gate=session.gate,
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
        gate=session.gate,
    )


# --- 待合室ゲート API ---
class ReadyRequest(BaseModel):
    team: str = ""       # 準備完了を押した人間の team 名（接続に使っているもの）
    ready: bool = True


class StartRequest(BaseModel):
    token: str = ""      # ホストのトークン（卓の作成者）


@app.get("/api/session/{session_id}/gate")
async def session_gate(session_id: str, token: str = "") -> dict[str, Any]:
    """待合室の状態。画面が1.5秒おきに読む（ハートビートも兼ねる）。"""
    session = lobby.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    session.last_seen = time.time()
    view = await lobby.gate_view(session, token)
    view["status"] = session.status
    view["error"] = session.error
    return view


@app.post("/api/session/{session_id}/ready")
async def session_ready(session_id: str, req: ReadyRequest) -> dict[str, Any]:
    """人間が「準備完了」を押した/外した。全員そろって席も揃えば自動で開始する。"""
    session = lobby.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        await lobby.set_ready(session, req.team, req.ready)
    except KeyError:
        raise HTTPException(status_code=400, detail="not a human seat")
    return await lobby.gate_view(session)


@app.post("/api/session/{session_id}/start")
async def session_start(session_id: str, req: StartRequest) -> dict[str, Any]:
    """ホストの開始ボタン。観戦（人間なし）はこれでしか始まらない。人間がいる卓は全員準備完了が条件。"""
    session = lobby.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if not req.token or req.token != session.host_token:
        raise HTTPException(status_code=403, detail="only the host can start")
    if not lobby.all_ready(session):
        raise HTTPException(status_code=409, detail="not everyone is ready")
    await lobby.release(session)
    return await lobby.gate_view(session, req.token)


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
    agent_prompts: dict[str, str] = {}  # 自作AIのリクエスト別プロンプト（任意）。AI席に注入。
    my_ai_count: int = -1            # AI席のうち自作AIにする数（観戦の構成用。-1=自作適用なら全AI）
    llm: LlmRequest | None = None    # 持ち込みキー（任意）
    talk_length: int = 0             # 1回の発言の目安（文字）。0 で指定なし
    publish_logs: bool = True        # 試合ログを公開ページへ送るか（既定: 公開）


class JoinRoomRequest(BaseModel):
    token: str = ""                  # 端末の匿名トークン
    agent_prompts: dict[str, str] = {}  # 離脱時の takeover に使う自作プロンプト（任意）


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
    gate: str = "off"                # 待合室ゲートの状態（off|hold|released|started）
    agent_slots: int = 0             # 持ち込み卓: 外部エージェントの席数
    agents: list[dict[str, Any]] = []  # 持ち込み卓: 取られた枠 [{name, count}]
    # 観戦のとき、ゲームサーバの実況ファイルを見分けるためのチーム名。
    # ファイル名が "<時刻>_<team>_<team>_..." なので、これで自分の卓を特定できる。
    ai_team: str = ""


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
        ai_team=session.team,
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
        gate=session.gate,
        agent_slots=session.agent_slots,
        agents=[{"name": e.name, "count": e.count} for e in session.agent_entries],
    )


class PromptPreviewRequest(BaseModel):
    text: str = ""                   # 1リクエストぶんのプロンプト本文（トークン入り）


@app.post("/api/prompt/preview")
async def prompt_preview(req: PromptPreviewRequest) -> dict[str, Any]:
    """自作AIエディタ用: 変数トークンをサンプル値に解決したプレビューを返す。"""
    return {"preview": preview_prompt(req.text), "max_chars": MAX_PROMPT_CHARS}


@app.get("/api/prompt/catalog")
async def prompt_catalog() -> dict[str, Any]:
    """自作AIエディタ用: 変数カタログ（変数一覧・リクエスト別の使える変数・リクエスト一覧）。"""
    return variable_catalog()


@app.get("/api/prompt/defaults")
async def prompt_defaults(language: str = DEFAULT_LANGUAGE) -> dict[str, Any]:
    """自作AIエディタ用: 指定言語の既定プロンプトをトークン文（Jinja非表示）で返す。"""
    return {"prompts": PROMPT_PROVIDER.defaults_as_tokens(language), "requests": REQUESTS}


@app.post("/api/rooms", response_model=RoomResponse)
async def create_room(req: CreateRoomRequest) -> RoomResponse:
    token = req.token or secrets.token_urlsafe(9)
    llm = sanitize_llm(req.llm.model_dump() if req.llm else None)
    if not llm and not await server_llm_ready():
        raise HTTPException(
            status_code=503,
            detail="いまは自分の API キーが要ります",
        )
    session = await lobby.create_room(
        mode=req.mode, size=req.size, language=req.language, human_slots=req.human_slots,
        token=token, agent_prompts=req.agent_prompts, my_ai_count=req.my_ai_count,
        llm=llm, talk_length=req.talk_length,
    )
    session.publish_logs = bool(req.publish_logs)
    if session.mode in ("solo", "spectate"):
        await lobby._schedule()  # noqa: SLF001  ソロ・観戦は即開始
    return _room_response(session, token)


class CreateByoRoomRequest(BaseModel):
    size: int = 5
    agent_slots: int = 1             # 外部エージェントの席数
    human_slots: int = 0             # 人間の席数（/demo の合言葉で入る）
    language: str = DEFAULT_LANGUAGE
    token: str = ""                  # 作成者（ホスト）の端末トークン
    llm: LlmRequest | None = None    # 埋めのサンプルAIに使う持ち込みキー（任意）
    talk_length: int = 0
    publish_logs: bool = True


@app.post("/api/rooms/byo", response_model=RoomResponse)
async def create_byo_room(req: CreateByoRoomRequest) -> RoomResponse:
    """持ち込み卓を作る（外部エージェント N ＋ 人間 M ＋ 残りサンプルAI）。合言葉を返す。"""
    token = req.token or secrets.token_urlsafe(9)
    llm = sanitize_llm(req.llm.model_dump() if req.llm else None)
    size = req.size if req.size in VALID_SIZES else 5
    ai_count = size - max(0, req.agent_slots) - max(0, req.human_slots)
    if ai_count > 0 and not llm and not await server_llm_ready():
        raise HTTPException(status_code=503, detail="いまは自分の API キーが要ります（サンプルAIを使わないなら不要）")
    try:
        session = await lobby.create_byo_room(
            size=size, agent_slots=req.agent_slots, human_slots=req.human_slots, language=req.language,
            token=token, llm=llm, talk_length=req.talk_length, publish_logs=req.publish_logs,
        )
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    await lobby._schedule()  # noqa: SLF001
    return _room_response(session, token)


class ClaimAgentRequest(BaseModel):
    name: str                        # チーム名（末尾数字なし）
    count: int = 1                   # この名前で繋ぐ体数
    token: str = ""


@app.post("/api/rooms/{code}/agent")
async def claim_agent(code: str, req: ClaimAgentRequest) -> dict[str, Any]:
    """持ち込み卓の外部エージェント枠を取る。接続 URL と使う名前を返す。"""
    try:
        session, entry = await lobby.claim_agent_slot(code.upper(), req.name.strip(), req.count, req.token)
    except KeyError:
        raise HTTPException(status_code=404, detail="room not found")
    except ValueError as ex:
        raise HTTPException(status_code=409, detail=str(ex))
    return {
        "room_id": session.id,
        "code": session.code,
        "name": entry.name,
        "count": entry.count,
        "ws_url": with_room(public_url_for(session.size, session.language), session.room),
        "size": session.size,
        "language": session.language,
        "remaining": session.agent_slots - lobby.agent_slots_taken(session),
    }


@app.get("/api/rooms/{code}/gate")
async def room_gate(code: str, token: str = "") -> dict[str, Any]:
    """合言葉で待合室の状態を見る（/byo のホスト画面・枠を取った人の画面用）。"""
    session = lobby.room_by_code(code.upper())
    if session is None:
        raise HTTPException(status_code=404, detail="room not found")
    session.last_seen = time.time()
    view = await lobby.gate_view(session, token)
    view["room_id"] = session.id
    view["status"] = session.status
    view["error"] = session.error
    view["ws_url"] = with_room(public_url_for(session.size, session.language), session.room)
    return view


@app.post("/api/rooms/{code}/join", response_model=RoomResponse)
async def join_room(code: str, req: JoinRoomRequest) -> RoomResponse:
    token = req.token or secrets.token_urlsafe(9)
    try:
        session, _ = await lobby.join_room(code, token, req.agent_prompts)
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
