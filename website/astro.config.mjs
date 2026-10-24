import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';
import turboTheme from './src/styles/shiki-turbo.json' with { type: 'json' };
import { rewriteDocLinks } from './src/lib/rewrite-doc-links.mjs';

export default defineConfig({
  site: 'https://turbodrf.com',
  output: 'static',
  trailingSlash: 'always',
  integrations: [react(), sitemap()],
  // Pages are fetched on hover, so moving between them comes from the browser cache.
  prefetch: { prefetchAll: true, defaultStrategy: 'hover' },
  markdown: {
    shikiConfig: { theme: turboTheme, wrap: false },
    remarkPlugins: [rewriteDocLinks],
  },
  vite: {
    plugins: [tailwindcss()],
    server: { fs: { allow: ['..'] } },
  },
});
