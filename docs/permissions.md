# Permissions

TurboDRF supports three permission modes.

## 1. No permissions (local development only)

```python
TURBODRF_DISABLE_PERMISSIONS = True
```

> **Never set this outside local development.** It removes every permission
> check, authentication included: anonymous callers can create, update and
> delete through every generated endpoint, and read any model that isn't
> row-scoped. Only actions that declare their own `permission_classes` keep
> a check.

## 2. Django default permissions

Uses Django's built-in `view_`, `add_`, `change_`, `delete_` permissions:

```python
TURBODRF_USE_DEFAULT_PERMISSIONS = True
```

Neither mode has field-level rules (a model's `sensitive_fields` included).
Filters and ordering still take only the configured fields, and the
`TURBODRF_SENSITIVE_FIELDS` deny-list still applies everywhere: responses,
filters, search, ordering and OPTIONS (see
[Security](security.md#sensitive-fields)).

## 3. Role-based permissions (default)

Field-level control per role:

```python
# settings.py
TURBODRF_ROLES = {
    'admin': [
        'books.book.read',
        'books.book.create',
        'books.book.update',
        'books.book.delete',
        'books.book.title.read',
        'books.book.price.read',
    ],
    'editor': [
        'books.book.read',
        'books.book.update',
        'books.book.title.read',
        'books.book.price.read',       # Can see price
    ],
    'viewer': [
        'books.book.read',
        'books.book.title.read',       # Sees title, not price
    ],
    'guest': [                         # anonymous callers on public_access models
        'books.book.title.read',       # title has read rules above
    ],
}
```

### Permission format

- Model-level: `app_label.model_name.action` (read, create, update, delete)
- Field-level: `app_label.model_name.field_name.read` or `.write`

Use the model's exact lowercase `app_label.model_name` and a concrete or
many-to-many field name (`author`, not `author_id`). There are no wildcards.
The router checks every string in `TURBODRF_ROLES` at startup and refuses to
boot on a typo (see [Security](security.md#permission-string-typo-gate)). In
database mode that startup check doesn't read the `RolePermission` table;
each row is validated when it's saved instead (see
[Database-backed permissions](#database-backed-permissions)).

### How field permissions work

1. Once any role has an explicit rule for a field (e.g. `price.read`), every other role loses that access unless it's granted the same rule -- in static and database mode, and for anonymous callers
2. Fields listed in the model's `sensitive_fields` attribute are readable only through an explicit `.read` grant, as if a rule existed (reads only; add a `.write` rule to restrict writes)
3. Fields without explicit rules fall back to model-level permission
4. This means: to restrict `price` for viewers, you must add `price.read` to at least one role (like admin)

### Roles that create need write on required fields

A field a role can't write is left out of that role's requests. So if a
required field (no default, not nullable) has a `.write` rule, every role
with `.create` on the model needs that `.write` too, or its POSTs get a 400
naming the field ("This field is required, but you can't set it through this
API."). For example, an `editor` with `books.book.create` needs
`books.book.price.write` once any role has a `price.write` rule.

### Anonymous callers and the `guest` role

`public_access: True` lets anonymous callers GET (and HEAD / OPTIONS) the
model's endpoints. It gives them model-level read, as if they held a role
with only `app.model.read`. Field rules bind them like everyone else: a field
with a read rule on any role needs an explicit grant, and anonymous callers
get grants only through a role named `guest`. Without one they see -- and can
filter, search and order on -- only the fields no role has a rule for. A
nested path needs read on each model along it, so the related model must be
`public_access` too, or `guest` must grant its `app.model.read`.

Upgrading from 0.5.2 or earlier, where anonymous callers without a `guest`
role saw every configured field: add a `guest` role granting the field reads
public visitors should see.

Anonymous callers can't write through the generated CRUD routes, whatever
`guest` grants.

`public_access` is for anonymous callers only. A signed-in user still needs a
role with the model's read: without one, they get a 403 where an anonymous
caller can read. That's intended. A signed-in user's rows are scoped by their
tenant and the model's predicates, so they may see rows an anonymous caller
can't, and only a role grants that.

### Assigning roles to users

In static mode (the default), TurboDRF reads `user.roles` -- an attribute or
property that returns a list of role names:

```python
# From Django groups
User.add_to_class('roles', property(lambda self: [g.name for g in self.groups.all()]))

# From a JSONField
class User(AbstractUser):
    user_roles = models.JSONField(default=list)

    @property
    def roles(self):
        return self.user_roles
```

Authenticated users with no roles get 403.

In database mode, roles come from the `UserRole` table and `user.roles` is
ignored. With the allauth or Keycloak integration on, TurboDRF installs a
`roles` property for you when the user model doesn't define one (see
[Integrations](integrations.md#how-the-integrations-resolve-roles)).

### Static vs database mode

`TURBODRF_PERMISSION_MODE` controls where role definitions live:

- **`'static'`** (default) — read from the `TURBODRF_ROLES` dict in
  settings. Fastest; changes require a deploy.
- **`'database'`** — read from `TurboDRFRole` / `RolePermission` /
  `UserRole` tables. Permissions can be mutated at runtime by an admin
  UI without redeploying, and take effect on the next request (see
  [Caching](#caching)).

The mode applies to the tenant scope. A platform router (staff across
tenants) has its own roles and map, `TURBODRF_PLATFORM_ROLE_GETTER` and
`TURBODRF_PLATFORM_ROLES`, always static: see
[Platform scope](platform.md#roles-and-permissions).

### Database-backed permissions

For runtime changes without redeployment:

```python
TURBODRF_PERMISSION_MODE = 'database'
TURBODRF_PERMISSION_CACHE_TIMEOUT = 300  # 5 minutes
```

```python
from turbodrf.models import TurboDRFRole, RolePermission, UserRole

role = TurboDRFRole.objects.create(name='editor')
RolePermission.objects.create(role=role, app_label='books', model_name='book', action='read')
RolePermission.objects.create(
    role=role, app_label='books', model_name='book',
    field_name='price', permission_type='read',
)
UserRole.objects.create(user=user, role=role)
```

Saving a `RolePermission` validates it like a `TURBODRF_ROLES` string: the
exact lowercase `app_label` and `model_name` of a registered model, and a
real concrete or many-to-many field name. `bulk_create()` and QuerySet
`update()` skip `save()`, so they skip this check.

### Caching

Permission snapshots are cached per `(user, model)` in Django's default
cache for `TURBODRF_PERMISSION_CACHE_TIMEOUT` seconds (default 300), under
the key prefix `TURBODRF_PERMISSION_CACHE_PREFIX` (default
`"turbodrf_perm"`).

The cache key covers the user's current roles, the role configuration
(`TURBODRF_ROLES` in static mode; every role's name and version in
database mode, for anonymous callers too), and the model's `sensitive_fields`
and `public_access`. So these take effect on the next request: assigning or
removing a user's role, changing `TURBODRF_ROLES`, saving or deleting a
`RolePermission` (which bumps its role's version), and a deploy that changes
a model's `sensitive_fields` or `public_access`.

A bulk `RolePermission` QuerySet `update()` or `delete()` skips the model's
`save()` / `delete()`, so it doesn't bump versions, and snapshots stay stale
until the TTL. Invalidate them after one:

```python
from django.contrib.auth.models import AnonymousUser
from turbodrf.backends import invalidate_user_permissions

invalidate_user_permissions(user)             # one user's snapshots
invalidate_user_permissions(AnonymousUser())  # anonymous callers'
invalidate_user_permissions()                 # everyone's
```

### Nested field permissions

Permissions are checked at each level of a nested field path. For `author__publisher__name`:

1. Can user read `author` on Book?
2. Can user read `publisher` on Author?
3. Can user read `name` on Publisher?

If any level fails, the field is excluded.

### Filter permissions

Users can only filter and order on configured fields (plus the primary key) they have read permission for; a nested filter needs its own path configured. Filters on other fields are silently ignored -- this prevents information leakage through binary search attacks. See [Filtering & Search](filtering.md#filtering).
