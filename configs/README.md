# configs/ — デモ用設定

| file | 用途 |
|---|---|
| `server.yml` | ゲームサーバ設定。`room_match:true`（?room=卓IDで卓構成）, `agent_count:5`, ターンベース, whisper無効, TTS無効, host `0.0.0.0`。 |
| `agents/base.yml` | AIエージェント設定の言語非依存部分（接続/`num`/`team`/LLM/ログ）。 |
| `agents/prompts/<lang>.yml` | 言語ごとのプロンプトテンプレート（14言語）。`ja`/`en` は本物、他は英語骨組みのプレースホルダ（`translated: false`）。 |

> 旧 `configs/agent.yml` は `agents/base.yml` ＋ `agents/prompts/ja.yml` に分割した。
> lobby は `base.yml` ＋ `prompts/<lang>.yml` ＋ 実行時上書き(URL/team/num/LLM) をマージして各卓の config を生成する
> （`lobby/prompt_provider.py: FilePromptProvider`）。ゲーム言語は `/api/join` の `language` で選ぶ。

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
#    まず base.yml + prompts/<lang>.yml をマージした単一 config を生成（手動検証用）
python lobby/prompt_provider.py --language ja --out configs/.generated/agent.ja.yml
cd repos/aiwolf-nlp-agent-llm
uv sync               # 初回のみ依存解決
uv run src/main.py -c ../../configs/.generated/agent.ja.yml
#   -> test1..test4 の4体が ws://127.0.0.1:8080/ws に接続
#   別言語で試すなら --language en など（python lobby/prompt_provider.py --list で一覧）

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

> 注意: これは手動検証用。`base.yml` の `team`/`num` は本番では lobby が動的生成する。
