# AGENTS.md — TurboDRF

Instructions for AI coding agents working in projects that use TurboDRF.
Follow these conventions to configure the framework correctly and avoid
the security footguns. Human-facing docs live in `README.md` and `docs/`.
Working on TurboDRF itself (its tests, CI, releases)? See `CONTRIBUTING.md`.

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

## Project setup

```python
# settings.py
INSTALLED_APPS = [..., 'rest_framework', 'drf_yasg', 'turbodrf']
# drf_yasg serves the Swagger / ReDoc pages. Without it, set
# TURBODRF_ENABLE_DOCS = False (otherwise check warns turbodrf.W001).

# urls.py
urlpatterns = [path('api/', include('turbodrf.urls'))]
```

Users need roles: see [Roles](#roles) below. Without one, every request is
a 403.

**Run `python manage.py check` in CI and before every deploy.** Tenancy
errors raise when Django starts, but the router validates the rest of the
configuration when Django first imports the URLconf, which `check` does and
`get_wsgi_application()` doesn't; without it a broken config deploys and
fails on the first request.

---

## Adding a TurboDRF model

```python
from turbodrf.mixins import TurboDRFMixin

class Project(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    workspace = models.ForeignKey('accounts.Workspace', on_delete=models.CASCADE)
    owner = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    archived = models.BooleanField(default=False)

    searchable_fields = ['title']        # opt-in: ?search=...
    sensitive_fields = ['price']         # readable only via 'projects.project.price.read'

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['id', 'title', 'workspace', 'owner', 'price'],
            'tenant_field': 'workspace',
            'owner_field': 'owner',
            'bypass_owner_roles': ['admin', 'manager'],
        }
```

Routes are auto-generated at `/api/projects/` and `/api/projects/{pk}/` (the
lowercase model name plus `s`; set `'endpoint'` to change it). No URL or view
file needed.

### `fields`

Always set `fields` to an explicit list (or a `{'list': [...], 'detail':
[...]}` dict). It isn't required, but it defaults to `'__all__'`, which
exposes every column, including ones added later. The primary key renders
only if listed. A name that doesn't exist on the model, or one ending on a
reverse one-to-one, raises at startup.

An unknown `turbodrf()` key (a typo like `'owner_feild'`) raises
`ImproperlyConfigured` at startup, listing the valid keys. `sensitive_fields`
is a class attribute on the model, not a key.

### Tenancy keys (pick exactly one approach per model)

| Approach | Use when |
|---|---|
| `'tenant_field': 'fk_to_tenant'` | Model is tenant-scoped (most models) |
| `'tenant_field': 'fk__chain__to_tenant'` | Indirect — model reaches tenant via FK chain |
| `'tenancy': 'shared'` | Reference data: currencies, country codes |
| `'visibility': [<predicate>, ...]` | Need OR logic / non-tenant scoping (see Predicates below) |

`tenant_field` must be a chain of forward foreign keys / one-to-ones, named
by field (`workspace`, not `workspace_id`), that ends at
`TURBODRF_TENANT_MODEL`; the last one must reference the tenant's primary key
(no `to_field`). `owner_field` alone doesn't count as a tenancy
decision. Nor does a `visibility` list without a `tenant_field`, unless one
of its predicates restricts every caller: an `Owner` without bypass roles,
`Members`, `Group` or `Custom` (an `Either` only if all its children do).
`'tenancy'` accepts only `'shared'`, which can't be combined with
`tenant_field`, `owner_field` or `visibility`. An empty `visibility` needs a
`tenant_field`.

The tenant model itself and the user model have their own recipe:
`docs/tenancy.md`, "Exposing the tenant model and the user model".

### Within-tenant access (sugar)

```python
'owner_field': 'owner',                  # FK to User
'owner_field': ['owner', 'created_by'],  # any of these → visible
'bypass_owner_roles': ['admin', 'manager'],        # these roles see all in tenant
```

### Custom endpoints

Add endpoints with `@turbodrf_action` and the `'actions'` key. They attach
to the generated viewset, so `self.get_object()` / `self.get_queryset()`
apply the same tenant and predicate scoping as the CRUD routes:

```python
from rest_framework.response import Response
from turbodrf import turbodrf_action

@turbodrf_action(detail=True, methods=['post'], url_path='archive')
def archive(self, request, pk=None):
    project = self.get_object()          # scoped: other tenants' pks 404
    project.archived = True
    project.save(update_fields=['archived'])
    return Response({'status': 'archived'})

# in Project.turbodrf():  'actions': [archive],
```

A POST action needs the role's `create` permission (GET → `read`, PUT/PATCH →
`update`, DELETE → `delete`), unless it declares `permission_classes`.
`@turbodrf_action` raises `TypeError` for `serializer_class=`, `queryset=` and
other arguments TurboDRF would ignore.

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
# entire class of "I forgot to scope this model" bugs at startup.
TURBODRF_REQUIRE_TENANCY = True
```

`TURBODRF_TENANT_USER_FIELD` is required as soon as any model declares a
`tenant_field`. `request.user.workspace` must return a model instance or an
integer PK. Strings, dicts, bound methods, and reverse-FK managers all coerce
to `None` and fail-closed (filter returns zero rows). Pick a real ForeignKey.

Never set `TURBODRF_DISABLE_PERMISSIONS = True` outside local development:
it lets anonymous callers create, update and delete.

---

## Roles

TurboDRF needs to know each user's roles. Pick one source:

- **Static mode (default):** `user.roles` returns a list of role names. Add a
  property to a custom user model, or derive it from Django groups:

  ```python
  # in an AppConfig.ready()
  User.add_to_class('roles', property(lambda u: [g.name for g in u.groups.all()]))
  ```

- **Database mode** (`TURBODRF_PERMISSION_MODE = 'database'`): roles come from
  `UserRole` rows and `user.roles` is ignored; permissions come from
  `TurboDRFRole` / `RolePermission` rows, validated when saved.
- **allauth / Keycloak integrations:** TurboDRF installs the `roles` property
  when the user model doesn't define one. Their middleware classes are
  deprecated no-ops; don't add them.

Details: `docs/permissions.md`, "Assigning roles to users".

### Permission strings

Permission strings must be `app_label.model_name.action` or
`app_label.model_name.field.action`, with the exact lowercase labels and a
concrete or M2M field name (not `owner_id`); the router refuses to boot on
anything else.

Once any role has a `.read` / `.write` rule for a field, every other role
loses that access unless granted the same rule. So a role with `.create`
needs `.write` on every required field that has a write rule, or its POSTs
fail. Anonymous callers on a `public_access` model get model-level read plus
only what a `guest` role grants.

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
            Group('team'),                 # FK to a team whose `members` include the user
        ),
    ],
}
```

