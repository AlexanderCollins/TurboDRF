# turbodrf.com

The TurboDRF website and docs: Astro (static) with React islands, Tailwind 4 and
daisyUI 5, self-hosted fonts, Pagefind search. Served from S3 behind CloudFront at
https://turbodrf.com (www redirects to the apex).

```bash
bun install
bun dev            # http://localhost:4321
bun run check      # astro check
bun run build      # dist/, then the Pagefind index
infra/deploy.sh    # apply the AWS stacks, upload dist/, invalidate CloudFront
```

## Where things come from

- **Docs** are the repo's own Markdown: every page in `../docs` plus `../AGENTS.md`
  and `../CHANGELOG.md`, read by `src/content.config.ts`. Sidebar order is
  `src/lib/docs-meta.mjs`; links like `tenancy.md#x` are rewritten to `/docs/tenancy/#x`
  (`src/lib/rewrite-doc-links.mjs`). Edit the docs in `../docs`, never here.
- **Every API response on the site is real.** `tools/demo` is a small TurboDRF project;
  `tools/demo/capture.py` runs it against in-memory SQLite, makes each request the site
  shows and writes `src/data/demo.json`. The home page's model view and permission
  matrix read that file (`src/lib/showcase.ts`), and the code shown is the demo's own
  source (`?raw` imports). Re-run it after a TurboDRF change:
  `cd tools/demo && ../../../venv/bin/python capture.py`.
- **Social cards and icons**: `tools/og/render.py` (Playwright) draws `public/og/*.jpg`
  from `tools/og/template.html`, and the favicons from `public/brand/icon.svg`.

`tools/.venv` holds the Python tools (`playwright pillow`, then
`playwright install chromium`).

## Deploy

`infra/deploy.sh` applies `infra/website-certs.yml` (ACM, us-east-1) and
`infra/website.yml` (bucket, CloudFront, routing function, Route 53 records in the
`turbodrf.com` hosted zone), syncs `dist/` with per-file cache headers and invalidates
CloudFront. `infra/deploy.sh plan` previews the AWS changes. Run it with credentials for
the AWS account that owns the domain.

Deploy after a release, so the docs match what's on PyPI.
