import { browser } from "$app/environment";
import { locale } from 'svelte-i18n';
import { createPersistentStore } from "./store-utils";

// 対応言語（UI言語。ゲーム言語のセレクタもこの一覧を流用する）。
// label はその言語のネイティブ表記。新言語追加時はここと src/i18n/<code>.json、
// src/lib/i18n.ts の register、configs/agents/prompts/<code>.yml を揃える。
export const LANGUAGES = [
    { code: 'ja', label: '日本語' },
    { code: 'en', label: 'English' },
    { code: 'zh', label: '中文' },
    { code: 'hi', label: 'हिन्दी' },
    { code: 'es', label: 'Español' },
    { code: 'ar', label: 'العربية' },
    { code: 'bn', label: 'বাংলা' },
    { code: 'fr', label: 'Français' },
    { code: 'ru', label: 'Русский' },
    { code: 'pt', label: 'Português' },
    { code: 'ur', label: 'اردو' },
    { code: 'id', label: 'Bahasa Indonesia' },
    { code: 'de', label: 'Deutsch' },
    { code: 'nl', label: 'Nederlands' },
] as const;

export type Language = (typeof LANGUAGES)[number]['code'];

const SUPPORTED_LANGUAGES: Language[] = LANGUAGES.map((l) => l.code);

// 右から左へ書く言語（UI 方向を dir=rtl に切り替えるのに使う）
export const RTL_LANGUAGES: Language[] = ['ar', 'ur'];

export function isValidLanguage(lang: unknown): lang is Language {
    return typeof lang === 'string' && SUPPORTED_LANGUAGES.includes(lang as Language);
}

// 任意の文字列（例: navigator.language="en-US" や API の lang）を対応言語コードへ正規化。
export function normalizeLanguage(lang: string | null | undefined, fallback: Language = 'ja'): Language {
    if (!lang) return fallback;
    const lower = lang.toLowerCase();
    const exact = SUPPORTED_LANGUAGES.find((c) => lower === c);
    if (exact) return exact;
    const prefix = SUPPORTED_LANGUAGES.find((c) => lower.startsWith(c));
    return prefix ?? fallback;
}

function getBrowserLanguage(): Language {
    if (browser) {
        const browserLang = navigator.language || navigator.languages?.[0];
        return normalizeLanguage(browserLang, 'ja');
    }
    return 'ja';
}

export const language = createPersistentStore<Language>({
    storageKey: 'language',
    defaultValue: getBrowserLanguage(),
    validate: isValidLanguage
});

language.subscribe((lang) => {
    locale.set(lang);
    if (browser) {
        // <html lang> と書字方向（アラビア語・ウルドゥー語は RTL）を言語に追従させる。
        document.documentElement.lang = lang;
        document.documentElement.dir = RTL_LANGUAGES.includes(lang) ? 'rtl' : 'ltr';
    }
});