| Predicate | Use for |
|---|---|
| `Owner(field_or_fields, bypass=[...])` | Owned rows; a list means any listed column. Bypass roles see all |
| `Members(m2m_field)` | Channels / projects with explicit membership (no write rule) |
| `Group(fk, user_via='members')` | Team-based visibility (no write rule). `user_via` is the lookup from the team model to users: `'user'` for `auth.Group` |
| `Either(*preds)` | OR composition for reads; writes must pass every child |
| `Conditional(when=Q(...), require_roles=[...])` | "Staff loans visible only to staff role" |
| `Custom(q_func, write_validator=...)` | Last resort — arbitrary Q. `write_validator` is required |

Several owner columns: use `Owner(['author', 'reviewer'])` (or a list
`owner_field`), never `Either(Owner('author'), Owner('reviewer'))`.

How `Either` treats writes: every child's write check must pass. `Owner`'s
check applies to every writer without a bypass role: they can't set an owner
column to another user, or change one that holds another user. An `Owner`
over a `__` path (`Owner('project__owner')`) is checked on the saved row
instead: the writer must still see it. `Custom` adds its `write_validator`; `Members`, `Group` and `Conditional` add none.
On create, `Either` fills a single-column `Owner` child's column with the
caller.

**Never put `Tenant()` inside `Either(...)`.** It would let OR-composition
escape the tenant boundary. The framework raises at config time. Use
the `tenant_field` setting instead.

---

