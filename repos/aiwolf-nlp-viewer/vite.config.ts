import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from 'vite';

// BASE_PATH を指定すると vite base を上書き（会場ルート配信は BASE_PATH="" → "/"）。
// 未指定時は従来どおり /aiwolf-nlp-viewer/。
const basePath =
	process.env.BASE_PATH !== undefined
		? process.env.BASE_PATH === ""
			? "/"
			: `${process.env.BASE_PATH}/`
		: "/aiwolf-nlp-viewer/";

export default defineConfig({
	base: basePath,
	plugins: [tailwindcss(), sveltekit()],
});
