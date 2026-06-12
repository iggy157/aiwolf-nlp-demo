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
  import { DefaultProfileAvatars, Request, type Talk } from "$lib/types/agent";
  import { demoSocketState } from "$lib/utils/demo-socket";
  import { onDestroy } from "svelte";
  import "../../app.css";

  // ---- socket state（demo-socket の writable を購読）----
  let status = $state("disconnected");
  let agent = $state<string | null>(null);
  let request = $state<Request | null>(null);
  let info = $state<ReturnType<() => any> | null>(null);
  let talkHistory = $state<Talk[]>([]);
  let currentTurnAgent = $state<string | null>(null);
  let deadline = $state<number | null>(null);
  let setting = $state<any>(null);

  let remain = $state<number | null>(null);
  let rafId: number | null = null;

  const unsub = demoSocketState.subscribe((s) => {
    status = s.status;
    agent = s.agent;
    request = s.request;
    info = s.info;
    talkHistory = s.talkHistory;
    currentTurnAgent = s.currentTurnAgent;
    setting = s.setting;
    if (s.deadline) {
      deadline = s.deadline.getTime();
      startCountdown();
    } else {
      deadline = null;
      remain = null;
      stopCountdown();
    }
  });

  // ---- 入力可否の判定 ----
  const SELECTION_REQUESTS = [
    Request.VOTE,
    Request.DIVINE,
    Request.GUARD,
    Request.ATTACK,
  ];
  // 自分の live なリクエストが pending（=deadline 有り）のときだけ送信可（誤送信防止：HANDOFF §5-4）
  const isMyTurn = $derived(deadline !== null && request !== null);
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
    if (isMyTurn) {
      if (isSelection) return "あなたの番です（対象を選んでください）";
      return "あなたの番です";
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

  function avatarSrc(name: string): string {
    const path = DefaultProfileAvatars[name as keyof typeof DefaultProfileAvatars];
    return path ? `${base}${path}` : "";
  }

  function handleSend() {
    if (!isMyTurn) return;
    const text = message.trim();
    if (!text) return;
    demoSocketState.send(text);
    message = "";
  }

  function sendValue(v: string) {
    if (!isMyTurn) return;
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

  async function startViaLobby() {
    lobbyPhase = "joining";
    lobbyError = null;
    try {
      const res = await fetch(`${lobbyBase}/api/join`, { method: "POST" });
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
    });
  }

  // ゲーム開始前のスタート画面を出すか
  const showStartScreen = $derived(
    !directMode && status !== "connected" && lobbyPhase !== "playing",
  );
</script>

<svelte:head><title>AI人狼 体験デモ</title></svelte:head>

<main class="h-dvh flex flex-col bg-base-300">
  <!-- ヘッダ：自分の情報＋状態バナー -->
  <header class="flex-none bg-base-100 px-4 py-2 flex items-center gap-3 shadow">
    <div class="flex items-center gap-2 min-w-0">
      {#if agent}
        <div class="avatar">
          <div class="w-9 rounded-full">
            <img src={avatarSrc(agent)} alt={agent} />
          </div>
        </div>
        <div class="leading-tight min-w-0">
          <div class="font-bold truncate">{agent}</div>
          <div class="text-xs opacity-60">
            {info ? `${info.day}日目` : ""}
          </div>
        </div>
      {:else}
        <div class="font-bold">AI人狼 体験デモ</div>
      {/if}
    </div>
    <div class="ml-auto flex items-center gap-2">
      <span class="badge {status === 'connected' ? 'badge-success' : status === 'connecting' ? 'badge-warning' : 'badge-error'}">
        {status === "connected" ? "接続中" : status === "connecting" ? "接続中…" : "未接続"}
      </span>
    </div>
  </header>

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
      <h1 class="text-2xl font-bold">AIと人狼で対戦</h1>
      <p class="opacity-70 text-sm max-w-xs">
        4体のAIエージェントと5人で人狼ゲーム。あなたの番になったら発言できます。
      </p>

      {#if lobbyPhase === "idle"}
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
  <div class="grow overflow-y-auto p-4 flex flex-col gap-1">
    {#if talkHistory.length === 0}
      <div class="m-auto text-center opacity-50">
        {#if status === "connected"}
          ゲームの開始を待っています…
        {:else}
          接続待ち。QRリンク（?url=…&team=…）から開いてください。
        {/if}
      </div>
    {/if}
    {#each talkHistory as talk (talk.idx)}
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
    {#if isSelection}
      <!-- 投票/占い/護衛/襲撃：生存対象ボタン -->
      <div class="flex flex-wrap gap-2">
        {#each aliveTargets as t}
          <button class="btn btn-sm" onclick={() => sendValue(t)}>{t}</button>
        {/each}
        {#if request === Request.ATTACK && setting?.attack_vote?.allow_no_target}
          <button class="btn btn-sm btn-ghost" onclick={() => sendValue("")}>対象なし</button>
        {/if}
      </div>
    {:else}
      <div class="flex items-end gap-2">
        <div class="flex gap-1">
          {#if isTalk && (info?.remain_skip ?? 0) > 0}
            <button class="btn btn-sm" disabled={!isMyTurn} onclick={() => sendValue("Skip")}>スキップ</button>
          {/if}
          {#if isTalk}
            <button class="btn btn-sm" disabled={!isMyTurn} onclick={() => sendValue("Over")}>終了</button>
          {/if}
        </div>
        <textarea
          class="textarea textarea-bordered grow resize-none"
          rows="1"
          placeholder={isMyTurn ? "メッセージを入力（Enterで送信）" : "あなたの番になると入力できます"}
          bind:value={message}
          onkeydown={onKeydown}
          disabled={!isTalk}
        ></textarea>
        <button class="btn btn-primary" disabled={!isTalk || message.trim() === ""} onclick={handleSend}>
          送信
        </button>
      </div>
    {/if}
  </footer>
  {/if}
</main>
