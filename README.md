# TurboDRF

[![PyPI Version](https://img.shields.io/pypi/v/turbodrf?label=pypi)](https://pypi.org/project/turbodrf/)
[![Tests](https://img.shields.io/github/actions/workflow/status/alexandercollins/turbodrf/ci.yml?branch=main&label=tests)](https://github.com/alexandercollins/turbodrf/actions)
[![Coverage](https://img.shields.io/badge/coverage-95.55%25-brightgreen)](https://github.com/alexandercollins/turbodrf)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![Django](https://img.shields.io/badge/django-4.2%20%7C%205.2%20%7C%206.0-darkgreen)](https://www.djangoproject.com/)
[![License](https://img.shields.io/badge/license-MIT-purple)](LICENSE)

**Dead simple Django REST Framework API generator with role-based permissions.**

Turn your Django models into fully-featured REST APIs with a mixin and a method. Zero boilerplate.

## Install

```bash
pip install turbodrf

# Optional: faster JSON rendering (7x faster than stdlib)
pip install turbodrf[fast]
```

## Quick Start

**1. Add to settings:**

```python
INSTALLED_APPS = [
    'rest_framework',
    'turbodrf',
]
```

**2. Add the mixin to your model:**

```python
from django.db import models
from turbodrf.mixins import TurboDRFMixin

class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    searchable_fields = ['title']

    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['title', 'author__name', 'price'],
                'detail': ['title', 'author__name', 'author__email', 'price']
            }
        }
```

**3. Add the router:**

```python
from turbodrf.router import TurboDRFRouter

router = TurboDRFRouter()

urlpatterns = [
    path('api/', include(router.urls)),
]
```

**Done.** You now have a full REST API with search, filtering, pagination, and field selection:

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

## Documentation

- [Configuration](docs/configuration.md) -- all `turbodrf()` options, field selection, nested fields
- [Permissions](docs/permissions.md) -- role-based, field-level, and Django default permissions
- [Tenancy & row-level access](docs/tenancy.md) -- predicates, multi-tenancy, FK injection defense
- [RLS (Postgres)](docs/rls.md) -- optional defense-in-depth at the database layer
- [Performance](docs/performance.md) -- compiled read path, fast JSON rendering, benchmarking
- [Filtering & Search](docs/filtering.md) -- filtering, search, ordering, OR queries
- [Integrations](docs/integrations.md) -- allauth, Keycloak, drf-api-tracking (all experimental)
- [Security](docs/security.md) -- sensitive fields, secure defaults, error responses
- [Management Commands](docs/commands.md) -- turbodrf_check, turbodrf_benchmark, turbodrf_explain
- **[Settings Reference](docs/settings_reference.md)** -- every TURBODRF_* setting in one place

## Permissions and access control

TurboDRF answers four standard authorization questions, in three layers
that all apply to every request (AND'd together):

| Question | Layer | Mechanism |
|---|---|---|
| Can this user reach this endpoint? | **RBAC** (Role-Based Access Control) | Roles in `TURBODRF_ROLES` map to permissions. `permissions.py` checks `<app>.<model>.<action>` for the request method. |
| Which rows can this user see? | **Row-level access** | Predicates declared in `turbodrf()` config. Mandatory **tenant boundary** + discretionary **within-tenant predicates** (Owner, Members, Either, Custom). |
| Which fields can this user read or write? | **Field-level permissions** | Per-field rules `<app>.<model>.<field>.read` / `.write` in `TURBODRF_ROLES`. Hidden fields are stripped from responses, search, ordering, filters, and OPTIONS metadata. |
| Are FK targets the user provides actually theirs? | **Write validation** | On every create/update, FKs in the request body are validated against the related model's predicate stack. Cross-tenant or invisible targets return 400. |

### How it actually works (concrete walk-through)

A multi-tenant SaaS has two workspaces (ABC and XYZ) and three roles: `member`, `manager`, `admin`. A `Project` model is configured:

```python
class Project(models.Model, TurboDRFMixin):
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
- `POST /api/comments/ {"document": <XYZ_bank_id>}` → 400 (FK injection rejected)

If a manager (with bypass) at ABC asks for `/api/projects/`:

```sql
WHERE project.workspace_id = <ABC's workspace id>
-- no owner filter (manager bypassed it)
```

Manager sees all ABC projects, but still can't see XYZ — the tenant boundary is **mandatory** and applied separately from the predicate algebra (it's a setting, not a predicate). This rules out an entire class of compositional bugs where bypass roles could OR-compose their way past the tenant wall.

### Optional: Postgres Row Level Security (defense in depth)

For Postgres deployments, TurboDRF can additionally generate RLS policies that enforce the same rules **at the database layer** — every connection is filtered by Postgres itself, so even raw SQL or admin scripts are blocked. App-layer is the source of truth; RLS is a backup. See [docs/rls.md](docs/rls.md). RLS is **off by default** (three manual steps to enable: install middleware, run `turbodrf_emit_rls`, apply the SQL).

### Performance

Tenant + owner predicates add **~0 measurable latency** vs. the unscoped baseline (predicates compile to a single Q AND'd onto the queryset; the WHERE clause runs at the DB layer with index hits). FK injection check on writes adds ~one `.exists()` query per FK in the request body. Both are negligible for typical workloads. See [docs/performance.md](docs/performance.md) for benchmarking the compiled vs DRF read paths.

### Quick recipes

```python
# Multi-tenant SaaS — most common case
{'tenant_field': 'store', 'owner_field': 'customer', 'bypass_owner_roles': ['staff']}

# Personal data app (no tenant)
{'owner_field': 'author', 'bypass_owner_roles': ['admin']}

# Reference data (currencies, country codes — not tenant-scoped)
{'tenancy': 'shared'}

# M2M membership (Slack channels, Linear projects)
{'visibility': [Tenant('workspace'), Members('participants')]}

# Power-form composition (when sugar doesn't fit)
{'visibility': [Tenant('workspace'), Either(Owner('owner'), Members('shared_with'))]}
```

See [docs/tenancy.md](docs/tenancy.md) for the full predicate vocabulary, hard-fail-at-startup behavior, and 404-vs-403 semantics.

## License

MIT License. See [LICENSE](LICENSE) for details.
