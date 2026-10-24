import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { docSlug } from './lib/docs-meta.mjs';

// The repo's own docs: every Markdown page in ../docs except build notes.
const docs = defineCollection({
  loader: glob({
    pattern: ['*.md', '!security_audit.md'],
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
