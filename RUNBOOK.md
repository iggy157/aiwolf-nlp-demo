# 運用メモ（起動・停止の全部）

```
        いつでも: https://133.167.32.100/game/demo   ゲーム（開いていなければ「準備中」が出る）
        いつでも: https://133.167.32.100:8013/demo   予備（ポート付き）（下の「サイトを起動」で開く）
```

登場するマシンは3つ。**あなたが操作するのは freccia と thalys だけ**。

| | 役割 | ふだん |
|---|---|---|
| 133.167.32.100 | 受付ページと中継 | **常時稼働。触らない** |
| freccia | ゲーム本体（サーバ・ロビー・画面） | 遊ぶときだけ起こす |
| thalys | vLLM（GPU） | 使うときだけ起こす |

---

## 1. サイトを起動する

普段は **systemd（ユーザ単位）が面倒を見る**。freccia が再起動しても自動で開店する（linger 有効化済み）。

```bash
systemctl --user status aiwolf-demo     # 状態（CGroup にサーバ×2・ロビー・Caddy・トンネルが並ぶ）
systemctl --user restart aiwolf-demo    # 設定やビルドを変えたあとの入れ替え
journalctl --user -u aiwolf-demo -n 50  # 起動時の出力
```

中身は `scripts/open.sh`（起動）と `scripts/close.sh`（停止）。手で動かすなら:

```bash
cd ~/pri/pri_site/aiwolf-nlp-demo
./scripts/open.sh
```

- 公開URL: https://133.167.32.100/game/demo
- SSH 逆トンネルは `scripts/tunnel-loop.sh` が張り続ける（切れたら 5 秒後に張り直す。`native/run/logs/tunnel.log`）
- ログ: `native/run/logs/{caddy,lobby,game5,game9,tunnel}.log`

## 2. サイトを停止する

```bash
systemctl --user stop aiwolf-demo       # 手で open.sh した場合は ./scripts/close.sh
```

トンネルも落ちるので公開URLは「いまは開いていません」になる。

## 3. vLLM で LLM を起動する（thalys）

**先に予約を取ること。** 使う GPU 番号は予約に合わせる。

```bash
ssh thalys
MODEL="google/gemma-4-31B-it" NAME="gemma-4-31b" GPUS="0,1" \
  setsid nohup ~/vllm-server/start.sh > ~/vllm-server/logs/vllm.log 2>&1 < /dev/null &
```

立ち上がりまで **31B で4〜5分**（重みの読み込み）。確認:

```bash
curl -s http://192.168.1.13:8000/v1/models
tail -f ~/vllm-server/logs/vllm.log      # 詰まったときはここ
```

そのうえで freccia 側の `native/env` を合わせて、サイトを起動し直す。

```
LLM_PROVIDER=vllm
LLM_MODEL=gemma-4-31b                       # ← NAME と同じ文字列
OPENAI_BASE_URL=http://192.168.1.13:8000/v1
```

## 4. vLLM を停止する（GPU を返す）

```bash
ssh thalys 'pkill -f vllm.entrypoints'
```

**サイトは止めなくていい。** vLLM が落ちると画面が自動で
「いまは自分のAPIキーが要ります」に切り替わり、各自のキーで遊べる状態になる。

---

## モデルを変える

`~/vllm-server/start.sh` は環境変数で上書きできる。

| 変数 | 既定 | |
|---|---|---|
| `MODEL` | `google/gemma-4-31B-it` | HuggingFace の名前 |
| `NAME` | | 画面に出る名前。`native/env` の `LLM_MODEL` と揃える |
| `GPUS` | `0` | `0,1` と書けば2枚（tensor-parallel） |
| `BIND` | `192.168.1.13` | LAN のみ。学内には出さない |
| `LEN` | `8192` | 最大トークン長 |

キャッシュ済みで**読めることを確認したモデル**（`~/.cache/huggingface`、ダウンロード不要）:

```
google/gemma-4-31B-it      59G  2枚  会話の質は良い。1発言に数秒
Qwen/Qwen3.5-9B            19G  1枚  速い。テンポ重視ならこれ
Qwen/Qwen3.8-27B           52G  2枚
mistralai/Mistral-Small-3.2-24B-Instruct-2506
ByteDance-Seed/Seed-OSS-36B-Instruct
LGAI-EXAONE/EXAONE-4.5-33B
llm-jp/llm-jp-4-8b-thinking
```

> `google/gemma-4-12B-it` は形式が `gemma4_unified` で、入っている transformers 5.8.1 が
> 対応していない。**使えない。**

