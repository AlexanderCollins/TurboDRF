# Platform scope (staff across tenants)

TurboDRF's default scope is the **tenant** scope: every request is held to the
caller's tenant and the model's predicates. An operator usually also needs an
API for its own staff (support, billing, operations) that reads and acts on
every tenant's rows: a job queue, webhook deliveries, an audit log, the
tenants themselves.

The **platform** scope serves that API from the same models:

- a model opts in with a `"platform"` block in its `turbodrf()` config;
- `TurboDRFRouter(scope="platform", gate=[...])` registers only those models,
  with no tenant boundary and no predicates;
- the `gate` (required) is checked before anything else on every route;
- roles and permissions are separate from the tenant ones: a tenant role never
  grants anything on the platform router, and a platform role never grants
  anything on the tenant router.

Everything else is the same machinery: field rules, filters, search, ordering,
pagination, the compiled list, the `@property` sandbox, `@turbodrf_action`
actions, OPTIONS, the sensitive-field deny-list and the OpenAPI schema.

## A worked example

A SaaS whose tenant is `accounts.Workspace`. Its staff console needs the job
queue (read-only, with a retry action), support tickets (staff create and
edit them for any workspace) and the workspaces.

### Settings

```python
# settings.py
TURBODRF_TENANT_MODEL = "accounts.Workspace"
TURBODRF_TENANT_USER_FIELD = "workspace"

TURBODRF_ROLES = {...}                    # tenant roles, as before

# Platform roles: the same shape as TURBODRF_ROLES, a separate map.
TURBODRF_PLATFORM_ROLES = {
    "support": [
        "accounts.workspace.read",
        "jobs.job.read",
        "support.ticket.read",
        "support.ticket.create",
        "support.ticket.update",
    ],
    "engineer": [
        "accounts.workspace.read",
        "jobs.job.read",
        "jobs.job.create",                # the POST retry action
        "jobs.job.payload.read",          # only engineers see raw payloads
    ],
}

# Where a user's platform roles come from: a dotted path or a callable
# user -> list of role names. Default: user.platform_roles.
TURBODRF_PLATFORM_ROLE_GETTER = "accounts.roles.platform_roles"
```

```python
# accounts/roles.py
def platform_roles(user):
    staff = getattr(user, "staff_profile", None)
    return list(staff.roles) if staff and staff.active else []
```

### Models

```python
# jobs/models.py
from rest_framework.response import Response
from turbodrf import TurboDRFMixin, turbodrf_action


@turbodrf_action(detail=True, methods=["post"], url_path="retry")
def retry(self, request, pk=None):
    job = self.get_object()          # any workspace's job, on this scope
    job.requeue()
    return Response({"status": job.status})


class Job(models.Model, TurboDRFMixin):
    workspace = models.ForeignKey("accounts.Workspace", on_delete=models.CASCADE)
    kind = models.CharField(max_length=50)
    status = models.CharField(max_length=20)
    attempts = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict)
    last_error = models.TextField(blank=True)

    @classmethod
    def turbodrf(cls):
        return {
            "enabled": False,                    # no tenant endpoint
            "tenant_field": "workspace",
            "fields": ["id", "kind"],
            "platform": {
                "endpoint": "jobs",
                "read_only": True,
                "fields": {
                    "list": ["id", "kind", "status", "attempts",
                             "workspace", "workspace__name"],
                    "detail": ["id", "kind", "status", "attempts",
                               "workspace", "workspace__name",
                               "payload", "last_error"],
                },
                "searchable_fields": ["kind", "last_error", "workspace__name"],
                "actions": [retry],
            },
        }
```

```python
# support/models.py
class Ticket(models.Model, TurboDRFMixin):
    workspace = models.ForeignKey("accounts.Workspace", on_delete=models.CASCADE)
    contact = models.ForeignKey("crm.Contact", null=True, on_delete=models.SET_NULL)
    subject = models.CharField(max_length=200)

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "workspace",
            "fields": ["id", "subject", "contact"],      # the tenant API
            "platform": {                                 # the console's
                "endpoint": "tickets",
                "fields": ["id", "subject", "workspace", "workspace__name",
                           "contact"],
            },
        }
```

