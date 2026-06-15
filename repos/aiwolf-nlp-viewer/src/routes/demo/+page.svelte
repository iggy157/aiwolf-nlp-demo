<script lang="ts">
  // aiwolf-nlp-demo: QR着地のプレイヤー視点UI（新ルート /demo）
  // 既存 /agent は不変。WebSocketロジックは demo-socket.ts（agent-socket のコピー＋拡張）を再利用。
  // M4 時点: 接続・LINE風逐次表示・ターンに応じた入力ロックの最小実装。
  //   - 採番/開始ボタン/キュー は M6（lobby）で前段に載せる。
  //   - サーバ側の逐次push＋ターンマーカー は M5。本UIは未送出でも request ベースで動作する。
  import { browser } from "$app/environment";
  import { base } from "$app/paths";
  import { page } from "$app/state";
  import { Status } from "$lib/constants/common";
  import { agentSettings } from "$lib/stores/agent-settings";
  import { DefaultProfileAvatars, Request } from "$lib/types/agent";
  import { demoSocketState, type FeedEntry } from "$lib/utils/demo-socket";
  import { onDestroy } from "svelte";
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

  // 役職の日本語表示
  const ROLE_JP: Record<string, string> = {
    VILLAGER: "村人",
    WEREWOLF: "人狼",
    SEER: "占い師",
    MEDIUM: "霊媒師",
    BODYGUARD: "騎士",
    POSSESSED: "狂人",
  };
  const roleJp = (r: string | null | undefined) => (r ? (ROLE_JP[r] ?? r) : "—");
  const speciesJp = (s: string | null | undefined) =>
    s === "WEREWOLF" ? "人狼" : s === "HUMAN" ? "人間" : (s ?? "—");
  // 役職の一言説明（開始ポップアップ用）
  const ROLE_DESC: Record<string, string> = {
    VILLAGER: "特殊能力はありません。議論と投票で人狼を見つけ出してください。",
    WEREWOLF: "あなたは人狼です。正体を隠し、夜は仲間と襲撃します。村人を欺いてください。",
    SEER: "毎晩1人を占い、人狼か人間かを知れます。情報を活かして村を導いてください。",
    MEDIUM: "追放された人が人狼だったか毎晩わかります。",
    BODYGUARD: "毎晩1人を護衛し、人狼の襲撃から守れます。",
    POSSESSED: "あなたは狂人（人間陣営に見えるが人狼の味方）。人狼の勝利に協力してください。",
  };

  let infoOpen = $state(false); // プロフィール/役職プレビューの開閉
  let introOpen = $state(false); // ゲーム開始時の説明ポップアップ
  let introAck = $state(false); // 説明を確認済みか

  // 勝敗の推定（FINISH時の役職開示＋生存状況から）
  const winnerText = $derived.by(() => {
    if (!finished || !info?.role_map || !info?.status_map) return null;
    let aliveWolf = 0;
    let aliveHuman = 0;
    for (const [name, st] of Object.entries(info.status_map as Record<string, string>)) {
      if (st !== "ALIVE") continue;
      if ((info.role_map as Record<string, string>)[name] === "WEREWOLF") aliveWolf++;
      else aliveHuman++;
    }
    return aliveWolf === 0 ? "村人陣営の勝利" : "人狼陣営の勝利";
  });
  const myResult = $derived.by(() => {
    if (!finished || !role || !winnerText) return null;
    const myCamp = role === "WEREWOLF" || role === "POSSESSED" ? "人狼陣営" : "村人陣営";
    return winnerText.startsWith(myCamp) ? "あなたの勝ち 🎉" : "あなたの負け…";
  });

  // ---- 入力可否の判定 ----
  const SELECTION_REQUESTS = [
    Request.VOTE,
    Request.DIVINE,
    Request.GUARD,
    Request.ATTACK,
  ];
  // 求められているアクション名
  const ACTION_JP: Record<string, string> = {
    TALK: "発言",
    WHISPER: "囁き",
    VOTE: "投票",
    DIVINE: "占い",
    GUARD: "護衛",
    ATTACK: "襲撃",
  };
  const actionName = $derived(ACTION_JP[request as string] ?? "アクション");
  const actionHint = $derived(
    request === Request.TALK || request === Request.WHISPER
      ? "メッセージを入力"
      : request === Request.VOTE
        ? "追放する相手を選択"
        : request === Request.DIVINE
          ? "占う相手を選択"
          : request === Request.GUARD
            ? "護衛する相手を選択"
            : request === Request.ATTACK
              ? "襲撃する相手を選択"
              : "対象を選択",
  );

  // 一時停止（開始ポップアップ表示中も実質停止扱い）
  let paused = $state(false);
  const effectivePaused = $derived(paused || introOpen);

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
      return status === "connecting" ? "接続中…" : "未接続";
    }
    if (isMyTurn && effectivePaused) {
      return `一時停止中（あなたの番：${actionName}）`;
    }
    if (isMyTurn) {
      return `あなたの番です：${actionName}（${actionHint}）`;
    }
    if (currentTurnAgent && currentTurnAgent !== agent) {
      return `${currentTurnAgent} さんが入力中…`;
    }
    // 自分のターンでも他者のターンでもない＝集計/夜など
    switch (request) {
      case Request.VOTE:
        return "投票中…";
      case Request.DIVINE:
      case Request.GUARD:
      case Request.ATTACK:
        return "夜のアクション中…";
      case Request.DAILY_INITIALIZE:
        return "朝になりました";
      case Request.DAILY_FINISH:
        return "夜になりました";
      case Request.FINISH:
        return "ゲーム終了";
      default:
        return info ? "進行中…" : "ゲーム開始を待っています";
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
    const path = DefaultProfileAvatars[name as keyof typeof DefaultProfileAvatars];
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

  async function startViaLobby() {
    lobbyPhase = "joining";
    lobbyError = null;
    introAck = false;
    introOpen = false;
    try {
      const res = await fetch(`${lobbyBase}/api/join`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ size: villageSize }),
      });
      if (!res.ok) throw new Error(`join failed: ${res.status}`);
      const data = await res.json();
      sessionId = data.session_id;
      displayName = data.display_name;
      // 人間枠の接続設定（チーム名＝一意セッションチーム）
      agentSettings.update((value) => ({
        ...value,
        connection: { url: data.ws_url, token: "" },
        team: data.team,
      }));
      applyLobbyStatus(data.status, data.position);
      pollSession();
    } catch (e) {
      lobbyPhase = "error";
      lobbyError = e instanceof Error ? e.message : String(e);
    }
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
      lobbyError = "セッションの起動に失敗しました";
    }
  }

  async function pollSession() {
    if (!sessionId) return;
    try {
      const res = await fetch(`${lobbyBase}/api/session/${sessionId}`);
      if (res.ok) {
        const data = await res.json();
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
    if (confirmFirst && !confirm("ゲームを中断してホームに戻りますか？（この卓は終了します）")) {
      return;
    }
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (sessionId) {
      // スロット解放を lobby に通知（AIプロセスも停止される）
      fetch(`${lobbyBase}/api/session/${sessionId}/leave`, { method: "POST" }).catch(() => {});
    }
    demoSocketState.reset();
    infoOpen = false;
    introOpen = false;
    introAck = false;
    paused = false;
    sessionId = null;
    displayName = null;
    queuePos = 0;
    lobbyError = null;
    lobbyPhase = "idle";
  }

  // ---- 接続（?url= 直接接続を優先。無ければロビー画面）----
  if (browser) {
    const params = page.url.searchParams;
    const url = params.get("url");
    const token = params.get("token");
    const team = params.get("team");
    const lobbyParam = params.get("lobby");
    if (lobbyParam) lobbyBase = lobbyParam.replace(/\/$/, "");

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
      // 離脱を lobby に通知（スロット解放）
      if (sessionId) {
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

<svelte:head><title>人狼知能大会 自然言語部門 体験デモ</title></svelte:head>

<main class="h-dvh flex flex-col bg-base-300">
  <!-- ヘッダ：タイトル＋自分の情報＋操作ボタン -->
  <header class="flex-none bg-base-100 px-3 py-2 flex items-center gap-2 shadow">
    <div class="flex items-center gap-2 min-w-0">
      {#if agent}
        <button class="avatar" onclick={() => (infoOpen = true)} aria-label="自分の情報">
          <div class="w-9 rounded-full ring ring-primary ring-offset-1">
            <img src={avatarSrc(agent)} alt={agent} />
          </div>
        </button>
        <div class="leading-tight min-w-0">
          <div class="font-bold truncate">{agent}<span class="ml-1 text-xs opacity-70">({roleJp(role)})</span></div>
          <div class="text-xs opacity-60">{info ? `${info.day}日目` : ""}</div>
        </div>
      {:else}
        <div class="font-bold text-sm leading-tight">人狼知能大会<br />自然言語部門 体験デモ</div>
      {/if}
    </div>
    <div class="ml-auto flex items-center gap-1.5">
      <span class="badge badge-sm {status === 'connected' ? 'badge-success' : status === 'connecting' ? 'badge-warning' : 'badge-error'}">
        {status === "connected" ? "接続中" : status === "connecting" ? "接続中…" : "未接続"}
      </span>
      {#if status === "connected" && !finished}
        <button
          class="btn btn-xs {paused ? 'btn-success' : 'btn-ghost'}"
          onclick={() => (paused = !paused)}
          aria-label={paused ? "再開" : "一時停止"}
        >
          <iconify-icon icon={paused ? "mdi:play" : "mdi:pause"}></iconify-icon>
          {paused ? "再開" : "一時停止"}
        </button>
        <button class="btn btn-xs btn-ghost" onclick={() => (infoOpen = true)} aria-label="情報">
          <iconify-icon icon="mdi:information-outline"></iconify-icon>情報
        </button>
        <button class="btn btn-xs btn-error btn-outline" onclick={() => leaveGame()} aria-label="中断">
          <iconify-icon icon="mdi:home"></iconify-icon>中断
        </button>
      {/if}
    </div>
  </header>

  <!-- プロフィール/役職プレビュー（ドロワー風モーダル）-->
  {#if infoOpen}
    <div class="fixed inset-0 z-50 flex">
      <button class="absolute inset-0 bg-black/50" onclick={() => (infoOpen = false)} aria-label="閉じる"></button>
      <div class="relative ml-auto h-full w-80 max-w-[85vw] bg-base-100 shadow-xl overflow-y-auto p-4 flex flex-col gap-4">
        <div class="flex items-center justify-between">
          <h2 class="font-bold text-lg">プレイヤー情報</h2>
          <button class="btn btn-sm btn-circle btn-ghost" onclick={() => (infoOpen = false)}>✕</button>
        </div>

        <!-- 自分 -->
        <div class="card bg-base-200 p-3">
          <div class="flex items-center gap-3">
            <div class="avatar"><div class="w-14 rounded-full"><img src={avatarSrc(agent ?? "")} alt={agent} /></div></div>
            <div>
              <div class="font-bold">{agent ?? "—"}</div>
              <div class="badge badge-primary badge-sm">役職: {roleJp(role)}</div>
              {#if team}
                <div class="text-xs opacity-60 mt-1">接続チーム: {team}</div>
              {/if}
            </div>
          </div>
          {#if profile}
            <div class="mt-2 text-sm whitespace-pre-wrap opacity-80">{profile}</div>
          {:else}
            <div class="mt-2 text-xs opacity-50">プロフィール情報なし</div>
          {/if}
        </div>

        <!-- 参加者一覧 -->
        <div>
          <h3 class="font-bold mb-2">参加者</h3>
          <div class="flex flex-col gap-1">
            {#each Object.entries(info?.status_map ?? {}) as [name, st]}
              {@const known = info?.role_map?.[name]}
              <div class="flex items-center gap-2 p-1.5 rounded {st === Status.ALIVE ? 'bg-base-200' : 'bg-base-300 opacity-60'}">
                <div class="avatar"><div class="w-8 rounded-full"><img src={avatarSrc(name)} alt={name} /></div></div>
                <span class="font-bold text-sm">{name}{name === agent ? "（あなた）" : ""}</span>
                {#if known}<span class="badge badge-xs">{roleJp(known)}</span>{/if}
                <span class="ml-auto badge badge-xs {st === Status.ALIVE ? 'badge-success' : 'badge-error'}">
                  {st === Status.ALIVE ? "生存" : "死亡"}
                </span>
              </div>
            {/each}
          </div>
          <p class="text-xs opacity-50 mt-2">※ 他プレイヤーの役職・プロフィールは人狼ゲームの性質上、原則非公開です。</p>
        </div>

        <!-- 占い結果（占い師のみ届く。target は人狼/人間が判明）-->
        {#if divineResults.length > 0}
          <div>
            <h3 class="font-bold mb-2">🔮 占い結果</h3>
            <div class="flex flex-col gap-1">
              {#each divineResults as j}
                <div class="flex items-center gap-2 p-1.5 rounded bg-base-200">
                  <span class="text-xs opacity-60">{j.day}日目</span>
                  <span class="font-bold text-sm">{j.target}</span>
                  <span class="ml-auto badge badge-sm {j.result === 'WEREWOLF' ? 'badge-error' : 'badge-success'}">
                    {speciesJp(j.result)}
                  </span>
                </div>
              {/each}
            </div>
          </div>
        {/if}
        <!-- 霊媒結果（霊媒師のみ）-->
        {#if mediumResults.length > 0}
          <div>
            <h3 class="font-bold mb-2">🔮 霊媒結果</h3>
            <div class="flex flex-col gap-1">
              {#each mediumResults as j}
                <div class="flex items-center gap-2 p-1.5 rounded bg-base-200">
                  <span class="text-xs opacity-60">{j.day}日目</span>
                  <span class="font-bold text-sm">{j.target}</span>
                  <span class="ml-auto badge badge-sm {j.result === 'WEREWOLF' ? 'badge-error' : 'badge-success'}">
                    {speciesJp(j.result)}
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
        <div class="avatar"><div class="w-24 rounded-full ring ring-primary"><img src={avatarSrc(agent)} alt={agent} /></div></div>
        <div class="text-sm opacity-70">あなたが担当するキャラクター</div>
        <div class="text-2xl font-bold">{agent}</div>
        <div class="badge badge-primary badge-lg">役職: {roleJp(role)}</div>
        <p class="text-sm opacity-80">{role ? (ROLE_DESC[role] ?? "") : ""}</p>
        {#if profile}
          <div class="text-xs whitespace-pre-wrap opacity-70 bg-base-200 rounded p-2 max-h-32 overflow-y-auto">{profile}</div>
        {/if}
        <p class="text-sm font-bold mt-1">あなたはこのキャラクター・役職としてゲーム内で振る舞ってください。</p>
        <button class="btn btn-primary btn-block" onclick={startGame}>
          ゲームを開始する
        </button>
      </div>
    </div>
  {/if}

  <!-- ゲーム終了結果画面 -->
  {#if finished}
    <div class="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/70">
      <div class="card bg-base-100 w-full max-w-sm p-5 flex flex-col gap-3 max-h-[90vh] overflow-y-auto">
        <h2 class="text-2xl font-bold text-center">🏁 ゲーム終了</h2>
        {#if winnerText}
          <div class="text-center text-lg font-bold">{winnerText}</div>
        {/if}
        {#if myResult}
          <div class="text-center text-xl font-bold {myResult.includes('勝ち') ? 'text-success' : 'text-error'}">{myResult}</div>
        {/if}
        <div class="divider my-1">役職の開示</div>
        <div class="flex flex-col gap-1">
          {#each Object.entries(info?.role_map ?? {}) as [name, r]}
            {@const rs = r as string}
            {@const alive = (info?.status_map ?? {})[name] === Status.ALIVE}
            <div class="flex items-center gap-2 p-1.5 rounded {alive ? 'bg-base-200' : 'bg-base-300 opacity-70'}">
              <div class="avatar"><div class="w-8 rounded-full"><img src={avatarSrc(name)} alt={name} /></div></div>
              <span class="font-bold text-sm">{name}{name === agent ? "（あなた）" : ""}</span>
              <span class="badge badge-sm {rs === 'WEREWOLF' ? 'badge-error' : ''}">{roleJp(rs)}</span>
              <span class="ml-auto text-xs opacity-60">{alive ? "生存" : "死亡"}</span>
            </div>
          {/each}
        </div>
        <button class="btn btn-primary btn-block mt-2" onclick={() => leaveGame(false)}>ホームに戻る</button>
      </div>
    </div>
  {/if}

  <!-- 状態バナー -->
  <div class="flex-none px-4 py-2 text-center font-bold
              {isMyTurn ? 'bg-primary text-primary-content' : 'bg-base-200'}">
    {banner}
    {#if isMyTurn && remainSec !== null}
      <span class="ml-2 font-mono">残り {remainSec}s</span>
    {/if}
  </div>

  {#if showStartScreen}
    <!-- スタート/順番待ち画面（ロビー連携）-->
    <div class="grow flex flex-col items-center justify-center gap-4 p-6 text-center">
      <h1 class="text-xl font-bold">人狼知能大会 自然言語部門 体験デモ</h1>
      <p class="opacity-70 text-sm max-w-xs">
        AIエージェントと人狼ゲーム。あなたの番になったら発言できます。
      </p>

      {#if lobbyPhase === "idle"}
        <!-- 村の人数を選択（最初のページで決定。最小/既定は5）-->
        <div class="flex flex-col items-center gap-2">
          <div class="text-sm font-bold opacity-70">村の人数</div>
          <div class="join">
            <button
              class="join-item btn {villageSize === 5 ? 'btn-primary' : 'btn-outline'}"
              onclick={() => (villageSize = 5)}>5人村</button>
            <button
              class="join-item btn {villageSize === 9 ? 'btn-primary' : 'btn-outline'}"
              onclick={() => (villageSize = 9)}>9人村</button>
          </div>
          <div class="text-xs opacity-60 max-w-xs text-center">
            {#if villageSize === 5}
              村人2・占い師・人狼・狂人（あなた＋AI4体）
            {:else}
              村人3・占い師・騎士・霊媒師・人狼2・狂人（あなた＋AI8体）
            {/if}
          </div>
        </div>
        <button class="btn btn-primary btn-lg" onclick={startViaLobby}>ゲーム開始</button>
      {:else if lobbyPhase === "joining"}
        <span class="loading loading-spinner loading-lg"></span>
        <div>参加を要求しています…</div>
      {:else if lobbyPhase === "queued"}
        <span class="loading loading-dots loading-lg"></span>
        <div class="text-lg font-bold">順番待ち</div>
        <div>あなたは <span class="text-primary font-bold">{queuePos}</span> 番目です</div>
        <div class="text-xs opacity-60">空き卓ができ次第、自動で開始します</div>
      {:else if lobbyPhase === "starting"}
        <span class="loading loading-spinner loading-lg"></span>
        <div>卓を準備しています…</div>
      {:else if lobbyPhase === "error"}
        <div class="alert alert-error">
          <span>エラー: {lobbyError ?? "不明なエラー"}</span>
        </div>
        <button class="btn" onclick={startViaLobby}>再試行</button>
      {/if}

      {#if displayName}
        <div class="text-xs opacity-50">あなたの表示名: {displayName}</div>
      {/if}
    </div>
  {:else}
  <!-- LINE風 逐次ストリーム -->
  <div class="grow overflow-y-auto p-4 flex flex-col gap-1" bind:this={streamEl}>
    {#if feed.length === 0}
      <div class="m-auto text-center opacity-50">
        {#if status === "connected"}
          ゲームの開始を待っています…
        {:else}
          接続待ち。QRリンク（?url=…&team=…）から開いてください。
        {/if}
      </div>
    {/if}
    {#each feed as entry, i (i)}
      {#if entry.kind === "system"}
        <!-- アナウンス（日付/夜/投票/結果/占い）-->
        <div class="my-1 text-center">
          <span class="badge badge-sm
            {entry.tone === 'day' ? 'badge-warning' : entry.tone === 'night' ? 'badge-neutral' : entry.tone === 'vote' ? 'badge-info' : entry.tone === 'result' ? 'badge-error' : 'badge-ghost'}
            whitespace-normal h-auto py-1">{entry.text}</span>
        </div>
      {:else}
        {@const talk = entry.talk}
        {@const mine = talk.agent === agent}
        <div class="chat {mine ? 'chat-end' : 'chat-start'}">
          {#if !mine}
            <div class="chat-image avatar">
              <div class="w-8 rounded-full">
                <img src={avatarSrc(talk.agent)} alt={talk.agent} />
              </div>
            </div>
          {/if}
          <div class="chat-header text-xs opacity-70">{talk.agent}</div>
          {#if talk.over}
            <div class="chat-bubble chat-bubble-neutral text-sm opacity-70">（発言終了）</div>
          {:else if talk.skip}
            <div class="chat-bubble chat-bubble-neutral text-sm opacity-70">（スキップ）</div>
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
          <div class="w-8 rounded-full"><img src={avatarSrc(currentTurnAgent)} alt={currentTurnAgent} /></div>
        </div>
        <div class="chat-bubble"><span class="loading loading-dots loading-sm"></span></div>
      </div>
    {/if}
  </div>

  <!-- 入力エリア：自分のターンだけ enable（HANDOFF §5-4 誤送信防止）-->
  <footer class="flex-none bg-base-200 p-3">
    {#if isMyTurn && effectivePaused}
      <div class="flex items-center justify-center gap-3 py-2">
        <span class="text-sm opacity-70">⏸ 一時停止中（あなたの番：{actionName}）</span>
        <button class="btn btn-sm btn-success" onclick={() => (paused = false)}>
          <iconify-icon icon="mdi:play"></iconify-icon>再開して入力
        </button>
      </div>
    {:else if isSelection}
      <!-- 投票/占い/護衛/襲撃：生存対象ボタン -->
      <div class="text-xs font-bold opacity-70 mb-1">{actionName}：{actionHint}</div>
      <div class="flex flex-wrap gap-2">
        {#each aliveTargets as t}
          <button class="btn btn-sm" disabled={!canAct} onclick={() => sendValue(t)}>{t}</button>
        {/each}
        {#if request === Request.ATTACK && setting?.attack_vote?.allow_no_target}
          <button class="btn btn-sm btn-ghost" disabled={!canAct} onclick={() => sendValue("")}>対象なし</button>
        {/if}
      </div>
    {:else}
      <div class="flex items-end gap-2">
        <div class="flex gap-1">
          {#if isTalk && (info?.remain_skip ?? 0) > 0}
            <button class="btn btn-sm" disabled={!canAct} onclick={() => sendValue("Skip")}>スキップ</button>
          {/if}
          {#if isTalk}
            <button class="btn btn-sm" disabled={!canAct} onclick={() => sendValue("Over")}>終了</button>
          {/if}
        </div>
        <textarea
          class="textarea textarea-bordered grow resize-none"
          rows="1"
          placeholder={isTalk ? "メッセージを入力（Enterで送信）" : "あなたの番になると入力できます"}
          bind:value={message}
          onkeydown={onKeydown}
          disabled={!canAct || !isTalk}
        ></textarea>
        <button class="btn btn-primary" disabled={!canAct || !isTalk || message.trim() === ""} onclick={handleSend}>
          送信
        </button>
      </div>
      <p class="text-[11px] opacity-60 mt-1 px-1">
        💡「終了」=本日の発言を終える ／「スキップ」=今回はパス。
        手入力する場合は大文字始まりの <code>Over</code> / <code>Skip</code>（小文字 over/skip は通常の発言になります）。
      </p>
    {/if}
  </footer>
  {/if}
</main>
