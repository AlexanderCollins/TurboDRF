# Migrating to the Predicate System

If your project doesn't set `TURBODRF_TENANT_MODEL`, **nothing changes** —
predicates are opt-in.

For multi-tenant projects:

## 1. Configure tenancy

```python
# settings.py
TURBODRF_TENANT_MODEL = 'accounts.Workspace'
TURBODRF_TENANT_USER_FIELD = 'workspace'   # request.user.workspace → tenant
```

`TURBODRF_TENANT_USER_FIELD` is the attribute on `request.user` that returns
the user's tenant. A direct FK on User is typical; a `@property` or
`workspaces.first()` works too.

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
    'visibility': [Tenant('workspace'), Either(Owner('a'), Owner('b'))],
}
```

## 3. Verify

```bash
python manage.py turbodrf_check          # see resolved predicates per model
pytest                                    # run your tests
```

## Behavior changes to plan for

- POSTs with cross-tenant FKs now return **400** (FK injection rejected).
- PATCHes that try to reassign the tenant FK now return **400**.
- Detail/PATCH/DELETE return **404** (not 403) when a row is outside scope.
- List endpoints return only scoped rows.
- Lists fixtures relying on "all rows visible to everyone" need per-tenant
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

## Optional: Postgres RLS

For Postgres deployments, see `docs/rls.md` for defense-in-depth at the
database layer.
