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
});

// 重複排除キー: talk.idx は「日ごとに 0 から振り直される」ため idx だけだと
// 2日目以降が1日目と衝突して消える。day と idx の組で一意化する。
function talkKey(t: Talk): string {
    return `${t.day}:${t.idx}`;
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

    function processPacket(state: DemoSocket, packet: DemoPacket): DemoSocket {
        // 逐次配信(TALK_BROADCAST等)は「自分への actionable リクエスト」ではないので
        // request 状態を上書きしない（入力可否判定を壊さないため）。
        const isBroadcast = BROADCAST_REQUESTS.has(packet.request as string);
        const newState: DemoSocket = {
            ...state,
            entries: [...state.entries, packet],
            request: isBroadcast ? state.request : (packet.request as Request)
        };

        // 逐次push: 新着トーク/囁きを idx 重複排除して追記
        if (packet.new_talk) {
            newState.talkHistory = appendUniqueTalks(newState.talkHistory, [packet.new_talk]);
        }
        if (packet.new_whisper) {
            newState.whisperHistory = appendUniqueTalks(newState.whisperHistory, [packet.new_whisper]);
        }

        if (packet.info) {
            newState.info = packet.info;

            if (packet.info.medium_result) {
                const judge = packet.info.medium_result;
                if (!newState.mediumResults.some(j => j.day === judge.day && j.agent === judge.agent)) {
                    newState.mediumResults = [...newState.mediumResults, judge];
                }
            }

            if (packet.info.divine_result) {
                const judge = packet.info.divine_result;
                if (!newState.divineResults.some(j => j.day === judge.day && j.agent === judge.agent)) {
                    newState.divineResults = [...newState.divineResults, judge];
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

        // talk_history / whisper_history は idx で重複排除して追記
        if (packet.talk_history) {
            newState.talkHistory = appendUniqueTalks(newState.talkHistory, packet.talk_history);
        }

        if (packet.whisper_history) {
            newState.whisperHistory = appendUniqueTalks(newState.whisperHistory, packet.whisper_history);
        }

        // ターンマーカー（M5）: 開始で currentTurnAgent をセット、終了でクリア
        if (packet.turn) {
            if (packet.turn.type === 'start') {
                newState.currentTurnAgent = packet.turn.agent;
            } else if (packet.turn.type === 'end') {
                newState.currentTurnAgent = null;
            }
        }

        if (packet.request === Request.INITIALIZE && newState.info) {
            newState.agent = newState.info.agent;
            newState.role = newState.info.role_map[newState.info.agent];
            newState.profile = newState.info.profile || null;
        }

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
                }
                actionTimer = new Timer(() => {
                    send("TIMEOUT");
                }, new Date(date + (actionTimeout ?? 60000)));
                update(state => ({
                    ...state,
                    deadline: actionTimer?.deadline() ?? null,
                    currentTurnAgent: state.agent,
                }));
                break;
            case Request.FINISH:
                disconnect();
                break;
        }
    }

    return {
        subscribe,
        connect,
        disconnect,
        send,
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
