# configs/ — デモ用設定

| file | 用途 |
|---|---|
| `server.yml` | ゲームサーバ設定。`room_match:true`（?room=卓IDで卓構成）, `agent_count:5`, ターンベース, whisper無効, TTS無効, host `0.0.0.0`。 |
| `agent.yml`  | AIエージェント設定テンプレ（手動コア検証用）。`team=test`, `num=4`, `llm.type=openai`。 |

## マイルストン2（コア検証）の手動起動手順 ※起動は運営（人間）が実施

土台の証明：パッチ無しの server に AI4体＋人間1枠が自動マッチして開始するか確認する。

```bash
# 0) APIキーを agent-llm に設定（OpenAI例）
#    repos/aiwolf-nlp-agent-llm/config/.env を作成し OPENAI_API_KEY=sk-... を記入
cp repos/aiwolf-nlp-agent-llm/config/.env.example repos/aiwolf-nlp-agent-llm/config/.env
# 上を編集して OPENAI_API_KEY を入れる

# 1) ゲームサーバ起動（Go 1.24）
cd repos/aiwolf-nlp-server
go run . -c ../../configs/server.yml
#   -> 0.0.0.0:8080 で待受。/ws が WebSocket エンドポイント。

# 2) AI4体を起動（別ターミナル, Python 3.11+, uv 推奨）
cd repos/aiwolf-nlp-agent-llm
uv sync               # 初回のみ依存解決
uv run src/main.py -c ../../configs/agent.yml
#   -> test1..test4 の4体が ws://127.0.0.1:8080/ws に接続

# 3) 人間1枠をビューア /agent で接続（5枠目）
#    会場ビルド or dev:  cd repos/aiwolf-nlp-viewer && pnpm install && pnpm dev
#    ブラウザで /agent を開き、接続URL=ws://localhost:8080/ws、チーム名=test を入力して接続。
#    （URL直指定なら /agent?url=ws://localhost:8080/ws でも可。チーム名は設定モーダルで test に）
```

### 成立条件のメモ
- サーバは接続名の末尾数字を除去して team 名を抽出する（`test1`→`test`）。
  AI4体（test1..test4）＋人間（team名 `test`）= 全員 team `test` の5接続 → `agent_count:5` 到達で自動開始。
- 人間のチーム名は末尾に数字を付けない（`test` のまま）こと。`test5` でも team は `test` になり可。
- M6 のロビーが発行するユニーク session team は、末尾数字除去で他卓と衝突しないよう
  「数字で終わらない一意プレフィックス」を持たせる（例 `s-user01-x9fk`）。

> 注意: これは手動検証用。`agent.yml` の `team`/`num` は本番では lobby が動的生成する。
