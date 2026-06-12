# lobby — デモ用ロビーbackend（FastAPI）

参加者の採番・一意セッションチーム発行・AIエージェント(agent-llm)の spawn・同時数キューを担う。
ゲームを叩くのはAIと人間（ビューア）で、lobby はオーケストレーションのみ。

## エンドポイント
| method | path | 役割 |
|---|---|---|
| POST | `/api/join` | 参加。採番・チーム発行し待機列へ。`{session_id, display_name, team, status, position, ws_url, ai_count}` |
| GET | `/api/session/{id}` | 状態取得（`queued`→`running`→`finished`/`error`、`position`）。フロントはこれをポーリング |
| POST | `/api/session/{id}/leave` | 離脱（スロット解放・AIプロセス停止） |
| GET | `/api/health` | 稼働状況（running/queued/max, provider/model） |

フロント(`/demo`)は `/api/join` → `position` を表示しつつポーリング → `running` で `ws_url`＋`team` に WebSocket 接続。

## 動作
- `MAX_CONCURRENT_GAMES` 卓まで同時進行。超過分は待機列で「あなたは N 番目」。
- バックグラウンドループ(1秒間隔)が「終了卓のスロット解放(reap)」→「空きがあれば待機列先頭を spawn(schedule)」。
- spawn: `configs/agent.yml` をテンプレに `web_socket.url`／`agent.team`／`agent.num`／`llm.*` を上書きした
  一時 config を `lobby/.generated/<id>.yml` に書き出し、`AGENT_LLM_PYTHON src/main.py -c <cfg>` を
  別プロセスグループで起動。APIキー類は子プロセスの環境変数で渡す。
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
| `AGENT_CONFIG_TEMPLATE` | `../configs/agent.yml` | プロンプト等を含む設定テンプレ |
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
