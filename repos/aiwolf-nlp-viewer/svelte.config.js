import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

const config = {
	preprocess: vitePreprocess(),

	kit: {
		adapter: adapter({
			pages: 'build',
			assets: 'build',
			fallback: null,
		}),
		paths: {
			// BASE_PATH を指定すると base を上書き（会場の Caddy でルート配信する場合は BASE_PATH="" ）。
			// 未指定時は従来どおり（GitHub Pages 用に本番は /aiwolf-nlp-viewer）。
			base: process.env.BASE_PATH !== undefined
				? process.env.BASE_PATH
				: (process.env.NODE_ENV === 'production' ? '/aiwolf-nlp-viewer' : ''),
		}
	}
};

export default config;
