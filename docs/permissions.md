# Permissions

TurboDRF supports three permission modes.

## 1. No permissions (development)

```python
TURBODRF_DISABLE_PERMISSIONS = True
```

## 2. Django default permissions

Uses Django's built-in `view_`, `add_`, `change_`, `delete_` permissions:

```python
TURBODRF_USE_DEFAULT_PERMISSIONS = True
```

Neither mode has field-level rules (a model's `sensitive_fields` included).
The `TURBODRF_SENSITIVE_FIELDS` deny-list still applies to responses,
filters and OPTIONS, but not to search or ordering (see
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
The router checks every string at startup and refuses to boot on a typo (see
[Security](security.md#permission-string-typo-gate)).

### How field permissions work

1. If ANY role defines an explicit field rule (e.g. `price.read`), that field requires explicit permission for ALL roles -- in static and database mode, and for anonymous callers
2. Fields listed in the model's `sensitive_fields` attribute are readable only through an explicit `.read` grant, as if a rule existed (reads only; add a `.write` rule to restrict writes)
3. Fields without explicit rules fall back to model-level permission
4. This means: to restrict `price` for viewers, you must add `price.read` to at least one role (like admin)

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

### Assigning roles to users

TurboDRF reads `user.roles` -- a property that returns a list of role names:

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

### Static vs database mode

`TURBODRF_PERMISSION_MODE` controls where role definitions live:

- **`'static'`** (default) — read from the `TURBODRF_ROLES` dict in
  settings. Fastest; changes require a deploy.
- **`'database'`** — read from `TurboDRFRole` / `RolePermission` /
  `UserRole` tables. Permissions can be mutated at runtime by an admin
  UI without redeploying, and take effect on the next request (see
  [Caching](#caching)).

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
UserRole.objects.create(user=user, role=role)
```

### Caching

Permission snapshots are cached per `(user, model)` in Django's default
cache for `TURBODRF_PERMISSION_CACHE_TIMEOUT` seconds (default 300), under
the key prefix `TURBODRF_PERMISSION_CACHE_PREFIX` (default
`"turbodrf_perm"`).

The cache key covers the user's current roles and the role configuration
(`TURBODRF_ROLES` in static mode; every role's name and version in
database mode), so these take effect on the next request: assigning or
removing a user's role, changing `TURBODRF_ROLES`, and saving or deleting a
`RolePermission` (which bumps its role's version).

A bulk `RolePermission` QuerySet `update()` or `delete()` skips the model's
`save()` / `delete()`, so it doesn't bump versions, and snapshots stay stale
until the TTL. Invalidate them after one:

```python
from turbodrf.backends import invalidate_user_permissions

invalidate_user_permissions(user)   # one user's snapshots
invalidate_user_permissions()       # everyone's
```

### Nested field permissions

Permissions are checked at each level of a nested field path. For `author__publisher__name`:

1. Can user read `author` on Book?
2. Can user read `publisher` on Author?
3. Can user read `name` on Publisher?

If any level fails, the field is excluded.

### Filter permissions

Users can only filter and order on configured fields (plus the primary key) they have read permission for. Filters on other fields are silently ignored -- this prevents information leakage through binary search attacks. See [Filtering & Search](filtering.md#filtering).
