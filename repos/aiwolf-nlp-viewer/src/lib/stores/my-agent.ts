import { createPersistentStore } from "./store-utils";

// ユーザがUI上で作る自作AI（リクエスト別プロンプトを編集）。localStorage に保存され、
// 卓を跨いで・タブを閉じても残る（端末/ブラウザ単位）。将来DB＋アカウントに紐付け移行する。
// キャラクター/名前は既存のものを使うので、ここでは「プロンプトエンジニアリング」だけを扱う。
//   prompts : リクエスト(initialize/talk/divine ...)ごとのプロンプト本文（変数トークン入り）。
//             lobby が変数を実行時Jinjaに解決して既定プロンプトを上書きする。
export interface MyAgent {
  prompts: Record<string, string>;
}

function isMyAgent(v: unknown): v is MyAgent {
  return (
    typeof v === "object" && v !== null &&
    typeof (v as MyAgent).prompts === "object" && (v as MyAgent).prompts !== null
  );
}

export const MY_AGENT_MAX_CHARS = 4000;

export const myAgent = createPersistentStore<MyAgent>({
  storageKey: "demo_my_agent_v2",
  defaultValue: { prompts: {} },
  validate: isMyAgent,
});
