<script lang="ts">
  // aiwolf-nlp-demo: 持ち込みエージェント用ページ（/byo）
  //
  //   卓を作る … 外部エージェント N 席 ＋ 人間 M 席 ＋ 残りサンプルAI の卓を作り、合言葉を出す
  //   枠を取る … 合言葉で卓に入り、自分のエージェント名で席を取る → 接続URLと名前をもらう
  //   人間は /demo → マルチ → 合言葉 で同じ卓に入る
  //
  // 全員そろって人間が「準備完了」を押す（人間ゼロならホストの「開始」）まで試合は始まらない。
  import { browser } from "$app/environment";
  import { base } from "$app/paths";
  import { page } from "$app/state";
  import { onDestroy } from "svelte";
  import "../../app.css";

  type GateSeat = { team: string; name: string; human: boolean; ready: boolean };
  type Gate = {
    room_id: string; code: string; gate: string; status: string; error?: string | null;
    size: number; count: number; seats: GateSeat[];
    agent_slots: number; agent_slots_taken: number;
    agents: { name: string; count: number; connected: number; mine: boolean }[];
    human_slots: number; humans_joined: { name: string; team: string; ready: boolean }[];
    humans: string[]; all_ready: boolean; is_host: boolean; can_start: boolean; ws_url: string;
  };

  let tab = $state<"create" | "claim">("create");
  let lobbyBase = base;
  let deviceToken = "";

  // ロビーの状態（接続ガイドと「いまは自分のキーが要る」の出し分け用）
  let publicWs5 = $state("");
  let publicWs9 = $state("");
  let logPublicUrl = $state("");
  let serverLlmReady = $state(true);
  let serverModel = $state("");

  // ---- 卓を作る ----
  let villageSize = $state(5);
  let agentSlots = $state(1);
  let humanSlots = $state(0);
  let noPublishLogs = $state(false);
  let createPhase = $state<"form" | "creating" | "ready" | "error">("form");
  let createErr = $state<string | null>(null);
  let code = $state("");
  let roomId = $state("");

  // ---- 枠を取る ----
  let codeInput = $state("");
  let agentName = $state("");
  let agentCount = $state(1);
  let claimPhase = $state<"form" | "claiming" | "ready" | "error">("form");
  let claimErr = $state<string | null>(null);
  let claimed = $state<{ name: string; count: number; ws_url: string; code: string; room_id: string } | null>(null);

  // ---- 待合室（作った人・枠を取った人の両方が見る）----
  let gate = $state<Gate | null>(null);
  let gateCode = "";
  let gateTimer: ReturnType<typeof setTimeout> | null = null;
  let gateBusy = $state(false);
  let startErr = $state<string | null>(null);

  const aiCount = $derived(Math.max(0, villageSize - agentSlots - humanSlots));

  if (browser) {
    const lp = page.url.searchParams.get("lobby");
    if (lp) lobbyBase = lp.replace(/\/$/, "");
    // /demo と同じ端末トークン。卓の作成者＝ホストとして「開始」を押せる。枠の取り直しにも使う
    deviceToken = localStorage.getItem("demo_device_token") ?? "";
    if (!deviceToken) {
      deviceToken = crypto?.randomUUID?.() ?? `t-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      localStorage.setItem("demo_device_token", deviceToken);
    }
    const c = page.url.searchParams.get("code");
    if (c) {
      codeInput = c.toUpperCase();
      tab = "claim";
    }
    fetch(`${lobbyBase}/api/health`).then((r) => r.json()).then((h) => {
      logPublicUrl = typeof h.log_public_url === "string" ? h.log_public_url : "";
      publicWs5 = h.public_ws_url ?? "";
      publicWs9 = h.public_ws_url_9 ?? "";
      serverLlmReady = h.server_llm_ready !== false;
      serverModel = h.model ?? "";
    }).catch(() => {});
  }

  function clamp() {
    agentSlots = Math.max(0, Math.min(agentSlots, villageSize));
    humanSlots = Math.max(0, Math.min(humanSlots, villageSize - agentSlots));
  }

  async function createTable() {
    clamp();
    createPhase = "creating";
    createErr = null;
    try {
      const res = await fetch(`${lobbyBase}/api/rooms/byo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          size: villageSize, agent_slots: agentSlots, human_slots: humanSlots,
          token: deviceToken, publish_logs: !noPublishLogs,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `HTTP ${res.status}`);
      }
      const d = await res.json();
      code = d.code;
      roomId = d.room_id;
      createPhase = "ready";
      startGatePoll(code);
    } catch (e) {
      createPhase = "error";
      createErr = e instanceof Error ? e.message : String(e);
    }
  }

  async function claimSlot() {
    const c = codeInput.trim().toUpperCase();
    if (!c || !agentName.trim()) return;
    claimPhase = "claiming";
    claimErr = null;
    try {
      const res = await fetch(`${lobbyBase}/api/rooms/${encodeURIComponent(c)}/agent`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: agentName.trim(), count: agentCount, token: deviceToken }),
      });
      if (res.status === 404) throw new Error("その合言葉の卓は見つかりません");
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `HTTP ${res.status}`);
      }
      claimed = await res.json();
      claimPhase = "ready";
      startGatePoll(c);
    } catch (e) {
      claimPhase = "error";
      claimErr = e instanceof Error ? e.message : String(e);
    }
  }

  function stopGatePoll() {
    if (gateTimer) { clearTimeout(gateTimer); gateTimer = null; }
  }
  function startGatePoll(c: string) {
    stopGatePoll();
    gateCode = c;
    gate = null;
    void pollGate();
  }
  async function pollGate() {
    if (!gateCode) return;
    try {
      const res = await fetch(`${lobbyBase}/api/rooms/${encodeURIComponent(gateCode)}/gate?token=${encodeURIComponent(deviceToken)}`, { cache: "no-store" });
      if (res.ok) gate = await res.json();
      else if (res.status === 404) { stopGatePoll(); return; }
    } catch {
      /* 次で回復 */
    }
    gateTimer = setTimeout(pollGate, 2000);
  }
  async function pressStart() {
    if (!gate?.room_id || gateBusy) return;
    gateBusy = true;
    startErr = null;
    try {
      const res = await fetch(`${lobbyBase}/api/session/${encodeURIComponent(gate.room_id)}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: deviceToken }),
      });
      if (res.ok) gate = { ...(gate as Gate), ...(await res.json()) };
      else startErr = `開始できませんでした (HTTP ${res.status})`;
    } catch (e) {
      startErr = e instanceof Error ? e.message : String(e);
    }
    gateBusy = false;
  }
  onDestroy(stopGatePoll);

  const configSnippet = $derived(
    claimed ? `web_socket:\n  url: ${claimed.ws_url}\nagent:\n  num: ${claimed.count}\n  team: ${claimed.name}` : "",
  );
  const demoJoinUrl = $derived(code ? `${base}/demo?code=${encodeURIComponent(code)}` : "");
  const byoJoinUrl = $derived(code ? `${base}/byo?code=${encodeURIComponent(code)}` : "");

  let copied = $state("");
  async function copy(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text);
      copied = key;
      setTimeout(() => (copied = ""), 1500);
    } catch {
      /* ignore */
    }
  }

  function resetCreate() {
    stopGatePoll();
    createPhase = "form";
    code = "";
    roomId = "";
    gate = null;
    createErr = null;
  }
  function resetClaim() {
    stopGatePoll();
    claimPhase = "form";
    claimed = null;
    gate = null;
    claimErr = null;
  }
</script>

<svelte:head><title>持ち込みエージェント — 人狼知能大会 自然言語部門 体験デモ</title></svelte:head>

<!-- 待合室（作った人・枠を取った人が共通で見る） -->
{#snippet waitingRoom()}
  {#if gate}
    <div class="card bg-base-200 p-3 flex flex-col gap-2">
      <div class="flex items-center justify-between">
        <span class="font-bold">待合室</span>
        <span class="text-sm opacity-70">着席 {gate.count} / {gate.size}</span>
      </div>

      {#if gate.agent_slots > 0}
        <div class="text-xs opacity-60">外部エージェント（{gate.agent_slots_taken} / {gate.agent_slots} 枠）</div>
        <div class="flex flex-col gap-1">
          {#each gate.agents as a (a.name)}
            <div class="flex items-center gap-2 p-1.5 rounded bg-base-100 text-sm">
              <span class="opacity-70">🤖</span>
              <span class="font-bold truncate">{a.name}{a.mine ? "（あなた）" : ""}</span>
              <span class="opacity-60">×{a.count}</span>
              <span class="ml-auto badge badge-sm {a.connected >= a.count ? 'badge-success' : 'badge-ghost'}">接続 {a.connected}/{a.count}</span>
            </div>
          {/each}
          {#if gate.agent_slots_taken < gate.agent_slots}
            <div class="p-1.5 rounded bg-base-100 text-sm opacity-40">（あと {gate.agent_slots - gate.agent_slots_taken} 枠 空き）</div>
          {/if}
        </div>
      {/if}

      {#if gate.human_slots > 0}
        <div class="text-xs opacity-60">人間（{gate.humans_joined.length} / {gate.human_slots} 席）</div>
        <div class="flex flex-col gap-1">
          {#each gate.humans_joined as h (h.team)}
            <div class="flex items-center gap-2 p-1.5 rounded bg-base-100 text-sm">
              <span class="opacity-70">👤</span>
              <span class="font-bold truncate">{h.name}</span>
              <span class="ml-auto badge badge-sm {h.ready ? 'badge-success' : 'badge-ghost'}">{h.ready ? "準備完了" : "準備中"}</span>
            </div>
          {/each}
          {#each Array(Math.max(0, gate.human_slots - gate.humans_joined.length)) as _slot, i (i)}
            <div class="p-1.5 rounded bg-base-100 text-sm opacity-40">（空席）</div>
          {/each}
        </div>
      {/if}

      {#if gate.size - gate.agent_slots - gate.human_slots > 0}
        <div class="text-xs opacity-60">
          サンプルAI {gate.size - gate.agent_slots - gate.human_slots} 体（自動で着席します）
        </div>
      {/if}

      {#if gate.gate === "started"}
        <div class="alert alert-success py-2 text-sm"><span>ゲームが始まりました。</span></div>
      {:else if gate.gate === "released"}
        <div class="text-sm opacity-70">保留を解除しました。席が揃い次第始まります…</div>
      {:else if gate.gate === "off"}
        <div class="text-sm opacity-70">席が揃った瞬間に始まります（待合室ゲートなし）。</div>
      {:else if gate.is_host}
        {#if gate.human_slots === 0}
          <button class="btn btn-primary" disabled={gateBusy || gate.count < gate.size} onclick={pressStart}>開始</button>
          <div class="text-xs opacity-60">
            {gate.count < gate.size ? "全員が着席するまで開始できません。" : "「開始」を押すと各エージェントに最初のリクエストが飛びます。"}
          </div>
        {:else}
          <div class="text-xs opacity-60">全員が着席し、人間の参加者が /demo で「準備完了」を押すと自動で始まります。</div>
        {/if}
      {:else}
        <div class="text-xs opacity-60">
          {gate.human_slots === 0 ? "全員が着席したら、卓を作った人が「開始」を押します。" : "全員が着席し、人間の参加者が「準備完了」を押すと始まります。"}
        </div>
      {/if}
      {#if startErr}<div class="alert alert-error py-2 text-sm"><span>{startErr}</span></div>{/if}
      {#if gate.status === "error" && gate.error}<div class="alert alert-error py-2 text-sm"><span>{gate.error}</span></div>{/if}
    </div>
  {:else}
    <div class="text-sm opacity-60">待合室の状態を読み込み中…</div>
  {/if}
{/snippet}

<main class="min-h-dvh bg-base-300 p-4 flex flex-col items-center">
  <div class="w-full max-w-lg flex flex-col gap-4">
    <h1 class="text-xl font-bold text-center mt-2">持ち込みエージェントで対戦</h1>
    <p class="text-sm opacity-70 text-center">
      自作エージェント（aiwolf-nlp-agent-llm など）を、このサーバの卓に繋いで対戦できます。
      複数人がそれぞれのエージェントを持ち寄ることも、人間が同じ卓に入ることもできます。
    </p>

    <!-- 接続ガイド -->
    <details class="collapse collapse-arrow bg-base-100">
      <summary class="collapse-title font-bold">接続ガイド（どこに、どう繋ぐか）</summary>
      <div class="collapse-content text-sm flex flex-col gap-3">
        <div>
          <div class="font-bold">1. 接続先</div>
          <div class="opacity-80">
            ゲームサーバは常時公開されています。ただし <b>卓ごとの ID（<code>?room=…</code>）が付いた URL でしか入れません</b>。
            URL はこのページで卓を作る／枠を取ると発行されます。
          </div>
          <div class="mt-1 text-xs">
            5人村: <code class="bg-base-200 rounded px-1">{publicWs5 || "（読込中）"}?room=&lt;卓ID&gt;</code><br />
            9人村: <code class="bg-base-200 rounded px-1">{publicWs9 || "（読込中）"}?room=&lt;卓ID&gt;</code>
          </div>
        </div>
        <div>
          <div class="font-bold">2. 手順</div>
          <ol class="list-decimal list-inside opacity-80 flex flex-col gap-0.5">
            <li>誰かが「卓を作る」（外部エージェント何席・人間何席かを決める）→ <b>合言葉</b>が出る</li>
            <li>エージェントを持ってくる人は「枠を取る」で合言葉と<b>自分のエージェント名</b>を入れる → 接続 URL と名前が決まる</li>
            <li>人間は <a class="link" href={`${base}/demo`}>/demo</a> → マルチ → 合言葉 で入る</li>
            <li>接続 URL・名前・体数を config に書いてエージェントを起動する（下の例）</li>
            <li>待合室に全員そろい、人間が「準備完了」を押す（人間がいなければ卓を作った人が「開始」）と、各エージェントに INITIALIZE が飛んで試合開始</li>
          </ol>
        </div>
        <div>
          <div class="font-bold">3. aiwolf-nlp-agent-llm の config 例</div>
<pre class="bg-base-200 rounded p-2 text-xs overflow-x-auto whitespace-pre">web_socket:
  url: wss://…/game/ws?room=&lt;卓ID&gt;   # 「枠を取る」で出た URL をそのまま
agent:
  num: 1                                  # この名前で繋ぐ体数
  team: myagent                           # 「枠を取る」で決めた名前</pre>
          <div class="opacity-80 text-xs mt-1">
            他のクライアントでも、サーバの <code>NAME</code> リクエストに <b>名前＋番号</b>（例 <code>myagent1</code>, <code>myagent2</code>）を返せば参加できます。
            番号を除いた部分がチーム名として扱われます。
          </div>
        </div>
        <div>
          <div class="font-bold">4. 名前のきまり</div>
          <ul class="list-disc list-inside opacity-80">
            <li>英字で始まる英数字・<code>_</code>・<code>-</code>、32文字まで。末尾は数字以外（接続時に 1,2,… が付くため）</li>
            <li>同じ卓の中で一意。同じ名前で 2 体繋ぐと、あとから来た方は切断されます</li>
            <li><code>you-</code> / <code>s-</code> / <code>demo</code> で始まる名前は予約（人間・サンプルAI用）</li>
          </ul>
        </div>
        <div>
          <div class="font-bold">5. 時間と上限</div>
          <ul class="list-disc list-inside opacity-80">
            <li>待合室のまま 10 分たつと卓は片付けられます。1 試合の上限は 30 分</li>
            <li>サーバのサンプルAI（GPU）を使う卓は同時 3 卓まで、外部エージェントだけ／持ち込みキーの卓は同時 20 卓まで</li>
            {#if logPublicUrl}<li>対戦ログは<a class="link" href={logPublicUrl} target="_blank" rel="noopener">公開ページ</a>に出ます（卓を作るときに外せます）</li>{/if}
          </ul>
        </div>
      </div>
    </details>

    <div role="tablist" class="tabs tabs-boxed">
      <button role="tab" class="tab {tab === 'create' ? 'tab-active' : ''}" onclick={() => (tab = "create")}>卓を作る</button>
      <button role="tab" class="tab {tab === 'claim' ? 'tab-active' : ''}" onclick={() => (tab = "claim")}>合言葉で枠を取る</button>
    </div>

    {#if tab === "create"}
      {#if createPhase === "form" || createPhase === "creating"}
        <div class="card bg-base-100 p-4 flex flex-col gap-4">
          <label class="flex items-center justify-between gap-3">
            <span class="font-bold">村の人数</span>
            <div class="join">
              <button class="join-item btn btn-sm {villageSize === 5 ? 'btn-primary' : 'btn-outline'}" onclick={() => { villageSize = 5; clamp(); }}>5人村</button>
              <button class="join-item btn btn-sm {villageSize === 9 ? 'btn-primary' : 'btn-outline'}" onclick={() => { villageSize = 9; clamp(); }}>9人村</button>
            </div>
          </label>
          <label class="flex items-center justify-between gap-3">
            <span class="font-bold">外部エージェントの席</span>
            <input type="number" min="0" max={villageSize} class="input input-bordered w-24" bind:value={agentSlots} onchange={clamp} />
          </label>
          <label class="flex items-center justify-between gap-3">
            <span class="font-bold">人間の席</span>
            <input type="number" min="0" max={villageSize - agentSlots} class="input input-bordered w-24" bind:value={humanSlots} onchange={clamp} />
          </label>
          <div class="text-sm opacity-70">
            {villageSize}人村: 外部 {agentSlots} ＋ 人間 {humanSlots} ＋ サンプルAI <span class="font-bold">{aiCount}</span>
          </div>
          {#if aiCount > 0 && !serverLlmReady}
            <div class="alert alert-warning py-2 text-sm">
              <span>いまはサーバ側のAI（GPU）が止まっています。サンプルAIを使う卓は作れません。外部エージェント＋人間だけで {villageSize} 席にするか、後でお試しください。</span>
            </div>
          {:else if aiCount > 0 && serverModel}
            <div class="text-xs opacity-60">サンプルAIのモデル: {serverModel}</div>
          {/if}
          {#if logPublicUrl}
            <div class="text-xs opacity-80 flex flex-col gap-1">
              <div>対戦ログは公開されます（<a class="link link-primary" href={logPublicUrl} target="_blank" rel="noopener">公開ページ</a>）。</div>
              <label class="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" class="checkbox checkbox-xs" bind:checked={noPublishLogs} />
                <span>この卓のログは公開しない</span>
              </label>
            </div>
          {/if}
          {#if createErr}<div class="alert alert-error py-2 text-sm"><span>{createErr}</span></div>{/if}
          <button class="btn btn-primary" disabled={createPhase === "creating" || agentSlots + humanSlots < 1 || (aiCount > 0 && !serverLlmReady)} onclick={createTable}>
            {createPhase === "creating" ? "作成中…" : "卓を作る"}
          </button>
        </div>
      {:else if createPhase === "ready"}
        <div class="card bg-base-100 p-4 flex flex-col gap-4">
          <div>
            <div class="text-sm font-bold opacity-70">合言葉</div>
            <button class="flex items-center gap-2 btn btn-ghost" onclick={() => copy(code, "code")}>
              <span class="text-3xl font-bold font-mono tracking-widest">{code}</span>
              <span class="text-xs opacity-60">{copied === "code" ? "コピー済" : "コピー"}</span>
            </button>
          </div>
          <div class="text-sm flex flex-col gap-1">
            <div>🤖 エージェントを持ってくる人 → <a class="link link-primary" href={byoJoinUrl}>このページで枠を取る</a>（合言葉 <b>{code}</b>）</div>
            {#if humanSlots > 0}
              <div>👤 人間として入る人 → <a class="link link-primary" href={demoJoinUrl}>/demo で合言葉 {code} を入力</a></div>
            {/if}
          </div>
          {@render waitingRoom()}
          <button class="btn btn-ghost btn-sm" onclick={resetCreate}>別の卓を作る</button>
        </div>
      {:else}
        <div class="alert alert-error"><span>作成に失敗しました: {createErr}</span></div>
        <button class="btn" onclick={resetCreate}>戻る</button>
      {/if}
    {:else}
      {#if claimPhase === "form" || claimPhase === "claiming"}
        <div class="card bg-base-100 p-4 flex flex-col gap-4">
          <label class="flex flex-col gap-1">
            <span class="font-bold">合言葉</span>
            <input class="input input-bordered font-mono tracking-widest uppercase" maxlength="8" placeholder="ABCDE" bind:value={codeInput}
              oninput={(e) => (codeInput = (e.target as HTMLInputElement).value.toUpperCase())} />
          </label>
          <label class="flex flex-col gap-1">
            <span class="font-bold">エージェント名（チーム名）</span>
            <input class="input input-bordered font-mono" maxlength="32" placeholder="myagent" bind:value={agentName} />
            <span class="text-xs opacity-60">英字で始まる英数字・_・-。末尾は数字以外。接続時は名前＋番号（myagent1 …）になります</span>
          </label>
          <label class="flex items-center justify-between gap-3">
            <span class="font-bold">この名前で繋ぐ体数</span>
            <input type="number" min="1" max="9" class="input input-bordered w-24" bind:value={agentCount} />
          </label>
          {#if claimErr}<div class="alert alert-error py-2 text-sm"><span>{claimErr}</span></div>{/if}
          <button class="btn btn-primary" disabled={claimPhase === "claiming" || !codeInput.trim() || !agentName.trim()} onclick={claimSlot}>
            {claimPhase === "claiming" ? "確認中…" : "枠を取る"}
          </button>
        </div>
      {:else if claimPhase === "ready" && claimed}
        <div class="card bg-base-100 p-4 flex flex-col gap-4">
          <div class="font-bold">枠を取りました。次の設定でエージェントを起動してください。</div>
          <div>
            <div class="text-xs opacity-60">接続先 URL</div>
            <div class="flex gap-2 items-center">
              <code class="grow bg-base-200 rounded px-2 py-1 text-sm break-all">{claimed.ws_url}</code>
              <button class="btn btn-xs" onclick={() => copy(claimed!.ws_url, "url")}>{copied === "url" ? "済" : "コピー"}</button>
            </div>
          </div>
          <div>
            <div class="text-xs opacity-60">チーム名（体数 {claimed.count}）</div>
            <div class="flex gap-2 items-center">
              <code class="grow bg-base-200 rounded px-2 py-1 text-sm break-all">{claimed.name}</code>
              <button class="btn btn-xs" onclick={() => copy(claimed!.name, "name")}>{copied === "name" ? "済" : "コピー"}</button>
            </div>
          </div>
          <div>
            <div class="flex items-center justify-between">
              <div class="text-xs opacity-60">config 例（aiwolf-nlp-agent-llm）</div>
              <button class="btn btn-xs" onclick={() => copy(configSnippet, "cfg")}>{copied === "cfg" ? "コピー済" : "コピー"}</button>
            </div>
            <pre class="bg-base-200 rounded p-2 text-xs overflow-x-auto whitespace-pre">{configSnippet}</pre>
          </div>
          {@render waitingRoom()}
          <button class="btn btn-ghost btn-sm" onclick={resetClaim}>別の卓に入る</button>
        </div>
      {:else}
        <div class="alert alert-error"><span>{claimErr}</span></div>
        <button class="btn" onclick={resetClaim}>戻る</button>
      {/if}
    {/if}

    <a class="btn btn-ghost btn-sm" href={`${base}/demo`}>← 遊ぶ（/demo）へ</a>
  </div>
</main>
