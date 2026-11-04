import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { DOC_GROUPS, docSlug } from './lib/docs-meta.mjs';

// The repo's own docs: only the pages DOC_GROUPS lists, so a stray Markdown
// file in ../docs (notes, drafts) is never published.
const PAGES = DOC_GROUPS.flatMap((g) => g.pages)
  .filter((slug) => slug !== 'agents')
  .map((slug) => `${slug.replace(/-/g, '_')}.md`);

const docs = defineCollection({
  loader: glob({
    pattern: PAGES,
    base: '../docs',
    generateId: ({ entry }) => docSlug(entry),
  }),
});

// AGENTS.md and CHANGELOG.md from the repo root.
const root = defineCollection({
  loader: glob({
    pattern: ['AGENTS.md', 'CHANGELOG.md'],
    base: '..',
    generateId: ({ entry }) => docSlug(entry),
  }),
});

export const collections = { docs, root };
