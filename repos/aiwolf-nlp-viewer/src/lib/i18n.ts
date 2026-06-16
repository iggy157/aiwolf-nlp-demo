import { getLocaleFromNavigator, init, register } from 'svelte-i18n';

// UI 翻訳の登録。ja/en は本物、それ以外は en.json のコピー（_meta.translated=false）。
// 翻訳が未了のキーは fallbackLocale(ja) に落ちる。新言語追加時はここに register を1行足す。
register('ja', () => import('../i18n/ja.json'));
register('en', () => import('../i18n/en.json'));
register('zh', () => import('../i18n/zh.json'));
register('hi', () => import('../i18n/hi.json'));
register('es', () => import('../i18n/es.json'));
register('ar', () => import('../i18n/ar.json'));
register('bn', () => import('../i18n/bn.json'));
register('fr', () => import('../i18n/fr.json'));
register('ru', () => import('../i18n/ru.json'));
register('pt', () => import('../i18n/pt.json'));
register('ur', () => import('../i18n/ur.json'));
register('id', () => import('../i18n/id.json'));
register('de', () => import('../i18n/de.json'));
register('nl', () => import('../i18n/nl.json'));

init({
  fallbackLocale: 'ja',
  initialLocale: getLocaleFromNavigator(),
});