`crm.Contact` needs a platform block of its own for `contact` to be writable
here (see [Relations](#relations-reach-platform-models-only)). If the console
has no contacts endpoint, `"platform": {"enabled": False}` on `Contact` makes
it reachable without a route.

### URLs

```python
# console/urls.py
from rest_framework.permissions import BasePermission
from turbodrf.router import TurboDRFRouter


class IsPlatformStaff(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_platform_staff)


console = TurboDRFRouter(scope="platform", gate=[IsPlatformStaff])
console_schema = console.get_schema_view(title="Console API")

urlpatterns = [
    path("console-api/", include(console.urls)),   # /console-api/jobs/, ...
    path("console-api/swagger/", console_schema.with_ui("swagger", cache_timeout=0)),
]
```

The tenant router (`turbodrf.urls`, or your own `TurboDRFRouter()`) is
unchanged: it serves `Ticket` with its top-level config and doesn't serve
`Job` at all.

### Requests

```bash
# An engineer: every workspace's jobs, payloads included on detail
GET  /console-api/jobs/?status=failed&search=timeout
GET  /console-api/jobs/41/
POST /console-api/jobs/41/retry/

# Support: no payload key in the job detail; can't retry (no jobs.job.create)
GET  /console-api/jobs/41/          # {"id": 41, "kind": "sync", ... no "payload"}
POST /console-api/jobs/41/retry/   # 403

# Support creates a ticket for workspace 7: the workspace is named explicitly
POST /console-api/tickets/  {"subject": "Refund", "workspace": 7}
```

## The router

```python
TurboDRFRouter(scope="platform", gate=[IsPlatformStaff])
```

- `scope`: `"tenant"` (the default; today's behaviour) or `"platform"`.
- `gate`: permission classes (DRF `BasePermission` subclasses, or composed ones
  such as `IsAuthenticated & IsPlatformStaff`), not instances. Required on the
  platform scope: the router raises `ImproperlyConfigured` without one. The
  tenant router accepts an optional gate too.
- `include_root_view`: off by default on the platform scope. If you turn it
  on, the index is gated like the routes.

`scope` and `gate` can also be class attributes, so a subclass works:

```python
class ConsoleRouter(TurboDRFRouter):
    scope = "platform"
    gate = [IsPlatformStaff]
```

Route names start with `platform-` (`platform-job-list`), so `reverse()` on
a tenant route's name still finds the tenant route.

### The gate

The gate's classes run first on every request to the router: every CRUD
route, every action (including one that declares its own
`permission_classes`, which replace the role check but never the gate),
OPTIONS, the browsable API's forms and the router's schema view. A subclass
that overrides `get_permissions()` doesn't remove it: the gate is checked in
`check_permissions()`.

A gate decides who may use the console at all; platform roles decide what
each staff member may do. The role check refuses anonymous callers whatever
the gate admits: the platform scope has no anonymous access (an action that
declares `permission_classes=[AllowAny]` skips the role check, so its gate
decides alone).

## The platform block

A `"platform"` block takes the keys that shape an API: `endpoint`, `fields`,
`searchable_fields`, `lookup_field`, `read_only`, `http_methods`, `actions`,
`compiled`, `enabled`, `full_clean`, `property_relations` and
`unscoped_properties`. Ordering is by the platform `fields`, as on the tenant
scope. Nothing is inherited from the top level: a key left out of the block
takes its default (`fields` defaults to `'__all__'`, so set it).

It can't take a tenancy key (`tenant_field`, `owner_field`,
`bypass_owner_roles`, `visibility`, `tenancy`, `public_access`): those raise
at startup. The platform scope applies no tenant boundary and no predicates,
and has no anonymous access. The model's top-level `tenant_field` still
matters there for writes (below).

- A model is served by a platform router only if it has a platform block.
- Top-level `"enabled": False` (no tenant endpoint) doesn't affect the
  platform block. `"enabled": False` inside the block gives no platform route
  but keeps the model reachable from other platform models.
- `searchable_fields` in a platform block must be in its `fields`. The
  `searchable_fields` class attribute is the tenant API's and doesn't apply.

### Startup checks

Both routers check every platform block at startup, so a broken block fails
whether or not a platform router is mounted. `ImproperlyConfigured` for:

- a block that isn't a dict, an unknown key, or a tenancy key;
- a `fields` entry the model doesn't have, or one ending on a reverse
  one-to-one; a `lookup_field` that isn't a unique column;
- a field, search or `property_relations` path whose relation leads to a
  model the platform scope can't reach (below);
- a `searchable_fields` entry that isn't in the block's `fields`;
- a block that allows creates (not `read_only`, and `post` in
  `http_methods` or no `http_methods`) on a tenant-scoped model whose
  `fields` leave out the first hop of its `tenant_field`: a create couldn't
  name its tenant.

A platform router also requires `TURBODRF_PLATFORM_ROLES` (a non-empty dict),
validates its permission strings as `TURBODRF_ROLES` is validated, imports
`TURBODRF_PLATFORM_ROLE_GETTER`, and runs the compiled-path and search-field
checks on the platform configs.

## Roles and permissions

| | Tenant scope | Platform scope |
|---|---|---|
| Roles | `user.roles` (static), `UserRole` rows (database mode) | `TURBODRF_PLATFORM_ROLE_GETTER` (default `user.platform_roles`) |
| Permissions | `TURBODRF_ROLES`, or `TurboDRFRole` rows | `TURBODRF_PLATFORM_ROLES`, always |
| Anonymous callers | `public_access` + `guest` role | none |
| `TURBODRF_DISABLE_PERMISSIONS`, `TURBODRF_USE_DEFAULT_PERMISSIONS` | apply | don't apply: every platform request is checked |

- **Database permission mode** applies to the tenant scope only. Platform
  roles are always static, from `TURBODRF_PLATFORM_ROLES`; `UserRole` rows never
  grant anything on the platform scope.
- A role name means what its own map says: `support` in `TURBODRF_ROLES` and
  `support` in `TURBODRF_PLATFORM_ROLES` are unrelated.
- Field rules work as on the tenant scope, from the platform map: once any
  platform role has `jobs.job.payload.read`, other platform roles lose
  `payload`. A model's `sensitive_fields` class attribute and
  `TURBODRF_SENSITIVE_FIELDS` apply on both scopes.
- A nested field (`workspace__name`) needs the platform role's read on the
  target model (`accounts.workspace.read`), as on the tenant scope.
- Permission snapshots and their cache keys are per scope
  (`turbodrf_perm:platform:<user>:...`). `invalidate_user_permissions(user)`
  drops both.

## Relations reach platform models only

The platform scope lifts row rules only on models that opted in. A relation
(a nested `__` field, a bare M2M or reverse relation, a filter, a search
path, a `property_relations` hop, a FK or M2M write) may reach:

- a model with a platform block (`"enabled": False` in it is enough);
- the tenant model (`TURBODRF_TENANT_MODEL`);
- a model that isn't a TurboDRF model: it has no row rules on either scope.

A TurboDRF model without a platform block declared rules for its tenants only;
on the platform scope it reads as having no rows. Paths into it are refused at
startup. A bare FK to it (`"contact"`) is allowed: it renders the stored id
without a JOIN, but writing it is a 400 ("not found or not accessible") and
filtering on it matches no rows.

## Writes

On the platform scope nothing is filled in from the staff member: no tenant,
no owner. For a model with a `tenant_field`:

- **A create names its tenant**: the first hop of `tenant_field`
  (`workspace`, or `ledger` for `ledger__firm`) is required, or the create is
  a 400.
- **A row and the rows it points at stay in one tenant.** Each FK and M2M
  value whose own tenant is known must have the row's tenant, or the write is
  a 400 naming the field (`"Invalid contact: it belongs to a different tenant
  than this row."`). A value's tenant is: itself for a row of the tenant
  model, its `tenant_field` for a tenant-scoped model, its
  `TURBODRF_TENANT_USER_FIELD` attribute for a user. A value without one (a
  staff user with no workspace, reference data) is accepted.
- **Moving a row** (an update that changes its tenant) checks the relations it
  keeps, not only the ones the request sets.

Writing to the tenant model itself checks its FK and M2M values against the
row being updated.

## Extending generated viewsets

Generated viewsets carry `turbodrf_scope` (`"tenant"` or `"platform"`) and
`turbodrf_gate`. The pattern for wrapping them works on a platform router
subclass as on a tenant one:

```python
class StaffAudit:
    """Log every platform read of a raw payload."""

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        if self.turbodrf_scope == "platform" and "payload" in response.data:
            StaffAccessLog.objects.create(
                user=request.user, model=self.model._meta.label,
                object_id=response.data["id"],
            )
        return response


class ConsoleRouter(TurboDRFRouter):
    scope = "platform"
    gate = [IsPlatformStaff]

    def discover_models(self):
        super().discover_models()
        self.registry = [
            (prefix, type(view.__name__, (StaffAudit, view), {"__module__": __name__}), basename)
            for prefix, view, basename in self.registry
        ]
```

`turbodrf.scope.current_scope()` returns the scope of the running request,
for code that isn't on the viewset (a permission class, a service).
`get_user_roles(user)` returns the platform roles inside a platform request.

## Schema

`router.get_schema_view(**kwargs)` returns a drf-yasg schema view of that
router's routes only, guarded by its gate and generated with the caller's
platform roles. The tenant schema (`turbodrf.urls`, or
`get_turbodrf_schema_view()`) never documents platform routes, and mounting a
platform router doesn't change it. `?role=<name>` narrows the platform schema
to a platform role the caller holds.

## Commands

```bash
python manage.py turbodrf_check --scope platform
python manage.py turbodrf_explain Job --scope platform --role support
```

`turbodrf_check --scope platform` lists the models with an enabled platform
block, each with its compiled status, fields and tenant path.
`turbodrf_explain --scope platform` explains the platform block's compiled
list; `--role` is a platform role.

## Not covered

- Nested parent routes (`/workspaces/<id>/jobs/`) and list annotations: use
  `?workspace=<id>` filters and `@property` fields.
- A streaming response (`StreamingHttpResponse`) from a custom action is
  consumed after the request's scope ends: build its content inside the
  action.
