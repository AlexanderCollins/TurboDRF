import { getCollection, getEntry, type CollectionEntry } from 'astro:content';
import { DOC_GROUPS, SHORT_TITLES } from './docs-meta.mjs';

export type DocEntry = CollectionEntry<'docs'> | CollectionEntry<'root'>;

/** A page's title: its first `# Heading`. */
export function docTitle(entry: DocEntry): string {
  const match = entry.body?.match(/^#\s+(.+)$/m);
  return match ? match[1].replace(/`/g, '').trim() : entry.id;
}

/** The first paragraph after the title, for meta descriptions and the docs index. */
export function docSummary(entry: DocEntry): string {
  const body = (entry.body ?? '').replace(/^#\s+.+$/m, '');
  const para = body
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .find((p) => p && !p.startsWith('#') && !p.startsWith('```') && !p.startsWith('|') && !p.startsWith('-') && !p.startsWith('>'));
  if (!para) return '';
  const text = para.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/[`*_]/g, '').replace(/\s+/g, ' ');
  return text.length > 180 ? text.slice(0, 177).replace(/\s+\S*$/, '') + '…' : text;
}

/** Every docs page, in sidebar order: the repo's docs/ plus AGENTS.md. */
export async function allDocs(): Promise<DocEntry[]> {
  const docs = await getCollection('docs');
  const agents = await getEntry('root', 'agents');
  return agents ? [...docs, agents] : docs;
}

export async function docNav() {
  const entries = new Map((await allDocs()).map((e) => [e.id, e]));
  const placed = new Set<string>();
  const groups = DOC_GROUPS.map((g: { title: string; pages: string[] }) => ({
    title: g.title,
    pages: g.pages
      .filter((slug) => entries.has(slug))
      .map((slug) => {
        placed.add(slug);
        const e = entries.get(slug)!;
        return { slug, title: (SHORT_TITLES as Record<string, string>)[slug] ?? docTitle(e), summary: docSummary(e) };
      }),
  }));
  // Only DOC_GROUPS' pages are loaded (content.config.ts); this keeps any other
  // entry (none today) visible rather than dropped.
  const rest = [...entries.keys()].filter((slug) => !placed.has(slug));
  if (rest.length) {
    groups.push({
      title: 'More',
      pages: rest.map((slug) => {
        const e = entries.get(slug)!;
        return { slug, title: docTitle(e), summary: docSummary(e) };
      }),
    });
  }
  return groups.filter((g: { pages: unknown[] }) => g.pages.length);
}