## Platform scope (staff across tenants)

For an operator's own staff console, don't hand-write cross-tenant views. Give
the model a `'platform'` block and serve it from a platform router:

```python
'platform': {
    'endpoint': 'jobs',
    'read_only': True,
    'fields': ['id', 'kind', 'status', 'workspace', 'workspace__name', 'payload'],
    'searchable_fields': ['kind'],          # must be in the block's fields
    'actions': [retry],
},

# urls.py
console = TurboDRFRouter(scope='platform', gate=[IsPlatformStaff])
```

- The block takes the API-shaping keys only; a tenancy key in it raises.
  Nothing is inherited from the top level: set `fields`.
- `gate` is required: permission classes checked first on every route.
- Platform roles: `TURBODRF_PLATFORM_ROLE_GETTER` (default
  `user.platform_roles`) and `TURBODRF_PLATFORM_ROLES` (always static). Tenant
  roles grant nothing there.
- A relation may reach only models with a `'platform'` block
  (`{'enabled': False}` for no route), the tenant model, or non-TurboDRF
  models.
- A platform create must name the row's tenant; FK / M2M values must belong
  to the same tenant.

Details: `docs/platform.md`.

---

## What NEVER to do

1. **Don't add endpoints that call `Model.objects.all()`.** They bypass the
   predicate stack and tenant filter. Use `@turbodrf_action` and
   `self.get_queryset()` / `self.get_object()` (the framework's filtered
   queryset). Generated viewsets have no `queryset` attribute:
   `self.queryset` is `None`.

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
cache key covers the user's roles, the role configuration (in database
mode, every role's version) and the model's `sensitive_fields` and
`public_access`, so granting or revoking a role, editing `TURBODRF_ROLES`, or
saving / deleting a `RolePermission` takes effect on the next request. A bulk
`RolePermission` QuerySet `update()` / `delete()` doesn't bump role versions
and stays stale until the TTL. After one:

```python
from turbodrf.backends import invalidate_user_permissions
invalidate_user_permissions(user)   # one user
invalidate_user_permissions()       # everyone — use sparingly
```

In tests use `cache.clear()` in `setUp`.

---

## Testing patterns

Give test users roles the same way production does (group membership, a
roles field, `UserRole` rows), and set their tenant FK. Then test through
the API with DRF's `APIClient`:

```python
from django.core.cache import cache
from rest_framework.test import APIClient

def setUp(self):
    cache.clear()
    self.user = User.objects.create_user('alice', workspace=self.workspace_a)
    self.user.groups.add(self.editors)
    self.client = APIClient()
    self.client.force_authenticate(self.user)

def test_other_tenant_is_404(self):
    other = Project.objects.create(title='x', workspace=self.workspace_b)
    self.assertEqual(self.client.get(f'/api/projects/{other.pk}/').status_code, 404)
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

- [docs/quickstart.md](docs/quickstart.md) — a working project, step by step
- [docs/configuration.md](docs/configuration.md) — every `turbodrf()` key, custom endpoints
- [docs/tenancy.md](docs/tenancy.md) — full predicate vocabulary, sugar vs power form, startup checks
- [docs/platform.md](docs/platform.md) — the platform scope: a staff API across tenants
- [docs/permissions.md](docs/permissions.md) — RBAC, roles, sensitive fields, field-perm syntax
- [docs/security.md](docs/security.md) — threat model + known limitations
- [docs/settings_reference.md](docs/settings_reference.md) — every setting
- [docs/migration_to_predicates.md](docs/migration_to_predicates.md) — upgrading existing TurboDRF apps

---

## Conventions for changes

- New TurboDRF models always declare a tenancy key (`tenant_field`,
  `visibility`, or `'shared'`). `TURBODRF_REQUIRE_TENANCY=True` enforces
  this at startup.
- Run `python manage.py check` before committing: it builds the router and
  runs every startup gate. Then `python manage.py turbodrf_check`, which
  reports each model's compiled status (and why an opted-in model isn't
  compiled), its list fields, and its resolved tenancy; it only reports.
- Preserve fail-closed defaults: when in doubt about a permission edge
  case, deny.
