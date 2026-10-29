# Settings Reference

Every TurboDRF setting in one place. Defaults shown in **bold**. Everything
that's not strictly required is gated and off by default.

---

## Core permissions

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_ROLES` | **`{}`** | Role → permissions mapping (static mode). Unset, no role grants anything. | [permissions.md](permissions.md) |
| `TURBODRF_PERMISSION_MODE` | **`"static"`** | `"static"` (use `TURBODRF_ROLES` dict) or `"database"` (use `TurboDRFRole` model) | [permissions.md](permissions.md#static-vs-database-mode) |
| `TURBODRF_USE_DEFAULT_PERMISSIONS` | **`False`** | Use Django's built-in model permissions instead of TurboDRF roles | [permissions.md](permissions.md) |
| `TURBODRF_DISABLE_PERMISSIONS` | **`False`** | Disable all permission checks, authentication included: anonymous callers can create, update and delete through the generated endpoints. **Local development only.** With `DEBUG = False`, `manage.py check` warns (`turbodrf.W002`). | [permissions.md](permissions.md#1-no-permissions-local-development-only) |
| `TURBODRF_PERMISSION_CACHE_TIMEOUT` | **`300`** | Permission snapshot cache TTL in seconds. Role changes already take effect on the next request; the TTL only bounds how long snapshots stay stale after a bulk `RolePermission` QuerySet `update()` / `delete()` (or call `invalidate_user_permissions()`). | [permissions.md](permissions.md#caching) |
| `TURBODRF_PERMISSION_CACHE_PREFIX` | **`"turbodrf_perm"`** | Prefix for cache keys | — |

## Platform scope

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_PLATFORM_ROLES` | **`None`** | Platform role → permissions mapping, the same shape as `TURBODRF_ROLES` and separate from it. Required (a non-empty dict) by a `TurboDRFRouter(scope="platform")`, which validates its permission strings at startup. Always static: database permission mode applies to the tenant scope only. | [platform.md](platform.md#roles-and-permissions) |
| `TURBODRF_PLATFORM_ROLE_GETTER` | **`None`** (`user.platform_roles`) | Where a user's platform roles come from: a dotted path or a callable `user -> list of role names`. Anonymous callers have none. | [platform.md](platform.md#roles-and-permissions) |

`TURBODRF_DISABLE_PERMISSIONS` and `TURBODRF_USE_DEFAULT_PERMISSIONS` don't
apply to the platform scope: every platform request is checked against
`TURBODRF_PLATFORM_ROLES`.

## Field protection

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_SENSITIVE_FIELDS` | **`['password', 'password_hash', 'secret_key', 'api_key', 'token', 'access_token', 'refresh_token', 'session_key']`** | Field names never exposed, at every `__` segment of a path: stripped from responses (`"__all__"` included), filters, search, ordering, OPTIONS metadata and the schema, in every permission mode. Your list is added to the defaults; it can't remove them. | [security.md](security.md#sensitive-fields) |
| `TURBODRF_MAX_NESTING_DEPTH` | **`3`** | Maximum number of `__` in nested fields and filters. Deeper fields are skipped with a warning. Values >3 are unsupported. | [configuration.md](configuration.md#nested-fields) |
| `TURBODRF_MAX_FILTER_VALUE_LENGTH` | **`1000`** | Maximum length of a filter value or `?search=` term. A request with a longer one returns no rows, which bounds filter and search cost. | [filtering.md](filtering.md) |

## Row-level access control (predicates)

A model's `tenant_field`, `owner_field` and `visibility` are enforced whether
or not `TURBODRF_TENANT_MODEL` is set. Setting it turns on the startup checks
that every model declares its tenancy and that each `tenant_field` ends at the
tenant model, plus (opt-in) auto-detection.

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_TENANT_MODEL` | **`None`** | Tenant model (e.g. `'accounts.Workspace'`). When set, every TurboDRF model must declare its tenancy and each `tenant_field` must end at this model, or the router refuses to start. | [tenancy.md](tenancy.md) |
| `TURBODRF_TENANT_USER_FIELD` | **`None`** | Attribute on `request.user` that resolves to the tenant (e.g. `'workspace'` so `request.user.workspace` returns the tenant). It must return a model instance or an integer pk; anything else counts as no tenant, and tenant-scoped queries match nothing. Required whenever a model declares a `tenant_field`: the router refuses to start without it. | [tenancy.md](tenancy.md#settings) |
| `TURBODRF_REQUIRE_TENANCY` | **`True`** | Hard-fail at startup if a model has no tenancy decision (`tenant_field`, `'tenancy': 'shared'`, or a `visibility` list with a predicate that restricts every caller; `owner_field` alone doesn't count). Only triggers when `TURBODRF_TENANT_MODEL` is also set. | [tenancy.md](tenancy.md#hard-fail-at-startup) |
| `TURBODRF_AUTODETECT_TENANT` | **`False`** | Walk the FK graph at startup to find the shortest unique path to the tenant model (needs `TURBODRF_TENANT_MODEL`). Off by default — explicit declarations are easier to reason about. | [tenancy.md](tenancy.md) |
| `TURBODRF_ALLOW_UNKNOWN_PERMISSIONS` | **`False`** | Skip the startup typo check for permission strings whose `app.model` isn't yet registered (plugin systems, lazy apps). Permissions on registered models are still checked. | [security.md](security.md#permission-string-typo-gate) |

## Documentation (Swagger / OpenAPI)

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_ENABLE_DOCS` | **`True`** | Generate Swagger / ReDoc docs at `/swagger/` and `/redoc/`. Set to `False` to disable doc URLs entirely (e.g. on production deployments where you don't want a public schema). Requires `'drf_yasg'` in `INSTALLED_APPS`; TurboDRF raises system-check warning `turbodrf.W001` if it's missing. | — |

The schema is scoped to whoever requests it. It lists only TurboDRF
operations that the caller's roles allow (and that the endpoint's
permission classes admit), request bodies contain only fields the caller
can write, response bodies only fields they can read,
`TURBODRF_SENSITIVE_FIELDS` never appear, and `definitions` only include
the serializers those operations use. An anonymous caller sees only the
read operations of `public_access` models, with only the fields it can read
(see [permissions.md](permissions.md#anonymous-callers-and-the-guest-role)).
By default the schema reflects all of
the caller's roles; `?role=<name>` narrows it to one role the caller
already holds (static permission mode only) and can never widen it.

`TURBODRF_SWAGGER_SHOW_ALL_FIELDS` has been removed. It never had an
effect; setting it does nothing.

## Integrations (all experimental, all opt-in)

> All integrations are **experimental**: gated behind settings, unit-tested
> with mocks, but not verified end-to-end against real third-party servers
> in this codebase. See [integrations.md](integrations.md).

| Setting | Default | Purpose | Details |
|---|---|---|---|
| `TURBODRF_KEYCLOAK_INTEGRATION` | **`False`** | Enable Keycloak / OIDC role mapping. When `True` at startup, TurboDRF installs a `roles` property on the user model (unless it already defines `roles`), so session, token and JWT requests get the same roles. No middleware is needed: `KeycloakRoleMiddleware` is a deprecated no-op. | [integrations.md#keycloak](integrations.md#keycloak--openid-connect) |
| `TURBODRF_KEYCLOAK_PROVIDER` | **`"keycloak"`** | social-auth provider name whose stored data is read for roles. Other linked providers (GitHub, Google, ...) are ignored. | [integrations.md](integrations.md#where-the-roles-come-from) |
| `TURBODRF_KEYCLOAK_ROLE_CLAIM` | **`"roles"`** | Dot-separated path to the roles claim in the claims social-auth stored at login (`extra_data`), e.g. `"realm_access.roles"` | [integrations.md](integrations.md) |
| `TURBODRF_KEYCLOAK_ROLE_MAPPING` | **`{}`** | Keycloak role → TurboDRF role mapping. Acts as an allow-list when `TURBODRF_KEYCLOAK_STRICT_ROLES=True`. | [integrations.md](integrations.md) |
| `TURBODRF_KEYCLOAK_STRICT_ROLES` | **`True`** | When True (default) and a mapping is configured, unmapped Keycloak roles are dropped. Set False for legacy passthrough. | [integrations.md](integrations.md#strict-mode-default) |
| `TURBODRF_KEYCLOAK_MAX_ROLE_AGE` | **`None`** | Seconds. Roles are captured at login and otherwise last for the session; when set, they're dropped once the stored `auth_time` is older than this (or missing). Doesn't hold if your app refreshes tokens with `UserSocialAuth.refresh_token()`, which resets `auth_time`. | [integrations.md](integrations.md#where-the-roles-come-from) |
| `TURBODRF_ALLAUTH_INTEGRATION` | **`False`** | Enable django-allauth Group → role mapping. When `True` at startup, TurboDRF installs a `roles` property on the user model (unless it already defines `roles`), so session, token and JWT requests get the same roles. No middleware is needed: `AllAuthRoleMiddleware` is a deprecated no-op. | [integrations.md#django-allauth](integrations.md#django-allauth) |
| `TURBODRF_ALLAUTH_ROLE_MAPPING` | **`{}`** | Django Group name → TurboDRF role. Acts as an allow-list when `TURBODRF_ALLAUTH_STRICT_ROLES=True`. | [integrations.md](integrations.md) |
| `TURBODRF_ALLAUTH_STRICT_ROLES` | **`True`** | When True (default) and a mapping is configured, groups not in the mapping give no role (and are logged). Set False for legacy passthrough of unmapped group names. | [integrations.md](integrations.md#strict-group-mapping-default) |
| `TURBODRF_ENABLE_TRACKING` | **`False`** | Enable drf-api-tracking request/response logging (needs `'rest_framework_tracking'` in `INSTALLED_APPS`). Values of `TURBODRF_SENSITIVE_FIELDS` keys are masked in the logs, raw bodies of requests rejected before parsing included. | [integrations.md#drf-api-tracking](integrations.md#drf-api-tracking) |
| `TURBODRF_TRACKING_ANONYMOUS` | **`False`** | When tracking is enabled, also log requests from anonymous (unauthenticated) callers. By default they aren't logged. | [integrations.md](integrations.md#drf-api-tracking) |
| `TURBODRF_ENABLE_SENTRY` | **`False`** | Enable Sentry user/tenant tagging + security-event breadcrumbs (requires `sentry-sdk`). On events from authenticated requests the Sentry user is just `{"id": <pk>}`; anonymous events and the request data sentry-sdk attaches follow `send_default_pii`. | [integrations.md#sentry](integrations.md#sentry) |

## Per-model configuration (in `turbodrf()` classmethod)

These are model-level toggles, not project settings. Documented here for
completeness. The table lists every valid key: any other key raises
`ImproperlyConfigured` at startup, with the valid keys in the message.

| Key | Default | Purpose | Details |
|---|---|---|---|
| `'enabled'` | **`True`** | Whether to register the model as an API endpoint. A disabled model's tenancy is still registered (and checked). | [configuration.md](configuration.md) |
| `'public_access'` | **`False`** | Allow unauthenticated GET / HEAD / OPTIONS. Anonymous callers get model-level read; a field with a read rule on any role needs a `guest` role grant. | [permissions.md](permissions.md#anonymous-callers-and-the-guest-role) |
| `'fields'` | **`'__all__'`** | Fields to expose; always set it explicitly. List or `{'list': [...], 'detail': [...]}` dict (a dict with only one key uses it for both views). May include model `@property` / computed read fields (read-only). An entry that names nothing on the model, or ends on a reverse one-to-one, raises at startup. | [configuration.md](configuration.md) |
| `'property_relations'` | `{}` | `{property: [relation path, ...]}`: the related objects a `@property` reads, loaded within the caller's scope. Properties run in a sandbox and can't query. | [configuration.md](configuration.md#the-property-sandbox) |
| `'unscoped_properties'` | `[]` | Properties that run outside the sandbox, on the full instance with unrestricted queries. | [configuration.md](configuration.md#the-property-sandbox) |
| `'endpoint'` | model name + `s` | Custom URL endpoint name. The default is the lowercase model name plus `s`, not a real plural (`categorys`). | [configuration.md](configuration.md) |
| `'lookup_field'` | `'pk'` | URL lookup field (e.g. `'slug'`). Must be a unique column of the model, or the router raises at startup. | [configuration.md](configuration.md) |
| `'searchable_fields'` | — | Field names / `__`-paths enabled for `?search=` (gated by field-level read perms). Also settable as a class attribute; the key wins when present, even as an empty list (`[]` turns search off). | [configuration.md](configuration.md#searchable-fields) |
| `'read_only'` | **`False`** | Serve `list` / `retrieve` only; CRUD writes (POST/PUT/PATCH/DELETE) return `405`. Declared `actions` keep their own methods. | [configuration.md](configuration.md#read-only-endpoints) |
| `'http_methods'` | all | Explicit HTTP method allow-list (e.g. `['get', 'post']`); disallowed verbs return `405`. `read_only` is the common shorthand. | [configuration.md](configuration.md#restricting-http-methods) |
| `'full_clean'` | **`False`** | Run Django model validation (`clean()` / constraints) on write, so business-rule violations return `400` instead of `500`. | [configuration.md](configuration.md#model-validation-full_clean) |
| `'actions'` | `[]` | Custom endpoints — a list of `@turbodrf_action`-decorated handlers attached to the generated viewset (inherit tenant + predicate scoping). | [configuration.md](configuration.md#custom-endpoints-actions) |
| `'platform'` | — | The model's config on a platform router (staff across tenants): the keys above that shape an API, no tenancy keys. Without it the model isn't served there. | [platform.md](platform.md#the-platform-block) |
| `'tenant_field'` | — | Path of forward FKs / one-to-ones to the tenant model (mandatory boundary) | [tenancy.md](tenancy.md) |
| `'owner_field'` | — | FK path(s) to the owner User. A list is one `Owner` over all the columns. Doesn't satisfy `TURBODRF_REQUIRE_TENANCY` on its own. | [tenancy.md](tenancy.md) |
| `'bypass_owner_roles'` | `[]` | Roles that ignore the owner check (still tenant-scoped) | [tenancy.md](tenancy.md) |
| `'visibility'` | — | Power-form predicate list (alternative to sugar form). Can't be empty without a `tenant_field`, and counts as a tenancy decision on its own only if one of its predicates restricts every caller. | [tenancy.md](tenancy.md) |
| `'tenancy'` | — | Use `'shared'` to declare model is not tenant-scoped (reference data). Any other value, or `'shared'` combined with `tenant_field` / `owner_field` / `visibility`, raises at startup. | [tenancy.md](tenancy.md#hard-fail-at-startup) |
| `sensitive_fields` *(class attribute)* | — | Fields readable only through an explicit `app.model.field.read` grant, never through model-level read. Restricts reads only; role-based permissions only. | [security.md](security.md#per-model-sensitive_fields) |

## Defaults at a glance

If you start fresh and configure nothing, you get:
- RBAC active via `TURBODRF_ROLES` (you must define one for anything to work)
- Field permissions active
- Sensitive deny-list active (passwords/tokens never leak)
- No row scoping until a model declares `tenant_field`, `owner_field` or
  `visibility`; the tenancy startup gate is off until `TURBODRF_TENANT_MODEL`
  is set
- All integrations **off**
- Swagger docs **on** at `/swagger/`, scoped to the caller
- Permission cache TTL **5 minutes** (role changes apply on the next request)

For multi-tenant data: set `TURBODRF_TENANT_MODEL` and
`TURBODRF_TENANT_USER_FIELD`, then declare tenancy on each model.
See [tenancy.md](tenancy.md) and [migration_to_predicates.md](migration_to_predicates.md).
