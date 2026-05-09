# Settings Reference

Every TurboDRF setting in one place. Defaults shown in **bold**. Everything
that's not strictly required is gated and off by default.

---

## Core permissions

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_ROLES` | **`{}`** | Role → permissions mapping (static mode) | [permissions.md](permissions.md) |
| `TURBODRF_PERMISSION_MODE` | **`"static"`** | `"static"` (use `TURBODRF_ROLES` dict) or `"database"` (use `TurboDRFRole` model) | [permissions.md](permissions.md#static-vs-database-mode) |
| `TURBODRF_USE_DEFAULT_PERMISSIONS` | **`False`** | Use Django's built-in model permissions instead of TurboDRF roles | [permissions.md](permissions.md) |
| `TURBODRF_DISABLE_PERMISSIONS` | **`False`** | Disable all permission checks. **Don't use in production.** | — |
| `TURBODRF_PERMISSION_CACHE_TIMEOUT` | **`300`** | Permission snapshot cache TTL in seconds. Lower for high-stakes systems where role revocations need to take effect quickly. | [permissions.md](permissions.md#caching) |
| `TURBODRF_PERMISSION_CACHE_PREFIX` | **`"turbodrf_perm"`** | Prefix for cache keys | — |

## Field protection

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_SENSITIVE_FIELDS` | **`['password', 'password_hash', 'secret_key', 'api_key', 'token', 'access_token', 'refresh_token', 'session_key']`** | Field names ALWAYS stripped from responses, search, ordering, filters, and OPTIONS metadata — at every `__` segment of a path. Override to add app-specific deny entries. | [security.md](security.md#sensitive-fields) |
| `TURBODRF_MAX_NESTING_DEPTH` | **`3`** | Maximum `__` depth for nested fields, filters, and predicate paths. Values >3 are unsupported. | [filtering.md](filtering.md) |

## Row-level access control (predicates)

These activate the row-scoping system. When `TURBODRF_TENANT_MODEL` is unset
(default), the predicate system stays dormant — existing TurboDRF apps
upgrade with zero behavior change.

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_TENANT_MODEL` | **`None`** | Tenant model (e.g. `'accounts.Workspace'`). When set, every TurboDRF model must declare its tenancy or the router refuses to register it. | [tenancy.md](tenancy.md) |
| `TURBODRF_TENANT_USER_FIELD` | **`None`** | Attribute on `request.user` that resolves to the tenant (e.g. `'workspace'` so `request.user.workspace` returns the tenant). Required when `TURBODRF_TENANT_MODEL` is set. | [tenancy.md](tenancy.md#configuration) |
| `TURBODRF_REQUIRE_TENANCY` | **`True`** | Hard-fail at startup if a model has no tenancy decision (`tenant_field`, `visibility`, or `'tenancy': 'shared'`). Only triggers when `TURBODRF_TENANT_MODEL` is also set. | [tenancy.md](tenancy.md#hard-fail-at-startup) |
| `TURBODRF_AUTODETECT_TENANT` | **`False`** | Walk the FK graph at startup to find the shortest unique path to the tenant model. Off by default — explicit declarations are easier to reason about. | [tenancy.md](tenancy.md) |
| `TURBODRF_LOG_UNRESTRICTED_CUSTOM` | **`False`** | When `True`, log a warning whenever a `Custom` predicate's `q_func` returns an empty Q. Useful for catching developer footguns where a Custom predicate accidentally returns "no within-tenant restriction". | — |
| `TURBODRF_ALLOW_UNSAFE_COMPILED_M2M` | **`False`** | Bypass the startup gate that blocks compiled-path M2M nesting into predicate-bearing targets. Logs a loud warning per offending model. Intended for migrations only. | [security.md](security.md#compiled-m2m-target-bypass) |
| `TURBODRF_ALLOW_UNSAFE_COMPILED_FK` | **`False`** | Bypass the startup gate that blocks compiled-path FK annotations whose target model carries predicates the JOIN does not apply. Logs a loud warning per offending pair. Intended for migrations only. | [security.md](security.md#compiled-fk-annotation-bypass) |
| `TURBODRF_ALLOW_UNSAFE_SEARCH_FIELDS` | **`False`** | Bypass the startup gate that blocks `searchable_fields` paths whose target model carries predicates DRF's `SearchFilter` does not apply to the join. Logs a loud warning per offending entry. Intended for migrations only. | [security.md](security.md#search-field-target-bypass) |
| `TURBODRF_ALLOW_UNSAFE_FILTER_TRAVERSAL` | **`False`** | Disable the request-time scoping that wraps `__`-path filter URLs with target-model predicate / tenant Q's. When `True`, `?fk__field=...` JOINs without applying the target's own visibility rules. Migrations only; not recommended. | [security.md](security.md#url-driven-join-scoping) |

## Documentation (Swagger / OpenAPI)

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_ENABLE_DOCS` | **`True`** | Generate Swagger / ReDoc docs at `/swagger/` and `/redoc/`. Set to `False` to disable doc URLs entirely (e.g. on production deployments where you don't want a public schema). | — |
| `TURBODRF_SWAGGER_SHOW_ALL_FIELDS` | **`False`** | ⚠ **Dangerous if flipped to `True`.** Bypasses field-level permissions in the schema. Useful for development; never enable in production. There's no automatic guard for this in `DEBUG=False`. | [security.md](security.md) |

## Integrations (all experimental, all opt-in)

> All integrations are **experimental**: gated behind settings, unit-tested
> with mocks, but not verified end-to-end against real third-party servers
> in this codebase. See [integrations.md](integrations.md).

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_KEYCLOAK_INTEGRATION` | **`False`** | Enable Keycloak / OIDC role extraction | [integrations.md#keycloak](integrations.md#keycloak--openid-connect) |
| `TURBODRF_KEYCLOAK_ROLE_CLAIM` | **`"roles"`** | Dot-separated path to the roles claim in the JWT (e.g. `"realm_access.roles"`) | [integrations.md](integrations.md) |
| `TURBODRF_KEYCLOAK_ROLE_MAPPING` | **`{}`** | Keycloak role → TurboDRF role mapping. Acts as an allow-list when `TURBODRF_KEYCLOAK_STRICT_ROLES=True`. | [integrations.md](integrations.md) |
| `TURBODRF_KEYCLOAK_STRICT_ROLES` | **`True`** | When True (default) and a mapping is configured, unmapped Keycloak roles are dropped. Set False for legacy passthrough. | [integrations.md](integrations.md#strict-mode-default) |
| `TURBODRF_ALLAUTH_INTEGRATION` | **`False`** | Enable django-allauth Group → role mapping | [integrations.md#django-allauth](integrations.md#django-allauth) |
| `TURBODRF_ALLAUTH_ROLE_MAPPING` | **`{}`** | Django Group name → TurboDRF role | [integrations.md](integrations.md) |
| `TURBODRF_ENABLE_TRACKING` | **`False`** | Enable drf-api-tracking request/response logging | [integrations.md#drf-api-tracking](integrations.md#drf-api-tracking) |
| `TURBODRF_TRACKING_ANONYMOUS` | **`False`** | Track anonymous users (when tracking is enabled) | [integrations.md](integrations.md) |
| `TURBODRF_ENABLE_SENTRY` | **`False`** | Enable Sentry per-request tagging + security-event breadcrumbs (requires `sentry-sdk`) | [integrations.md#sentry](integrations.md#sentry) |

## Per-model configuration (in `turbodrf()` classmethod)

These are model-level toggles, not project settings. Documented here for
completeness.

| Key | Default | Purpose | Details |
|---|---|---|---|
| `'enabled'` | **`True`** | Whether to register the model as an API endpoint | [configuration.md](configuration.md) |
| `'compiled'` | **`True`** | Use the compiled `.values()` read path (faster than DRF serializer). Set to `False` to fall back to the DRF path if needed. | [performance.md](performance.md) |
| `'public_access'` | **`False`** | Allow unauthenticated GET. When `True`, anonymous users can read this model's endpoints (subject to field-level perms via the `guest` role). | [permissions.md](permissions.md) |
| `'fields'` | **`'__all__'`** | Fields to expose. List or `{'list': [...], 'detail': [...]}` dict. | [configuration.md](configuration.md) |
| `'endpoint'` | model name | Custom URL endpoint name | [configuration.md](configuration.md) |
| `'lookup_field'` | `'pk'` | URL lookup field (e.g. `'slug'`) | [configuration.md](configuration.md) |
| `'tenant_field'` | — | FK path to the tenant model (mandatory boundary) | [tenancy.md](tenancy.md) |
| `'owner_field'` | — | FK path(s) to the owner User | [tenancy.md](tenancy.md) |
| `'bypass_owner_roles'` | `[]` | Roles that ignore the owner check (still tenant-scoped) | [tenancy.md](tenancy.md) |
| `'visibility'` | — | Power-form predicate list (alternative to sugar form) | [tenancy.md](tenancy.md) |
| `'tenancy'` | — | Use `'shared'` to declare model is not tenant-scoped (reference data) | [tenancy.md](tenancy.md) |

## Postgres RLS (defense-in-depth, optional)

RLS is **off by default**. Three manual steps to enable:

1. Add `'turbodrf.rls.TurboDRFTenancyMiddleware'` to `MIDDLEWARE` (after `AuthenticationMiddleware`)
2. Run `python manage.py turbodrf_emit_rls > rls.sql` to generate draft policies
3. Review and apply the SQL via a Django RunSQL migration

There are no Postgres-RLS-specific settings — the middleware reads
`TURBODRF_TENANT_USER_FIELD` (already required for app-layer scoping) and
sets Postgres session vars from the request. See [rls.md](rls.md) for the
full setup including the non-superuser requirement.

## Defaults at a glance

If you start fresh and configure nothing, you get:
- RBAC active via `TURBODRF_ROLES` (you must define one for anything to work)
- Field permissions active
- Sensitive deny-list active (passwords/tokens never leak)
- Predicate system **dormant** (no `TURBODRF_TENANT_MODEL` set)
- All integrations **off**
- Compiled read path **on per-model**
- Swagger docs **on** at `/swagger/`
- Permission cache TTL **5 minutes**

To enable the predicate system: set `TURBODRF_TENANT_MODEL` and
`TURBODRF_TENANT_USER_FIELD`, then declare tenancy on each model.
See [tenancy.md](tenancy.md) and [migration_to_predicates.md](migration_to_predicates.md).
