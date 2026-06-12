# aiwolf-nlp-demo ハンドオフ仕様書

> 学会デモ向け「スマホで誰でもAIと人狼対戦」システムの実装ハンドオフ。
> このファイルは、新しい作業ディレクトリ `aiwolf-nlp-demo/` の直下に置かれ、そこで起動した Claude Code セッションが
> **STEP 0（自動セットアップ）→ デモ用実装まで全部**を行うための設計コンテキスト一式です。
> 既存4リポジトリのコードを実際に読んで確定した事実に基づいています。

---

## ⚠️ 作業ルール（次セッションは必ず守ること）

- **git の commit / push / tag を一切しない。** clone とブランチ作成（`switch -c`）まではOK。変更のコミットはせず、レビューは作業ツリーの差分で行う。
- **このMDの場所＝作業ルート**：このファイルが置かれているディレクトリ（`aiwolf-nlp-demo/`）が作業ルート。**以降のパスはすべてこのディレクトリ基準の相対**。絶対パスをハードコードしない。
- **最初に §3 の STEP 0 を実行**して clone とディレクトリ整備を済ませてから実装に入る。
- **起動は人間（運営）側が行う**。次セッションは「ローカルで動く実装」まで作り切る。実起動（vLLM or APIキー投入 + `compose up`）はやらなくてよい。
- **自動で完結できない外部依存**（実装はできるが、最終的に人間が用意）：
  - LLM：**vLLM の実起動**、または**商用APIキー**（どちらでも動くよう両対応で実装する。§8）
  - `wss` 公開用の独自ドメイン・TLS証明書（会場依存）。ローカル検証は `ws://localhost` で可。
  - 会場ネットワークのインバウンド可否。
- 進め方：まず **§9 マイルストン1（コア検証）→ ローカルで1卓回る状態** を最優先。公開・モデル確定は最後。

---

## 0. 一行サマリ

QRを読んだ参加者が、ビューアの**プレイヤー視点UI（新ルート `/demo`）**で、**4体のLLMエージェント**と人狼対戦できる。LLMは **vLLM（ローカル）でも商用API（OpenAI/Google等）でも**動く。運営は **LLM準備（vLLM起動 or APIキー設定）+ `docker compose up` の初回のみ**、以降は参加者依存で各自が勝手にゲームを開始・進行する。

---

## 1. ゴールと運用イメージ（学会30分デモ）

- 会場でQRを掲示 → 参加者がスマホで読み込み → `/demo` ルートに入る
- 入室順に `user01, user02, …` を自動採番（表示名。マッチングキーは別途ユニークなチーム名）
- 参加者が「ゲーム開始」ボタンを押す → そのセッション専用の4体AIが自動spawn → 自動でゲーム開始
- **発話は1件ずつ流れてくる（LINE風）。** 誰のターンかが常に明確で、自分のターンのときだけ入力可能
- 運営の試合ごとの操作はゼロ。GPU/同時数はキューで制御

### 運営の起動手順（目標）
1. LLM準備：**vLLM起動**（GPU）**または** `.env` に**商用APIキー**を設定（GPU不要）
2. `docker compose up`（または両方束ねた `make demo`）

これ以降は無人。「無人」を成立させるには §7 の自動回収・キューを実装に含めること。

---

## 2. コンポーネント構成と依存

| repo | 言語 | 役割 | 今回パッチするか |
|---|---|---|---|
| `aiwolf-nlp-server` | Go | ゲーム本体（WebSocket、マッチング、ゲーム進行） | **する**（プレイヤー向け逐次push＋ターンマーカー） |
| `aiwolf-nlp-viewer` | SvelteKit（静的） | ビューア。`/agent` がプレイヤー視点プレイUI | **する**（既存 `/agent` は不変。**新ルート `/demo` を追加**） |
| `aiwolf-nlp-agent-llm` | Python | LLMサンプルエージェント | **する**（LLM接続を **vLLM/商用API 両対応**に＋小改修） |
| `aiwolf-nlp-common` | Python | プロトコル/パケット層 | **しない見込み**（PyPI `==0.7.0`。AIエージェントの受信プロトコルは不変） |