---

## よく触る設定（freccia の `native/env`）

| | |
|---|---|
| `MAX_CONCURRENT_GAMES` | 同時に走らせる試合数。超えたぶんは順番待ちになる（既定 3） |
| `LLM_PROVIDER` / `LLM_MODEL` / `OPENAI_BASE_URL` | サーバ側が使う LLM |
| `START_VLLM` | 1 にすると open.sh が freccia の GPU で vLLM も起こす。thalys を使うなら 0 |
| `LOCAL_PORT` | Caddy が待つポート（既定 8088）。トンネルはここを公開する |
| `MAX_CONCURRENT_EXTERNAL` | GPU を使わない卓（外部エージェントのみ／持ち込みキー）の同時数（既定 20）。上とは別枠 |
| `CONTROL_KEY` | 待合室ゲートの共有鍵。空だとゲート無し（人数が揃った瞬間に始まる） |
| `LOG_PUBLISH_DEST` / `LOG_PUBLIC_URL` | 試合ログの公開先（scp 宛先）と公開ページのURL。DEST が空なら公開しない |
| `GATE_WAIT_TTL` | 待合室のまま誰も「準備完了/開始」を押さないとき卓を片付けるまでの秒数（既定 600） |


## 困ったとき

| 症状 | 見るところ |
|---|---|
| サイトが開かない | `native/run/logs/{caddy,lobby,game5,game9}.log` |
| AIが発言しない | vLLM が生きているか（`curl .../v1/models`）／`native/env` の `LLM_MODEL` が `NAME` と一致しているか |
| 観戦が真っ白 | 実況は数秒遅れる。それでも出ないなら `curl https://133.167.32.100/game/gs/realtime/games.json` |
| 公開URLが 503 | トンネルが落ちている。`./scripts/open.sh` をやり直す |
| vLLM が起動しない | `~/vllm-server/logs/vllm.log` の末尾。過去に踏んだのは ninja の PATH・バッチ長・フラグ名 |

古い実況ファイルが溜まったら:

```bash
rm -rf repos/aiwolf-nlp-server/log/realtime repos/aiwolf-nlp-server/log/realtime9
```

## 持ち込みAPIキー（BYO）とモデル選択（2026-09 追加）

遊ぶ人が自分のキーで遊ぶ機能まわりの現仕様。

- 対応プロバイダ: **OpenAI / Gemini / Claude(Anthropic)**
- 画面の流れ: 企業を選ぶ → キーを入れて「モデルを選ぶ」→ ロビーの `/api/models` が
  **そのキーで実際に使えるモデル一覧**をプロバイダに問い合わせて返す（キーの有効性チェック兼用）
  → おすすめ（価格つき）か全一覧から選ぶ。
- おすすめと価格は `lobby/main.py` の `RECOMMENDED_MODELS` に手書き（`PRICE_ASOF` 時点）。
  一覧と突き合わせるので、古くなっても存在しないモデルを勧めることはない（消えるだけ）。
- モデル名を空で持ち込まれたときの既定は `BYO_DEFAULT_MODELS`（プロバイダ別）。
  以前のように vLLM のモデル名（gemma-…）が商用APIへ流れて死ぬことはもう無い。
- **平文HTTPのページではキー入力を受け付けない**（画面側でブロック。localhost は例外）。
  つまり SSH 中継（https://133.167.32.100/game）のままでは BYO は使えない。使うには ↓

### HTTPS で公開する（TUNNEL=quick）

```bash
# native/env に1行
TUNNEL=quick
./scripts/open.sh
```

- cloudflared のクイックトンネルで `https://～.trycloudflare.com` が割り当たる。
  無料・アカウント不要・ドメイン不要。**URLは起動のたびに変わる**ので、表示されたURLをQRにする。
- このモードでは SSH 中継は張らない（固定の受付ページ https://133.167.32.100/game/demo は「準備中」のままになる）。
- 固定URLでHTTPSにしたくなったら、独自ドメイン（年1,000〜2,000円）を取り
  Cloudflare Tunnel の named tunnel か、公開サーバの Caddy に載せる（証明書は無料）。

### キーの扱い（設計）

- キーはロビーの Session のメモリと、AI子プロセスの環境変数にだけ載る。
  ログ・レスポンス・生成YAMLには出さない。`/api/models` でもヘッダにだけ載せて転送する。
- ブラウザ側は「このブラウザに覚えさせる」に同意した人だけ localStorage に保存。

## 待合室ゲート（2026-09-27 追加）

