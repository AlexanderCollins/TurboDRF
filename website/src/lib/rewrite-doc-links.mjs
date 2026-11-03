// The docs are the repo's own Markdown (../docs, AGENTS.md, CHANGELOG.md), written
// with links between files for GitHub. On the site each file is a page, so a link
// to `tenancy.md#x` becomes `/docs/tenancy/#x`, and a link to a file that isn't a
// page goes to the file on GitHub.
import { REPO_URL, docSlug } from './docs-meta.mjs';

const PAGES = { 'AGENTS.md': '/docs/agents/', 'CHANGELOG.md': '/changelog/' };

function rewrite(url) {
  if (/^[a-z]+:|^#|^\//i.test(url)) return url;
  const [path, hash = ''] = url.split('#');
  const anchor = hash ? `#${hash}` : '';
  const file = path.replace(/^(\.\/|\.\.\/|docs\/)+/, '');
  if (PAGES[file]) return PAGES[file] + anchor;
  if (/^[\w-]+\.md$/.test(file) && !path.startsWith('../')) {
    return `/docs/${docSlug(file)}/${anchor}`;
  }
  const fromRoot = path.startsWith('../') ? file : `docs/${file}`;
  return `${REPO_URL}/blob/main/${fromRoot}${anchor}`;
}

export function rewriteDocLinks() {
  return (tree) => {
    const visit = (node) => {
      if ((node.type === 'link' || node.type === 'definition') && node.url) {
        node.url = rewrite(node.url);
      }
      if (node.children) node.children.forEach(visit);
    };
    visit(tree);
  };
}