- 新規開発：**ロビーbackend**（採番・セッション発行・AI spawn・キュー）。agent-llm を同梱して subprocess で起動。
- `common` は AI側に新イベントを渡す設計に変えた場合のみ clone 対象に追加。

---

## 3. STEP 0：自動セットアップ（次セッションが最初に実行）

作業ルート（このMDのあるディレクトリ）で以下を実行。clone・ブランチ作成・スケルトン生成・`.env` 雛形までを冪等に行う（**commitはしない**）。

```bash
set -e
mkdir -p repos configs lobby

# --- server: tag v0.6.5 から feat/demo ブランチ ---
[ -d repos/aiwolf-nlp-server ] || git clone https://github.com/aiwolfdial/aiwolf-nlp-server.git repos/aiwolf-nlp-server
git -C repos/aiwolf-nlp-server checkout v0.6.5
git -C repos/aiwolf-nlp-server switch -c feat/demo 2>/dev/null || true

# --- viewer: ⚠️ main ではなく feature ブランチ ---
[ -d repos/aiwolf-nlp-viewer ] || git clone -b feat/freeform-archive-timestamp-view https://github.com/aiwolfdial/aiwolf-nlp-viewer.git repos/aiwolf-nlp-viewer
git -C repos/aiwolf-nlp-viewer switch -c feat/demo 2>/dev/null || true

# --- agent-llm: main から feat/demo ブランチ ---
[ -d repos/aiwolf-nlp-agent-llm ] || git clone https://github.com/aiwolfdial/aiwolf-nlp-agent-llm.git repos/aiwolf-nlp-agent-llm
git -C repos/aiwolf-nlp-agent-llm switch -c feat/demo 2>/dev/null || true
# common はクローンしない（PyPI 0.7.0 を agent-llm が取得）

# --- .env 雛形（未存在なら作成） ---
[ -f .env ] || cat > .env <<'ENV'
# ===== LLM 切替：vllm | openai | google のいずれか =====
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
# 商用API（LLM_PROVIDER=openai/google のとき）
OPENAI_API_KEY=
GOOGLE_API_KEY=
# vLLM（LLM_PROVIDER=vllm のとき）OpenAI互換エンドポイント。キーはダミー可
OPENAI_BASE_URL=http://localhost:8000/v1
VLLM_API_KEY=dummy
# ===== ネットワーク =====
PUBLIC_HOST=localhost
SECRET_KEY=changeme
ENV

echo "STEP 0 done."
```

### clone する正確な ref（再現性ピン留め）
| repo | ref | commit | 備考 |
|---|---|---|---|
| server | tag `v0.6.5`（branch develop） | `02fc379` | Go 1.24 |
| viewer | branch `feat/freeform-archive-timestamp-view` | `51c0806` | **tagなし・mainではない** |
| agent-llm | branch `main` | `a28f574` | tag v0.3.3 より先 |
| common | tag `v0.7.0`（PyPI取得） | `c347e37` | clone不要 |

### STEP 0 後のディレクトリ構成
```
aiwolf-nlp-demo/                 ← 作業ルート（このMDとClaude Code起動場所）
├── HANDOFF.md
├── .env
├── docker-compose.yml           # ★これから作る
├── Caddyfile                    # ★これから作る
├── repos/
│   ├── aiwolf-nlp-server/        # feat/demo
│   ├── aiwolf-nlp-viewer/        # feat/demo
│   └── aiwolf-nlp-agent-llm/     # feat/demo
├── lobby/                        # ★新規backend（FastAPI等）
└── configs/
    ├── server.yml               # self_match:true, agent_count:5, turn-based
    └── agent.yml                # LLM接続（env から生成）
```

---

## 4. なぜ成立するか（既存コードの根拠）

### 4-1. 自動マッチ＝同一チーム5接続で即開始
`repos/aiwolf-nlp-server/core/waiting_room.go` の `GetConnections()`：`self_match: true` のとき「**同一チーム名の接続が agent_count(=5) に達した瞬間にゲーム開始**」。中央ロビーも運営の開始操作も不要。
`core/server.go` の `handleConnections()`：接続のたびに待機部屋へ追加し即マッチ判定 → 成立で goroutine 開始。ゲームは `sync.Map` で並行管理。

