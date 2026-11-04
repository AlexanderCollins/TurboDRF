# TurboDRF

[![PyPI Version](https://img.shields.io/pypi/v/turbodrf?label=pypi)](https://pypi.org/project/turbodrf/)
[![Tests](https://img.shields.io/github/actions/workflow/status/AlexanderCollins/TurboDRF/ci.yml?branch=main&label=tests)](https://github.com/AlexanderCollins/TurboDRF/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/AlexanderCollins/be56a59111eeae7a6d64b60d119d2bc9/raw/turbodrf-coverage.json)](https://github.com/AlexanderCollins/TurboDRF/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![Django](https://img.shields.io/badge/django-4.2%20%7C%205.2%20%7C%206.0-darkgreen)](https://www.djangoproject.com/)
[![License](https://img.shields.io/badge/license-MIT-purple)](https://github.com/AlexanderCollins/TurboDRF/blob/main/LICENSE)

**Turn your Django models into a secure REST API.** Describe each model's API
next to its fields. TurboDRF serves the endpoints and enforces who can see
which rows and fields on every request.

```python
class Project(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    budget = models.DecimalField(max_digits=12, decimal_places=2)

    sensitive_fields = ['budget']                 # only roles granted it see it

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['id', 'title', 'workspace', 'owner', 'budget'],
            'tenant_field': 'workspace',          # nobody sees another workspace
            'owner_field': 'owner',               # members see their own projects
            'bypass_owner_roles': ['manager'],    # managers see all of the workspace's
        }
```

That's a full API at `/api/projects/`: list, detail, create, update, delete,
search, filters and paging, plus its docs. Each caller only gets the rows and
fields their role allows, and that applies everywhere: nested fields, filters,
search, ordering, writes and the API schema.

## Why

In a hand-written DRF API every viewset repeats the same checks: which rows
this user may see, which fields, and whether the IDs in a request body belong
to them. Miss one in one place, say a filter on a related model or an FK in a
POST, and another tenant's data leaks.

TurboDRF writes those checks once, in the framework, and applies them on every
path. You declare the rules; you can't forget to call them.

## What you get

- **CRUD endpoints** with pagination, search, filters (including OR filters),
  ordering and `?fields=` selection.
- **Role-based permissions**, per model and per field, for reading and
  writing.
- **Multi-tenant row scoping:** a tenant boundary nothing can bypass, plus
  owner, member, team and custom rules inside it.
- **Write protection:** every FK and M2M value in a request must be a row the
  caller can see, so nobody can attach another tenant's data.
- **Custom endpoints** that get the same scoping as the generated ones.
- **API docs:** Swagger, ReDoc and an OpenAPI 3 schema, each showing a caller
  only what they can use.
- **A staff API across tenants**, behind its own gate, with its own roles.
- **Startup checks** that refuse unsafe configs, and `turbodrf_explain` to
  answer "why can't this user see that?"

## Install

```bash
pip install turbodrf
pip install "turbodrf[openapi3]"   # optional: an OpenAPI 3 schema for typed clients
pip install "turbodrf[fast]"       # optional: faster JSON rendering (msgspec)
```

Django 4.2, 5.2, 6.0 and 6.1 on Python 3.10–3.14.

## Quick start

**1. Settings.** Add the apps, and give each role the actions it may perform:

```python
INSTALLED_APPS = [
    # ... Django's default apps ...
    'rest_framework',
    'drf_yasg',      # the Swagger / ReDoc pages
    'turbodrf',
    'shop',
]

TURBODRF_ROLES = {
    'editor': [
        'shop.book.read', 'shop.book.create', 'shop.book.update', 'shop.book.delete',
        'shop.author.read',
    ],
    'viewer': ['shop.book.read', 'shop.author.read'],
}

# Roles are the names of the user's Django groups.
TURBODRF_ROLE_GETTER = 'turbodrf.backends.group_names'
```

**2. Models.** Add the mixin and say what the API exposes:

```python
from django.db import models
from turbodrf.mixins import TurboDRFMixin

class Author(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=100)

    @classmethod
    def turbodrf(cls):
        return {'fields': ['id', 'name']}

class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['id', 'title', 'author', 'author__name', 'price'],
            'searchable_fields': ['title'],
        }
```

**3. URLs.**

```python
from django.urls import include, path

urlpatterns = [path('api/', include('turbodrf.urls'))]
```

**4. Run it.** Run `makemigrations` and `migrate`, then put a user in the
`editor` group:

```
GET    /api/books/                       list (paged)
GET    /api/books/1/                     detail
POST   /api/books/                       create
PATCH  /api/books/1/                     update
DELETE /api/books/1/                     delete
GET    /api/books/?search=django         search
GET    /api/books/?price__lt=20          filter
GET    /api/books/?ordering=-price       order
GET    /api/books/?fields=title,price    pick fields
GET    /api/swagger/                     docs, for this caller
```

Run `python manage.py check` in CI: it runs every startup check. The
[quick start guide](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/quickstart.md)
walks through these steps in a new project.

## How access works

Every request passes three checks, and all three must pass:

| Check | Question | Declared in |
|---|---|---|
| **Role** | May this user read, create, update or delete this model? | `TURBODRF_ROLES`: `shop.book.read` |
| **Rows** | Which rows may they touch? | the model's `tenant_field`, `owner_field`, `visibility` |
| **Fields** | Which fields may they read or write? | `TURBODRF_ROLES`: `shop.book.price.read` / `.write` |

The row rules are applied in one place in the framework, so they hold on every
path, not just the list. For the `Project` model above, a `member` named Alice
in workspace ABC gets:

```sql
WHERE project.workspace_id = <ABC>      -- the tenant boundary: always, first
  AND project.owner_id = <Alice>        -- the owner rule (managers skip it)
```

And these attempts all fail:

| Alice tries | She gets |
|---|---|
| `GET /api/projects/<XYZ's project>/` | 404, the same as a project that doesn't exist |
| `PATCH` her project with `{"workspace": <XYZ>}` | 400: it can't leave her workspace |
| `POST /api/tasks/` with `{"project": <XYZ's project>}` | 400: she can't point at rows she can't see |
| reading `budget` | it isn't in the response: no role of hers grants `budget.read` |
| `?budget__gte=100000` | ignored: a field she can't read can't be filtered, searched or ordered by either |

The tenant boundary is a setting, not a rule you compose. A manager's bypass,
or an `Either(...)` of rules, can widen what someone sees inside their
workspace, but never outside it.

### Row rules for common cases

```python
{'tenant_field': 'store', 'owner_field': 'customer', 'bypass_owner_roles': ['staff']}  # most SaaS models
{'tenant_field': 'workspace', 'owner_field': ['author', 'reviewer']}                   # several owner columns
{'tenant_field': 'workspace', 'visibility': [Members('participants')]}                 # M2M membership
{'tenant_field': 'workspace', 'visibility': [Group('team')]}                           # the row's team
{'tenant_field': 'workspace',                                                          # owner, or shared with you
 'visibility': [Either(Owner('owner'), Members('shared_with'))]}
{'tenancy': 'shared'}                                                                  # reference data
```

With `TURBODRF_TENANT_MODEL` set, a model that makes no tenancy decision
doesn't start. See [Tenancy](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/tenancy.md)
for the full rule set.

## More features

### Models you can't edit

Skip the mixin and register the same config from an `AppConfig.ready()`.
This works for `auth.User` and other apps' models:

```python
from django.contrib.auth import get_user_model
import turbodrf

turbodrf.register(get_user_model(), {
    'fields': ['id', 'username', 'first_name'],
    'tenant_field': 'workspace',      # a FK on your user model
    'read_only': True,
})
```

### Custom endpoints

```python
from rest_framework.response import Response
from turbodrf import turbodrf_action

@turbodrf_action(detail=True, methods=['post'])
def archive(self, request, pk=None):
    project = self.get_object()      # scoped: another workspace's project is a 404
    project.archived = True
    project.save(update_fields=['archived'])
    return Response({'status': 'archived'})

# in turbodrf():  'actions': [archive]
```

### Typed clients

With the `openapi3` extra, `/api/openapi/` serves an OpenAPI 3 schema scoped to
the caller. Fetch it as the user whose client you're generating:

```bash
curl -H "Authorization: Bearer $TOKEN" https://example.com/api/openapi/ -o openapi.json
npx openapi-typescript openapi.json -o src/api/schema.d.ts
```

`python manage.py turbodrf_schema --role editor` prints the Swagger schema a
role sees, offline, for CI.

### "Why can't this user see that?"

```
$ python manage.py turbodrf_explain --user sam --model demo.Project --pk 1
Caller: sam (pk 1)
  Roles: viewer
  Tenant: Acme (pk 1, from user.workspace)
...
  budget [list, detail]
    read:  no (in the model's sensitive_fields: needs demo.project.budget.read, not granted)
    write: no (not readable, so not in the serializer)

Row 1: exists (base manager)
  Default manager (Project.objects): includes it
  Tenant boundary (workspace): passes (row's tenant 1, caller's 1)
  Owner('owner', bypass=['admin', 'manager']): EXCLUDES it
  Verdict: NOT visible through the API: Owner('owner', bypass=['admin', 'manager']) excludes it
```

With `DEBUG = True`, a 403 response also says which permission was missing.

### Staff across tenants

Your own staff console gets a second router with a required gate and its own
roles. Each model opts in with a `'platform'` block:

```python
console = TurboDRFRouter(scope='platform', gate=[IsPlatformStaff])
```

See [Platform scope](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/platform.md).

## Design principles

The security model rests on a few rules the code holds on every path:

1. Nothing is exposed until you say so: models, roles, fields.
2. Rows are scoped in one place, for every way a request reaches them.
3. The tenant boundary sits outside the rule logic, so nothing can OR it away.
4. Rules OR for reads and AND for writes: seeing a row through one rule never
   lifts another rule's write check.
5. A hidden field is hidden everywhere: responses, filters, search, ordering,
   the schema.
6. When in doubt, deny: no tenant or no role means no rows.
7. Unsafe configs fail at startup, not on the first request.
8. A hidden row looks like a missing one.

[Design principles](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/principles.md)
explains each one, the guarantees they add up to and how they're tested.

## When to use it

TurboDRF fits a backend serving its own frontend: a multi-tenant SaaS, an
internal tool, an app with many CRUD models and real access rules. The API
mirrors your models, which is what makes the generated checks possible.

For a public API with a long-lived contract that has to stay different from
your models, write those endpoints by hand. Hand-written DRF views can sit
next to TurboDRF's in the same project.

## Documentation

Full docs: <https://turbodrf.com/docs/>

- [Quick start](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/quickstart.md) and [Installation](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/installation.md)
- [Configuration](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/configuration.md): every `turbodrf()` option, custom endpoints, `register()`
- [Permissions](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/permissions.md): roles, field rules, anonymous access, database-backed roles
- [Tenancy](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/tenancy.md): the tenant boundary and the row rules
- [Design principles](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/principles.md) and [Security](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/security.md): the model, the threat model, known limitations
- [Platform scope](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/platform.md): a staff API across tenants
- [Filtering & search](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/filtering.md), [Performance](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/performance.md), [Management commands](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/commands.md), [Integrations](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/integrations.md)
- [Settings reference](https://github.com/AlexanderCollins/TurboDRF/blob/main/docs/settings_reference.md): every `TURBODRF_*` setting
- [AGENTS.md](https://github.com/AlexanderCollins/TurboDRF/blob/main/AGENTS.md): the rules in one page, for AI coding agents

## License

MIT. See [LICENSE](https://github.com/AlexanderCollins/TurboDRF/blob/main/LICENSE).
