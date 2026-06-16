// キャラクター名・プロフィールのローカライズ。
// 名前/性格はゲームサーバ(custom_profile)が割り当てる固定の24キャラ。サーバは全卓共通で
// 日本語を送ってくるため、表示だけ言語別データ(profiles/<lang>.json)で差し替える。
//   - キー(原名)はサーバの識別子そのまま（status_map/role_map/talk.agent、アバター対応に使う）。
//   - 表示名・性格文だけを言語に応じて返す。サーバへ送る値は常に原名のまま。
import { normalizeLanguage } from "$lib/stores/language";

import ja from "$lib/data/profiles/ja.json";
import en from "$lib/data/profiles/en.json";
import zh from "$lib/data/profiles/zh.json";
import hi from "$lib/data/profiles/hi.json";
import es from "$lib/data/profiles/es.json";
import ar from "$lib/data/profiles/ar.json";
import bn from "$lib/data/profiles/bn.json";
import fr from "$lib/data/profiles/fr.json";
import ru from "$lib/data/profiles/ru.json";
import pt from "$lib/data/profiles/pt.json";
import ur from "$lib/data/profiles/ur.json";
import id from "$lib/data/profiles/id.json";
import de from "$lib/data/profiles/de.json";
import nl from "$lib/data/profiles/nl.json";

export interface ProfileEntry {
  name: string;
  personality: string;
  avatar?: string; // ローカルアバターパス(例 /images/male/01.png)。gen_i18n.py が埋め込む。
}

type ProfileTable = Record<string, ProfileEntry>;

const TABLES: Record<string, ProfileTable> = {
  ja, en, zh, hi, es, ar, bn, fr, ru, pt, ur, id, de, nl,
};

// 原名キーの正本順（= サーバ custom_profile の profiles 並び）。character の index はこの順。
const CANONICAL_KEYS: string[] = Object.keys(ja);

/** キャラ選択用の一覧。index はサーバの profiles 並びと一致（?character=<index> でそのまま渡せる）。
 *  name / avatar は指定ロケールの表示用。 */
export function characterList(
  locale: string | null | undefined,
): { index: number; name: string; avatar: string | null }[] {
  const lang = normalizeLanguage(locale, "ja");
  return CANONICAL_KEYS.map((jp, index) => {
    const e = TABLES[lang]?.[jp] ?? TABLES.ja[jp];
    return { index, name: e?.name ?? jp, avatar: e?.avatar ?? null };
  });
}

/** 原名(サーバ名)を、指定言語の表示名に変換する。未登録なら原名をそのまま返す。 */
export function localizedName(name: string | null | undefined, locale: string | null | undefined): string {
  if (!name) return "—";
  const lang = normalizeLanguage(locale, "ja");
  return TABLES[lang]?.[name]?.name ?? TABLES.ja?.[name]?.name ?? name;
}

/** 原名に対応する性格文(プロフィール)を指定言語で返す。未登録なら fallback、無ければ null。 */
export function localizedPersonality(
  name: string | null | undefined,
  locale: string | null | undefined,
  fallback: string | null = null,
): string | null {
  if (!name) return fallback;
  const lang = normalizeLanguage(locale, "ja");
  return TABLES[lang]?.[name]?.personality ?? TABLES.ja?.[name]?.personality ?? fallback;
}

/** 受け取った名前(サーバの GameName=現地名 or 原名)から avatar(ローカルパス)を解決する。
 *  言語別サーバは現地名を送るので、現ロケールのテーブルを「表示名→avatar」で逆引きする。
 *  見つからなければ ja テーブル(原名キー)も試し、それも無ければ null。 */
export function localizedAvatar(
  name: string | null | undefined,
  locale: string | null | undefined,
): string | null {
  if (!name) return null;
  const lang = normalizeLanguage(locale, "ja");
  const find = (t: ProfileTable | undefined): string | null => {
    if (!t) return null;
    // 原名キー直引き（ja サーバや原名がそのまま来た場合）
    if (t[name]?.avatar) return t[name].avatar ?? null;
    // 表示名で逆引き（言語別サーバの現地名）
    for (const e of Object.values(t)) {
      if (e.name === name && e.avatar) return e.avatar;
    }
    return null;
  };
  return find(TABLES[lang]) ?? find(TABLES.ja) ?? null;
}
