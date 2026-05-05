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
pip install turbodrf[allauth]
```

```python
# settings.py
INSTALLED_APPS = [
    'allauth',
    'allauth.account',
    'allauth.headless',
    'turbodrf',
]

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'turbodrf.integrations.allauth.AllAuthRoleMiddleware',
]

TURBODRF_ALLAUTH_INTEGRATION = True   # default: False
TURBODRF_ALLAUTH_ROLE_MAPPING = {
    'Administrators': 'admin',
    'Editors': 'editor',
}
```

When the integration is enabled, the middleware looks up the user's Django
Groups, maps them via `TURBODRF_ALLAUTH_ROLE_MAPPING`, and exposes them as
TurboDRF roles for the duration of the request.

---

## Keycloak / OpenID Connect

**Status:** experimental — the role-mapping logic is unit-tested; **not
verified against a live Keycloak server** in this codebase.

Extracts roles from a Keycloak-issued JWT and maps them to TurboDRF roles.
Common pattern in B2B SaaS where Keycloak is the central IdP.

```bash
pip install social-auth-app-django
```

```python
# settings.py
MIDDLEWARE = [
    'turbodrf.integrations.keycloak.KeycloakRoleMiddleware',
]

TURBODRF_KEYCLOAK_INTEGRATION = True   # default: False
TURBODRF_KEYCLOAK_ROLE_CLAIM = 'realm_access.roles'  # JSON path in JWT
TURBODRF_KEYCLOAK_ROLE_MAPPING = {
    'realm-admin': 'admin',
    'content-editor': 'editor',
}
TURBODRF_KEYCLOAK_STRICT_ROLES = True  # default: True (recommended)
```

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

## Sentry

**Status:** experimental — opt-in, gated by `TURBODRF_ENABLE_SENTRY`
(default `False`). Tested with mocks; integration with a real Sentry
project depends on your `sentry_sdk.init(...)` configuration.

When enabled, the framework:

1. **Tags the current Sentry scope** on every request with the user's
   `tenant_id`, `user_id`, and `user_roles`. Makes Sentry events for that
   request searchable / filterable by tenant.
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

When `TURBODRF_ENABLE_TRACKING=True` and `rest_framework_tracking` is
installed, the framework's viewset base classes pick up the `LoggingMixin`
automatically. View logs in Django admin under **API Request Logs**.

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
