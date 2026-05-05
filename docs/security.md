# Security

## Secure by default

Endpoints require authentication unless `public_access: True` is explicitly set.

## Sensitive fields

Fields like `password`, `token`, and `secret_key` are never exposed in API responses or available for filtering, regardless of permissions.

Default deny-list:

```python
TURBODRF_SENSITIVE_FIELDS = [
    'password', 'password_hash', 'secret_key', 'api_key',
    'token', 'access_token', 'refresh_token', 'session_key',
]
```

Override in your settings to customise.

## No-roles denial

Authenticated users with no assigned roles get 403 on all endpoints. This prevents accidental access when a user account exists but hasn't been configured.

## Filter security

Users can only filter on fields they have read permission for. This prevents binary search attacks where an attacker infers hidden values by filtering (e.g. `?salary__gte=100000`).

## Error responses

All errors follow a consistent format. Enable with:

```python
REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'turbodrf.exceptions.turbodrf_exception_handler',
}
```

```json
{
    "error": {
        "status": 403,
        "code": "permission_denied",
        "message": "You do not have permission to perform this action."
    }
}
```

## Fail-closed design

If a permission check fails due to an error (database issue, malformed data), access is denied. TurboDRF never grants access on exception.

## Row-level access control (predicates)

TurboDRF enforces row-level access through *predicates* declared per model.
The system answers all four standard authorization questions:

| Concern | What it answers | TurboDRF handles it? |
|---------|----------------|---------------------|
| **RBAC** | "Can this user read orders?" | Yes |
| **Field-level** | "Can this user see the `price` field?" | Yes |
| **Row-level scoping** | "Which orders can this user see?" | Yes (predicates) |
| **Write validation** | "Can this user assign this order to that customer?" | Yes (FK-injection defense) |

A typical configuration looks like:

```python
class Order(models.Model, TurboDRFMixin):
    @classmethod
    def turbodrf(cls):
        return {
            'tenant_field': 'store',
            'owner_field': 'customer',
            'bypass_owner_roles': ['staff', 'admin'],
            'fields': '__all__',
        }
```

When `TURBODRF_TENANT_MODEL` is set in settings and `TURBODRF_REQUIRE_TENANCY=True`
(default), the router refuses to register any model that has neither a tenancy
declaration nor an explicit `'tenancy': 'shared'` opt-out — closing the
"developer forgot to scope this" class of bugs at boot.

See `docs/tenancy.md` for the full predicate vocabulary, sugar form, real-app
coverage table, and configuration reference.

### What the predicate system enforces

- **List/detail/PATCH/DELETE** — every queryset and `get_object()` is AND'd with
  the user's predicate Q. Detail/PATCH/DELETE return 404 (not 403) on filtered
  rows to avoid existence leaks.
- **Tenant auto-fill on create** — the tenant FK is filled from `request.user`,
  client values for the tenant field are overwritten.
- **FK injection defense** — every FK in the request body must resolve to a row
  visible under the related model's predicate stack. Cross-tenant FK targets
  return 400.
- **Tenant reassignment rejection** — PATCH cannot change a row's tenant FK to
  a different tenant.
- **Owner write check** — non-bypass roles cannot assign rows to other users.

### Optional: Postgres RLS as defense in depth

For Postgres deployments, TurboDRF can additionally generate Row Level Security
policies that enforce the same rules at the database layer (catches raw SQL,
admin scripts, ORM bugs). See `docs/rls.md`.
