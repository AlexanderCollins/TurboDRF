# Changelog

## 0.5.1 — Permission-snapshot cache isolation fix

### Security

- **Fixed a permission-snapshot cache collision on user models with a
  non-`id` primary key (HIGH, broken access control / OWASP A01).** The
  cache key derived the user segment from `getattr(user, "id", "mock")`.
  A custom user model whose primary key is not named `id` (e.g. a UUID pk)
  has no `.id` attribute, so *every* such user collapsed to the single
  literal `"mock"` — one identity's cached action/field-permission snapshot
  was then served to all other users until the cache TTL expired
  (`TURBODRF_PERMISSION_CACHE_TIMEOUT`, default 300s). Row-level visibility
  predicates are not cached, so cross-row *reads* were unaffected; the
  exposure was action/field-level write escalation.

  The key now derives from `user.pk`, which resolves the real primary key
  on every Django user model regardless of field name. An authenticated
  user with no persistent pk (unsaved / mock) is treated as **uncacheable**
  (the snapshot is rebuilt per request) rather than falling back to a
  shared sentinel — a permissions snapshot is never shared across distinct
  identities. Anonymous callers continue to share one key by design (same
  guest/none role; predicates uncached).

  Reported against the 0.4.x line; on 0.5.0 the specific escalation was
  already mitigated because the user's own resolved roles were folded into
  the key hash (0.5.0), but the broken user segment was still a latent
  isolation defect and is now fixed outright.

## 0.5.0 — Security hardening, custom actions, conformance suite

### Security

Fixes from a full adversarial security audit. No cross-tenant isolation
break was found; these close field-level confidentiality gaps within a
tenant and robustness issues.

- **Sensitive fields are never filterable.** Fields matching the
  sensitive-name deny-list (`api_key`, `password`, …) were stripped from
  responses but still accepted as filter params, giving a blind
  value-confirmation / substring oracle for the hidden value
  (e.g. `?api_key__istartswith=sk-a`). Filters on them are now dropped.
- **Swagger schema no longer discloses to anonymous / unauthorized
  callers.** Previously an anonymous request received the full unfiltered
  OpenAPI schema, and any caller could pass `?role=admin` to preview a
  privileged role's schema. A role (query param or session) is now honored
  only when the caller actually holds it; with no held role the schema
  filters to empty.
