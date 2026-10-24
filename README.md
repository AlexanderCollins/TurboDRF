# TurboDRF

[![PyPI Version](https://img.shields.io/pypi/v/turbodrf?label=pypi)](https://pypi.org/project/turbodrf/)
[![Tests](https://img.shields.io/github/actions/workflow/status/AlexanderCollins/TurboDRF/ci.yml?branch=main&label=tests)](https://github.com/AlexanderCollins/TurboDRF/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/AlexanderCollins/be56a59111eeae7a6d64b60d119d2bc9/raw/turbodrf-coverage.json)](https://github.com/AlexanderCollins/TurboDRF/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![Django](https://img.shields.io/badge/django-4.2%20%7C%205.2%20%7C%206.0-darkgreen)](https://www.djangoproject.com/)
[![License](https://img.shields.io/badge/license-MIT-purple)](https://github.com/AlexanderCollins/TurboDRF/blob/main/LICENSE)

**Dead simple Django REST Framework API generator with role-based permissions.**

Turn your Django models into fully-featured REST APIs with a mixin and a method. Zero boilerplate.

## Install

```bash
pip install turbodrf

# Optional: faster JSON rendering with msgspec
pip install "turbodrf[fast]"
```

## Quick start

**1. Settings.** Add the apps, and roles that grant each action:

```python
INSTALLED_APPS = [
    # ... Django's default apps ...
    'rest_framework',
    'drf_yasg',      # serves the Swagger / ReDoc pages
    'turbodrf',
    'shop',
]

TURBODRF_ROLES = {
    'editor': [
        'shop.author.read', 'shop.author.create', 'shop.author.update', 'shop.author.delete',
        'shop.book.read', 'shop.book.create', 'shop.book.update', 'shop.book.delete',
    ],
    'viewer': ['shop.author.read', 'shop.book.read'],
}
```

**2. Models.** Add the mixin and a `turbodrf()` classmethod:

```python
from django.db import models
from turbodrf.mixins import TurboDRFMixin

class Author(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=100)
    email = models.EmailField()

    @classmethod
    def turbodrf(cls):
        return {'fields': ['id', 'name', 'email']}

class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['id', 'title', 'author', 'author__name', 'price'],
                'detail': ['id', 'title', 'author', 'author__name', 'author__email', 'price'],
            },
            'searchable_fields': ['title'],
        }
```

**3. Give users roles.** TurboDRF reads `user.roles`, a list of role names.
For example, use the names of the user's Django groups, in your app's
`AppConfig.ready()`:

```python
from django.contrib.auth.models import User

User.add_to_class('roles', property(lambda user: [g.name for g in user.groups.all()]))
```

Other options: [Assigning roles to users](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/permissions.md#assigning-roles-to-users).

**4. URLs.**

```python
from django.urls import include, path

urlpatterns = [
    path('api/', include('turbodrf.urls')),
]
```

Run `makemigrations` and `migrate`, put a user in the `editor` group, and
the API is up:

```
GET    /api/books/                          # List
GET    /api/books/1/                        # Detail
POST   /api/books/                          # Create
PUT    /api/books/1/                        # Update
DELETE /api/books/1/                        # Delete
GET    /api/books/?search=django            # Search
GET    /api/books/?price__lt=20             # Filter
GET    /api/books/?fields=title,price       # Select fields
```

A user with no role gets a 403 on every endpoint, and a nested field such as
`author__name` needs read on the related model (`shop.author.read`). Roles
don't scope rows: every editor sees every book until the model declares an
`owner_field` or `tenant_field` (see [Tenancy](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/tenancy.md)).

The [quick start guide](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/quickstart.md) runs these steps in a new
project, with users and requests.

## Documentation

Full docs: <https://turbodrf.com/docs/>

- [Installation](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/installation.md) -- requirements, extras, checking the configuration
- [Configuration](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/configuration.md) -- all `turbodrf()` options: field selection, nested & computed fields, custom endpoints (`actions`), read-only endpoints, method allow-lists, `full_clean` validation
- [Permissions](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/permissions.md) -- role-based, field-level, and Django default permissions
- [Tenancy & row-level access](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/tenancy.md) -- predicates, multi-tenancy, FK injection defense
- [Performance](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/performance.md) -- compiled read path, fast JSON rendering, benchmarking
- [Filtering & Search](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/filtering.md) -- filtering, search, ordering, OR queries
- [Integrations](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/integrations.md) -- allauth, Keycloak, drf-api-tracking, Sentry (all experimental)
- [Security](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/security.md) -- sensitive fields, secure defaults, error responses
- [Management Commands](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/commands.md) -- turbodrf_check, turbodrf_benchmark, turbodrf_explain
- **[Settings Reference](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/settings_reference.md)** -- every TURBODRF_* setting in one place

## Permissions and access control

TurboDRF answers four standard authorization questions, in three layers
that all apply to every request (AND'd together):

| Question | Layer | Mechanism |
|---|---|---|
| Can this user reach this endpoint? | **RBAC** (Role-Based Access Control) | Roles in `TURBODRF_ROLES` map to permissions. `permissions.py` checks `<app>.<model>.<action>` for the request method. |
| Which rows can this user see? | **Row-level access** | Predicates declared in `turbodrf()` config. Mandatory **tenant boundary** + discretionary **within-tenant predicates** (Owner, Members, Either, Custom). |
| Which fields can this user read or write? | **Field-level permissions** | Per-field rules `<app>.<model>.<field>.read` / `.write` in `TURBODRF_ROLES`. Hidden fields are stripped from responses, search, ordering, filters, and OPTIONS metadata. |
| Are FK targets the user provides actually theirs? | **Write validation** | On every create/update, FK and M2M values in the request body are validated against the related model's tenant boundary and predicate stack, on every model (shared ones included). Cross-tenant or invisible targets return 400. |

Field rules bind anonymous callers too: `public_access: True` gives them model-level read, and a field that any role has a read rule for needs a grant from a `guest` role. See [Permissions](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/permissions.md#anonymous-callers-and-the-guest-role).

### How it actually works (concrete walk-through)

A multi-tenant SaaS has two workspaces (ABC and XYZ) and three roles: `member`, `manager`, `admin`. A `Project` model is configured:

```python
class Project(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)

    @classmethod
    def turbodrf(cls):
        return {
            'tenant_field': 'workspace',                       # mandatory wall
            'owner_field': 'owner',             # within-tenant rule
            'bypass_owner_roles': ['manager', 'admin'],        # roles ignore owner check
            'fields': ['title', 'workspace', 'owner'],
        }
```

Plus two project-wide settings:

```python
TURBODRF_TENANT_MODEL = 'accounts.Workspace'
TURBODRF_TENANT_USER_FIELD = 'workspace'   # request.user.workspace → tenant
```

Now a request `GET /api/projects/` from Alice (member at ABC) goes through:

1. **Permission gate** — Alice's role `member` has `app.project.read`. Pass.
2. **Tenant filter** (mandatory, applied first, never bypassable):
   ```sql
   WHERE project.workspace_id = <Alice's workspace>
   ```
3. **Owner filter** (Alice has no bypass role, so this layer applies):
   ```sql
   AND project.owner_id = <Alice's user id>
   ```
4. **Field stripping** — Alice's role has read on `title`, `workspace`, `owner` but maybe not all configured fields. Hidden ones are removed from the response.

If Alice tries cross-tenant tricks:
- `GET /api/projects/<XYZ_project_id>/` → 404 (not 403, no existence leak)
- `PATCH /api/projects/<her_project_id>/ {"workspace": <XYZ>}` → 400 (tenant reassignment rejected)
- `POST /api/tasks/ {"project": <XYZ_project_id>}` → 400 (FK injection: she can't reference another tenant's project)

If a manager (with bypass) at ABC asks for `/api/projects/`:

```sql
WHERE project.workspace_id = <ABC's workspace id>
-- no owner filter (manager bypassed it)
```

Manager sees all ABC projects, but still can't see XYZ — the tenant boundary is **mandatory** and applied separately from the predicate algebra (it's a setting, not a predicate). This rules out an entire class of compositional bugs where bypass roles could OR-compose their way past the tenant wall.

### Quick recipes

```python
# Multi-tenant SaaS — most common case
{'tenant_field': 'store', 'owner_field': 'customer', 'bypass_owner_roles': ['staff']}

# Personal data app (no TURBODRF_TENANT_MODEL; with one, owner_field alone fails the startup check)
{'owner_field': 'author', 'bypass_owner_roles': ['admin']}

# Reference data (currencies, country codes — not tenant-scoped)
{'tenancy': 'shared'}

# M2M membership (Slack channels, Linear projects)
{'tenant_field': 'workspace', 'visibility': [Members('participants')]}

# Several owner columns (a match on any of them shows the row)
{'tenant_field': 'workspace', 'owner_field': ['author', 'reviewer']}

# Power-form composition: shared_with users can edit the row, not reassign its owner
{'tenant_field': 'workspace', 'visibility': [Either(Owner('owner'), Members('shared_with'))]}
```

See [Tenancy](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/tenancy.md) for the full predicate vocabulary, hard-fail-at-startup behavior, and 404-vs-403 semantics.

## License

MIT License. See [LICENSE](https://github.com/AlexanderCollins/TurboDRF/blob/main/LICENSE) for details.
