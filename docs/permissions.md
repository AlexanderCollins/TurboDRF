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
    ],
    'editor': [
        'books.book.read',
        'books.book.update',
        'books.book.price.read',       # Can see price
    ],
    'viewer': [
        'books.book.read',
        'books.book.title.read',       # Can only see title
    ],
    'guest': [
        'books.book.read',             # For public_access endpoints
    ],
}
```

### Permission format

- Model-level: `app_label.model_name.action` (read, create, update, delete)
- Field-level: `app_label.model_name.field_name.read` or `.write`

### How field permissions work

1. If ANY role defines an explicit field rule (e.g. `price.read`), that field requires explicit permission for ALL roles
2. Fields without explicit rules fall back to model-level permission
3. This means: to restrict `price` for viewers, you must add `price.read` to at least one role (like admin)

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

### Nested field permissions

Permissions are checked at each level of a nested field path. For `author__publisher__name`:

1. Can user read `author` on Book?
2. Can user read `publisher` on Author?
3. Can user read `name` on Publisher?

If any level fails, the field is excluded.

### Filter permissions

Users can only filter on fields they have read permission for. Filters on hidden fields are silently ignored -- this prevents information leakage through binary search attacks.
