# Changelog

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
