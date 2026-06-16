# lobby — デモ用ロビーbackend（FastAPI）

参加者の採番・一意セッションチーム発行・AIエージェント(agent-llm)の spawn・同時数キューを担う。
ゲームを叩くのはAIと人間（ビューア）で、lobby はオーケストレーションのみ。

## エンドポイント
| method | path | 役割 |
|---|---|---|
| POST | `/api/join` | 人間1枠で参加（/demo用）。body `{size:int, language:str}`。残り(AGENT_TOTAL-1)をAIが埋める。`{session_id, team, ws_url, ai_count, language, ...}` |
| POST | `/api/byo` | 持ち込みエージェント卓を作成。body `{agents:int, human:bool, size:int, language:str}`。外部枠=agents+human、残りをAIが埋める。`{team, ws_url, ai_count, language, human_join_url, ...}` |
| GET | `/api/session/{id}` | 状態取得（`queued`→`running`→`finished`/`error`、`position`、`language`）。フロントはこれをポーリング |
| POST | `/api/session/{id}/leave` | 離脱（スロット解放・AIプロセス停止） |
| GET | `/api/languages` | 選択可能なゲーム言語コード一覧と既定値 `{languages, default}` |
| GET | `/api/health` | 稼働状況（running/queued/max, provider/model） |

**Room API（ソロ/マルチ）**: /demo の前段。匿名デバイストークン（`token`）で席を識別（アカウント不要）。

| method | path | 役割 |
|---|---|---|
| POST | `/api/rooms` | 卓作成。`{mode:solo|multi, size, language, human_slots, token}`。solo=即開始、multi=合言葉発行して待機。`{room_id, code, host_token, you, status, ...}` |
| POST | `/api/rooms/{code}/join` | マルチ卓に合言葉で参加。`{token}` → `{you, status, participants, ...}`（満員=409, 無い=404） |
| GET | `/api/rooms/{code}?token=` | 卓状態のポーリング。running時に `ws_url` と自分の `you.team` を返す |
| POST | `/api/rooms/{code}/start` | ホスト（`host_token`）が開始。空席を AI で補充して spawn |
| POST | `/api/rooms/{code}/leave` | 退出（ホスト=解散 / 待機中の参加者=離席） |

設計: 卓は `RoomStore`（今はインメモリ、Phase1 で DB 実装に差し替え）で保持。匿名トークンは将来アカウントに紐付け可能。
旧 `/api/join`・`/api/session/{id}` は後方互換で残置（ソロのポーリングに再利用）。

**ゲーム言語**: `language`（既定 `DEFAULT_LANGUAGE`）で卓ごとにAIの発話言語を決める。
`prompt_provider.py` が `configs/agents/prompts/<lang>.yml` を解決し、未対応言語は既定言語にフォールバックする。
UI言語（画面表示）は viewer 側 svelte-i18n の別管理で、`/api/byo` の `human_join_url` には `&lang=` が付き
人間UIの初期表示言語を卓のゲーム言語に合わせる（UI言語は後から変更可）。

**卓の構成（fill-to-5）**: 1卓は `AGENT_TOTAL`(=5) 名。`external_slots`(外部接続=人間+持ち込みエージェント)を
指定すると、残り `AGENT_TOTAL - external_slots` 体をサンプルAIが埋める。`external_slots=5` なら AI 0体（全持ち込み）。

フロント(`/demo`)は `/api/join` → `position` を表示しつつポーリング → `running` で `ws_url`＋`team` に WebSocket 接続。

## 動作
- `MAX_CONCURRENT_GAMES` 卓まで同時進行。超過分は待機列で「あなたは N 番目」。
- バックグラウンドループ(1秒間隔)が「終了卓のスロット解放(reap)」→「空きがあれば待機列先頭を spawn(schedule)」。
- spawn: `prompt_provider` が `configs/agents/base.yml` ＋ `prompts/<lang>.yml` をマージし、それに
  `web_socket.url`／`agent.team`／`agent.num`／`llm.*` を上書きした一時 config を `lobby/.generated/<id>.yml`
  に書き出し、`AGENT_LLM_PYTHON src/main.py -c <cfg>` を別プロセスグループで起動。APIキー類は子プロセスの環境変数で渡す。
- ゲーム終了で agent-llm プロセスが自然終了 → reap がスロットを解放。

## 環境変数（.env 由来。すべて任意・既定あり）
| 変数 | 既定 | 説明 |
|---|---|---|
| `MAX_CONCURRENT_GAMES` | `1` | 同時卓数（vLLMならGPU、商用APIならレート/コストで決める） |
| `AI_COUNT` | `4` | 1卓あたりのAI体数（人間1枠を除く） |
| `LLM_PROVIDER` | `openai` | `openai`\|`google`\|`vllm`（agent.py が解釈） |
| `LLM_MODEL` | `gpt-4o-mini` | モデル名 |
| `OPENAI_BASE_URL` | (空) | vLLM等のOpenAI互換エンドポイント |
| `OPENAI_API_KEY` / `GOOGLE_API_KEY` / `VLLM_API_KEY` | — | 子プロセスへ受け渡すAPIキー |
| `GAME_WS_INTERNAL_URL` | `ws://127.0.0.1:8080/ws` | AIが接続する内部URL（docker: `ws://game-server:8080/ws`） |
| `GAME_WS_PUBLIC_URL` | `ws://localhost:8080/ws` | 人間が接続する公開URL（本番: `wss://<host>/ws`） |
| `AGENT_LLM_DIR` | `../repos/aiwolf-nlp-agent-llm` | agent-llm の場所 |
| `AGENTS_DIR` | `../configs/agents` | `base.yml` ＋ `prompts/<lang>.yml` の置き場 |
| `DEFAULT_LANGUAGE` | `ja` | ゲーム言語の既定／未対応言語のフォールバック先 |
| `AGENT_LLM_PYTHON` | venv 自動検出→`python3` | agent-llm を起動する Python |

## ローカル起動（※起動は運営が実施）
```bash
cd lobby
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# .env を読み込ませる場合は環境変数 or python-dotenv で。最低限 OPENAI_API_KEY を設定。
export OPENAI_API_KEY=sk-...
export AGENT_LLM_PYTHON=../repos/aiwolf-nlp-agent-llm/.venv/bin/python   # uv 環境なら
uvicorn main:app --host 0.0.0.0 --port 8002
```
`/demo` からは `?lobby=http://localhost:8002` を付けて開くと、このロビーを使う
（本番は Caddy が同一オリジンの `/api/*` をロビーへ proxy するのでパラメータ不要）。
