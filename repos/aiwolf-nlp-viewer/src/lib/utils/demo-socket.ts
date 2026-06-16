// aiwolf-nlp-demo: /demo 専用 WebSocket ロジック
// ベース: src/lib/utils/agent-socket.ts（プレイヤー視点プロトコルの再利用）
// 既存 /agent の agent-socket.ts は一切変更しないため、コピーして拡張する。
//
// 拡張点（M5 のサーバ側プレイヤー逐次push と対になる設計）:
//   1. talkHistory / whisperHistory を talk.idx で重複排除
//      （逐次push と自分のターン時の talk_history 差分が重なっても二重表示しない）
//   2. 任意フィールド `turn`（ターン開始/終了マーカー）を解釈し currentTurnAgent を保持
//      （サーバが未送出でも null のままで動作。request ベースの入力可否判定にフォールバック）
import type { Role } from '$lib/constants/common';
import { agentSettings } from '$lib/stores/agent-settings';
import { Request, type Info, type Judge, type Packet, type Setting, type Talk } from '$lib/types/agent';
import type { AgentSettings } from '$lib/types/agent-settings';
import { writable } from 'svelte/store';

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected';

// サーバのプレイヤー向け push が付加する任意フィールド（M5）
export interface TurnMarker {
    type: 'start' | 'end';       // ターン開始 / 終了
    agent: string;               // 対象エージェント名（ゲーム内名）
    phase?: string;              // "talk" | "whisper" など
    idx?: number;                // 対象エージェントのインデックス
}

// サーバの R_TALK_BROADCAST / R_WHISPER_BROADCAST は viewer の Request enum に無いため
// 文字列リテラルで受ける（types/agent.ts は共有のため変更しない）。
export type DemoRequest = Request | 'TALK_BROADCAST' | 'WHISPER_BROADCAST';

export interface DemoPacket extends Omit<Packet, 'request'> {
    request: DemoRequest;
    turn?: TurnMarker;
    new_talk?: Talk;
    new_whisper?: Talk;
}

const BROADCAST_REQUESTS = new Set<string>(['TALK_BROADCAST', 'WHISPER_BROADCAST']);

// サーバ側の応答待ちタイマーを止める/再開する制御トークン（model/talk.go の C_PAUSE/C_RESUME と一致）。
// 自分のターン（応答待ち中）にのみ送る。通常の発話・役職名・Over/Skip とは衝突しない予約文字列。
const C_PAUSE = '__PAUSE__';
const C_RESUME = '__RESUME__';

// LINE風ストリームの要素: トーク or システムアナウンス（日付/夜/投票/結果/占い等）
export type FeedTone = 'day' | 'night' | 'vote' | 'result' | 'info';
// system アナウンスは「表示文言」ではなく「i18nキー＋パラメータ」で持つ。
// 描画は +page.svelte 側で $_(i18nKey, {values}) で行い、言語切替に即追従させる。
// name は原名（サーバ名）で持ち、表示時にローカライズする。species は 'WEREWOLF'|'HUMAN'。
export type FeedEntry =
    | { kind: 'talk'; talk: Talk }
    | { kind: 'system'; key: string; i18nKey: string; tone: FeedTone; day?: number; name?: string; species?: string };

export interface DemoSocket {
    status: ConnectionStatus;
    deadline: Date | null;
    entries: (DemoPacket | string)[];
    agent: string | null;
    role: Role | null;
    profile: string | null;
    request: Request | null;
    info: Info | null;
    mediumResults: Judge[];
    divineResults: Judge[];
    setting: Setting | null;
    talkHistory: Talk[];
    whisperHistory: Talk[];
    executedAgents: string[];
    attackedAgents: string[];
    // /demo 拡張
    currentTurnAgent: string | null;   // いま発話中（入力中）のエージェント名。自分以外なら入力ロック
    feed: FeedEntry[];                 // トーク＋アナウンスの統合ストリーム
    finished: boolean;                 // FINISH 受信＝ゲーム終了
}

const createInitialState = (): DemoSocket => ({
    status: 'disconnected',
    deadline: null,
    entries: [],
    agent: null,
    role: null,
    profile: null,
    request: null,
    info: null,
    mediumResults: [],
    divineResults: [],
    setting: null,
    talkHistory: [],
    whisperHistory: [],
    executedAgents: [],
    attackedAgents: [],
    currentTurnAgent: null,
    feed: [],
    finished: false,
});