### 4-2. セッション隔離＝ユニークなチーム名
待機部屋はチーム名でグルーピング。**1セッション = 使い捨てのユニークなチーム名**（例 `s-user01-x9f2`）にすれば、その人間＋その4体AIだけが同一卓にマッチし混線しない。1台のサーバで複数卓が同時進行可。表示名 `user01` は飾り、team名が隔離キー。

### 4-3. AI 4体は1プロセスで起動
`repos/aiwolf-nlp-agent-llm/src/main.py`：`agent.num` の数だけ `multiprocessing.Process` で接続。`num: 4` で1プロセス=4体。ロビーbackendがセッションのチーム名でこれを spawn。

### 4-4. トークは完全な逐次・1人ずつ
`repos/aiwolf-nlp-server/logic/communication_turn.go` の `runTurnBased()`：
```go
rand.Shuffle(agents)                 // 毎フェーズ順番シャッフル
for round := range MaxCount.PerDay {
    for _, agent := range agents {   // 1人ずつ
        text := requestToAgent(agent) // ← その1人に同期ブロッキング要求
    }
}
```
- 常に「発話中の1人」だけ存在 → **サーバは誰のターンかを常に正確に把握**
- 人間は自分のターンまで要求されない → **プロトコル上、割り込み送信は構造的に不可能**
- 順番は固定でない（シャッフル＋「Over」途中終了＋回数上限）→ **ターン表示はサーバのイベント駆動必須**

---

## 4-5. ルート分離方針（重要）

ビューアは**常時公開デプロイ（GitHub Pages）**。デモ機能を既存 `/agent` に足すと本番に混入するため、**専用の新ルート `/demo` として追加**する。

- 既存 `/agent`（手動接続の汎用プレイUI）は**一切触らない**。
- 新 `/demo`：**QRの直リンク先**。トップから導線を張らず、URLを知る人＝参加者だけが入る。中身は採番・開始ボタン・ターンUI・逐次表示。WebSocket処理は `agent-socket.ts` のロジックを**再利用**（コピー or 共通化）し、その上にオーケストレーション層を載せる。
- **配信元の推奨**：会場では `/demo` 含むビルドを**会場の Caddy から配信**（§6）。GitHub Pages の公開デプロイは無改変のまま、会場で自己完結。GitHub Pages にも置くなら導線なしの直リンク限定。

---

## 5. 発話単位ブロードキャスト＆ターン可視化（本システムの核心）

### 5-1. 神視点配信は使わない
`service/realtime_broadcaster.go` は1発話ごとにイベントを生成（`logic/communication_session.go::logTalk` が `Event:"トーク"`/`Message`/`BubbleIdx` を毎発話 Broadcast）。が、これは**神視点（囁き・役職・占い結果を含む）**で、`/realtime` が JSONL を約1秒ポーリング表示。**プレイヤーに見せると情報漏洩**するので、プレイヤー表示には使わない。

### 5-2. プレイヤー向け逐次push をサーバに追加（必須スコープ）
純粋なエージェントモード（`agent-socket.ts`、`/demo` でも同ロジック再利用）は、自分のターンが来たとき talk_history がまとめて届くだけ。LINE風ストリームを**プレイヤー視点のまま**出すには、サーバにプレイヤー向け逐次pushを足すのが**前提**。

**急所は1か所**＝ `runTurnBased` のループ内：
- `requestToAgent(agent)` の**直前** → 「ターン開始マーカー（今からagent Xの番）」を他プレイヤーへ push
- `appendTalk` の**直後** → 「agent X の発話」を他プレイヤーへ push