卓に人数が揃っても、**全員が「準備完了」を押す（人間のいない卓は「開始」を押す）まで最初のリクエスト
（INITIALIZE）は飛ばない**。ソロ・マルチ・観戦・持ち込み（/byo）で共通。

仕組みは3段:

1. ゲームサーバ（Go）に `/control/hold|release|drop|room?room=` を足した。`hold` 中の room は揃っても卓を
   立てない。`CONTROL_KEY`（`X-Control-Key` ヘッダ）が要り、Caddy は `/gs/control/*` を 403 で塞ぐ。
   待機中の切断は 10 秒おきの ping 書き込み失敗で掃除する（検出まで最大 20 秒ほど）。
2. ロビーは卓を作るとき先に `hold` してからサンプルAIを起動し、`/api/session/{id}/gate|ready|start` で
   状態・準備完了・開始を受ける。人間全員が ready かつ席が満席で自動 `release`。人間ゼロ（観戦、
   AIだけの持ち込み卓）はホストの `start` だけが解除する。
3. ビューアは `/demo` の待合室パネル（着席 n/size・名前・準備完了/開始ボタン）と `/byo` の開始ボタン。
   `/byo` の「人間として参加」リンクには `sid` が付き、それで /demo が待合室を出す。

外部エージェント（aiwolf-nlp-agent-llm 等）は `/byo` の「持ち込み卓」で入る（次節）。

確認手順（LLM 不要）: `scratchpad/gate_test.py` 相当 — `/api/byo` で卓を作り、5本の ws を `?room=` 付きで
繋いで NAME に答え、4 秒待って INITIALIZE が来ないこと → `start` 後に来ることを見る。

## 試合ログの公開（2026-09-27 追加）

- ゲームサーバは `log/game/*.log`（大会と同じ CSV）と `log/json/*.json` を書く。
- ロビーが 60 秒おきに `log/game` を見て、**末尾が `result` で勝者が NONE でない（＝完走した）未送信のファイル**を
  `scp` で `LOG_PUBLISH_DEST` の `log/`・`json/` へ 1 回だけ送る。繰り返しの rsync はしない。
- 公開ページ: https://133.167.32.100/aiwolf/2026/demo/log/ （VPS の nginx fancyindex。大会ログと同じ見た目）
- 卓を作る画面（/demo の各モード・/byo）に「対戦ログは公開されます（リンク）」と「この卓のログは公開しない」がある。
  外した卓のチーム名は `log/private-teams.txt` に残り、ファイル名に含まれるログは送らない。
- 送った／スキップしたファイル名は `log/published.txt`。もう一度送りたければその行を消す。
- 中断した試合（result が NONE）や結果が書かれないまま 6 時間たったものは送らない。

## 持ち込み卓（/byo）と接続ガイド（2026-09-27 追加）

`https://133.167.32.100/game/byo`。ページ上部に接続ガイド（接続先・手順・config 例・名前のきまり・上限）がある。

- **卓を作る**: 村の人数、外部エージェントの席数、人間の席数を決める → 残りはサンプルAI → **合言葉**が出る。
  サンプルAIが 1 体でも要る卓はサーバ側 LLM（GPU）か持ち込みキーが必要。外部＋人間だけなら LLM 不要。
- **枠を取る**: 合言葉と自分のエージェント名（末尾数字なし、卓内で一意、`you-`/`s-`/`demo` は予約）と体数 →
  接続 URL（`?room=` 付き）と名前が決まる。`POST /api/rooms/{code}/agent`。
- **人間**: `/demo` → マルチ → 合言葉（`/demo?code=XXXX` で入力欄が埋まる）。複数人可。
- 待合室（`GET /api/rooms/{code}/gate`）に外部エージェントの接続数・人間の準備完了が並ぶ。
  全員着席＋人間全員が準備完了で自動開始。人間ゼロの卓は作った人の「開始」。
- ゲームサーバは **同じ卓に同じ接続名が既に居たら切断**する（`{"error":"duplicate name in room: …"}` を返す）。
  卓が違えば同名でもよい（room で隔離）。
- 旧 `POST /api/byo`（ロビーがチーム名を振る方式）は互換のため残しているが、画面からは使わない。

確認手順（LLM 不要）: `scratchpad/byo_test.py` 相当 — 外部 3 席（alpha×2, beta×1）＋人間 2 席の卓を作り、
名前の決まり・重複・満席の拒否、同名接続の切断、人間 2 人の準備完了で INITIALIZE が 5 席に届くことを見る。

