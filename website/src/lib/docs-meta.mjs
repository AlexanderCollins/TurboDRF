// Where each docs page sits in the sidebar. The pages themselves are the repo's
// Markdown files in ../docs (plus AGENTS.md), so the site and GitHub never drift.
export const REPO_URL = 'https://github.com/AlexanderCollins/TurboDRF';

/** `settings_reference.md` -> `settings-reference` */
export function docSlug(file) {
  return file.replace(/\.md$/, '').toLowerCase().replace(/_/g, '-');
}

export const DOC_GROUPS = [
  { title: 'Getting started', pages: ['installation', 'quickstart'] },
  {
    title: 'Guide',
    pages: ['configuration', 'permissions', 'filtering', 'performance', 'commands', 'integrations'],
  },
  { title: 'Authorization & tenancy', pages: ['principles', 'tenancy', 'platform', 'security', 'migration-to-predicates'] },
  { title: 'Reference', pages: ['settings-reference', 'agents'] },
];

/** Sidebar labels where the page's own H1 is too long or too terse. */
export const SHORT_TITLES = {
  agents: 'AGENTS.md (for coding agents)',
  'migration-to-predicates': 'Migrating to predicates',
  'settings-reference': 'Settings reference',
  tenancy: 'Tenancy & predicates',
  commands: 'Management commands',
};