### 5-3. 実装が軽くなる理由
1. **既存パケット形式を流用**：`agent-socket.ts::processPacket` は `packet.talk_history` を append し、**`packet.request` が無ければアクションを促さない**。→「request無し・talk_history差分」を流せばほぼ無改修で逐次表示。ターンマーカーは新フィールド or 専用イベント種別を1つ追加。
2. **デフォルト5人設定は囁き無効**（`config/default_5.yml` の whisper `per_agent: 0`）→ **見せてよい発話＝全部公開トークだけ**。役職依存フィルタ不要。占い/護衛/襲撃結果は従来どおり `info` で本人にだけ届く。

### 5-4. UI状態モデル（誤送信防止）
`agent-socket.ts` は `deadline`（アクション期限カウントダウン）を保持。これを使う。

| 状態 | トリガー | UI |
|---|---|---|
| 他者のターン | ターン開始マーカー受信 | 入力欄＋送信を **disable**、「○○さんが入力中…」＋当人アバターをハイライト |
| **あなたのターン** | 自分への `TALK` リクエスト到着 | 入力欄を **enable**、「あなたの番です」＋残秒カウントダウン、自分のアバター強調 |
| 集計/夜 | VOTE/夜アクション・フェーズ遷移 | 入力ロック、「投票中」「夜になりました」等のシステム表示 |

原則：**入力欄は「自分の live な TALK/WHISPER リクエストが pending のときだけ enable」**。これで誤送信ゼロ。「マイクを持つのは常に1人」をUIに出す。

### 5-5. freeform は使わない
`runFreeform`（`talk.duration` 指定、`config/freeform_5.yml`）はターン境界が曖昧でターン可視化と衝突。**turn-based 固定**。

---

## 6. Docker Compose 構成

### 外すもの
- **vLLM はcompose外**：GPU都合＆運営前提。商用APIモード時はそもそも不要。
- **AIエージェントはサービスにしない**：セッションごとに spawn する動的プロセス。**agent-llm をロビーbackendイメージに同梱**し subprocess 起動。

### サービス（3つ）
| service | 中身 | 役割 |
|---|---|---|
| `caddy` | Caddy | TLS終端 + 静的ビューア配信（**`/demo` 含むビルド**） + `wss` リバプロ + リアルタイムJSONL配信 |
| `game-server` | Go（パッチ版） | ゲーム本体。`self_match:true`, agent_count:5, turn-based |
| `lobby` | 新規backend＋agent-llm同梱 | 採番・セッションteam発行・ボタンでAI spawn・キュー。`.env` のLLM設定を子へ渡す |

（TTS音声を出すなら `voicevox` 追加。プレイには不要なので初期は無し）

### ネットワーク注意
- ビューアは https 配信 → `ws://` 不可（mixed content）。**`wss://` 必須**（TLS終端、Caddyで数行）。ローカル検証は同一オリジン `ws://localhost` で可。
- 会場ネットワークのインバウンド可否・ポート制限を**事前確認**。

---

## 7. 「無人運転」を成立させる必須2機能
1. **ハング卓の自動回収**：タイムアウト/切断で固まったセッションを自動 kill＆解放（`agent.kill_on_timeout: true`、サーバ `timeout.action: 60s` と連動）。spawn したAIプロセスの確実な回収も含む。
2. **同時数キュー**：1度に走れる卓数は有限（vLLMならGPU、商用APIならレート/コスト）。超過分は「順番待ち（あなたは○番目）」で自動制御。

---

## 8. LLM接続：vLLM / 商用API の両対応（重要）

**モデル設定の置き場所＝agent-llm だけ**。LLMを叩くのはエージェントのみで、server / viewer は無関係。実体は `repos/aiwolf-nlp-agent-llm/config.yml` の **`llm.type`（プロバイダ）＋ `<provider>.model`（モデル名）**。vLLMの場合の `model` は「vLLM起動時に指定したモデル名」をそのまま書く（type=openai＋base_url）。

運用上の単一の真実は `.env`。**ロビーbackendがAIをspawnする際、`.env` から config.yml を生成して渡す**ので、運営はモデル変更＝`.env` の2行（`LLM_PROVIDER` / `LLM_MODEL`）を書き換えるだけ。`.env` の `LLM_PROVIDER` で切り替えられるよう実装する。

