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
python manage.py turbodrf_check        # validate every model's config
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
    sensitive_fields = ['price']         # never serialized to anyone w/o read perm

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

# Roles → field-level perms:
TURBODRF_ROLES = {
    'admin':  ['*'],                                  # everything
    'manager': ['projects.project.read', 'projects.project.update'],
    'viewer':  ['projects.project.read'],
}

# Hard-fail at startup if any model has no tenancy declared.
# Keep True. Set False ONLY for legacy migration. Hard-fail catches the
# entire class of "I forgot to scope this model" bugs at import time.
TURBODRF_REQUIRE_TENANCY = True
```

`request.user.workspace` must return a model instance or PK. Strings,
dicts, bound methods, and reverse-FK managers all coerce to `None` and
fail-closed (filter returns zero rows). Pick a real ForeignKey.

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
| `Members(m2m)` | Channels / projects with explicit membership |
| `Group(fk, user_via='user_set')` | Team-based visibility |
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

3. **Don't widen serializer fields with `extra_kwargs` or custom
   serializers.** Field permissions are enforced via `sensitive_fields`
   and `TURBODRF_ROLES`. Bypassing them leaks data to lower roles.

4. **Don't store secrets in `searchable_fields`.** `?search=guess` does
   substring-matching against every searchable field. The framework
   intersects this with the user's read permissions, but a field with
   no permission rule defaults to readable.

5. **Don't declare `tenancy: 'shared'`** on tenant-scoped data because
   "the test was failing." That opens BOLA.

6. **Don't bypass `_validate_fk_predicates`** in serializer
   `create()` / `update()`. FK injection (POST with another tenant's PK)
   is blocked there.

7. **Don't hand-roll DELETE-allow logic.** RBAC `'.delete'` perms +
   predicate visibility decide who can delete what. If a role shouldn't
   delete, omit `.delete` from its perms.

---

## Permission cache

Permission snapshots are cached for 5 minutes per `(user, model)` pair.
After granting/revoking a role, snapshots remain stale until the cache
expires. To force-refresh in code:

```python
from turbodrf.backends import invalidate_user_permissions
invalidate_user_permissions(user)   # one user
invalidate_user_permissions()       # everyone — use sparingly
```

In tests use `cache.clear()` in `setUp`.

---

## Testing patterns

```python
from tests.test_app.apps import set_test_workspace  # in this repo
# In downstream projects: implement an analogous helper that
# both (a) sets `user._test_workspace` and (b) registers the user in
# a class-level dict so DRF's User re-fetch survives.

self.user = User.objects.create_user(...)
self.user._test_roles = ['member']
set_test_workspace(self.user, self.workspace_a)
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

- `docs/tenancy.md` — full predicate vocabulary + 12-app coverage table
- `docs/permissions.md` — RBAC, sensitive fields, field-perm syntax
- `docs/security.md` — threat model + known limitations
- `docs/settings_reference.md` — every setting
- `docs/rls.md` — optional Postgres RLS for defense-in-depth
- `docs/migration_to_predicates.md` — upgrading existing TurboDRF apps

---

## Conventions for changes

- New TurboDRF models always declare a tenancy key (`tenant_field`,
  `visibility`, or `'shared'`). `TURBODRF_REQUIRE_TENANCY=True` enforces
  this at startup.
- Run `python manage.py turbodrf_check` before committing — it
  validates every model's `turbodrf()` config and resolves tenancy
  paths.
- Keep `tests/test_app/models.py` in sync when adding new
  framework-level fields; downstream apps mirror its patterns.
- Preserve fail-closed defaults: when in doubt about a permission edge
  case, deny.