// 重複排除キー: talk.idx は「日ごとに 0 から振り直される」ため idx だけだと
// 2日目以降が1日目と衝突して消える。day と idx の組で一意化する。
function talkKey(t: Talk): string {
    return `${t.day}:${t.idx}`;
}

function pushSystem(
    feed: FeedEntry[],
    key: string,
    i18nKey: string,
    tone: FeedTone,
    extra: { day?: number; name?: string; species?: string } = {},
): FeedEntry[] {
    if (feed.some(e => e.kind === 'system' && e.key === key)) return feed; // 同一イベントは1回だけ
    return [...feed, { kind: 'system', key, i18nKey, tone, ...extra }];
}

function appendUniqueTalks(existing: Talk[], incoming: Talk[]): Talk[] {
    const seen = new Set(existing.map(talkKey));
    const merged = existing.slice();
    for (const t of incoming) {
        const k = talkKey(t);
        if (!seen.has(k)) {
            seen.add(k);
            merged.push(t);
        }
    }
    return merged;
}

function createDemoSocketState() {
    const { subscribe, update } = writable<DemoSocket>(createInitialState());

    let socket: WebSocket | null = null;
    let settings: AgentSettings | null = null;
    let actionTimeout: number | null = null;
    let actionTimer: Timer | null = null;
    // 一時停止状態。paused=UI上の一時停止。pausedServer=サーバへC_PAUSE送信済み（C_RESUMEが必要）。
    // pauseRemainingMs=一時停止した時点での残り時間（再開時にこの残り時間でタイマーを張り直す）。
    let paused = false;
    let pausedServer = false;
    let pauseRemainingMs: number | null = null;

    agentSettings.subscribe((value) => {
        settings = value;
    });

    function disconnect() {
        if (socket) {
            socket.close();
            socket = null;
            update(state => ({ ...state, status: "disconnected", currentTurnAgent: null }));
        }
        if (actionTimer) {
            actionTimer.clear();
            actionTimer = null;
        }
        paused = false;
        pausedServer = false;
        pauseRemainingMs = null;
    }

    function connect() {
        if (!settings) return;

        if (socket) {
            update(() => createInitialState());
        }

        update(state => ({ ...state, status: "connecting" }));
        const socketUrl = new URL(settings.connection.url);
        if (settings.connection.token) {
            socketUrl.searchParams.set('token', settings.connection.token);
        }

        socket = new WebSocket(socketUrl);

        socket.onopen = () => {
            update(state => ({ ...state, status: "connected" }));
        };

        socket.onclose = () => {
            disconnect();
        };

        socket.onerror = () => {
            disconnect();
        };

        socket.onmessage = (event) => {
            try {
                const date = Date.now();
                const packet = JSON.parse(event.data) as DemoPacket;

                update(state => processPacket(state, packet));
                handlePacketRequest(packet, date);
            } catch (e) {
                console.error("Failed to parse message:", e);
            }
        };
    }

    function send(text: string) {
        if (actionTimer) {
            actionTimer.clear();
            actionTimer = null;
            update(state => ({ ...state, deadline: null }));
        }
        if (socket && socket.readyState === WebSocket.OPEN) {
            try {
                socket.send(text);
                update(state => {
                    const newEntries = state.entries.slice();
                    newEntries.push(text);
                    return { ...state, entries: newEntries };
                });
            } catch (e) {
                console.error("Failed to send message:", e);
            }
        }
    }

    // 制御トークン等をUI副作用なし（feed非表示・タイマー非操作）でそのまま送る。
    function rawSend(text: string) {
        if (socket && socket.readyState === WebSocket.OPEN) {
            try {
                socket.send(text);
            } catch (e) {
                console.error("Failed to send control:", e);
            }
        }
    }

    // 一時停止: ローカルのTIMEOUTタイマーを止めて残り時間を保持し、
    // 自分のターン（応答待ち中）ならサーバにも C_PAUSE を送ってタイムアウト計測を止める。
    function pause() {
        if (paused) return;
        paused = true;
        if (actionTimer) {
            pauseRemainingMs = Math.max(0, actionTimer.deadline().getTime() - Date.now());
            actionTimer.clear();
            actionTimer = null;
            rawSend(C_PAUSE);
            pausedServer = true;
        }
        // 自分のターンでないとき（AIの番など）はサーバ側に進行中のタイマーが無いので送らない。
    }

    // 再開: サーバに C_RESUME を送り、保持していた残り時間でローカルタイマーを張り直す。
    function resume() {
        if (!paused) return;
        paused = false;
        if (pausedServer) {
            rawSend(C_RESUME);
            pausedServer = false;
        }
        if (pauseRemainingMs !== null) {
            const newDeadline = new Date(Date.now() + pauseRemainingMs);
            actionTimer = new Timer(() => {
                send("TIMEOUT");
            }, newDeadline);
            update(state => ({ ...state, deadline: newDeadline }));
            pauseRemainingMs = null;
        }
    }

    function processPacket(state: DemoSocket, packet: DemoPacket): DemoSocket {
        // 逐次配信(TALK_BROADCAST等)は「自分への actionable リクエスト」ではないので
        // request 状態を上書きしない（入力可否判定を壊さないため）。
        const isBroadcast = BROADCAST_REQUESTS.has(packet.request as string);
        const newState: DemoSocket = {
            ...state,
            entries: [...state.entries, packet],
            request: isBroadcast ? state.request : (packet.request as Request)
        };
        let feed = state.feed;

        // 新着トーク（逐次push or 自分のターン時の talk_history 差分）を day:idx で重複排除し、
        // talkHistory と feed の両方へ追記。
        const incomingTalks: Talk[] = [];
        if (packet.new_talk) incomingTalks.push(packet.new_talk);
        if (packet.talk_history) incomingTalks.push(...packet.talk_history);
        if (incomingTalks.length) {
            const seen = new Set(newState.talkHistory.map(talkKey));
            const fresh: Talk[] = [];
            for (const t of incomingTalks) {
                const k = talkKey(t);
                if (!seen.has(k)) { seen.add(k); fresh.push(t); }
            }
            if (fresh.length) {
                newState.talkHistory = [...newState.talkHistory, ...fresh];
                feed = [...feed, ...fresh.map((t): FeedEntry => ({ kind: 'talk', talk: t }))];
            }
        }
        // 囁き（通常デモでは無効だが一応保持）
        if (packet.new_whisper) newState.whisperHistory = appendUniqueTalks(newState.whisperHistory, [packet.new_whisper]);
        if (packet.whisper_history) newState.whisperHistory = appendUniqueTalks(newState.whisperHistory, packet.whisper_history);

        if (packet.info) {
            newState.info = packet.info;

            if (packet.info.medium_result) {
                const judge = packet.info.medium_result;
                if (!newState.mediumResults.some(j => j.day === judge.day && j.agent === judge.agent)) {
                    newState.mediumResults = [...newState.mediumResults, judge];
                    feed = pushSystem(feed, `medium-${judge.day}-${judge.target}`,
                        'demo.feed.mediumResult', 'info', { name: judge.target, species: judge.result });
                }
            }
            if (packet.info.divine_result) {
                const judge = packet.info.divine_result;
                if (!newState.divineResults.some(j => j.day === judge.day && j.agent === judge.agent)) {
                    newState.divineResults = [...newState.divineResults, judge];
                    feed = pushSystem(feed, `divine-${judge.day}-${judge.target}`,
                        'demo.feed.divineResult', 'info', { name: judge.target, species: judge.result });
                }
            }
            if (packet.info.executed_agent) {
                newState.executedAgents = [...newState.executedAgents, packet.info.executed_agent];
            }
            if (packet.info.attacked_agent) {
                newState.attackedAgents = [...newState.attackedAgents, packet.info.attacked_agent];
            }
        }

        if (packet.setting) {
            newState.setting = packet.setting;
            actionTimeout = packet.setting.timeout.action;
        }

        // ターンマーカー（M5）: 開始で currentTurnAgent をセット、終了でクリア
        if (packet.turn) {
            if (packet.turn.type === 'start') newState.currentTurnAgent = packet.turn.agent;
            else if (packet.turn.type === 'end') newState.currentTurnAgent = null;
        } else if (
            !isBroadcast &&
            packet.request !== Request.TALK &&
            packet.request !== Request.WHISPER
        ) {
            // トークフェーズ外（夜のアクション/投票/日替わり）のパケットでは
            // 「○○さんが入力中」表示を消す。夜の実行者が漏れないようにするため。
            newState.currentTurnAgent = null;
        }

        // フェーズ・日付アナウンス（request 種別で判定。同一キーは1回だけ）
        const day = newState.info?.day;
        switch (packet.request) {
            case Request.INITIALIZE:
                if (newState.info) {
                    newState.agent = newState.info.agent;
                    newState.role = newState.info.role_map[newState.info.agent];
                    newState.profile = newState.info.profile || null;
                }
                break;
            case Request.DAILY_INITIALIZE:
                if (day !== undefined) {
                    feed = pushSystem(feed, `morning-${day}`, 'demo.feed.morning', 'day', { day });
                    const ex = newState.info?.executed_agent;
                    const at = newState.info?.attacked_agent;
                    if (ex) feed = pushSystem(feed, `exec-${day}-${ex}`, 'demo.feed.executed', 'result', { name: ex });
                    if (at) feed = pushSystem(feed, `attack-${day}-${at}`, 'demo.feed.attacked', 'night', { name: at });
                }
                break;
            case Request.VOTE:
                if (day !== undefined) feed = pushSystem(feed, `vote-${day}`, 'demo.feed.vote', 'vote', { day });
                break;
            case Request.DAILY_FINISH:
                if (day !== undefined) feed = pushSystem(feed, `night-${day}`, 'demo.feed.night', 'night');
                break;
            case Request.FINISH:
                newState.finished = true;
                feed = pushSystem(feed, 'finish', 'demo.feed.finished', 'result');
                break;
        }

        newState.feed = feed;
        return newState;
    }

    function handlePacketRequest(packet: DemoPacket, date: number) {
        switch (packet.request) {
            case Request.NAME:
                send(settings?.team || 'demo' + Math.floor(Math.random() * 1000));
                break;
            case Request.TALK:
            case Request.WHISPER:
            case Request.VOTE:
            case Request.DIVINE:
            case Request.GUARD:
            case Request.ATTACK:
                // 自分のリクエストが来た = 自分のターン。currentTurnAgent も自分に。
                if (actionTimer) {
                    actionTimer.clear();
                    actionTimer = null;
                }
                if (paused) {
                    // 一時停止中に自分の番が来た（AIの番の間に一時停止していた等）:
                    // ローカルタイマーは張らず、サーバ側のタイムアウト計測も止めておく。
                    // 残り時間は満タン（actionTimeout）として保持し、再開時にそこから計測再開。
                    pauseRemainingMs = actionTimeout ?? 60000;
                    rawSend(C_PAUSE);
                    pausedServer = true;
                    update(state => ({
                        ...state,
                        deadline: new Date(date + (actionTimeout ?? 60000)),
                        currentTurnAgent: state.agent,
                    }));
                } else {
                    actionTimer = new Timer(() => {
                        send("TIMEOUT");
                    }, new Date(date + (actionTimeout ?? 60000)));
                    update(state => ({
                        ...state,
                        deadline: actionTimer?.deadline() ?? null,
                        currentTurnAgent: state.agent,
                    }));
                }
                break;
            case Request.FINISH:
                disconnect();
                break;
        }
    }

    // 切断して状態を初期化（ホームに戻る/中断用。finished や feed もリセットされる）
    function reset() {
        disconnect();
        update(() => createInitialState());
    }

    // 外部からフィードにシステム告知を1件追加する（マルチのAI引き継ぎ通知など）。
    // key で重複排除されるため、同じ key を二度渡しても1回だけ表示される。
    function pushNotice(key: string, i18nKey: string, extra: { name?: string } = {}) {
        update((state) => ({ ...state, feed: pushSystem(state.feed, key, i18nKey, 'result', extra) }));
    }

    return {
        subscribe,
        connect,
        disconnect,
        send,
        pause,
        resume,
        reset,
        pushNotice,
    };
}

export const demoSocketState = createDemoSocketState();

class Timer {
    private timeout: ReturnType<typeof setTimeout>;
    private _deadline: Date;

    constructor(callback: () => void, deadline: Date) {
        this.timeout = setTimeout(callback, deadline.getTime() - new Date().getTime());
        this._deadline = deadline;
    }

    deadline() {
        return this._deadline;
    }

    clear() {
        clearTimeout(this.timeout);
    }
}