- **Compiled read path denies instead of falling through when a role has
  zero readable fields.** The internal helpers returned `None` ("no
  snapshot — use full config") instead of an empty set, exposing every
  configured field to roles that should see none.
- **Compiled M2M merge is scoped at request time.** The merge's second
  query is now filtered to target rows visible via the target's own
  tenant + predicates (`scoped_target_queryset`), matching the DRF
  serializer path — defense-in-depth on top of the existing boot-time
  gate, and it holds even under `TURBODRF_ALLOW_UNSAFE_COMPILED_M2M`.
- **`__`-path filters scope the JOIN target.** Filtering across a
  relation (`?fk__field=x`) now ANDs in the target model's own
  tenant/predicate rules, closing a row-existence oracle. Opt out with
  `TURBODRF_ALLOW_UNSAFE_FILTER_TRAVERSAL` (not recommended).
- **Cross-tenant FK writes no longer confirm row existence.** The error
  for an inaccessible FK target is now indistinguishable from a
  nonexistent one ("not found or not accessible").
- **Model `@property` fields with sensitive names are excluded from
  API output**, mirroring the concrete-field deny-list.
- **`?search=` terms are length-capped** (`TURBODRF_MAX_FILTER_VALUE_LENGTH`,
  default 1000) via a new `TurboDRFSearchFilter`; oversized terms
  previously drove unbounded multi-field ILIKE scans (SQLite 500s,
  Postgres CPU burn). Filter values were already capped; search/ordering
  params now are too.
- **Fast JSON renderers can no longer 500 on non-native types.** msgspec /
  orjson encoders now coerce `Decimal`, `UUID`, lazy strings, paths, etc.
  via an `enc_hook` (Decimal stays a string for DRF parity — no float
  precision loss), with a last-resort fallback to DRF's `JSONRenderer`.
  DRF `ErrorDetail` in error responses encodes correctly with no
  exception-handler wiring required.
- **Custom action names that collide with ViewSet internals are rejected
  at boot** (`ImproperlyConfigured`) instead of silently shadowing
  methods like `get_queryset`.
- **Permission-snapshot cache keys use `hashlib`, not builtin `hash()`.**
  `hash()` is randomized per process, so multi-worker deployments sharing
  Redis/memcached silently computed different keys per worker.

### Added

- **Custom actions:** `@turbodrf_action` decorator
  (`turbodrf.decorators`) plus an `"actions"` list in the `turbodrf()`
  config. Actions attach to the generated viewset and inherit
  tenant/predicate scoping when they use `self.get_object()` /
  `self.get_queryset()`.
- **`"read_only": True`** model config — restricts the endpoint to
  GET/HEAD/OPTIONS (writes return 405). **`"http_methods": [...]`** for
  an explicit method allow-list.
- **`"full_clean": True`** model config — runs model `full_clean()`
  (validators, `clean()`, constraints) on API writes.
- **Model `@property` / computed fields in `fields`** render on both
  read paths (compiled and DRF serializer), with sensitive-named
  properties excluded.
- **`"searchable_fields"` in the `turbodrf()` config dict** — previously
  only the class-attribute form was read and the config-dict form
  silently did nothing.
- **Conformance test suite** (`tests/conformance/`): an independent
  oracle that recomputes each user's authorized view from raw DB facts
  (never calling the enforcement code) and asserts API output matches,
  plus Hypothesis fuzzing over hostile query params.

### Changed

- `TURBODRF_DISABLE_PERMISSIONS` and related settings are resolved
  per-request (`get_permissions()`) instead of frozen at import — the
  kill-switch and `override_settings` now take effect without a process
  restart.
- Renderers resolve per-request via `get_renderers()`, upgrading the
  stock `JSONRenderer` to the fast msgspec/orjson renderer in place. The
  project's `DEFAULT_RENDERER_CLASSES` is respected — e.g. removing the
  browsable API in production now works; previously a class-level
  override ignored it.
- Packaging: `setup.py` removed — `pyproject.toml` is authoritative;
  `tox.ini` matrix aligned with the supported Python (3.10+) / Django
  (4.2–6.0) range.

### Removed

- **Postgres RLS module (`turbodrf.rls`) and `turbodrf_emit_rls`
  command.** Keeping RLS policies in sync with the Python predicate
  config was hard to get right and a stale or misconfigured policy is
  a worse failure mode than not having one. TurboDRF now enforces
  row-level access at the application layer only. Teams that need
  DB-layer defense in depth should author and version Postgres RLS
  policies in Django `RunSQL` migrations directly. See `docs/security.md`
  for the rationale.
- `to_rls_using_clause()` / `to_rls_policy()` methods on `Predicate`,
  `Tenant`, `Owner`, `Either`.

## 0.4.0 — Predicate-based row-level access control

### Added

- **Django 6.0 support.** CI matrix tests Django 4.2, 5.2, and 6.0
  across Python 3.10–3.14 (with appropriate version exclusions per
  Django's official Python support matrix).
- **Parallel test execution by default** via `pytest-xdist` (`-n auto`
  in the pytest config). Full suite runs in ~12s on a 14-core machine
  vs 71s serial. Disable for debugging with `-p no:xdist`.
- Predicate system (`turbodrf.predicates`) for declarative row-level access
  control. Core primitives: `Tenant`, `Owner`, `Either`, `Custom`. Advanced
  primitives (importable but not surfaced in the main docs): `Members`,
  `Group`, `Conditional`. See `docs/tenancy.md`.
- Sugar form: `tenant_field`, `owner_field`, `bypass_owner_roles` in
  `turbodrf()` config — compiles to a predicate list internally.
- Hard-fail-at-startup when a model has no tenancy declaration and
  `TURBODRF_TENANT_MODEL` is set. Catches "forgot to scope this model" bugs
  at boot.
- Field-path validation at startup with did-you-mean suggestions.
- Bypass-role validation against `TURBODRF_ROLES` at startup (typo guard).
- FK injection defense: every FK in create/update is validated against the
  related model's predicate stack.
- Tenant reassignment rejection: PATCH cannot move a row to another tenant.
- Detail / PATCH / DELETE return 404 (not 403) on filtered rows — no
  existence leak.
- Optional Postgres RLS module (`turbodrf.rls`):
  - `TurboDRFTenancyMiddleware` sets `app.user_id`, `app.tenant_id`,
    `app.user_roles` per request.
  - `to_rls_using_clause()` / `to_rls_policy()` on predicates.
  - `turbodrf_emit_rls` management command.
- `turbodrf_check` now reports tenancy / predicates per model.
- New settings: `TURBODRF_TENANT_MODEL`, `TURBODRF_TENANT_USER_FIELD`,
  `TURBODRF_REQUIRE_TENANCY` (default `True`),
  `TURBODRF_AUTODETECT_TENANT` (default `False`).

### Fixed

- Exception handler now coerces DRF `ErrorDetail` to plain strings before the
  fast JSON renderer (msgspec/orjson) encodes them.
- `get_queryset()` now fails closed when called without a request (schema
  generation, programmatic use) instead of silently returning all rows.

### Security

- Closes the IDOR/BOLA, FK injection, and FK ownership validation gaps
  identified in the security audit. Multi-tenant isolation is now
  declarative on the model.
- Promotes tenant boundary to a first-class setting separate from the
  predicate algebra. The framework rejects `Tenant()` inside `Either`
  at startup — keeps OR-composition from escaping the tenant boundary.
- Removed `Custom(unrestricted_ok=...)` flag — no longer needed under the
  two-layer design (tenant boundary always enforced separately).

### Migration

Existing projects with no `TURBODRF_TENANT_MODEL` configured see no behavior
change. Multi-tenant projects: see `docs/migration_to_predicates.md`.
