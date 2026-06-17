<script lang="ts">
  // aiwolf-nlp-demo: QR着地のプレイヤー視点UI（新ルート /demo）
  // 既存 /agent は不変。WebSocketロジックは demo-socket.ts（agent-socket のコピー＋拡張）を再利用。
  //   - 採番/開始ボタン/キュー は lobby（M6）で前段に載せる。
  //   - 表示文字列はすべて svelte-i18n（demo 名前空間）。UI言語は右上の LanguageSwitcher で切替。
  //   - ゲーム言語（AIの発話言語）はスタート画面で選び、初期値は現在のUI言語に追従する。
  import { browser } from "$app/environment";
  import { base } from "$app/paths";
  import { page } from "$app/state";
  import { Status } from "$lib/constants/common";
  import LanguageSwitcher from "$lib/components/LanguageSwitcher.svelte";
  import { agentSettings } from "$lib/stores/agent-settings";
  import { language, normalizeLanguage } from "$lib/stores/language";
  import { MY_AGENT_MAX_CHARS, myAgent } from "$lib/stores/my-agent";
  import { characterList, localizedAvatar, localizedName, localizedPersonality } from "$lib/stores/profiles";
  import { DefaultProfileAvatars, Request } from "$lib/types/agent";
  import { demoSocketState, type FeedEntry } from "$lib/utils/demo-socket";
  import { onDestroy } from "svelte";
  import { _, locale } from "svelte-i18n";
  import "../../app.css";

  // ---- socket state（demo-socket の writable を購読）----
  let status = $state("disconnected");
  let agent = $state<string | null>(null);
  let role = $state<string | null>(null);
  let profile = $state<string | null>(null);
  let request = $state<Request | null>(null);
  let info = $state<ReturnType<() => any> | null>(null);
  let feed = $state<FeedEntry[]>([]);
  let finished = $state(false);
  let divineResults = $state<any[]>([]);
  let mediumResults = $state<any[]>([]);
  let currentTurnAgent = $state<string | null>(null);
  let deadline = $state<number | null>(null);
  let setting = $state<any>(null);

  let remain = $state<number | null>(null);
  let rafId: number | null = null;

  const unsub = demoSocketState.subscribe((s) => {
    status = s.status;
    agent = s.agent;
    role = s.role;
    profile = s.profile;
    request = s.request;
    info = s.info;
    feed = s.feed;
    finished = s.finished;
    divineResults = s.divineResults;
    mediumResults = s.mediumResults;
    currentTurnAgent = s.currentTurnAgent;
    setting = s.setting;
    // ゲーム開始時(役職判明)に1回だけ説明ポップアップを出す
    if (s.role && s.agent && !introAck) introOpen = true;
    if (s.finished) introOpen = false;
    if (s.deadline) {
      deadline = s.deadline.getTime();
    } else {
      deadline = null;
      remain = null;
    }
  });

  // 接続チーム名（人間と分かる識別名。ロビーが you-userNN を割り当てる）。
  // room_match により卓は room で分離され、各参加者は別チーム名のまま同卓に入る。
  let team = $state<string | null>(null);
  const unsubSettings = agentSettings.subscribe((v) => {
    team = v?.team ?? null;
  });

  // 役職・種別の表示（i18n: game.role / game.species を再利用）
  const roleName = (r: string | null | undefined) => (r ? $_(`game.role.${r}`) : "—");
  const speciesName = (s: string | null | undefined) =>
    s === "WEREWOLF" || s === "HUMAN" ? $_(`game.species.${s}`) : (s ?? "—");
  // キャラ名・プロフィールを現在のUI言語にローカライズ（サーバへ送る値は常に原名のまま）。
  const nameOf = (n: string | null | undefined) => localizedName(n, $locale);
  const personalityOf = (n: string | null | undefined, fallback: string | null = null) =>
    localizedPersonality(n, $locale, fallback);
  // 自分のプロフィール（性格文）をローカライズ。未登録ならサーバ送出の profile 文字列にフォールバック。
  const myPersonality = $derived(personalityOf(agent, profile));

  let infoOpen = $state(false); // プロフィール/役職プレビューの開閉
  let introOpen = $state(false); // ゲーム開始時の説明ポップアップ
  let introAck = $state(false); // 説明を確認済みか

  // 勝敗の推定（FINISH時の役職開示＋生存状況から）。表示文字列ではなく陣営キーで持つ。
  const winnerCamp = $derived.by(() => {
    if (!finished || !info?.role_map || !info?.status_map) return null;
    let aliveWolf = 0;
    for (const [name, st] of Object.entries(info.status_map as Record<string, string>)) {
      if (st !== "ALIVE") continue;
      if ((info.role_map as Record<string, string>)[name] === "WEREWOLF") aliveWolf++;
    }
    return aliveWolf === 0 ? "VILLAGER" : "WEREWOLF";
  });
  const winnerText = $derived(
    winnerCamp === "VILLAGER"
      ? $_("demo.result.villagerWin")
      : winnerCamp === "WEREWOLF"
        ? $_("demo.result.werewolfWin")
        : null,
  );
  const myCamp = $derived(role === "WEREWOLF" || role === "POSSESSED" ? "WEREWOLF" : "VILLAGER");
  const iWon = $derived(finished && winnerCamp !== null && winnerCamp === myCamp);
  const myResult = $derived.by(() => {
    if (!finished || !role || !winnerCamp) return null;
    return iWon ? $_("demo.result.youWin") : $_("demo.result.youLose");
  });

  // ---- 入力可否の判定 ----
  const SELECTION_REQUESTS = [
    Request.VOTE,
    Request.DIVINE,
    Request.GUARD,
    Request.ATTACK,
  ];
  // 求められているアクション名
  const ACTION_KEYS = ["TALK", "WHISPER", "VOTE", "DIVINE", "GUARD", "ATTACK"];
  const actionName = $derived(
    request && ACTION_KEYS.includes(request as string)
      ? $_(`demo.action.${request}`)
      : $_("demo.action.fallback"),
  );
  const actionHint = $derived(
    request === Request.TALK || request === Request.WHISPER
      ? $_("demo.hint.talk")
      : request === Request.VOTE
        ? $_("demo.hint.vote")
        : request === Request.DIVINE
          ? $_("demo.hint.divine")
          : request === Request.GUARD
            ? $_("demo.hint.guard")
            : request === Request.ATTACK
              ? $_("demo.hint.attack")
              : $_("demo.hint.fallback"),
  );

  // 一時停止（開始ポップアップ表示中も実質停止扱い）。
  // マルチプレイでは1人の一時停止が全員を止めてしまうため無効化する（isMulti のとき常に false）。
  let paused = $state(false);
  let isMulti = $state(false); // この卓がマルチプレイか（接続時に確定）
  let isSpectate = $state(false); // AI観戦モード（自分は操作せず自動パス）
  const effectivePaused = $derived(isMulti || isSpectate ? false : (paused || introOpen));

  // 自分の live なリクエストが pending（=deadline 有り）のときだけ送信可（誤送信防止：HANDOFF §5-4）
  const isMyTurn = $derived(deadline !== null && request !== null);
  const canAct = $derived(isMyTurn && !effectivePaused);
  const isSelection = $derived(
    isMyTurn && SELECTION_REQUESTS.includes(request as Request),
  );
  const isTalk = $derived(
    isMyTurn && (request === Request.TALK || request === Request.WHISPER),
  );

  // 状態バナーの文言
  const banner = $derived.by(() => {
    if (status !== "connected") {
      return status === "connecting" ? $_("demo.banner.connecting") : $_("demo.banner.disconnected");
    }
    if (isMyTurn && effectivePaused) {
      return $_("demo.banner.pausedYourTurn", { values: { action: actionName } });
    }
    if (isMyTurn) {
      return $_("demo.banner.yourTurn", { values: { action: actionName, hint: actionHint } });
    }
    if (currentTurnAgent && currentTurnAgent !== agent) {
      return $_("demo.banner.othersTurn", { values: { name: nameOf(currentTurnAgent) } });
    }
    // 自分のターンでも他者のターンでもない＝集計/夜など
    switch (request) {
      case Request.VOTE:
        return $_("demo.banner.voting");
      case Request.DIVINE:
      case Request.GUARD:
      case Request.ATTACK:
        return $_("demo.banner.nightAction");
      case Request.DAILY_INITIALIZE:
        return $_("demo.banner.morning");
      case Request.DAILY_FINISH:
        return $_("demo.banner.night");
      case Request.FINISH:
        return $_("demo.banner.finished");
      default:
        return info ? $_("demo.banner.inProgress") : $_("demo.banner.waitingStart");
    }
  });

  const aliveTargets = $derived.by(() => {
    if (!info?.status_map) return [] as string[];
    return Object.entries(info.status_map as Record<string, Status>)
      .filter(([k, v]) => v === Status.ALIVE && k !== agent)
      .map(([k]) => k);
  });

  let message = $state("");
  let streamEl = $state<HTMLElement | null>(null);

  // 開始ポップアップの「ゲームを開始する」: 確認した時点から議論を読み始められるよう先頭へ
  function startGame() {
    introAck = true;
    introOpen = false;
    requestAnimationFrame(() => streamEl?.scrollTo({ top: 0 }));
  }

  function avatarSrc(name: string): string {
    // 言語別サーバは現地名を送るので、まず現ロケールの 表示名→avatar で解決し、
    // 無ければ従来の原名(JP)→avatar マップにフォールバックする。
    const path =
      localizedAvatar(name, $locale) ??
      DefaultProfileAvatars[name as keyof typeof DefaultProfileAvatars];
    return path ? `${base}${path}` : "";
  }

  function handleSend() {
    if (!canAct) return;
    const text = message.trim();
    if (!text) return;
    demoSocketState.send(text);
    message = "";
  }

  function sendValue(v: string) {
    if (!canAct) return;
    demoSocketState.send(v);
    message = "";
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  // ---- カウントダウン ----
  function startCountdown() {
    stopCountdown();
    const tick = () => {
      if (deadline === null) return;
      remain = Math.max(0, deadline - Date.now());
      if (remain > 0) rafId = requestAnimationFrame(tick);
      else rafId = null;
    };
    rafId = requestAnimationFrame(tick);
  }
  function stopCountdown() {
    if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
  }

  // 一時停止中はカウントダウン表示を凍結。再開時は実 deadline から再計算。
  $effect(() => {
    if (deadline !== null && !effectivePaused) startCountdown();
    else stopCountdown();
  });

  // 一時停止をサーバ側にも反映（自分のターン中は応答待ちタイムアウトの計測を止める）。
  // effectivePaused は手動の一時停止 or 開始ポップアップ表示中に true。
  $effect(() => {
    if (effectivePaused) demoSocketState.pause();
    else demoSocketState.resume();
  });

  const remainSec = $derived(remain !== null ? Math.ceil(remain / 1000) : null);

  // ---- ロビー連携（採番・キュー・AI spawn は lobby backend が担う）----
  // lobbyPhase: idle=未開始 / joining=参加要求中 / queued=順番待ち / starting=卓準備中 / playing=接続済 / error
  let lobbyPhase = $state<"idle" | "joining" | "queued" | "starting" | "playing" | "error">("idle");
  let displayName = $state<string | null>(null);
  let queuePos = $state(0);
  let lobbyError = $state<string | null>(null);
  let sessionId: string | null = null;
  let lobbyBase = ""; // 同一オリジン（Caddy 経由）。?lobby= で上書き可。
  let directMode = $state(false); // ?url= 直接接続（手動検証用）

  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let villageSize = $state(5); // 村の人数（5 or 9）。最初のページで選択。

  // 任意の役職・キャラ指定（既定=おまかせ=ランダム。今と同じ挙動）。
  // 役職は村サイズに存在するものだけ。キャラは characterList の index（サーバの profiles 並びと一致）。
  let selectedRole = $state<string>(""); // ""=おまかせ
  let selectedCharacter = $state<number>(-1); // -1=おまかせ
  const roleChoices = $derived(
    villageSize === 9
      ? ["VILLAGER", "SEER", "BODYGUARD", "MEDIUM", "WEREWOLF", "POSSESSED"]
      : ["VILLAGER", "SEER", "WEREWOLF", "POSSESSED"],
  );
  // 村サイズを変えたら、その村に無い役職の選択はおまかせに戻す。
  $effect(() => {
    if (selectedRole && !roleChoices.includes(selectedRole)) selectedRole = "";
  });
  const characters = $derived(characterList($locale));
  const selectedCharacterAvatar = $derived(
    selectedCharacter >= 0 ? (characters[selectedCharacter]?.avatar ?? null) : null,
  );

  // ---- 自作AI（リクエスト別プロンプトエンジニアリング）----
  type Screen = "mode" | "solo" | "multiCreate" | "multiJoin" | "waiting" | "agent" | "spectate";
  type VarDef = { key: string; token: string; composite: boolean };
  type BranchVar = { key: string; type: "enum" | "number"; ops: string[]; values: string[] };
  type Catalog = { vars: VarDef[]; by_request: Record<string, string[]>; requests: string[]; branch_vars: BranchVar[] };
  // ブロックエディタ: 本文は「文章ブロック」と「条件分岐ブロック」の並び。条件分岐は children を持ち
  // **入れ子**にできる（条件の中にさらに条件/文章）。if/endif はUIに出さない。
  type Block =
    | { id: number; kind: "text"; value: string }
    | { id: number; kind: "cond"; v: string; op: string; value: string; children: Block[] };
  let editBlocks = $state<Record<string, Block[]>>({}); // request -> ブロック列
  let agentDefaults = $state<Record<string, string>>({}); // 既定プロンプト（トークン文）
  let agentCatalog = $state<Catalog>({ vars: [], by_request: {}, requests: [], branch_vars: [] });
  let selectedRequest = $state("initialize");
  let agentSaved = $state(false);
  let previewText = $state<string | null>(null);
  let useMyAgent = $state(false); // AI席/引き継ぎに自作プロンプトを使うか
  let blockSeq = 0; // ブロックID採番
  // 直近にフォーカスした文章ブロック（変数挿入の対象）。ブロックは $state なので直接書き換えれば反映。
  let activeEl: HTMLTextAreaElement | null = null;
  let activeBlock: Block | null = null;
  // ドラッグ並べ替え（同じ並びの中だけ）。dragList で「どの並びの」何番目かを区別する。
  let dragIndex = $state<number | null>(null);
  let dragList = $state<Block[] | null>(null);
  const OP_SYM: Record<string, string> = { "=": "＝", "!=": "≠", ">=": "≧", "<=": "≦", ">": "＞", "<": "＜" };
  // 保存トークン（{if:..} 本文 {endif}、入れ子可）を走査して木に戻すためのタグ正規表現。
  const TAG_RE = /\{if:([a-z_]+)(!=|>=|<=|=|>|<)([^}]*)\}|\{endif\}/g;
  const branchSpec = (k: string): BranchVar | undefined => agentCatalog.branch_vars?.find((b) => b.key === k);
  // 自作AIが作成済みか（どれか1リクエストでも保存されていれば）
  const hasCustomAgent = $derived(Object.values($myAgent.prompts ?? {}).some((t) => (t ?? "").trim().length > 0));
  // 変数は全リクエストで挿入可能（描画コンテキストは共通＝lobby が None セーフに解決。
  // 該当しない変数は実行時に空になるだけでエラーにはならない）。ピッカーは全変数を出しつつ、
  // 「このリクエストでよく使う」ものを先頭に寄せて目印を付ける。
  const relevantKeys = $derived(new Set(agentCatalog.by_request?.[selectedRequest] ?? []));
  const pickerVars = $derived(
    [...agentCatalog.vars]
      .map((v) => ({ ...v, relevant: relevantKeys.has(v.key) }))
      .sort((a, b) => Number(b.relevant) - Number(a.relevant)),
  );

  async function openAgentEditor() {
    screen = "agent";
    agentSaved = false;
    previewText = null;
    try {
      const [cat, def] = await Promise.all([
        fetch(`${lobbyBase}/api/prompt/catalog`).then((r) => r.json()),
        fetch(`${lobbyBase}/api/prompt/defaults?language=${encodeURIComponent(gameLanguage)}`).then((r) => r.json()),
      ]);
      agentCatalog = cat;
      agentDefaults = def.prompts ?? {};
    } catch {
      /* 取得失敗でもエディタは開ける */
    }
    const reqs = agentCatalog.requests.length ? agentCatalog.requests : Object.keys(agentDefaults);
    const saved = $myAgent;
    const buf: Record<string, Block[]> = {};
    for (const r of reqs) buf[r] = parseBlocks(saved.prompts?.[r] ?? agentDefaults[r] ?? "");
    editBlocks = buf;
    if (!reqs.includes(selectedRequest)) selectedRequest = reqs[0] ?? "initialize";
  }

  // ── ブロック ⇄ 保存トークン文字列 の相互変換 ──────────────────────
  // 文章ブロック=段落（空行で区切る単位）、条件分岐ブロックは {if:var op value} 本文 {endif}。
  // ブロック同士は空行（\n\n）で連結＝Notion風に「段落＝ブロック」。
  function serializeBlocks(blocks: Block[]): string {
    return (blocks ?? [])
      .map((b) =>
        b.kind === "text"
          ? b.value.trim()
          : `{if:${b.v}${b.op}${b.value}}\n${serializeBlocks(b.children)}\n{endif}`,
      )
      .filter((s) => s !== "")
      .join("\n\n");
  }
  // テキスト区間を段落（空行区切り）ごとに文章ブロックへ。段落内の単一改行はそのまま残す。
  function pushTextParas(out: Block[], seg: string) {
    for (const para of seg.split(/\n[ \t]*\n/)) {
      const v = para.replace(/^\n+|\n+$/g, "");
      if (v.trim() !== "") out.push({ id: ++blockSeq, kind: "text", value: v });
    }
  }
  // {if:..}/{endif} を順に走査し、スタックで入れ子の木に組み立てる（再帰の代わりにスタック）。
  function parseBlocks(text: string): Block[] {
    const root: Block[] = [];
    const stack: Block[][] = [root]; // 現在の挿入先 = stack の末尾
    const cur = () => stack[stack.length - 1];
    let last = 0;
    let m: RegExpExecArray | null;
    TAG_RE.lastIndex = 0;
    while ((m = TAG_RE.exec(text)) !== null) {
      pushTextParas(cur(), text.slice(last, m.index));
      last = m.index + m[0].length;
      if (m[1]) {
        // {if:var op value} → 条件分岐ブロックを開いて children を新しい挿入先に
        const cond: Block = { id: ++blockSeq, kind: "cond", v: m[1], op: m[2], value: m[3], children: [] };
        cur().push(cond);
        stack.push(cond.children);
      } else if (stack.length > 1) {
        stack.pop(); // {endif} → 親に戻る（root は閉じない＝防御）
      }
    }
    pushTextParas(cur(), text.slice(last));
    if (root.length === 0) root.push({ id: ++blockSeq, kind: "text", value: "" });
    return root;
  }
  // 各リクエストの現在の本文（直列化）。保存・プレビュー・dirty 判定に使う。
  const currentPrompts = $derived.by(() => {
    const out: Record<string, string> = {};
    for (const r of Object.keys(editBlocks)) out[r] = serializeBlocks(editBlocks[r]);
    return out;
  });

  function saveAgent() {
    // 既定と同じものは保存しない（編集した差分だけ localStorage に持つ）
    const out: Record<string, string> = {};
    for (const [r, t] of Object.entries(currentPrompts)) {
      const v = (t ?? "").trim();
      if (v && v !== (agentDefaults[r] ?? "").trim()) out[r] = v.slice(0, MY_AGENT_MAX_CHARS);
    }
    myAgent.set({ prompts: out });
    agentSaved = true;
    setTimeout(() => (agentSaved = false), 1500);
  }

  async function previewAgent() {
    try {
      const res = await fetch(`${lobbyBase}/api/prompt/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: currentPrompts[selectedRequest] ?? "" }),
      });
      if (res.ok) previewText = (await res.json()).preview ?? "";
    } catch {
      /* ignore */
    }
  }

  function resetRequest() {
    editBlocks[selectedRequest] = parseBlocks(agentDefaults[selectedRequest] ?? "");
    previewText = null;
  }

  // ── ブロック操作 ───────────────────────────────────────────────
  // 各操作は「対象の並び list」と「その並びを差し替える set」を受け取り、**新しい配列を set** する。
  // 最上位でも入れ子(children)でも、owner への再代入になるので確実に再描画される。
  type SetList = (list: Block[]) => void;
  function makeText(): Block {
    return { id: ++blockSeq, kind: "text", value: "" };
  }
  function makeCond(): Block {
    const spec = agentCatalog.branch_vars?.[0];
    return {
      id: ++blockSeq, kind: "cond",
      v: spec?.key ?? "role", op: spec?.ops?.[0] ?? "=",
      value: spec?.type === "enum" ? (spec?.values?.[0] ?? "") : "1", children: [],
    };
  }
  // 最上位（選択中リクエスト）の並びを差し替える set。
  function setTop(nl: Block[]) {
    editBlocks[selectedRequest] = nl;
  }
  function opAppend(list: Block[], block: Block, set: SetList) {
    set([...list, block]);
  }
  function opInsert(list: Block[], afterIdx: number, block: Block, set: SetList) {
    set([...list.slice(0, afterIdx + 1), block, ...list.slice(afterIdx + 1)]);
  }
  function opRemove(list: Block[], id: number, set: SetList) {
    set(list.filter((b) => b.id !== id));
  }
  function opMove(list: Block[], from: number, to: number, set: SetList) {
    if (from < 0 || from >= list.length || to < 0 || to >= list.length || from === to) return;
    const nl = [...list];
    const [m] = nl.splice(from, 1);
    nl.splice(to, 0, m);
    set(nl);
  }
  function opMoveBy(list: Block[], idx: number, delta: number, set: SetList) {
    opMove(list, idx, idx + delta, set);
  }
  // ドラッグ&ドロップは同じ並びの中だけ（dragList が一致するときのみ並べ替え）。
  function dropOn(list: Block[], to: number, set: SetList) {
    if (dragList === list && dragIndex !== null) opMove(list, dragIndex, to, set);
    dragIndex = null;
    dragList = null;
  }
  // ＋メニューで選んだ後にドロップダウンを閉じる（DaisyUI dropdown は blur で閉じる）。
  function blurActive() {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  }
  // textarea を中身に合わせて自動で縦に伸ばす（1つの大きなエディタに見せるため）。
  function autogrow(node: HTMLTextAreaElement, _value: string) {
    const resize = () => { node.style.height = "auto"; node.style.height = `${node.scrollHeight}px`; };
    requestAnimationFrame(resize);
    return { update: () => requestAnimationFrame(resize) };
  }
  // 指定ブロックの textarea にフォーカスしてキャレットを置く（分割/結合後に呼ぶ）。
  function focusBlock(id: number, caret: number) {
    requestAnimationFrame(() => {
      const el = document.querySelector<HTMLTextAreaElement>(`textarea[data-bid="${id}"]`);
      if (el) { el.focus(); el.setSelectionRange(caret, caret); }
    });
  }
  // 文章ブロックの Enter で段落分割、行頭 Backspace で同じ並びの前の文章ブロックへ結合。
  // Shift+Enter は段落内の改行。IME変換中の Enter は無視。list/set はこのブロックが属する並び。
  function onTextKeydown(list: Block[], block: Block, i: number, set: SetList, ev: KeyboardEvent) {
    if (block.kind !== "text") return;
    const el = ev.currentTarget as HTMLTextAreaElement;
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      const pos = el.selectionStart ?? block.value.length;
      const after = block.value.slice(pos);
      block.value = block.value.slice(0, pos);
      const nb = makeText() as Extract<Block, { kind: "text" }>;
      nb.value = after;
      opInsert(list, i, nb, set);
      focusBlock(nb.id, 0);
    } else if (ev.key === "Backspace" && el.selectionStart === 0 && el.selectionEnd === 0) {
      const prev = list[i - 1];
      if (prev && prev.kind === "text") {
        ev.preventDefault();
        const caret = prev.value.length;
        prev.value = prev.value + block.value;
        opRemove(list, block.id, set);
        focusBlock(prev.id, caret);
      }
    }
  }
  // 分岐の対象変数を変えたら、演算子と値をその型の既定に合わせ直す。
  function onCondVarChange(block: Block) {
    if (block.kind !== "cond") return;
    const spec = agentCatalog.branch_vars?.find((b) => b.key === block.v);
    if (!spec) return;
    block.op = spec.ops?.[0] ?? "=";
    block.value = spec.type === "enum" ? (spec.values?.[0] ?? "") : "1";
  }

  // フォーカス中の文章ブロックに変数トークンを挿入（生 Jinja は打たせない）。
  function focusArea(block: Block, ev: FocusEvent) {
    activeBlock = block;
    activeEl = ev.currentTarget as HTMLTextAreaElement;
  }
  function insertAtCursor(text: string) {
    const el = activeEl;
    const block = activeBlock;
    if (!el || !block || block.kind !== "text") return; // 文章ブロックにフォーカス中のときだけ
    const cur = block.value;
    const s = el.selectionStart ?? cur.length;
    const e = el.selectionEnd ?? cur.length;
    const top = el.scrollTop;
    block.value = cur.slice(0, s) + text + cur.slice(e);
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(s + text.length, s + text.length);
      el.scrollTop = top;
    });
  }

  // 自作プロンプト送信用（useMyAgent かつ作成済みのとき辞書、なければ空）
  function myPromptsPayload(): Record<string, string> {
    return useMyAgent && hasCustomAgent ? ($myAgent.prompts ?? {}) : {};
  }

  // ゲーム言語（AIの発話言語）＝開始時のUI言語。言語セレクタはUIとゲームを同時に切り替える
  // （言語を選ぶ＝画面もAIも即座にその言語）。卓開始後はこのゲーム言語が固定される。
  const gameLanguage = $derived(normalizeLanguage($locale, "ja"));

  // ---- ソロ/マルチのモード選択（接続前の画面遷移）----
  // mode=最初の選択 / solo=ソロ設定 / multiCreate=部屋作成 / multiJoin=合言葉参加 / waiting=待機部屋
  let screen = $state<Screen>("mode");

  // 上部タブ（遊ぶ / エージェント編集）。中央は試合用、編集はタブで分離。
  // 注意: agentDirty は screen を参照するため screen 宣言より後に置く（SSR の初期化順）。
  let lastPlayScreen = $state<Screen>("mode");
  // 編集中に未保存の変更があるか（保存済み or 既定と違えば dirty）。
  const agentDirty = $derived.by(() => {
    if (screen !== "agent") return false;
    const saved = $myAgent.prompts ?? {};
    for (const r of Object.keys(currentPrompts)) {
      const cur = (currentPrompts[r] ?? "").trim();
      const persisted = (saved[r] ?? agentDefaults[r] ?? "").trim();
      if (cur !== persisted) return true;
    }
    return false;
  });
  function confirmLeaveAgent(): boolean {
    return !agentDirty || confirm($_("demo.agent.unsavedConfirm"));
  }
  function gotoAgentTab() {
    if (screen === "agent") return;
    lastPlayScreen = screen;
    openAgentEditor();
  }
  function gotoPlayTab() {
    if (screen === "agent" && !confirmLeaveAgent()) return;
    screen = lastPlayScreen === "agent" ? "mode" : lastPlayScreen;
  }
  let myAiCount = $state(1); // 観戦: AI席のうち自作AIにする数
  let deviceToken = ""; // 端末の匿名トークン（localStorage）。アカウント無しの席識別用。
  let humanSlots = $state(2); // マルチの人間席数（1..villageSize）
  let roomCode = $state<string>(""); // 参加中のマルチ部屋の合言葉
  let hostToken = $state<string>(""); // 自分がホストのときの start 認可トークン
  let isHost = $state(false);
  let roomParticipants = $state<{ display_name: string; is_host: boolean }[]>([]);
  let roomStatus = $state<string>("");
  let joinCodeInput = $state("");
  let myTeam: string | null = null; // 接続に使う自分の team（you.team）
  let codeCopied = $state(false);
  let roomPollTimer: ReturnType<typeof setTimeout> | null = null;
  // 村サイズを変えたら人間席数を範囲内に収める。
  $effect(() => {
    if (humanSlots > villageSize) humanSlots = villageSize;
  });

  // role/character の希望を ws URL に付ける（ソロのみ。未指定はランダム）。
  function withSeatPrefs(wsUrl: string): string {
    const q: string[] = [];
    if (selectedRole) q.push(`role=${encodeURIComponent(selectedRole)}`);
    if (selectedCharacter >= 0) q.push(`character=${selectedCharacter}`);
    return q.length ? wsUrl + (wsUrl.includes("?") ? "&" : "?") + q.join("&") : wsUrl;
  }

  // ---- ソロ: 即開始（人間1＋AI）----
  async function startSolo() {
    lobbyPhase = "joining";
    lobbyError = null;
    introAck = false;
    introOpen = false;
    isMulti = false;
    try {
      const res = await fetch(`${lobbyBase}/api/rooms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: "solo", size: villageSize, language: gameLanguage, token: deviceToken,
          // 自作AIを k 体・残りはサンプル（k は AI席=size-1 を超えない）
          agent_prompts: hasCustomAgent ? ($myAgent.prompts ?? {}) : {},
          my_ai_count: hasCustomAgent ? Math.min(myAiCount, villageSize - 1) : 0,
        }),
      });
      if (!res.ok) throw new Error(`create failed: ${res.status}`);
      const data = await res.json();
      sessionId = data.room_id;
      myTeam = data.you?.team ?? null;
      displayName = data.you?.display_name ?? null;
      if (data.ws_url) {
        agentSettings.update((value) => ({
          ...value,
          connection: { url: withSeatPrefs(data.ws_url), token: "" },
          team: myTeam ?? value.team,
        }));
      }
      applyLobbyStatus(data.status, data.position);
      pollSession();
    } catch (e) {
      lobbyPhase = "error";
      lobbyError = e instanceof Error ? e.message : String(e);
    }
  }

  // ---- AI観戦: AI同士の対戦を観る（自分は席に着くが自動でパス）----
  async function startSpectate() {
    lobbyPhase = "joining";
    lobbyError = null;
    introAck = true; // 観戦では役職ポップアップを出さない
    introOpen = false;
    isMulti = false;
    isSpectate = true;
    try {
      const res = await fetch(`${lobbyBase}/api/rooms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: "solo", size: villageSize, language: gameLanguage, token: deviceToken,
          agent_prompts: hasCustomAgent ? ($myAgent.prompts ?? {}) : {},
          my_ai_count: hasCustomAgent ? Math.min(myAiCount, villageSize - 1) : 0,
        }),
      });
      if (!res.ok) throw new Error(`create failed: ${res.status}`);
      const data = await res.json();
      sessionId = data.room_id;
      myTeam = data.you?.team ?? null;
      displayName = data.you?.display_name ?? null;
      if (data.ws_url) {
        agentSettings.update((value) => ({
          ...value,
          connection: { url: data.ws_url, token: "" },
          team: myTeam ?? value.team,
        }));
      }
      applyLobbyStatus(data.status, data.position);
      pollSession();
    } catch (e) {
      lobbyPhase = "error";
      lobbyError = e instanceof Error ? e.message : String(e);
    }
  }

  // 観戦中は自分の手番を自動でパスする（発言はOver、投票/夜は生存対象の先頭）。
  let spectateActedDeadline: number | null = null;
  $effect(() => {
    if (!isSpectate || !isMyTurn || deadline === null) return;
    if (spectateActedDeadline === deadline) return; // この手番は処理済み
    spectateActedDeadline = deadline;
    const talkReq = request === Request.TALK || request === Request.WHISPER;
    const targets = aliveTargets;
    const t = setTimeout(() => {
      if (talkReq) demoSocketState.send("Over");
      else if (targets.length) demoSocketState.send(targets[0]);
      else demoSocketState.send("");
    }, 700);
    return () => clearTimeout(t);
  });

  // ---- マルチ: 部屋を作る（ホスト）----
  async function createRoom() {
    lobbyError = null;
    try {
      const res = await fetch(`${lobbyBase}/api/rooms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: "multi", size: villageSize, language: gameLanguage,
          human_slots: humanSlots, token: deviceToken,
          agent_prompts: myPromptsPayload(),
        }),
      });
      if (!res.ok) throw new Error(`create failed: ${res.status}`);
      const data = await res.json();
      isHost = true;
      hostToken = data.host_token;
      enterWaiting(data);
    } catch (e) {
      lobbyError = e instanceof Error ? e.message : String(e);
    }
  }

  // ---- マルチ: 合言葉で参加 ----
  async function joinRoom() {
    lobbyError = null;
    const code = joinCodeInput.trim().toUpperCase();
    if (!code) return;
    try {
      const res = await fetch(`${lobbyBase}/api/rooms/${encodeURIComponent(code)}/join`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token: deviceToken,
          agent_prompts: myPromptsPayload(),
        }),
      });
      if (res.status === 404) { lobbyError = $_("demo.multi.notFound"); return; }
      if (res.status === 409) { lobbyError = $_("demo.multi.full"); return; }
      if (!res.ok) throw new Error(`join failed: ${res.status}`);
      const data = await res.json();
      isHost = false;
      enterWaiting(data);
    } catch (e) {
      lobbyError = e instanceof Error ? e.message : String(e);
    }
  }

  function enterWaiting(data: any) {
    isMulti = true; // マルチ卓 → 一時停止は無効
    roomCode = data.code;
    sessionId = data.room_id;
    myTeam = data.you?.team ?? null;
    displayName = data.you?.display_name ?? null;
    screen = "waiting";
    applyRoom(data);
    pollRoom();
  }

  function applyRoom(data: any) {
    roomStatus = data.status;
    roomParticipants = data.participants ?? [];
    if (data.status === "running" && data.ws_url) {
      // 卓が立った → 自分の team で接続
      agentSettings.update((value) => ({
        ...value,
        connection: { url: data.ws_url, token: "" },
        team: myTeam ?? value.team,
      }));
      stopRoomPoll();
      lobbyPhase = "starting";
      demoSocketState.connect();
      lobbyPhase = "playing";
      pollRoomNotices(); // プレイ中、離脱→AI引き継ぎの告知を拾ってフィードに出す
    } else if (data.status === "finished" || data.status === "error") {
      // ホスト退出などで部屋が閉じた
      stopRoomPoll();
      lobbyError = $_("demo.multi.roomClosed");
    }
  }

  // ---- プレイ中の引き継ぎ告知ポーリング（マルチのみ）----
  // 誰かが離脱すると lobby の takeover_events が増える。それを拾って
  // 「○○が退出。AIが代わりに参加」をフィードに1回だけ出す。
  let shownTakeovers = 0;
  let playPollTimer: ReturnType<typeof setTimeout> | null = null;
  async function pollRoomNotices() {
    if (!roomCode || !isMulti) return;
    try {
      const res = await fetch(`${lobbyBase}/api/rooms/${encodeURIComponent(roomCode)}?token=${encodeURIComponent(deviceToken)}`);
      if (res.ok) {
        const data = await res.json();
        const events: string[] = data.takeover_events ?? [];
        for (let i = shownTakeovers; i < events.length; i++) {
          demoSocketState.pushNotice(`takeover-${i}`, "demo.feed.takeover", { name: events[i] });
        }
        shownTakeovers = Math.max(shownTakeovers, events.length);
        if (data.status === "finished" || data.status === "error") { stopPlayPoll(); return; }
      } else if (res.status === 404) { stopPlayPoll(); return; }
    } catch {
      /* 一時失敗は次で回復 */
    }
    if (!finished) playPollTimer = setTimeout(pollRoomNotices, 3000);
  }
  function stopPlayPoll() {
    if (playPollTimer) { clearTimeout(playPollTimer); playPollTimer = null; }
  }

  async function pollRoom() {
    if (!roomCode) return;
    try {
      const res = await fetch(`${lobbyBase}/api/rooms/${encodeURIComponent(roomCode)}?token=${encodeURIComponent(deviceToken)}`);
      if (res.ok) applyRoom(await res.json());
      else if (res.status === 404) { roomStatus = "finished"; lobbyError = $_("demo.multi.roomClosed"); stopRoomPoll(); }
    } catch {
      /* 一時失敗は次のポーリングで回復 */
    }
    if (roomStatus === "waiting") roomPollTimer = setTimeout(pollRoom, 1500);
  }

  function stopRoomPoll() {
    if (roomPollTimer) { clearTimeout(roomPollTimer); roomPollTimer = null; }
  }

  // ---- マルチ: ホストが開始 ----
  async function startMultiGame() {
    try {
      await fetch(`${lobbyBase}/api/rooms/${encodeURIComponent(roomCode)}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: hostToken }),
      });
      // 次のポーリングで running を拾って接続する
    } catch (e) {
      lobbyError = e instanceof Error ? e.message : String(e);
    }
  }

  function copyCode() {
    if (browser && roomCode) {
      navigator.clipboard?.writeText(roomCode).then(() => {
        codeCopied = true;
        setTimeout(() => (codeCopied = false), 1500);
      }).catch(() => {});
    }
  }

  // 待機部屋から抜ける（ホストなら部屋解散、参加者なら離席）。
  function leaveRoom() {
    stopRoomPoll();
    if (roomCode) {
      fetch(`${lobbyBase}/api/rooms/${encodeURIComponent(roomCode)}/leave`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: deviceToken }),
      }).catch(() => {});
    }
    stopPlayPoll();
    shownTakeovers = 0;
    roomCode = "";
    isHost = false;
    hostToken = "";
    roomParticipants = [];
    roomStatus = "";
    sessionId = null;
    lobbyError = null;
    screen = "mode";
  }

  function applyLobbyStatus(s: string, position: number) {
    if (s === "queued") {
      lobbyPhase = "queued";
      queuePos = position;
    } else if (s === "running") {
      if (status !== "connected" && lobbyPhase !== "playing") {
        lobbyPhase = "starting";
        demoSocketState.connect(); // 卓が立った → 人間枠を接続
        lobbyPhase = "playing";
      }
    } else if (s === "error") {
      lobbyPhase = "error";
      lobbyError = $_("demo.start.startFailed");
    }
  }

  async function pollSession() {
    if (!sessionId) return;
    try {
      const res = await fetch(`${lobbyBase}/api/session/${sessionId}`);
      if (res.ok) {
        const data = await res.json();
        // 順番待ち→開始のときに接続URL(役職/キャラ付き)を確定（未接続のときだけ）。
        if (data.status === "running" && data.ws_url && status !== "connected" && lobbyPhase !== "playing") {
          agentSettings.update((value) => ({
            ...value,
            connection: { url: withSeatPrefs(data.ws_url), token: "" },
            team: myTeam ?? value.team,
          }));
        }
        applyLobbyStatus(data.status, data.position);
        if (data.error) {
          lobbyError = data.error;
        }
      }
    } catch {
      /* ネットワーク一時失敗は次のポーリングで回復 */
    }
    // 接続完了するまで（または終了まで）ポーリング継続
    if (lobbyPhase === "queued" || lobbyPhase === "starting") {
      pollTimer = setTimeout(pollSession, 1500);
    }
  }

  // ゲームを中断してホーム（スタート画面）に戻る
  function leaveGame(confirmFirst = true) {
    if (confirmFirst && !confirm($_("demo.leaveConfirm"))) {
      return;
    }
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    // マルチ卓は room leave（進行中なら席をAIが引き継ぎ、卓は続行）。ソロは session leave（卓終了）。
    if (roomCode) {
      fetch(`${lobbyBase}/api/rooms/${encodeURIComponent(roomCode)}/leave`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: deviceToken }),
      }).catch(() => {});
    } else if (sessionId) {
      fetch(`${lobbyBase}/api/session/${sessionId}/leave`, { method: "POST" }).catch(() => {});
    }
    stopRoomPoll();
    stopPlayPoll();
    shownTakeovers = 0;
    demoSocketState.reset();
    infoOpen = false;
    introOpen = false;
    introAck = false;
    paused = false;
    isMulti = false;
    isSpectate = false;
    spectateActedDeadline = null;
    sessionId = null;
    displayName = null;
    queuePos = 0;
    lobbyError = null;
    lobbyPhase = "idle";
    // モード選択へ戻す＋マルチ部屋の状態をクリア
    roomCode = "";
    isHost = false;
    hostToken = "";
    roomParticipants = [];
    roomStatus = "";
    screen = "mode";
  }

  // ---- 接続（?url= 直接接続を優先。無ければロビー画面）----
  if (browser) {
    // 端末の匿名トークン（席識別用。アカウント機能を足すときはこれをアカウントに紐付ける）。
    deviceToken = localStorage.getItem("demo_device_token") ?? "";
    if (!deviceToken) {
      deviceToken =
        (crypto?.randomUUID?.() ?? `t-${Date.now()}-${Math.random().toString(36).slice(2)}`);
      localStorage.setItem("demo_device_token", deviceToken);
    }
    const params = page.url.searchParams;
    const url = params.get("url");
    const token = params.get("token");
    const team = params.get("team");
    const lobbyParam = params.get("lobby");
    const langParam = params.get("lang");
    if (lobbyParam) lobbyBase = lobbyParam.replace(/\/$/, "");
    // 直リンクに lang が付いていれば UI 言語を卓のゲーム言語に合わせる（UI言語は後から変更可）。
    if (langParam) language.set(normalizeLanguage(langParam, "ja"));

    if (url) {
      directMode = true;
      agentSettings.update((value) => ({
        ...value,
        connection: { url, token: token ?? "" },
        team: team ?? value.team,
      }));
      demoSocketState.connect();
    }

    const beforeUnload = (e: BeforeUnloadEvent) => {
      if (status === "connected") e.preventDefault();
    };
    window.addEventListener("beforeunload", beforeUnload);

    onDestroy(() => {
      window.removeEventListener("beforeunload", beforeUnload);
      if (pollTimer) clearTimeout(pollTimer);
      stopRoomPoll();
      stopPlayPoll();
      // 離脱を lobby に通知。マルチ卓は room leave（進行中なら席をAIが引き継ぎ卓は続行）、
      // ソロは session leave（卓終了）。
      if (roomCode) {
        navigator.sendBeacon?.(
          `${lobbyBase}/api/rooms/${encodeURIComponent(roomCode)}/leave`,
          new Blob([JSON.stringify({ token: deviceToken })], { type: "application/json" }),
        );
      } else if (sessionId) {
        navigator.sendBeacon?.(`${lobbyBase}/api/session/${sessionId}/leave`);
      }
      stopCountdown();
      unsub();
      unsubSettings();
    });
  }

  // ゲーム開始前のスタート画面を出すか
  const showStartScreen = $derived(
    !directMode && status !== "connected" && lobbyPhase !== "playing",
  );
</script>

<svelte:head><title>{$_("demo.title")}</title></svelte:head>

<main class="h-dvh flex flex-col bg-base-300">
  <!-- ヘッダ：タイトル＋自分の情報＋操作ボタン -->
  <header class="flex-none bg-base-100 px-3 py-2 flex flex-wrap items-center gap-2 shadow">
    <div class="flex items-center gap-2 min-w-0">
      {#if agent}
        <button class="avatar" onclick={() => (infoOpen = true)} aria-label={$_("demo.header.myInfo")}>
          <div class="w-9 rounded-full ring ring-primary ring-offset-1">
            <img src={avatarSrc(agent)} alt={nameOf(agent)} />
          </div>
        </button>
        <div class="leading-tight min-w-0">
          <div class="font-bold truncate">{nameOf(agent)}<span class="ml-1 text-xs opacity-70">({roleName(role)})</span></div>
          <div class="text-xs opacity-60">{info ? $_("demo.day", { values: { day: info.day } }) : ""}</div>
        </div>
      {:else}
        <div class="font-bold text-sm leading-tight">{$_("demo.titleShort")}<br />{$_("demo.subtitle")}</div>
      {/if}
    </div>
    <div class="ml-auto flex items-center gap-1.5">
      <!-- UI言語スイッチャー（常設）。いつでも切替でき、ヘッダー/フッター等が即座に切り替わる。 -->
      <LanguageSwitcher />
      <span class="badge badge-sm {status === 'connected' ? 'badge-success' : status === 'connecting' ? 'badge-warning' : 'badge-error'}">
        {status === "connected" ? $_("demo.status.connected") : status === "connecting" ? $_("demo.status.connecting") : $_("demo.status.disconnected")}
      </span>
      {#if status === "connected" && !finished && !isMulti}
        <!-- 一時停止はソロのみ（マルチでは全員を止めてしまうため非表示）-->
        <button
          class="btn btn-xs {paused ? 'btn-success' : 'btn-ghost'}"
          onclick={() => (paused = !paused)}
          aria-label={paused ? $_("demo.header.resume") : $_("demo.header.pause")}
        >
          <iconify-icon icon={paused ? "mdi:play" : "mdi:pause"}></iconify-icon>
          {paused ? $_("demo.header.resume") : $_("demo.header.pause")}
        </button>
      {/if}
      {#if status === "connected" && !finished}
        <button class="btn btn-xs btn-ghost" onclick={() => (infoOpen = true)} aria-label={$_("demo.header.info")}>
          <iconify-icon icon="mdi:information-outline"></iconify-icon>{$_("demo.header.info")}
        </button>
        <button class="btn btn-xs btn-error btn-outline" onclick={() => leaveGame()} aria-label={$_("demo.header.leave")}>
          <iconify-icon icon="mdi:home"></iconify-icon>{$_("demo.header.leave")}
        </button>
      {/if}
    </div>
  </header>

  <!-- プロフィール/役職プレビュー（ドロワー風モーダル）-->
  {#if infoOpen}
    <div class="fixed inset-0 z-50 flex">
      <button class="absolute inset-0 bg-black/50" onclick={() => (infoOpen = false)} aria-label={$_("demo.info.close")}></button>
      <div class="relative ml-auto h-full w-80 max-w-[85vw] bg-base-100 shadow-xl overflow-y-auto p-4 flex flex-col gap-4">
        <div class="flex items-center justify-between">
          <h2 class="font-bold text-lg">{$_("demo.info.title")}</h2>
          <button class="btn btn-sm btn-circle btn-ghost" onclick={() => (infoOpen = false)}>✕</button>
        </div>

        <!-- 自分 -->
        <div class="card bg-base-200 p-3">
          <div class="flex items-center gap-3">
            <div class="avatar"><div class="w-14 rounded-full"><img src={avatarSrc(agent ?? "")} alt={nameOf(agent)} /></div></div>
            <div>
              <div class="font-bold">{nameOf(agent)}</div>
              <div class="badge badge-primary badge-sm">{$_("demo.info.role", { values: { role: roleName(role) } })}</div>
              {#if team}
                <div class="text-xs opacity-60 mt-1">{$_("demo.info.team", { values: { team } })}</div>
              {/if}
            </div>
          </div>
          {#if myPersonality}
            <div class="mt-2 text-sm whitespace-pre-wrap opacity-80">{myPersonality}</div>
          {:else}
            <div class="mt-2 text-xs opacity-50">{$_("demo.info.noProfile")}</div>
          {/if}
        </div>

        <!-- 参加者一覧 -->
        <div>
          <h3 class="font-bold mb-2">{$_("demo.info.participants")}</h3>
          <div class="flex flex-col gap-1">
            {#each Object.entries(info?.status_map ?? {}) as [name, st]}
              {@const known = info?.role_map?.[name]}
              <div class="flex items-center gap-2 p-1.5 rounded {st === Status.ALIVE ? 'bg-base-200' : 'bg-base-300 opacity-60'}">
                <div class="avatar"><div class="w-8 rounded-full"><img src={avatarSrc(name)} alt={nameOf(name)} /></div></div>
                <span class="font-bold text-sm">{nameOf(name)}{name === agent ? $_("demo.info.you") : ""}</span>
                {#if known}<span class="badge badge-xs">{roleName(known)}</span>{/if}
                <span class="ml-auto badge badge-xs {st === Status.ALIVE ? 'badge-success' : 'badge-error'}">
                  {st === Status.ALIVE ? $_("demo.info.alive") : $_("demo.info.dead")}
                </span>
              </div>
            {/each}
          </div>
          <p class="text-xs opacity-50 mt-2">{$_("demo.info.privacyNote")}</p>
        </div>

        <!-- 占い結果（占い師のみ届く。target は人狼/人間が判明）-->
        {#if divineResults.length > 0}
          <div>
            <h3 class="font-bold mb-2">{$_("demo.info.divineResults")}</h3>
            <div class="flex flex-col gap-1">
              {#each divineResults as j}
                <div class="flex items-center gap-2 p-1.5 rounded bg-base-200">
                  <span class="text-xs opacity-60">{$_("demo.day", { values: { day: j.day } })}</span>
                  <span class="font-bold text-sm">{nameOf(j.target)}</span>
                  <span class="ml-auto badge badge-sm {j.result === 'WEREWOLF' ? 'badge-error' : 'badge-success'}">
                    {speciesName(j.result)}
                  </span>
                </div>
              {/each}
            </div>
          </div>
        {/if}
        <!-- 霊媒結果（霊媒師のみ）-->
        {#if mediumResults.length > 0}
          <div>
            <h3 class="font-bold mb-2">{$_("demo.info.mediumResults")}</h3>
            <div class="flex flex-col gap-1">
              {#each mediumResults as j}
                <div class="flex items-center gap-2 p-1.5 rounded bg-base-200">
                  <span class="text-xs opacity-60">{$_("demo.day", { values: { day: j.day } })}</span>
                  <span class="font-bold text-sm">{nameOf(j.target)}</span>
                  <span class="ml-auto badge badge-sm {j.result === 'WEREWOLF' ? 'badge-error' : 'badge-success'}">
                    {speciesName(j.result)}
                  </span>
                </div>
              {/each}
            </div>
          </div>
        {/if}
      </div>
    </div>
  {/if}

  <!-- ゲーム開始ポップアップ（役職・キャラ確認 → 確認で開始）-->
  {#if introOpen && agent}
    <div class="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/60">
      <div class="card bg-base-100 w-full max-w-sm p-5 flex flex-col items-center gap-3 text-center">
        <div class="avatar"><div class="w-24 rounded-full ring ring-primary"><img src={avatarSrc(agent)} alt={nameOf(agent)} /></div></div>
        <div class="text-sm opacity-70">{$_("demo.intro.yourCharacter")}</div>
        <div class="text-2xl font-bold">{nameOf(agent)}</div>
        <div class="badge badge-primary badge-lg">{$_("demo.info.role", { values: { role: roleName(role) } })}</div>
        <p class="text-sm opacity-80">{role ? $_(`demo.roleDesc.${role}`) : ""}</p>
        {#if myPersonality}
          <div class="text-xs whitespace-pre-wrap opacity-70 bg-base-200 rounded p-2 max-h-32 overflow-y-auto">{myPersonality}</div>
        {/if}
        <p class="text-sm font-bold mt-1">{$_("demo.intro.instruction")}</p>
        <button class="btn btn-primary btn-block" onclick={startGame}>
          {$_("demo.intro.start")}
        </button>
      </div>
    </div>
  {/if}

  <!-- ゲーム終了結果画面 -->
  {#if finished}
    <div class="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/70">
      <div class="card bg-base-100 w-full max-w-sm p-5 flex flex-col gap-3 max-h-[90vh] overflow-y-auto">
        <h2 class="text-2xl font-bold text-center">{$_("demo.result.title")}</h2>
        {#if winnerText}
          <div class="text-center text-lg font-bold">{winnerText}</div>
        {/if}
        {#if myResult}
          <div class="text-center text-xl font-bold {iWon ? 'text-success' : 'text-error'}">{myResult}</div>
        {/if}
        <div class="divider my-1">{$_("demo.result.reveal")}</div>
        <div class="flex flex-col gap-1">
          {#each Object.entries(info?.role_map ?? {}) as [name, r]}
            {@const rs = r as string}
            {@const alive = (info?.status_map ?? {})[name] === Status.ALIVE}
            <div class="flex items-center gap-2 p-1.5 rounded {alive ? 'bg-base-200' : 'bg-base-300 opacity-70'}">
              <div class="avatar"><div class="w-8 rounded-full"><img src={avatarSrc(name)} alt={nameOf(name)} /></div></div>
              <span class="font-bold text-sm">{nameOf(name)}{name === agent ? $_("demo.info.you") : ""}</span>
              <span class="badge badge-sm {rs === 'WEREWOLF' ? 'badge-error' : ''}">{roleName(rs)}</span>
              <span class="ml-auto text-xs opacity-60">{alive ? $_("demo.info.alive") : $_("demo.info.dead")}</span>
            </div>
          {/each}
        </div>
        <button class="btn btn-primary btn-block mt-2" onclick={() => leaveGame(false)}>{$_("demo.result.backHome")}</button>
      </div>
    </div>
  {/if}

  <!-- 状態バナー -->
  <div class="flex-none px-4 py-2 text-center font-bold
              {isMyTurn ? 'bg-primary text-primary-content' : 'bg-base-200'}">
    {banner}
    {#if isMyTurn && remainSec !== null}
      <span class="ml-2 font-mono">{$_("demo.remain", { values: { sec: remainSec } })}</span>
    {/if}
  </div>

  <!-- マルチ作成/参加で共通: 「離脱したら自作AIが引き継ぐ」トグル -->
  {#snippet takeoverToggle()}
    <div class="flex flex-col items-center gap-1 w-full max-w-xs">
      <label class="flex items-center gap-2 cursor-pointer {hasCustomAgent ? '' : 'opacity-50'}">
        <input type="checkbox" class="checkbox checkbox-sm" bind:checked={useMyAgent} disabled={!hasCustomAgent} />
        <span class="text-sm text-left">
          {hasCustomAgent ? $_("demo.multi.useMyAgent") : $_("demo.multi.useMyAgentNone")}
        </span>
      </label>
      {#if !hasCustomAgent}
        <button class="text-[11px] opacity-60 underline" onclick={gotoAgentTab}>{$_("demo.tab.agent")}</button>
      {/if}
    </div>
  {/snippet}

  <!-- ソロ/観戦で共通: 自作AIを何体起動するか（残りはサンプル）。maxSeats を超えない。 -->
  {#snippet customAiSlider(maxSeats: number)}
    {#if hasCustomAgent}
      <div class="flex flex-col items-center gap-1 w-full max-w-xs">
        <div class="text-sm font-bold opacity-70">{$_("demo.spectate.myAi")}: <span class="text-primary">{Math.min(myAiCount, maxSeats)}</span> / {maxSeats}</div>
        <input type="range" min="0" max={maxSeats} bind:value={myAiCount} class="range range-primary range-sm w-full" />
        <div class="text-xs opacity-60">{$_("demo.spectate.composition", { values: { my: Math.min(myAiCount, maxSeats), def: maxSeats - Math.min(myAiCount, maxSeats) } })}</div>
      </div>
    {:else}
      <button class="text-[11px] opacity-60 underline" onclick={gotoAgentTab}>{$_("demo.spectate.needAgent")}</button>
    {/if}
  {/snippet}

  <!-- 入れ子対応のブロックエディタ（自分自身を再帰呼び出し）。list=この並び / set=この並びの差し替え。 -->
  {#snippet blockEditor(list: Block[], set: SetList)}
    {#each list as block, i (block.id)}
      <div
        role="listitem"
        class="group relative flex items-start gap-0.5 rounded {dragList === list && dragIndex === i ? 'opacity-40' : ''}"
        ondragover={(e) => { if (dragIndex !== null) e.preventDefault(); }}
        ondrop={(e) => { e.preventDefault(); dropOn(list, i, set); }}
      >
        <!-- 行頭ガター: ＋でこの位置に挿入 / ⠿でドラッグ移動 -->
        <div class="flex shrink-0 pt-1 opacity-25 group-hover:opacity-100 transition-opacity">
          <div class="dropdown">
            <button type="button" tabindex="0" class="btn btn-ghost btn-xs px-0.5 min-h-0 h-6" aria-label={$_("demo.agent.insertBlock")} title={$_("demo.agent.insertBlock")}><iconify-icon icon="mdi:plus"></iconify-icon></button>
            <ul class="dropdown-content menu menu-xs z-10 w-32 rounded-box border border-base-300 bg-base-100 p-1 shadow">
              <li><button type="button" onclick={() => { opInsert(list, i, makeText(), set); blurActive(); }}>{$_("demo.agent.textBlock")}</button></li>
              <li><button type="button" onclick={() => { opInsert(list, i, makeCond(), set); blurActive(); }}>{$_("demo.agent.condBlock")}</button></li>
            </ul>
          </div>
          <button
            type="button" draggable="true" tabindex="-1"
            class="cursor-grab active:cursor-grabbing btn btn-ghost btn-xs px-0.5 min-h-0 h-6"
            aria-label={$_("demo.agent.dragHint")} title={$_("demo.agent.dragHint")}
            ondragstart={(e) => { dragIndex = i; dragList = list; e.dataTransfer?.setData("text/plain", String(i)); }}
            ondragend={() => { dragIndex = null; dragList = null; }}
          ><iconify-icon icon="mdi:drag-vertical"></iconify-icon></button>
        </div>

        <!-- 中身: 文章 or 条件分岐（条件は children を再帰描画＝入れ子） -->
        <div class="grow min-w-0">
          {#if block.kind === "text"}
            <textarea
              use:autogrow={block.value}
              rows="1"
              data-bid={block.id}
              class="w-full resize-none rounded border-0 bg-transparent px-2 py-1 text-sm leading-relaxed placeholder:opacity-40 hover:bg-base-200/40 focus:bg-base-200/50 focus:outline-none"
              placeholder={$_("demo.agent.textPh")}
              bind:value={block.value}
              onfocus={(e) => focusArea(block, e)}
              onkeydown={(e) => onTextKeydown(list, block, i, set, e)}
            ></textarea>
          {:else}
            {@const spec = branchSpec(block.v)}
            {@const setChildren = (nl: Block[]) => { if (block.kind === "cond") block.children = nl; }}
            <div class="my-0.5 rounded-md border border-accent/30 bg-accent/5 px-2 py-1.5">
              <div class="flex flex-wrap items-center gap-1 text-xs">
                <span class="font-bold opacity-70">{$_("demo.agent.condIf")}</span>
                <select class="select select-bordered select-xs" bind:value={block.v} onchange={() => onCondVarChange(block)}>
                  {#each agentCatalog.branch_vars as bv}<option value={bv.key}>{$_(`demo.agent.var.${bv.key}`)}</option>{/each}
                </select>
                <select class="select select-bordered select-xs" bind:value={block.op}>
                  {#each (spec?.ops ?? ["="]) as op}<option value={op}>{OP_SYM[op] ?? op}</option>{/each}
                </select>
                {#if spec?.type === "enum"}
                  <select class="select select-bordered select-xs" bind:value={block.value}>
                    {#each (spec?.values ?? []) as val}<option value={val}>{$_(`game.role.${val}`)}</option>{/each}
                  </select>
                {:else}
                  <input type="number" min="0" class="input input-bordered input-xs w-16" bind:value={block.value} />
                {/if}
              </div>
              <!-- この条件のときの中身。さらに条件分岐も置ける（入れ子）。 -->
              <div class="mt-1 border-l-2 border-accent/30 pl-1">
                {#if block.children.length > 0}
                  {@render blockEditor(block.children, setChildren)}
                {:else}
                  <div class="px-2 py-1 text-[11px] opacity-40">{$_("demo.agent.condBodyPh")}</div>
                {/if}
                <div class="flex flex-wrap items-center gap-1 pt-0.5">
                  <button type="button" class="btn btn-ghost btn-xs gap-0.5" onclick={() => opAppend(block.children, makeText(), setChildren)}><iconify-icon icon="mdi:plus"></iconify-icon>{$_("demo.agent.textBlock")}</button>
                  <button type="button" class="btn btn-ghost btn-xs gap-0.5 text-accent" onclick={() => opAppend(block.children, makeCond(), setChildren)}><iconify-icon icon="mdi:plus"></iconify-icon>{$_("demo.agent.condBlock")}</button>
                </div>
              </div>
            </div>
          {/if}
        </div>

        <!-- 行アクション: 上へ / 下へ / 削除 -->
        <div class="flex shrink-0 pt-1 opacity-25 group-hover:opacity-100 transition-opacity">
          <button type="button" class="btn btn-ghost btn-xs px-0.5 min-h-0 h-6" aria-label={$_("demo.agent.moveUp")} disabled={i === 0} onclick={() => opMoveBy(list, i, -1, set)}><iconify-icon icon="mdi:chevron-up"></iconify-icon></button>
          <button type="button" class="btn btn-ghost btn-xs px-0.5 min-h-0 h-6" aria-label={$_("demo.agent.moveDown")} disabled={i === list.length - 1} onclick={() => opMoveBy(list, i, 1, set)}><iconify-icon icon="mdi:chevron-down"></iconify-icon></button>
          <button type="button" class="btn btn-ghost btn-xs px-0.5 min-h-0 h-6 text-error" aria-label={$_("demo.agent.removeBlock")} onclick={() => opRemove(list, block.id, set)}><iconify-icon icon="mdi:close"></iconify-icon></button>
        </div>
      </div>
    {/each}
  {/snippet}

  {#if showStartScreen}
    <!-- スタート/順番待ち画面（ロビー連携）-->
    <div class="grow flex flex-col items-center justify-center gap-4 p-6 text-center">
      <h1 class="text-xl font-bold">{$_("demo.title")}</h1>
      <p class="opacity-70 text-sm max-w-xs">
        {$_("demo.tagline")}
      </p>

      {#if lobbyPhase === "idle"}
        <!-- 上部タブ: 遊ぶ（試合）/ エージェント編集（プロンプト）。中央は試合用に専念させる。 -->
        <div role="tablist" class="tabs tabs-boxed">
          <button role="tab" class="tab {screen === 'agent' ? '' : 'tab-active'}" onclick={gotoPlayTab}>{$_("demo.tab.play")}</button>
          <button role="tab" class="tab {screen === 'agent' ? 'tab-active' : ''}" onclick={gotoAgentTab}>{$_("demo.tab.agent")}</button>
        </div>

        {#if screen === "mode"}
          <!-- ① モード選択: ソロ/マルチ/AI観戦 を同サイズの3カードで。合言葉参加は試合系なので中央に残す。 -->
          <div class="flex flex-col gap-3 w-full max-w-xs">
            <button class="btn btn-primary h-auto py-3 flex-col" onclick={() => (screen = "solo")}>
              <span class="text-base font-bold">{$_("demo.mode.solo")}</span>
              <span class="text-xs font-normal opacity-80">{$_("demo.mode.soloDesc")}</span>
            </button>
            <button class="btn btn-outline h-auto py-3 flex-col" onclick={() => (screen = "multiCreate")}>
              <span class="text-base font-bold">{$_("demo.mode.multi")}</span>
              <span class="text-xs font-normal opacity-80">{$_("demo.mode.multiDesc")}</span>
            </button>
            <button class="btn btn-outline h-auto py-3 flex-col" onclick={() => (screen = "spectate")}>
              <span class="text-base font-bold">{$_("demo.mode.spectate")}</span>
              <span class="text-xs font-normal opacity-80">{$_("demo.mode.spectateDesc")}</span>
            </button>
            <button class="btn btn-ghost btn-sm" onclick={() => (screen = "multiJoin")}>{$_("demo.multi.join")}</button>
          </div>
        {:else if screen === "spectate"}
          <!-- 👁 AI観戦: AI同士の対戦を観る -->
          <div class="text-lg font-bold">{$_("demo.spectate.title")}</div>
          <p class="text-xs opacity-60 max-w-xs">{$_("demo.spectate.intro")}</p>
          <div class="flex flex-col items-center gap-2">
            <div class="text-sm font-bold opacity-70">{$_("demo.start.villageSize")}</div>
            <div class="join">
              <button class="join-item btn {villageSize === 5 ? 'btn-primary' : 'btn-outline'}" onclick={() => (villageSize = 5)}>{$_("demo.start.village5")}</button>
              <button class="join-item btn {villageSize === 9 ? 'btn-primary' : 'btn-outline'}" onclick={() => (villageSize = 9)}>{$_("demo.start.village9")}</button>
            </div>
          </div>
          {@render customAiSlider(villageSize - 1)}
          <button class="btn btn-primary btn-lg" onclick={startSpectate}>{$_("demo.spectate.start")}</button>
          <button class="btn btn-ghost btn-sm" onclick={() => (screen = "mode")}>← {$_("demo.mode.back")}</button>
        {:else if screen === "agent"}
          <!-- AIエージェントのプロンプト編集。PC=2ペイン(左リクエスト/右エディタ)、スマホ=縦積み。 -->
          <div class="w-full max-w-4xl text-left">
            <div class="text-lg font-bold text-center">{$_("demo.agent.title")}</div>
            <p class="text-xs opacity-60 text-center mb-3 max-w-xl mx-auto">{$_("demo.agent.intro")}</p>
            <div class="flex flex-col md:flex-row gap-3 items-stretch">
              <!-- 左ペイン: リクエスト一覧（PC=縦サイドバー / スマホ=横スクロール） -->
              <nav class="md:w-44 md:shrink-0">
                <div class="text-[11px] font-bold opacity-50 mb-1 px-1">{$_("demo.agent.requests")}</div>
                <div class="flex md:flex-col gap-1 overflow-x-auto md:overflow-visible pb-1">
                  {#each (agentCatalog.requests.length ? agentCatalog.requests : Object.keys(editBlocks)) as req}
                    {@const edited = (currentPrompts[req] ?? "").trim() !== (agentDefaults[req] ?? "").trim()}
                    <button
                      type="button"
                      class="btn btn-sm whitespace-nowrap md:w-full md:justify-start {selectedRequest === req ? 'btn-primary' : 'btn-ghost'}"
                      onclick={() => { selectedRequest = req; previewText = null; activeBlock = null; activeEl = null; }}
                    >
                      <span class="md:grow md:text-left">{$_(`demo.agent.req.${req}`)}</span>
                      {#if edited}<span class="text-accent">●</span>{/if}
                    </button>
                  {/each}
                </div>
              </nav>

              <!-- 右ペイン: エディタ -->
              <div class="grow min-w-0 flex flex-col gap-2">
                <!-- 変数パレット（折りたたみ）。出しっぱなしをやめてまとまりを出す。 -->
                {#if pickerVars.length}
                  <details class="rounded border border-base-300 bg-base-100" open>
                    <summary class="cursor-pointer select-none text-[11px] font-bold opacity-70 px-2 py-1">{$_("demo.agent.vars")}</summary>
                    <div class="flex flex-wrap items-stretch gap-1 px-2 pb-1">
                      {#each pickerVars as v}
                        <button
                          type="button"
                          title={v.token}
                          class="btn btn-xs h-auto py-0.5 flex-col gap-0 leading-tight {v.relevant ? (v.composite ? 'btn-accent' : 'btn-primary btn-outline') : 'btn-ghost border border-base-300 opacity-70'}"
                          onclick={() => insertAtCursor(v.token)}
                        >
                          <span class="text-[11px]">{v.relevant ? "★ " : ""}{$_(`demo.agent.var.${v.key}`)}</span>
                          <span class="text-[9px] font-mono opacity-70">{v.token}</span>
                        </button>
                      {/each}
                    </div>
                    <div class="text-[10px] opacity-50 px-2 pb-1">{$_("demo.agent.pickFieldHint")}</div>
                  </details>
                {/if}

                <!-- 1つの大きなエディタ（Notion風・入れ子対応）。最上位の並びの差し替えは setTop。 -->
                <div role="list" class="rounded-lg border border-base-300 bg-base-100 p-1 flex flex-col">
                  {@render blockEditor(editBlocks[selectedRequest] ?? [], setTop)}
                  {#if (editBlocks[selectedRequest] ?? []).length === 0}
                    <div class="px-2 py-3 text-xs opacity-40">{$_("demo.agent.emptyBlocks")}</div>
                  {/if}
                </div>
                <!-- 末尾に追加（空のときもここから。位置は行頭＋/↑↓/ドラッグで調整） -->
                <div class="flex flex-wrap items-center gap-2">
                  <button type="button" class="btn btn-outline btn-xs" onclick={() => opAppend(editBlocks[selectedRequest] ?? [], makeText(), setTop)}>{$_("demo.agent.addText")}</button>
                  <button type="button" class="btn btn-outline btn-accent btn-xs" onclick={() => opAppend(editBlocks[selectedRequest] ?? [], makeCond(), setTop)}>{$_("demo.agent.addCond")}</button>
                  <span class="text-[11px] opacity-50 ml-auto">{$_("demo.agent.chars", { values: { count: (currentPrompts[selectedRequest] ?? "").length, max: MY_AGENT_MAX_CHARS } })}</span>
                </div>
                <p class="text-[10px] opacity-40">{$_("demo.agent.splitHint")}</p>

                <!-- 操作 -->
                <div class="flex flex-wrap gap-2">
                  <button class="btn btn-primary btn-sm grow" onclick={saveAgent}>{agentSaved ? $_("demo.agent.saved") : $_("demo.agent.save")}</button>
                  <button class="btn btn-outline btn-sm" onclick={previewAgent}>{$_("demo.agent.preview")}</button>
                  <button class="btn btn-ghost btn-sm" onclick={resetRequest}>{$_("demo.agent.resetReq")}</button>
                </div>
                {#if previewText !== null}
                  <div class="text-xs">
                    <div class="font-bold opacity-70 mb-1">{$_("demo.agent.previewTitle")}</div>
                    <pre class="bg-base-200 rounded p-2 whitespace-pre-wrap break-words max-h-48 overflow-y-auto text-[11px]">{previewText || $_("demo.agent.empty")}</pre>
                  </div>
                {/if}
              </div>
            </div>
            <div class="text-center mt-3">
              <button class="btn btn-ghost btn-sm" onclick={gotoPlayTab}>← {$_("demo.tab.play")}</button>
            </div>
          </div>
        {:else if screen === "solo"}
          <!-- ② ソロ: 村サイズ + 役職/キャラ + 開始 -->
          <div class="flex flex-col items-center gap-2">
            <div class="text-sm font-bold opacity-70">{$_("demo.start.villageSize")}</div>
            <div class="join">
              <button class="join-item btn {villageSize === 5 ? 'btn-primary' : 'btn-outline'}" onclick={() => (villageSize = 5)}>{$_("demo.start.village5")}</button>
              <button class="join-item btn {villageSize === 9 ? 'btn-primary' : 'btn-outline'}" onclick={() => (villageSize = 9)}>{$_("demo.start.village9")}</button>
            </div>
            <div class="text-xs opacity-60 max-w-xs text-center">
              {villageSize === 5 ? $_("demo.start.comp5") : $_("demo.start.comp9")}
            </div>
          </div>
          <div class="flex flex-col items-center gap-2">
            <div class="text-sm font-bold opacity-70">{$_("demo.start.role")}</div>
            <select class="select select-bordered select-sm" bind:value={selectedRole}>
              <option value="">{$_("demo.start.random")}</option>
              {#each roleChoices as r}<option value={r}>{$_(`game.role.${r}`)}</option>{/each}
            </select>
          </div>
          <div class="flex flex-col items-center gap-2">
            <div class="text-sm font-bold opacity-70">{$_("demo.start.character")}</div>
            <div class="flex items-center gap-2">
              {#if selectedCharacterAvatar}
                <div class="avatar"><div class="w-8 rounded-full"><img src={`${base}${selectedCharacterAvatar}`} alt="" /></div></div>
              {/if}
              <select class="select select-bordered select-sm" bind:value={selectedCharacter}>
                <option value={-1}>{$_("demo.start.random")}</option>
                {#each characters as c}<option value={c.index}>{c.name}</option>{/each}
              </select>
            </div>
          </div>
          <!-- 自作AIをAI席に何体使うか（残りはサンプル）。あなた1席を除く size-1 が上限。 -->
          {@render customAiSlider(villageSize - 1)}
          <button class="btn btn-primary btn-lg" onclick={startSolo}>{$_("demo.start.start")}</button>
          <button class="btn btn-ghost btn-sm" onclick={() => (screen = "mode")}>← {$_("demo.mode.back")}</button>
        {:else if screen === "multiCreate"}
          <!-- ③ マルチ: 部屋を作る（村サイズ + 人数スライダー） -->
          <div class="text-lg font-bold">{$_("demo.multi.create")}</div>
          <div class="flex flex-col items-center gap-2">
            <div class="text-sm font-bold opacity-70">{$_("demo.start.villageSize")}</div>
            <div class="join">
              <button class="join-item btn {villageSize === 5 ? 'btn-primary' : 'btn-outline'}" onclick={() => (villageSize = 5)}>{$_("demo.start.village5")}</button>
              <button class="join-item btn {villageSize === 9 ? 'btn-primary' : 'btn-outline'}" onclick={() => (villageSize = 9)}>{$_("demo.start.village9")}</button>
            </div>
          </div>
          <div class="flex flex-col items-center gap-1 w-full max-w-xs">
            <div class="text-sm font-bold opacity-70">{$_("demo.multi.humans")}: <span class="text-primary">{humanSlots}</span> / {villageSize}</div>
            <input type="range" min="1" max={villageSize} bind:value={humanSlots} class="range range-primary range-sm w-full" />
            <div class="text-xs opacity-60">{$_("demo.multi.aiFill", { values: { count: villageSize - humanSlots } })}</div>
          </div>
          {@render takeoverToggle()}
          <button class="btn btn-primary btn-lg" onclick={createRoom}>{$_("demo.multi.createBtn")}</button>
          <button class="btn btn-ghost btn-sm" onclick={() => (screen = "mode")}>← {$_("demo.mode.back")}</button>
        {:else if screen === "multiJoin"}
          <!-- ④ マルチ: 合言葉で参加 -->
          <div class="text-lg font-bold">{$_("demo.multi.join")}</div>
          <input
            class="input input-bordered input-lg text-center tracking-widest uppercase w-48 font-mono"
            placeholder={$_("demo.multi.enterCode")}
            maxlength="8"
            bind:value={joinCodeInput}
            oninput={(e) => (joinCodeInput = (e.target as HTMLInputElement).value.toUpperCase())}
          />
          {@render takeoverToggle()}
          {#if lobbyError}<div class="alert alert-error py-2"><span>{lobbyError}</span></div>{/if}
          <button class="btn btn-primary btn-lg" disabled={!joinCodeInput.trim()} onclick={joinRoom}>{$_("demo.multi.joinBtn")}</button>
          <button class="btn btn-ghost btn-sm" onclick={() => { lobbyError = null; screen = "mode"; }}>← {$_("demo.mode.back")}</button>
        {:else if screen === "waiting"}
          <!-- ⑤ 待機部屋（合言葉・参加者・開始） -->
          <div class="text-sm font-bold opacity-70">{$_("demo.multi.code")}</div>
          <button class="flex items-center gap-2 btn btn-ghost" onclick={copyCode} aria-label={$_("demo.multi.copy")}>
            <span class="text-3xl font-bold font-mono tracking-widest">{roomCode}</span>
            <iconify-icon icon={codeCopied ? "mdi:check" : "mdi:content-copy"} class="text-lg opacity-70"></iconify-icon>
          </button>
          <div class="text-xs opacity-60 max-w-xs">{codeCopied ? $_("demo.multi.copied") : $_("demo.multi.codeHint")}</div>

          <div class="w-full max-w-xs">
            <div class="text-sm font-bold opacity-70 mb-1">
              {$_("demo.multi.participants", { values: { count: roomParticipants.length, max: humanSlots } })}
            </div>
            <div class="flex flex-col gap-1">
              {#each roomParticipants as p}
                <div class="flex items-center gap-2 p-1.5 rounded bg-base-200">
                  <iconify-icon icon="mdi:account" class="opacity-70"></iconify-icon>
                  <span class="text-sm font-bold">{p.display_name}{p.display_name === displayName ? `（${$_("demo.multi.you2")}）` : ""}</span>
                  {#if p.is_host}<span class="ml-auto badge badge-sm badge-primary">{$_("demo.multi.host")}</span>{/if}
                </div>
              {/each}
            </div>
          </div>

          {#if isHost}
            <button class="btn btn-primary btn-lg" onclick={startMultiGame}>{$_("demo.multi.startBtn")}</button>
          {:else}
            <span class="loading loading-dots loading-md"></span>
            <div class="text-sm opacity-70">{$_("demo.multi.waitHost")}</div>
          {/if}
          {#if lobbyError}<div class="alert alert-error py-2"><span>{lobbyError}</span></div>{/if}
          <button class="btn btn-ghost btn-sm" onclick={leaveRoom}>{$_("demo.multi.leave")}</button>
        {/if}
      {:else if lobbyPhase === "joining"}
        <span class="loading loading-spinner loading-lg"></span>
        <div>{$_("demo.start.joining")}</div>
      {:else if lobbyPhase === "queued"}
        <span class="loading loading-dots loading-lg"></span>
        <div class="text-lg font-bold">{$_("demo.start.queued")}</div>
        <div>{$_("demo.start.queuePosition", { values: { pos: queuePos } })}</div>
        <div class="text-xs opacity-60">{$_("demo.start.queueNote")}</div>
      {:else if lobbyPhase === "starting"}
        <span class="loading loading-spinner loading-lg"></span>
        <div>{$_("demo.start.preparing")}</div>
      {:else if lobbyPhase === "error"}
        <div class="alert alert-error">
          <span>{$_("demo.start.error", { values: { message: lobbyError ?? $_("demo.start.unknownError") } })}</span>
        </div>
        <button class="btn" onclick={() => { lobbyPhase = "idle"; lobbyError = null; screen = "mode"; }}>{$_("demo.start.retry")}</button>
      {/if}

      {#if displayName}
        <div class="text-xs opacity-50">{$_("demo.start.displayName", { values: { name: displayName } })}</div>
      {/if}
    </div>
  {:else}
  <!-- LINE風 逐次ストリーム -->
  <div class="grow overflow-y-auto p-4 flex flex-col gap-1" bind:this={streamEl}>
    {#if feed.length === 0}
      <div class="m-auto text-center opacity-50">
        {#if status === "connected"}
          {$_("demo.feed.waitingStart")}
        {:else}
          {$_("demo.feed.waitingConnect")}
        {/if}
      </div>
    {/if}
    {#each feed as entry, i (i)}
      {#if entry.kind === "system"}
        <!-- アナウンス（日付/夜/投票/結果/占い）。i18nキー＋params を描画時に翻訳。
             name は原名なのでローカライズ、species は game.species で翻訳。-->
        <div class="my-1 text-center">
          <span class="badge badge-sm
            {entry.tone === 'day' ? 'badge-warning' : entry.tone === 'night' ? 'badge-neutral' : entry.tone === 'vote' ? 'badge-info' : entry.tone === 'result' ? 'badge-error' : 'badge-ghost'}
            whitespace-normal h-auto py-1">{$_(entry.i18nKey, { values: { day: entry.day, name: nameOf(entry.name), species: speciesName(entry.species) } })}</span>
        </div>
      {:else}
        {@const talk = entry.talk}
        {@const mine = talk.agent === agent}
        <div class="chat {mine ? 'chat-end' : 'chat-start'}">
          {#if !mine}
            <div class="chat-image avatar">
              <div class="w-8 rounded-full">
                <img src={avatarSrc(talk.agent)} alt={nameOf(talk.agent)} />
              </div>
            </div>
          {/if}
          <div class="chat-header text-xs opacity-70">{nameOf(talk.agent)}</div>
          {#if talk.over}
            <div class="chat-bubble chat-bubble-neutral text-sm opacity-70">{$_("demo.feed.talkOver")}</div>
          {:else if talk.skip}
            <div class="chat-bubble chat-bubble-neutral text-sm opacity-70">{$_("demo.feed.talkSkip")}</div>
          {:else}
            <div class="chat-bubble {mine ? 'chat-bubble-primary' : ''} break-words">{talk.text}</div>
          {/if}
        </div>
      {/if}
    {/each}

    <!-- 他者が入力中インジケータ -->
    {#if status === "connected" && currentTurnAgent && currentTurnAgent !== agent && !isMyTurn}
      <div class="chat chat-start">
        <div class="chat-image avatar">
          <div class="w-8 rounded-full"><img src={avatarSrc(currentTurnAgent)} alt={nameOf(currentTurnAgent)} /></div>
        </div>
        <div class="chat-bubble"><span class="loading loading-dots loading-sm"></span></div>
      </div>
    {/if}
  </div>

  <!-- 入力エリア：自分のターンだけ enable（HANDOFF §5-4 誤送信防止）-->
  <footer class="flex-none bg-base-200 p-3">
    {#if isSpectate}
      <div class="flex items-center justify-center gap-2 py-2 text-sm opacity-70">
        <span class="loading loading-dots loading-sm"></span>
        {$_("demo.spectate.watching")}
      </div>
    {:else if isMyTurn && effectivePaused}
      <div class="flex items-center justify-center gap-3 py-2">
        <span class="text-sm opacity-70">{$_("demo.footer.pausedYourTurn", { values: { action: actionName } })}</span>
        <button class="btn btn-sm btn-success" onclick={() => (paused = false)}>
          <iconify-icon icon="mdi:play"></iconify-icon>{$_("demo.footer.resumeInput")}
        </button>
      </div>
    {:else if isSelection}
      <!-- 投票/占い/護衛/襲撃：生存対象ボタン -->
      <div class="text-xs font-bold opacity-70 mb-1">{$_("demo.footer.actionHint", { values: { action: actionName, hint: actionHint } })}</div>
      <div class="flex flex-wrap gap-2">
        {#each aliveTargets as t}
          <!-- 表示はローカライズ名、送信は原名(t) のまま（サーバはゲーム内名で判定する） -->
          <button class="btn btn-sm" disabled={!canAct} onclick={() => sendValue(t)}>{nameOf(t)}</button>
        {/each}
        {#if request === Request.ATTACK && setting?.attack_vote?.allow_no_target}
          <button class="btn btn-sm btn-ghost" disabled={!canAct} onclick={() => sendValue("")}>{$_("demo.footer.noTarget")}</button>
        {/if}
      </div>
    {:else}
      <div class="flex items-end gap-2">
        <div class="flex gap-1">
          {#if isTalk && (info?.remain_skip ?? 0) > 0}
            <button class="btn btn-sm" disabled={!canAct} onclick={() => sendValue("Skip")}>{$_("demo.footer.skip")}</button>
          {/if}
          {#if isTalk}
            <button class="btn btn-sm" disabled={!canAct} onclick={() => sendValue("Over")}>{$_("demo.footer.over")}</button>
          {/if}
        </div>
        <textarea
          class="textarea textarea-bordered grow resize-none"
          rows="1"
          placeholder={isTalk ? $_("demo.footer.talkPlaceholder") : $_("demo.footer.lockedPlaceholder")}
          bind:value={message}
          onkeydown={onKeydown}
          disabled={!canAct || !isTalk}
        ></textarea>
        <button class="btn btn-primary" disabled={!canAct || !isTalk || message.trim() === ""} onclick={handleSend}>
          {$_("demo.footer.send")}
        </button>
      </div>
      <p class="text-[11px] opacity-60 mt-1 px-1">
        {$_("demo.footer.help")}
      </p>
    {/if}
  </footer>
  {/if}
</main>
