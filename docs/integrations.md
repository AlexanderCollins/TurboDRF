# Integrations

> ⚠ **All integrations on this page are experimental and opt-in.**
> They're gated behind settings (default `False`) and have unit-test
> coverage with mocks but are **not verified end-to-end against the
> real third-party servers** they integrate with. Treat them as
> starting points and verify against your stack before depending on
> them in production.

---

## django-allauth

**Status:** experimental — unit-tested with mocks; not verified against
a live django-allauth installation in this codebase.

Maps Django Group memberships to TurboDRF roles. Useful when your app
already uses allauth for session-based auth (typical for SSR Django
projects with social login).

```bash
pip install "turbodrf[allauth]"
```

```python
# settings.py
INSTALLED_APPS = [
    'allauth',
    'allauth.account',
    'allauth.headless',
    'turbodrf',
]

TURBODRF_ALLAUTH_INTEGRATION = True   # default: False
TURBODRF_ALLAUTH_ROLE_MAPPING = {
    'Administrators': 'admin',
    'Editors': 'editor',
}
TURBODRF_ALLAUTH_STRICT_ROLES = True  # default: True (recommended)
```

When the integration is enabled, TurboDRF looks up the user's Django
Groups, maps them via `TURBODRF_ALLAUTH_ROLE_MAPPING`, and uses them as the
user's TurboDRF roles, whatever authenticated the request (see
[How the integrations resolve roles](#how-the-integrations-resolve-roles)).

`turbodrf.integrations.allauth.AllAuthRoleMiddleware` is deprecated: it does
nothing, and you can remove it from `MIDDLEWARE`.

### Strict group mapping (default)

When a role mapping is configured and `TURBODRF_ALLAUTH_STRICT_ROLES=True`
(default), the mapping is an **allow-list**. A group that isn't in the
mapping gives no role, and a warning is logged. Without this, a Django
Group that happens to be named `admin` would become the TurboDRF `admin`
role even though the mapping doesn't mention it.

Set `TURBODRF_ALLAUTH_STRICT_ROLES = False` to restore the old passthrough,
where unmapped groups become roles under their own names.

With no mapping configured, every group name is used as a role name
directly.

### Role helpers

`turbodrf.integrations` has helpers that work with roles through groups:
`assign_roles_to_user`, `get_users_with_role`, `get_or_create_role_group`,
`create_role_groups` and `sync_groups_to_roles`. They follow
`TURBODRF_ALLAUTH_ROLE_MAPPING` and strict mode as role resolution does, so
a role maps to the group that gives it. A role no group can give (strict
mode, not a mapping value) raises `ValueError`.

---

## Keycloak / OpenID Connect

**Status:** experimental — the role-mapping logic is unit-tested; **not
verified against a live Keycloak server** in this codebase.

Maps the roles Keycloak granted a user to TurboDRF roles. Common pattern
in B2B SaaS where Keycloak is the central IdP.

```bash
pip install social-auth-app-django
```

```python
# settings.py
TURBODRF_KEYCLOAK_INTEGRATION = True   # default: False
TURBODRF_KEYCLOAK_PROVIDER = 'keycloak'              # default: 'keycloak'
TURBODRF_KEYCLOAK_ROLE_CLAIM = 'realm_access.roles'  # path in the stored claims
TURBODRF_KEYCLOAK_ROLE_MAPPING = {
    'realm-admin': 'admin',
    'content-editor': 'editor',
}
TURBODRF_KEYCLOAK_STRICT_ROLES = True  # default: True (recommended)
TURBODRF_KEYCLOAK_MAX_ROLE_AGE = None  # default: None (no limit), seconds
```

The roles apply to every authentication method (see
[How the integrations resolve roles](#how-the-integrations-resolve-roles)).
`turbodrf.integrations.keycloak.KeycloakRoleMiddleware` is deprecated: it
does nothing, and you can remove it from `MIDDLEWARE`.

### Where the roles come from

TurboDRF does **not** read or verify a Keycloak token on each request. It
reads the claims social-auth-app-django stored in the user's
`UserSocialAuth.extra_data` when they logged in, follows
`TURBODRF_KEYCLOAK_ROLE_CLAIM` to the role list, and maps it.

That means:

- **Roles are captured at login and last for the session.** A role you
  revoke in Keycloak still applies in TurboDRF until the user logs in again
  (which refreshes `extra_data`). Keycloak access tokens expiring doesn't
  change this.
- Only the association whose provider is `TURBODRF_KEYCLOAK_PROVIDER`
  (default `'keycloak'`, the name social-core's Keycloak backend uses) is
  read. If the user also linked GitHub, Google or any other account, role
  claims in that account's data are ignored. Set this to your backend's
  `name` if you use a custom one.
- To bound how long login-time roles stay valid, set
  `TURBODRF_KEYCLOAK_MAX_ROLE_AGE` to a number of seconds. Once the stored
  `auth_time` (written by social-auth at login) is older than that, the user
  gets no Keycloak roles until they log in again. If `auth_time` is missing
  the roles are dropped too. Leave it `None` (default) to keep roles for the
  whole session. social-auth's `UserSocialAuth.refresh_token()` resets
  `auth_time` but keeps the stored role claims, so if your app refreshes
  tokens that way, this limit doesn't bound how old the roles are.

Make sure the claim you point `TURBODRF_KEYCLOAK_ROLE_CLAIM` at is actually
saved in `extra_data` by your social-auth pipeline / `EXTRA_DATA` settings.

### Strict mode (default)

When a role mapping is configured AND `TURBODRF_KEYCLOAK_STRICT_ROLES=True`
(default), the mapping acts as an **allow-list**. Any Keycloak role NOT
listed in the mapping is dropped (with a logged warning). This closes a
class of bug where a Keycloak role accidentally named `admin` would have
silently matched a TurboDRF role of the same name.

### Permissive mode (legacy)

Setting `TURBODRF_KEYCLOAK_STRICT_ROLES = False` restores the legacy
passthrough behavior where unmapped Keycloak roles are accepted under
their original names. Use only if you've audited the consequences.

### When no mapping is configured

If `TURBODRF_KEYCLOAK_ROLE_MAPPING` is empty/unset, all Keycloak roles pass
through under their original names. This is the "use Keycloak names as
TurboDRF role names directly" mode and is opt-in by virtue of providing
no mapping.

---

## How the integrations resolve roles

When `TURBODRF_ALLAUTH_INTEGRATION` or `TURBODRF_KEYCLOAK_INTEGRATION` is
`True` at startup, TurboDRF installs a `roles` property on your user model.
TurboDRF reads `user.roles` when it checks permissions, so the roles are
resolved from the authenticated user object itself (Keycloak roles first,
then allauth group roles, without duplicates), whether a session, a DRF
token, a JWT or any other authentication class authenticated it. They're
cached on the user instance, so the lookup runs once per request.

`AllAuthRoleMiddleware` and `KeycloakRoleMiddleware` used to set the roles
instead. Django middleware runs before DRF authenticates the request, so
they never saw token or JWT users; they're now deprecated no-ops.

Limits:

- If your user model (or another app) already defines `roles` — a field,
  property or method — TurboDRF leaves it alone and logs that it didn't
  install the resolver. Your `roles` is then the only source of roles, and
  neither integration is consulted.
- In `TURBODRF_PERMISSION_MODE = "database"`, roles come from the
  `UserRole` table and neither integration is consulted.
- The flags are read at startup to decide whether to install the
  resolver; turning an integration on later needs a restart.

---

## Sentry

**Status:** experimental — opt-in, gated by `TURBODRF_ENABLE_SENTRY`
(default `False`). Tested with mocks; integration with a real Sentry
project depends on your `sentry_sdk.init(...)` configuration.

When enabled, the framework:

1. **Describes the user on each event.** An event captured during an
   authenticated request gets its Sentry user replaced with `{"id": <pk>}`
   (no username, email or IP address) and the tags `tenant_id` and
   `user_roles`, so you can filter events by tenant. Events from anonymous
   requests, and the request data sentry-sdk attaches (headers, cookies,
   body), follow your `send_default_pii` setting. Each request runs in its
   own Sentry scope, and the user, roles and tenant are looked up only when
   an event is captured.
2. **Records security-relevant events as breadcrumbs** — FK injection
   blocked, predicate write rejection, tenant reassignment rejected. If a
   user later triggers an unrelated error, the breadcrumbs show what
   security checks fired before the error.

```bash
pip install sentry-sdk
```

```python
# settings.py
import sentry_sdk
sentry_sdk.init(dsn="...", traces_sample_rate=0.1)

TURBODRF_ENABLE_SENTRY = True   # default: False

MIDDLEWARE = [
    # ... existing middleware ...
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'turbodrf.integrations.sentry.SentryContextMiddleware',
]
```

Because the user is read when an event is captured, token and JWT requests,
which DRF authenticates after the middleware runs, get the user id and tags
too. Breadcrumbs don't depend on the user.

Programmatic API for app-specific events:

```python
from turbodrf.integrations.sentry import (
    report_security_event, capture_security_message
)

# Breadcrumb (cheap, batched into the next captured event)
report_security_event(
    "custom_security_event",
    "User attempted suspicious action",
    user_id=request.user.pk,
)

# Discrete Sentry message (creates its own issue — use sparingly)
capture_security_message(
    "Repeated cross-tenant attempts from user",
    user_id=request.user.pk, count=10,
)
```

Both are no-ops when `TURBODRF_ENABLE_SENTRY=False` or `sentry-sdk` is
not installed — safe to call from anywhere in your app.

---

## drf-api-tracking

**Status:** experimental — gated by `TURBODRF_ENABLE_TRACKING` (default
`False`); not verified end-to-end against a real `drf-api-tracking`
deployment in this codebase.

Logs every request through the standard drf-api-tracking middleware. When
enabled, all TurboDRF viewsets gain request/response logging.

```bash
pip install drf-api-tracking
```

```python
# settings.py
INSTALLED_APPS = [
    'rest_framework_tracking',
    'turbodrf',
]

TURBODRF_ENABLE_TRACKING = True              # default: False
TURBODRF_TRACKING_ANONYMOUS = False          # default: False
```

When `TURBODRF_ENABLE_TRACKING=True` and `'rest_framework_tracking'` is in
`INSTALLED_APPS`, the framework's viewset base classes pick up the
`LoggingMixin` automatically. Without the app in `INSTALLED_APPS`, TurboDRF
logs a warning and nothing is tracked. View logs in Django admin under
**API Request Logs**.

TurboDRF's version of the mixin differs from the stock one in three ways:

- **Anonymous requests aren't logged** unless
  `TURBODRF_TRACKING_ANONYMOUS = True`. Only requests whose user is
  authenticated (after DRF authentication) are recorded by default.
- **Sensitive values are masked.** drf-api-tracking masks the values of
  keys named `api`, `token`, `key`, `secret`, `password` and `signature`.
  TurboDRF adds every name in `TURBODRF_SENSITIVE_FIELDS` (`password_hash`,
  `secret_key`, `api_key`, `access_token`, `refresh_token`, `session_key`,
  plus your own additions) to the mixin's `sensitive_fields`, so those
  values are masked wherever drf-api-tracking masks its own list.
- **Bodies logged before parsing are masked too.** drf-api-tracking logs the
  raw body of a request rejected before DRF parses it (401, 403, throttled,
  malformed JSON). TurboDRF masks a JSON body as above, and replaces any
  other body that mentions a sensitive name.

`TURBODRF_ENABLE_TRACKING` is read once, when `turbodrf.views` is imported,
so changing it needs a restart.

---

## Verification before production

For any of the above:

1. Install the third-party dep and configure your real IdP / tracking server
2. Write integration tests in your project that authenticate via the real backend
3. Confirm role / log records flow through TurboDRF correctly
4. Add the result to your security review

The unit tests in `tests/unit/test_keycloak_integration.py`,
`tests/unit/test_allauth_integration.py`, and
`tests/unit/test_tracking_integration.py` exercise the mapping logic but
**not** the wire format from the real servers.

If you adopt one of these in production, please open an issue — we'd like
to upgrade them from "experimental" to "verified" with reproducible
end-to-end test environments (Docker / Compose / fixtures).
