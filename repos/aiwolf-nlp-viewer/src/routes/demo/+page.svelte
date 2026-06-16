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

  // ゲーム言語（AIの発話言語）＝開始時のUI言語。言語セレクタはUIとゲームを同時に切り替える
  // （言語を選ぶ＝画面もAIも即座にその言語）。卓開始後はこのゲーム言語が固定される。
  const gameLanguage = $derived(normalizeLanguage($locale, "ja"));

  async function startViaLobby() {
    lobbyPhase = "joining";
    lobbyError = null;
    introAck = false;
    introOpen = false;
    try {
      const res = await fetch(`${lobbyBase}/api/join`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ size: villageSize, language: gameLanguage }),
      });
      if (!res.ok) throw new Error(`join failed: ${res.status}`);
      const data = await res.json();
      sessionId = data.session_id;
      displayName = data.display_name;
      // 役職・キャラの希望を ws URL に付与（サーバが ?role=/?character= を解釈。未指定はランダム）。
      let wsUrl = data.ws_url as string;
      const q: string[] = [];
      if (selectedRole) q.push(`role=${encodeURIComponent(selectedRole)}`);
      if (selectedCharacter >= 0) q.push(`character=${selectedCharacter}`);
      if (q.length) wsUrl += (wsUrl.includes("?") ? "&" : "?") + q.join("&");
      // 人間枠の接続設定（チーム名＝一意セッションチーム）
      agentSettings.update((value) => ({
        ...value,
        connection: { url: wsUrl, token: "" },
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
      lobbyError = $_("demo.start.startFailed");
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
    if (confirmFirst && !confirm($_("demo.leaveConfirm"))) {
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
      {#if status === "connected" && !finished}
        <button
          class="btn btn-xs {paused ? 'btn-success' : 'btn-ghost'}"
          onclick={() => (paused = !paused)}
          aria-label={paused ? $_("demo.header.resume") : $_("demo.header.pause")}
        >
          <iconify-icon icon={paused ? "mdi:play" : "mdi:pause"}></iconify-icon>
          {paused ? $_("demo.header.resume") : $_("demo.header.pause")}
        </button>
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

  {#if showStartScreen}
    <!-- スタート/順番待ち画面（ロビー連携）-->
    <div class="grow flex flex-col items-center justify-center gap-4 p-6 text-center">
      <h1 class="text-xl font-bold">{$_("demo.title")}</h1>
      <p class="opacity-70 text-sm max-w-xs">
        {$_("demo.tagline")}
      </p>

      {#if lobbyPhase === "idle"}
        <!-- 村の人数を選択（最初のページで決定。最小/既定は5）-->
        <div class="flex flex-col items-center gap-2">
          <div class="text-sm font-bold opacity-70">{$_("demo.start.villageSize")}</div>
          <div class="join">
            <button
              class="join-item btn {villageSize === 5 ? 'btn-primary' : 'btn-outline'}"
              onclick={() => (villageSize = 5)}>{$_("demo.start.village5")}</button>
            <button
              class="join-item btn {villageSize === 9 ? 'btn-primary' : 'btn-outline'}"
              onclick={() => (villageSize = 9)}>{$_("demo.start.village9")}</button>
          </div>
          <div class="text-xs opacity-60 max-w-xs text-center">
            {#if villageSize === 5}
              {$_("demo.start.comp5")}
            {:else}
              {$_("demo.start.comp9")}
            {/if}
          </div>
        </div>

        <!-- 役職の指定（任意。既定=おまかせ=ランダム）-->
        <div class="flex flex-col items-center gap-2">
          <div class="text-sm font-bold opacity-70">{$_("demo.start.role")}</div>
          <select class="select select-bordered select-sm" bind:value={selectedRole}>
            <option value="">{$_("demo.start.random")}</option>
            {#each roleChoices as r}
              <option value={r}>{$_(`game.role.${r}`)}</option>
            {/each}
          </select>
        </div>

        <!-- キャラクターの指定（任意。既定=おまかせ=ランダム）-->
        <div class="flex flex-col items-center gap-2">
          <div class="text-sm font-bold opacity-70">{$_("demo.start.character")}</div>
          <div class="flex items-center gap-2">
            {#if selectedCharacterAvatar}
              <div class="avatar"><div class="w-8 rounded-full"><img src={`${base}${selectedCharacterAvatar}`} alt="" /></div></div>
            {/if}
            <select class="select select-bordered select-sm" bind:value={selectedCharacter}>
              <option value={-1}>{$_("demo.start.random")}</option>
              {#each characters as c}
                <option value={c.index}>{c.name}</option>
              {/each}
            </select>
          </div>
        </div>

        <button class="btn btn-primary btn-lg" onclick={startViaLobby}>{$_("demo.start.start")}</button>
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
        <button class="btn" onclick={startViaLobby}>{$_("demo.start.retry")}</button>
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
    {#if isMyTurn && effectivePaused}
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
