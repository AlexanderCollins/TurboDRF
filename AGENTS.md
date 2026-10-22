# AGENTS.md — TurboDRF

Instructions for AI coding agents working in projects that use TurboDRF.
Follow these conventions to configure the framework correctly and avoid
the security footguns. Human-facing docs live in `README.md` and `docs/`.

---

## What TurboDRF is

A Django REST Framework auto-generator. One mixin + one `turbodrf()`
classmethod on a Django model produces a full CRUD ViewSet, serializer,
URL route, OpenAPI schema, role-based field permissions, search, and
row-level access control. There are no per-view files.

Two layers of security run on every request:

1. **Field-level RBAC** — what fields can a role read/write?
2. **Row-level predicates** — which rows can this user see/touch at all?

Both are declared on the model. Never bypass them in custom views.

---

## Build & Test

```bash
pip install -e ".[dev]"                # install with dev dependencies (incl pytest-xdist)
python -m pytest                       # full test suite, parallel by default (-n auto)
python -m pytest -p no:xdist           # disable parallel for debugging / pdb
python -m pytest tests/unit -q         # unit only — sub-second
python -m pytest -k <name>             # single test
python manage.py turbodrf_check        # report each model's compiled status + tenancy
```

The test suite runs in parallel by default via `pytest-xdist` (`-n auto`
in `[tool.pytest.ini_options]`). Each worker gets its own SQLite test
database and process-local cache, so tests are isolation-safe across
workers. Disable with `-p no:xdist` when you need pdb / single-threaded
debugging.

CI runs `pytest`, `black --check`, `flake8`, `isort --check`. Keep them green.

---

## Adding a TurboDRF model

```python
from turbodrf.mixins import TurboDRFMixin

class Project(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    workspace = models.ForeignKey('accounts.Workspace', on_delete=models.CASCADE)
    owner = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    searchable_fields = ['title']        # opt-in: ?search=...
    sensitive_fields = ['price']         # readable only via 'projects.project.price.read'

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['title', 'workspace', 'owner', 'price'],
            'tenant_field': 'workspace',
            'owner_field': 'owner',
            'bypass_owner_roles': ['admin', 'manager'],
        }
```

Routes are auto-generated at `/api/projects/` and `/api/projects/{pk}/`. No
URL or view file needed.

### Required keys

- `fields`: list of field names exposed on the API. Use `'__all__'` only
  for trusted models — explicit lists are safer.

### Tenancy keys (pick exactly one approach per model)

| Approach | Use when |
|---|---|
| `'tenant_field': 'fk_to_tenant'` | Model is tenant-scoped (most models) |
| `'tenant_field': 'fk__chain__to_tenant'` | Indirect — model reaches tenant via FK chain |
| `'tenancy': 'shared'` | Reference data: currencies, country codes |
| `'visibility': [<predicate>, ...]` | Need OR logic / non-tenant scoping (see Predicates below) |

`owner_field` alone doesn't count as a tenancy decision. `'tenancy'` accepts
only `'shared'`, which can't be combined with `tenant_field`, `owner_field`
or `visibility`. An empty `visibility` needs a `tenant_field`.

### Within-tenant access (sugar)

```python
'owner_field': 'owner',                  # FK to User
'owner_field': ['owner', 'created_by'],  # any of these → visible
'bypass_owner_roles': ['admin', 'manager'],        # these roles see all in tenant
```

---

## Settings (settings.py)

```python
INSTALLED_APPS = [..., 'turbodrf']

# Required for tenancy:
TURBODRF_TENANT_MODEL = 'accounts.Workspace'
TURBODRF_TENANT_USER_FIELD = 'workspace'   # request.user.workspace → tenant

# Roles → model- and field-level perms. No wildcards: list each one.
TURBODRF_ROLES = {
    'admin':   ['projects.project.read', 'projects.project.create',
                'projects.project.update', 'projects.project.delete',
                'projects.project.price.read', 'projects.project.price.write'],
    'manager': ['projects.project.read', 'projects.project.update'],
    'viewer':  ['projects.project.read'],
}

# Hard-fail at startup if any model has no tenancy declared.
# Keep True. Set False ONLY for legacy migration. Hard-fail catches the
# entire class of "I forgot to scope this model" bugs at import time.
TURBODRF_REQUIRE_TENANCY = True
```

Permission strings must be `app_label.model_name.action` or
`app_label.model_name.field.action`, with the exact lowercase labels and a
concrete or M2M field name (not `owner_id`); the router refuses to boot on
anything else. A field with a `.read` / `.write` rule on any role needs that
rule for every role. Anonymous callers on a `public_access` model get
model-level read plus only what a `guest` role grants.

`request.user.workspace` must return a model instance or an integer PK.
Strings, dicts, bound methods, and reverse-FK managers all coerce to `None`
and fail-closed (filter returns zero rows). Pick a real ForeignKey.

---

## Predicates (when sugar isn't enough)

Use `visibility=[...]` with these primitives:

```python
from turbodrf.predicates import Owner, Members, Group, Either, Conditional, Custom

return {
    'tenant_field': 'workspace',           # tenant ALWAYS stays a setting
    'visibility': [
        Either(
            Owner('created_by'),
            Members('participants'),       # M2M: user is a participant
            Group('team'),                 # FK to a team that user belongs to
        ),
    ],
}
```

| Predicate | Use for |
|---|---|
| `Owner(field, bypass=[...])` | Single-owner rows; bypass list of roles see all |
| `Members(m2m)` | Channels / projects with explicit membership (creates aren't membership-checked) |
| `Group(fk, user_via='user_set')` | Team-based visibility (creates aren't membership-checked) |
| `Either(*preds)` | OR composition |
| `Conditional(when=Q(...), require_roles=[...])` | "Staff loans visible only to staff role" |
| `Custom(callable, write_validator=...)` | Last resort — arbitrary Q |

**Never put `Tenant()` inside `Either(...)`.** It would let OR-composition
escape the tenant boundary. The framework raises at config time. Use
the `tenant_field` setting instead.

---

## What NEVER to do

1. **Don't add custom `@action` endpoints that call `Model.objects.all()`.**
   They bypass the predicate stack and tenant filter. Use
   `self.get_queryset()` (the framework's filtered queryset).

2. **Don't override `get_queryset()`** in TurboDRF viewsets. The
   framework's implementation applies the tenant boundary and predicate
   stack. Override = silent BOLA.

3. **Don't reach for `unscoped_properties`** to make a `@property` work.
   Properties run in a sandbox: only the caller's readable columns, the
   relations declared in `property_relations` (loaded within the caller's
   scope), and no queries. Declare the relations the property reads
   (`'property_relations': {'label': ['portfolio']}`) and iterate
   `self.items.all()` instead of querying. `unscoped_properties` returns
   whatever the property reads to every reader of the model.

4. **Don't widen serializer fields with `extra_kwargs` or custom
   serializers.** Field permissions are enforced via `sensitive_fields`
   and `TURBODRF_ROLES`. Bypassing them leaks data to lower roles.

5. **Don't store secrets in `searchable_fields`.** `?search=guess` does
   substring-matching against every searchable field. The framework
   intersects this with the user's read permissions, but a field with
   no permission rule defaults to readable.

6. **Don't declare `tenancy: 'shared'`** on tenant-scoped data because
   "the test was failing." That opens BOLA.

7. **Don't bypass `TurboDRFSerializer.create()` / `update()`** (call
   `super()` if you override them). They run `_apply_predicate_writes`,
   which validates the tenant and predicates and blocks FK / M2M injection
   (POST with another tenant's PK).

8. **Don't hand-roll DELETE-allow logic.** RBAC `'.delete'` perms +
   predicate visibility decide who can delete what. If a role shouldn't
   delete, omit `.delete` from its perms.

---

## Permission cache

Permission snapshots are cached for 5 minutes per `(user, model)` pair. The
cache key covers the user's roles and the role configuration (in database
mode, every role's version), so granting or revoking a role, editing
`TURBODRF_ROLES`, or saving / deleting a `RolePermission` takes effect on the
next request. A bulk `RolePermission` QuerySet `update()` / `delete()`
doesn't bump role versions and stays stale until the TTL. After one:

```python
from turbodrf.backends import invalidate_user_permissions
invalidate_user_permissions(user)   # one user
invalidate_user_permissions()       # everyone — use sparingly
```

In tests use `cache.clear()` in `setUp`.

---

## Testing patterns

```python
from tests.test_app.apps import set_test_brokerage  # in this repo (tenant = Brokerage)
# In downstream projects: implement an analogous helper that
# both (a) sets `user._test_brokerage` and (b) registers the user in
# a class-level dict so DRF's User re-fetch survives.

self.user = User.objects.create_user(...)
self.user._test_roles = ['member']
set_test_brokerage(self.user, self.brokerage_a)
```

Field-level perm tests:

```python
from turbodrf.backends import build_permission_snapshot
snap = build_permission_snapshot(user, Model, use_cache=False)
assert 'price' in snap.readable_fields
assert snap.can_perform_action('update')
```

---

## Where to look

- `docs/tenancy.md` — full predicate vocabulary, sugar vs power form, startup checks
- `docs/permissions.md` — RBAC, sensitive fields, field-perm syntax
- `docs/security.md` — threat model + known limitations
- `docs/settings_reference.md` — every setting
- `docs/migration_to_predicates.md` — upgrading existing TurboDRF apps

---

## Conventions for changes

- New TurboDRF models always declare a tenancy key (`tenant_field`,
  `visibility`, or `'shared'`). `TURBODRF_REQUIRE_TENANCY=True` enforces
  this at startup.
- Run `python manage.py turbodrf_check` before committing — it reports
  each model's compiled status (and why an opted-in model isn't compiled),
  its list fields, and its resolved tenancy. It doesn't run the startup
  gates or fail on problems; building the router (e.g. running the test
  suite) does.
- Keep `tests/test_app/models.py` in sync when adding new
  framework-level fields; downstream apps mirror its patterns.
- Preserve fail-closed defaults: when in doubt about a permission edge
  case, deny.
