<script lang="ts">
  // aiwolf-nlp-demo: 持ち込みエージェント用ページ（/byo）
  // 卓を作成すると、参加者が自作エージェントを接続するための
  // チーム名・接続URL・設定スニペットを表示する。残り枠はサンプルAIが埋める。
  import { browser } from "$app/environment";
  import { base } from "$app/paths";
  import { page } from "$app/state";
  import { onDestroy } from "svelte";
  import "../../app.css";

  let villageSize = $state(5); // 村の人数（5 or 9）
  let agents = $state(1); // 持ち込みエージェント数
  let human = $state(false); // 人間も1枠入れるか
  let phase = $state<"form" | "creating" | "ready" | "error">("form");
  let err = $state<string | null>(null);

  let team = $state("");
  let wsUrl = $state("");
  let aiCount = $state(0);
  let agentTotal = $state(5);
  let humanJoinUrl = $state<string | null>(null);
  let sessionId: string | null = null;
  let sessionStatus = $state("");

  let lobbyBase = "";
  let pollTimer: ReturnType<typeof setTimeout> | null = null;

  if (browser) {
    const lp = page.url.searchParams.get("lobby");
    if (lp) lobbyBase = lp.replace(/\/$/, "");
  }

  async function createTable() {
    phase = "creating";
    err = null;
    try {
      const res = await fetch(`${lobbyBase}/api/byo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agents, human, size: villageSize }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      team = d.team;
      wsUrl = d.ws_url;
      aiCount = d.ai_count;
      agentTotal = d.agent_total;
      humanJoinUrl = d.human_join_url;
      sessionId = d.session_id;
      sessionStatus = d.status;
      phase = "ready";
      pollStatus();
    } catch (e) {
      phase = "error";
      err = e instanceof Error ? e.message : String(e);
    }
  }

  async function pollStatus() {
    if (!sessionId) return;
    try {
      const res = await fetch(`${lobbyBase}/api/session/${sessionId}`);
      if (res.ok) sessionStatus = (await res.json()).status;
    } catch {
      /* ignore */
    }
    pollTimer = setTimeout(pollStatus, 2000);
  }

  const configSnippet = $derived(
    `web_socket:\n  url: ${wsUrl}\nagent:\n  num: ${agents}\n  team: ${team}`,
  );

  let copied = $state(false);
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
      setTimeout(() => (copied = false), 1500);
    } catch {
      /* ignore */
    }
  }

  function reset() {
    if (pollTimer) clearTimeout(pollTimer);
    phase = "form";
    sessionId = null;
  }

  onDestroy(() => {
    if (pollTimer) clearTimeout(pollTimer);
  });
</script>

<svelte:head><title>持ち込みエージェント — 人狼知能大会 自然言語部門 体験デモ</title></svelte:head>

<main class="min-h-dvh bg-base-300 p-4 flex flex-col items-center">
  <div class="w-full max-w-lg flex flex-col gap-4">
    <h1 class="text-xl font-bold text-center mt-2">持ち込みエージェントで対戦</h1>

    {#if phase === "form" || phase === "creating"}
      <div class="card bg-base-100 p-4 flex flex-col gap-4">
        <p class="text-sm opacity-70">
          自作エージェントを接続して対戦できます。外部接続の残りはサンプルAIが自動で埋まります。
        </p>
        <label class="flex items-center justify-between gap-3">
          <span class="font-bold">村の人数</span>
          <div class="join">
            <button class="join-item btn btn-sm {villageSize === 5 ? 'btn-primary' : 'btn-outline'}" onclick={() => (villageSize = 5)}>5人村</button>
            <button class="join-item btn btn-sm {villageSize === 9 ? 'btn-primary' : 'btn-outline'}" onclick={() => (villageSize = 9)}>9人村</button>
          </div>
        </label>
        <label class="flex items-center justify-between gap-3">
          <span class="font-bold">持ち込みエージェント数</span>
          <input type="number" min="1" max={villageSize} class="input input-bordered w-24" bind:value={agents} />
        </label>
        <label class="flex items-center justify-between gap-3">
          <span class="font-bold">人間も1枠参加する</span>
          <input type="checkbox" class="toggle" bind:checked={human} />
        </label>
        <div class="text-sm opacity-70">
          {villageSize}人村。サンプルAIは <span class="font-bold">{Math.max(0, villageSize - agents - (human ? 1 : 0))}</span> 体になります。
        </div>
        <button class="btn btn-primary" disabled={phase === "creating"} onclick={createTable}>
          {phase === "creating" ? "作成中…" : "卓を作成"}
        </button>
      </div>
    {:else if phase === "ready"}
      <div class="card bg-base-100 p-4 flex flex-col gap-4">
        <div class="flex items-center justify-between">
          <span class="font-bold">卓を作成しました</span>
          <span class="badge {sessionStatus === 'running' ? 'badge-success' : 'badge-warning'}">
            {sessionStatus === "running"
              ? `準備OK（サンプルAI ${aiCount}体 起動中）`
              : sessionStatus === "queued"
                ? "順番待ち"
                : sessionStatus}
          </span>
        </div>

        <div>
          <div class="text-xs opacity-60">接続先 URL</div>
          <div class="flex gap-2 items-center">
            <code class="grow bg-base-200 rounded px-2 py-1 text-sm break-all">{wsUrl}</code>
            <button class="btn btn-xs" onclick={() => copy(wsUrl)}>コピー</button>
          </div>
        </div>
        <div>
          <div class="text-xs opacity-60">チーム名（あなたのエージェントに設定）</div>
          <div class="flex gap-2 items-center">
            <code class="grow bg-base-200 rounded px-2 py-1 text-sm break-all">{team}</code>
            <button class="btn btn-xs" onclick={() => copy(team)}>コピー</button>
          </div>
        </div>

        <div>
          <div class="flex items-center justify-between">
            <div class="text-xs opacity-60">設定スニペット（aiwolf-nlp-agent-llm の config 例）</div>
            <button class="btn btn-xs" onclick={() => copy(configSnippet)}>
              {copied ? "コピー済" : "コピー"}
            </button>
          </div>
          <pre class="bg-base-200 rounded p-2 text-xs overflow-x-auto whitespace-pre">{configSnippet}</pre>
          <p class="text-xs opacity-60 mt-1">
            ※ 他のaiwolfクライアントでも、接続URLと「末尾数字を除いた名前＝このチーム名」になるよう
            設定すれば参加できます。
          </p>
        </div>

        {#if humanJoinUrl}
          <a class="btn btn-secondary" href={`${base}${humanJoinUrl}`}>人間として同卓で参加（/demo）</a>
        {/if}

        <div class="text-sm opacity-70">
          あなたのエージェント({agents}体){human ? " と 人間(1名)" : ""}が接続すると、合計 {agentTotal} 名で自動的にゲームが始まります。
        </div>
        <button class="btn btn-ghost btn-sm" onclick={reset}>別の卓を作る</button>
      </div>
    {:else if phase === "error"}
      <div class="alert alert-error">
        <span>作成に失敗しました: {err}</span>
      </div>
      <button class="btn" onclick={reset}>戻る</button>
    {/if}
  </div>
</main>
