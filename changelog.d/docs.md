### Changed

- **Docs:** installation and quick start are Markdown pages
  (`docs/installation.md`, `docs/quickstart.md`) and the single source for
  both; `INSTALL.md` points to them. The docs are published at
  <https://turbodrf.com/docs/> (the package's Documentation URL; Homepage is
  <https://turbodrf.com>).
- **Docs:** `Either` write semantics documented: a write must pass every
  child's check, so `Owner`'s rules hold inside `Either`, and `Either` fills a
  single-column `Owner` child on create. Multi-owner examples use
  `Owner(['author', 'reviewer'])` instead of `Either(Owner, Owner)`.
  `Members`, `Group` and `Conditional` signatures are documented.
- **Docs:** the startup checks (tenant field paths, `TURBODRF_TENANT_USER_FIELD`,
  `fields` / `lookup_field` names, `Group(user_via=...)`) are listed, with
  the advice to run `python manage.py check` in CI and before deploys.
- **Docs:** allauth / Keycloak role middleware documented as deprecated
  no-ops.
- `AGENTS.md` covers downstream projects only; contributor notes moved to
  the new `CONTRIBUTING.md`, which also documents the release steps.
- `example_project/` is one small store-scoped example with a README and an
  idempotent `manage.py seed`.
- **CI:** publishing a release first runs the tests and lint, checks that the
  tag matches the package version, and runs `twine check`. Actions are pinned
  to commit SHAs; workflows have `permissions`, pip caching and concurrency
  cancelling. The docs build fails on warnings.
- **CI / dev:** black, isort and flake8 are pinned in the `dev` extra
  (`make lint` runs them, locally and in CI); the `dev` extra adds Pillow
  and drops mypy.
- **Packaging:** SPDX `license = "MIT"` with `license-files`, setuptools
  >= 77; the sdist includes the root `conftest.py` the tests need.

### Fixed

- The README quick start produced a 403 on every request and silently
  dropped `author__name`: it now defines roles, gives users a `roles`
  property, imports what it uses, and says rows aren't scoped until tenancy
  is configured. README links are absolute so they work on PyPI.
- `TURBODRF_DISABLE_PERMISSIONS` is documented as local-development only (it
  lets anonymous callers write).
- Docs: the tenant model and the user model have a tested recipe; endpoint
  names are "model name + s", not pluralized; database mode ignores
  `user.roles`; `turbodrf_benchmark --warmup` is documented; unknown
  `turbodrf()` keys are called out as silently ignored; `TURBODRF_TENANT_MODEL`
  no longer described as turning predicates on.
- The example project's editor role couldn't create books (500): it now has
  `books.book.price.write`; permissions.md explains why.
- Removed broken or unused tooling: the release scripts (they edited the
  removed `setup.py` and pushed to `main`), `tox.ini`, the stray `pytest.ini `,
  `.pre-commit-config.yaml`, the disabled Security workflow, the workflow that
  committed coverage badges to `main` (the README badge now reads the gist CI
  already updates), `examples/`, and the example project's scratch scripts.
  `coverage.json` is no longer tracked.
