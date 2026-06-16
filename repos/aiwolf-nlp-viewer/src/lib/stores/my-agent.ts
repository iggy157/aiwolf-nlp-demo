import { createPersistentStore } from "./store-utils";

// ユーザがUI上で作る自作AI（プロンプトエンジニアリング）。localStorage に保存され、
// 卓を跨いで・タブを閉じても残る（端末/ブラウザ単位）。将来DB＋アカウントに紐付け移行する。
//   nickname: AIの名前（自分用ラベル）
//   persona : プレイスタイル/性格の自由文（L1）。lobby が AI の initialize に安全注入する。
export interface MyAgent {
  nickname: string;
  persona: string;
}

function isMyAgent(v: unknown): v is MyAgent {
  return (
    typeof v === "object" && v !== null &&
    typeof (v as MyAgent).nickname === "string" &&
    typeof (v as MyAgent).persona === "string"
  );
}

export const MY_AGENT_MAX_CHARS = 2000;

export const myAgent = createPersistentStore<MyAgent>({
  storageKey: "demo_my_agent",
  defaultValue: { nickname: "", persona: "" },
  validate: isMyAgent,
});
