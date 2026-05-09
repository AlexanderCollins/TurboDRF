# TurboDRF

[![PyPI Version](https://img.shields.io/pypi/v/turbodrf?label=pypi)](https://pypi.org/project/turbodrf/)
[![Tests](https://img.shields.io/github/actions/workflow/status/alexandercollins/turbodrf/ci.yml?branch=main&label=tests)](https://github.com/alexandercollins/turbodrf/actions)
[![Coverage](https://img.shields.io/badge/coverage-95.45%25-brightgreen)](https://github.com/alexandercollins/turbodrf)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![Django](https://img.shields.io/badge/django-4.2%20%7C%205.2%20%7C%206.0-darkgreen)](https://www.djangoproject.com/)
[![License](https://img.shields.io/badge/license-MIT-purple)](LICENSE)

**Dead simple Django REST Framework API generator with role-based permissions.**

Turn your Django models into fully-featured REST APIs with a mixin and a method. Zero boilerplate.

## Walkthrough

A 16-minute walkthrough covering setup, query parameters, writes, role-based access control, predicates, and the security model:

<!-- After pushing to main, GitHub renders this as an inline player. -->
<video src="https://github.com/alexandercollins/turbodrf/raw/main/docs/walkthrough.mp4" controls width="100%"></video>

If your renderer doesn't show the player, use the [direct link](docs/walkthrough.mp4).

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

## Why trust this framework

If you're evaluating TurboDRF for production, you should know exactly
what it guarantees, how those guarantees are verified, and where your
responsibility starts. This section is the honest version.

### What TurboDRF guarantees

These are structural properties of the framework. They hold for every
model that uses `TurboDRFMixin`:

1. **Tenant isolation cannot be composed away.** The tenant boundary is
   a setting (`tenant_field`), not a predicate. It's AND'd onto every
   queryset outside the predicate algebra, so no `Either(...)` OR-
   composition can escape it. `Either(Tenant(...), ...)` is rejected
   at config-parse time.

2. **Every URL surface is filtered.** List, detail, search, ordering,
   filter, OPTIONS, browsable API, M2M renders — each has a named
   protection at a specific code location. Filter `__`-traversals
   through predicate-bearing targets are scoped at request time so
   they can't bypass the target's visibility rules.

3. **Cross-tenant rows return 404, not 403.** Detail/PATCH/DELETE on a
   row that's filtered out doesn't reveal whether the row exists.

4. **Writes go through three independent checks.** Tenant FK auto-fill
   (always overwrites client values), predicate `validate_write`,
   FK-injection guard (every FK in the body must resolve to a row the
   caller can see under the related model's predicate stack).

5. **Misconfiguration fails loud at startup.** Five gates run on
   router init:

   - Tenancy validation — every model declares its tenancy.
   - Compiled-path safety — M2M/FK joins to predicate-bearing targets.
   - Searchable-fields safety — `__`-paths through predicate-bearing
     models.
   - Custom-predicate write safety — `Custom` requires explicit
     `write_validator`.
   - Permission-string typo check — every entry in `TURBODRF_ROLES`
     resolves to a real model + field + action.

   Each gate emits a directed error message naming the offending
   model/role/field. None has a default kill switch.

6. **Anonymous and unresolved-tenant requests fail closed.** Missing
   user → `Q(pk__in=[])`. Missing tenant value → `Q(pk__in=[])`. No
   request that can't prove its tenant ever sees data.

### How those guarantees are verified

- **1,558 unit + integration tests**, including ~200 in
  `tests/integration/test_security_*` that explicitly attempt
  cross-tenant attacks (FK injection, search inference, ordering-by-
  hidden-field, filter traversal, PATCH-to-other-tenant, etc.).
- **A separate sanity-check project** (recipe in [docs/sanity_check.md](docs/sanity_check.md))
  that wires up TurboDRF with two-tenant fixtures and runs 32 explicit
  attacks against a live API. Use it as a reference for what the
  framework claims to do, and adapt it to your own deployment.
- **Static gates run at every boot.** Even in CI, a misconfigured app
  refuses to start. Production deploys can't ship a config the gates
  would reject.

### Where your responsibility starts

The framework can't read your mind. You own:

- **Intentional opt-outs.** `tenancy: "shared"`, `public_access: True`,
  `TURBODRF_DISABLE_PERMISSIONS=True`, `TURBODRF_REQUIRE_TENANCY=False`,
  and any `TURBODRF_ALLOW_UNSAFE_*` kill-switches. If you flipped one
  by mistake, the gates won't catch it.
- **The contents of `JSONField`s.** The sensitive deny-list matches
  field names, not content. Don't store passwords inside JSON blobs.
- **Custom `@action` methods on viewset subclasses.** If your action
  doesn't call `get_queryset()` (or `_get_base_queryset()` and apply
  scoping), it bypasses the framework's filters. This is documented at
  the top of `views.py:get_queryset`.
- **`Custom` predicate `q_func` correctness.** Your function returns an
  arbitrary Django `Q`. If the logic is wrong, no gate catches it.
  Keep `q_func`s small and unit-test them.
- **Adjacent permission classes.** TurboDRF's `permission_classes` is
  hardcoded to `[TurboDRFPermission]`. If you need MFA / subscription
  / IP gates, add them at a layer in front (middleware, custom
  authentication backend) — not by editing the viewset.
- **Postgres RLS, if you want defense in depth.** RLS is opt-in; see
  `docs/rls.md`. Without it, app-layer scoping is the only defense.

### What this is *not*

- Not audited by an independent security firm.
- Not certified for any specific compliance regime (SOC 2, HIPAA, PCI).
  If you need certified controls, deploy TurboDRF behind defense-in-
  depth (RLS, network segmentation, etc.) and have your environment
  audited holistically.
- Not a substitute for understanding what your roles + predicates
  declare. The gates verify *internal consistency*; they cannot tell
  you whether the rules you wrote match your business intent.
- Not warranted. See the License section below.

### Quick "should I ship this?" checklist

- [ ] Boot completes without `ImproperlyConfigured` from any of the
      five gates.
- [ ] `TURBODRF_REQUIRE_TENANCY = True` (the default).
- [ ] `TURBODRF_DISABLE_PERMISSIONS` is not set in production settings.
- [ ] No `TURBODRF_ALLOW_UNSAFE_*` kill switches are enabled in
      production.
- [ ] Every `Custom` predicate has an explicit `write_validator`
      (the gate enforces this).
- [ ] Run the sanity-check recipe against your own model setup at
      least once.

If all six are true, the cross-tenant authz layer is doing what it
claims. From there, your residual risk is intentional configuration
choices and code outside the framework's reach.

## License

MIT License. See [LICENSE](LICENSE) for details.

```
Copyright (c) the TurboDRF authors

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

The MIT license disclaims warranty for a reason. TurboDRF is built
with care, tested against an intentionally adversarial test suite, and
designed to fail loud when misconfigured — but it's a library that
runs inside *your* application against *your* data. You are
responsible for verifying it does what you need before you ship it.
The "Quick should I ship this?" checklist above is the bar; clear it
before depending on the framework in production.
