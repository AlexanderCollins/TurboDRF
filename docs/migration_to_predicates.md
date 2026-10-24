# Migrating to the Predicate System

A model's `tenant_field`, `owner_field` and `visibility` scope its rows
whether or not `TURBODRF_TENANT_MODEL` is set. A project that declares none
of them and doesn't set `TURBODRF_TENANT_MODEL` sees no change.

For multi-tenant projects:

## 1. Configure tenancy

```python
# settings.py
TURBODRF_TENANT_MODEL = 'accounts.Workspace'
TURBODRF_TENANT_USER_FIELD = 'workspace'   # request.user.workspace → tenant
```

`TURBODRF_TENANT_USER_FIELD` is the attribute on `request.user` that returns
the user's tenant, as a model instance or an integer pk. A direct foreign
key on the user model is the usual choice.

## 2. Declare tenancy on every model

When `TURBODRF_TENANT_MODEL` is set, the router refuses to register any
TurboDRF model without a tenancy decision. For each model, do **one** of:

```python
# Tenant-scoped (sugar form)
return {
    'tenant_field': 'workspace',
    'owner_field': 'assigned_to',          # optional
    'bypass_owner_roles': ['admin'],       # optional
}

# Reference data (currencies, system enums)
return {'tenancy': 'shared'}

# Power form (when sugar doesn't fit)
return {
    'tenant_field': 'workspace',   # mandatory boundary — a setting, not a predicate
    'visibility': [Either(Owner('owner'), Members('collaborators'))],
}
```

Rows with several owner columns don't need the power form:
`'owner_field': ['author', 'reviewer']`. The tenant model itself and the user
model need their own rule; see
[Exposing the tenant model and the user model](tenancy.md#exposing-the-tenant-model-and-the-user-model).

## 3. Verify

```bash
python manage.py check                    # builds the router: runs every startup check
python manage.py turbodrf_check           # see resolved predicates per model
pytest                                    # run your tests
```

## Behavior changes to plan for

- POSTs with cross-tenant FK or M2M values now return **400** (FK injection
  rejected).
- A model with only `owner_field` fails the startup check: add a
  `tenant_field` (or `'tenancy': 'shared'`).
- A `tenant_field` must be a path of forward foreign keys ending at the
  tenant model, and `TURBODRF_TENANT_USER_FIELD` must be set; otherwise the
  router refuses to start.
- PATCHes that try to reassign the tenant FK now return **400**.
- Detail/PATCH/DELETE return **404** (not 403) when a row is outside scope.
- List endpoints return only scoped rows.
- Test fixtures relying on "all rows visible to everyone" need per-tenant
  setup.
- Custom `@action` routes that call `Model.objects.get(...)` directly bypass
  scoping — switch to `self.get_object()` or apply predicates manually.

## Roll out gradually

```python
TURBODRF_REQUIRE_TENANCY = False
```

Disables the startup hard-fail so models without tenancy declarations still
register (with no row scoping applied to them). Migrate models one at a
time, then flip the setting back to `True`.