| `LLM_PROVIDER` | 使うもの | 設定 |
|---|---|---|
| `openai` | 商用 OpenAI API | `OPENAI_API_KEY`, `LLM_MODEL`(例 gpt-4o-mini)。GPU不要 |
| `google` | 商用 Gemini API | `GOOGLE_API_KEY`, `LLM_MODEL`(例 gemini-2.0-flash-lite)。GPU不要 |
| `vllm` | ローカル vLLM | `OPENAI_BASE_URL`(例 http://host:8000/v1), `VLLM_API_KEY`=dummy, `LLM_MODEL`=起動モデル名 |

実装メモ：
- agent-llm の `llm.type` は既に `openai/google/ollama` をサポート。ただし **`openai` 型に `base_url` を渡す口が無い**ので、`openai` 型に**任意の `base_url` を渡せるよう小改修**する。こうすると「商用OpenAI（base_url無し）」と「vLLM（base_url有り）」が同じ `openai` 型で env 切替できて最小実装になる。
- vLLM は OpenAI 互換 `/v1/chat/completions` を出すので、上記でそのまま接続可。
- モデルは商用なら gpt-4o-mini / gemini-flash 系、vLLMなら 7〜8B級（Qwen2.5-7B / Llama-3.1-8B 等）＋出力短めが `action:60s` と相性良い。

---

## 9. 推奨実装順（マイルストン）

1. **STEP 0 実行**（§3）：clone＋ディレクトリ整備＋.env雛形。
2. **コア検証（最優先）**：パッチ無しで server を `self_match:true, agent_count:5` 起動 → agent-llm を `team=test, num=4`（まずは商用APIキーで）→ 手元ビューア `/agent` を `team=test` で接続 → **自動開始するか確認**。土台の証明。
3. **LLM両対応**：agent-llm の `openai` 型に base_url 追加 → `.env` で openai/google/vllm を切替できるように。
4. **新ルート `/demo` 雛形**：viewer に `/demo` 追加（`/agent` 不変）。`agent-socket` ロジック再利用で接続する最小UI。
5. **逐次push＋ターンマーカー**：server `runTurnBased` にプレイヤー向け push 追加 → `/demo` で逐次表示＆ターンUI＆入力ロック。
6. **ロビー/採番/ボタン/キュー**：lobby backend。セッションteam発行→AI spawn→順番待ち。`/demo` から叩く。
7. **compose化＋Caddy(TLS/wss)**：会場再現。`/demo` 含むビルドを会場配信。`make demo` で起動を束ねる。
8. **無人運転2機能**（§7）＋スマホ実機確認。

---

## 10. 参照ファイル早見表（すべて `repos/` 配下）
- 自動マッチ：`aiwolf-nlp-server/core/waiting_room.go`（`GetConnections`）、`core/server.go`（`handleConnections`）
- ターン進行：`aiwolf-nlp-server/logic/communication_turn.go`（`runTurnBased`）、`logic/communication.go`（`conductCommunication`）
- 逐次イベント生成元：`aiwolf-nlp-server/logic/communication_session.go`（`logTalk`）、`service/realtime_broadcaster.go`
- プレイヤープロトコル受信：`aiwolf-nlp-viewer/src/lib/utils/agent-socket.ts`（`processPacket` / `handlePacketRequest` / `send`）
- 神視点配信の消費（参考）：`aiwolf-nlp-viewer/src/lib/utils/realtime-socket.ts`
- プレイUI（**改造せず参考にする**）：`aiwolf-nlp-viewer/src/routes/agent/`（`+page.svelte`, `ActionBar.svelte`, `TalkColumn.svelte`, `ChatBubble.svelte`, `AgentColumn.svelte`）
- デモUI（**新規作成**）：`aiwolf-nlp-viewer/src/routes/demo/`（QR着地・採番・ボタン・ターンUI・逐次表示。`agent-socket.ts` 再利用）
- サーバ設定：`aiwolf-nlp-server/config/default_5.yml`（self_match / agent_count:5 / whisper:0 / timeout.action:60s）
- エージェント設定：`aiwolf-nlp-agent-llm/config/config.jp.yml.example`（web_socket.url / agent.num / agent.team / llm.type）
