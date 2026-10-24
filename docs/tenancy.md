# Row-Level Access Control (Tenancy & Predicates)

TurboDRF enforces row-level access control through **predicates** declared on
each model. Every list / detail / PATCH / DELETE is filtered by the user's
predicate Q. Writes are validated against the predicate stack: tenant
reassignment is rejected and FK and M2M targets must resolve to rows the user
can see.

The system answers four questions per request:

| Concern | Handled by |
|---|---|
| Can this user read orders? | RBAC (existing) |
| Can this user see the `price` field? | Field perms (existing) |
| Which orders can this user see? | **Predicates** |
| Can this user assign this order to that customer? | **Predicates** |

---

## Designing predicates: a walkthrough

If you've never written a predicate before, work through this section in
order. By the end you'll know which shape to reach for and why.

### Step 1: Identify your tenant

Your **tenant** is whatever logical boundary separates your customers from
each other. Workspace, organisation, account, household — pick the term
that fits your domain. Every row belongs to exactly one tenant.

If you don't have a tenant model yet, create one:

```python
class Workspace(models.Model):
    name = models.CharField(max_length=200)
```

In `settings.py`:

```python
TURBODRF_TENANT_MODEL = "accounts.Workspace"
TURBODRF_TENANT_USER_FIELD = "workspace"  # request.user.workspace → tenant
TURBODRF_REQUIRE_TENANCY = True           # refuse to boot without tenancy
```

The third setting is the safety net — with it, the framework refuses to
register any model that doesn't say how it relates to the tenant.

### Step 2: Decide each model's relationship to the tenant

For every TurboDRFMixin model, ask: **how does this row belong to a
tenant?**

| Answer | Config |
|---|---|
| It has a direct FK to the tenant model | `"tenant_field": "workspace"` |
| It chains through another model to the tenant | `"tenant_field": "project__workspace"` |
| It's reference data, not tenant-scoped | `"tenancy": "shared"` |
| It *is* the tenant model, or the user model | See [Exposing the tenant model and the user model](#exposing-the-tenant-model-and-the-user-model) |

If none of these apply, you almost certainly have a design issue, not a
TurboDRF question. Sort the data model first, then come back.

### Step 3: Decide the within-tenant rule

You're now inside a single tenant. Question: **within this tenant, who
sees which rows?** Pick the simplest answer that fits.

```
Does each row belong to one user (and that user owns it)?
├── Yes  → use sugar form: owner_field
└── No
    ├── Multiple users have access (members, collaborators)?
    │   ├── Yes  → use power form: Either(Owner, Custom)
    │   └── No   → continue
    └── Anyone in the tenant can see all rows?
        └── Yes  → leave it tenant-only (no within-tenant rule)
```

Most models land at "single-owner with admin bypass" — that's the sugar
form. A few will need `Either(Owner, Custom)` for cross-user grants
(e.g. a document an owner shares with a designated read-only contact).
A handful might be tenant-shared (all members of a workspace see all
projects).

### Step 4: Write the simplest config that fits

**Single owner with admin bypass (90% of models):**

```python
@classmethod
def turbodrf(cls):
    return {
        "tenant_field": "workspace",
        "owner_field": "owner",
        "bypass_owner_roles": ["admin"],
    }
```

Translated: "rows belong to a workspace via the `workspace` FK; within a
workspace, only the user listed on `owner` sees the row, except admins
who see all rows in their workspace."

**Tenant-only (no within-tenant rule):**

```python
@classmethod
def turbodrf(cls):
    return {
        "tenant_field": "workspace",
    }
```

Translated: "rows belong to a workspace; any workspace member can see
any row." Useful for shared resources within an organisation.

**Reference data:**

```python
@classmethod
def turbodrf(cls):
    return {
        "tenancy": "shared",
    }
```

Translated: "this isn't tenant-scoped at all — Categories, Tags, Status
codes, currency lists, etc."

**Cross-user grant (the `Either` case):**

```python
@classmethod
def turbodrf(cls):
    return {
        "tenant_field": "workspace",
        "visibility": [
            Either(
                Owner("owner", bypass=["admin"]),
                Custom(
                    q_func=collaborator_q,
                    write_validator=block_collaborator_writes,
                ),
            ),
        ],
    }
```

Translated: "owners or admins see the row, AND users matched by
`collaborator_q` also see the row, AND collaborator writes are
explicitly blocked." A write has to pass every child's write check, so
`Owner`'s rules (nobody without a bypass role reassigns the owner) hold for
every writer.

### Step 5: Write your `Custom` predicate's `q_func`

A `q_func` is a regular Python function that returns a Django `Q`
object. The framework calls it with `(request, user_roles)` and AND's
the result into the queryset.

The shape that always works:

```python
from django.db.models import Q

def collaborator_q(request, user_roles):
    if "collaborator" not in user_roles:
        return Q(pk__in=[])  # no match — this user isn't a collaborator
    return Q(collaborators=request.user)
```

The early-return-empty pattern is important. If your function returns
`Q()` (no filter) for users who shouldn't see anything, you've silently
disabled the within-tenant rule. The framework will warn you about
this at runtime — but you should code it to return `Q(pk__in=[])`
whenever the user doesn't qualify.

### Step 6: Always write a `write_validator`

**The framework refuses to start if a `Custom` predicate has no
`write_validator`**, so that every `Custom` states its write rule on
purpose. The validator receives `(validated_data, instance, request)`
(`instance` is `None` on create) and returns a list of error strings: empty
allows the write, anything else rejects it with a 400. Inside `Either`, a
write must pass every child's check, so a `write_validator` can only add
restrictions, never lift `Owner`'s.

Two valid shapes:

```python
# Block writes for the role this predicate is meant for:
def block_collaborator_writes(validated_data, instance, request):
    from turbodrf.backends import get_user_roles
    if "collaborator" in set(get_user_roles(request.user)):
        return ["collaborators cannot write."]
    return []

Custom(q_func=collaborator_q, write_validator=block_collaborator_writes)
```

```python
# Or explicitly state "this predicate adds no write rule":
Custom(q_func=collaborator_q, write_validator=lambda d, i, r: [])
```

Which to use:

- If the users this predicate admits are *read-only by intent*: use the
  blocking validator. That way, even if their role later gains write
  actions, the predicate still blocks their writes.
- If they *should* be able to edit the rows they can see: use the no-op
  `lambda d, i, r: []`. `Owner`'s checks still stop them reassigning the
  owner.

### Step 7: Check the role grants

Predicates only matter once a user has the model-level permission to do
the action at all. In `TURBODRF_ROLES`, grant your roles the actions
they need:

```python
TURBODRF_ROLES = {
    "member": [
        "myapp.project.read",
        "myapp.project.create",
        "myapp.project.update",
        "myapp.project.delete",
        "myapp.project.title.read",
        "myapp.project.title.write",
    ],
    "collaborator": [
        "myapp.project.read",  # read-only — predicate enforces ownership
        "myapp.project.title.read",
    ],
}
```

The framework validates these at startup — typos like
`myapp.project.titel.read` raise a clear error pointing at the role
and listing close matches.

### Step 8: Boot the app and trust the gates

TurboDRF checks the configuration at startup and raises a directed
`ImproperlyConfigured` error on a problem: tenancy declarations, unknown
`turbodrf()` keys, compiled-path and search-field joins into
predicate-bearing targets, `Custom` without `write_validator`, permission
string typos, and more (see [Hard-fail-at-startup](#hard-fail-at-startup)).
Tenancy errors raise when Django starts; the rest when Django first imports
your URLconf. Run `python manage.py check` in CI and before each deploy: it
imports the URLconf, so it runs them all. (`get_wsgi_application()` doesn't,
so without it a broken config deploys and fails on the first request.)

If the app boots, the static checks have passed. Exercise the runtime
behaviour against your own test suite to confirm it matches what you expect.

---

## Quickstart

```python
# settings.py
TURBODRF_TENANT_MODEL = 'accounts.Workspace'
TURBODRF_TENANT_USER_FIELD = 'workspace'   # request.user.workspace → tenant

# models.py
class Project(models.Model, TurboDRFMixin):
    workspace = models.ForeignKey(Workspace, ...)
    owner = models.ForeignKey(User, ...)
    # ...

    @classmethod
    def turbodrf(cls):
        return {
            'tenant_field': 'workspace',
            'owner_field': 'owner',
            'bypass_owner_roles': ['admin', 'manager'],
        }
```

That's the whole configuration for the example above. Members see
only their own projects; managers and admins see all projects in the tenant; nobody
crosses tenants; FK injection is rejected; the tenant FK is auto-filled on
create.

For reference data (currencies, country codes — not tenant-scoped):

```python
return {'tenancy': 'shared', ...}
```

---

## Two-layer model

Tenant is a **setting**, not a predicate. It's applied as a separate AND
outside the predicate algebra and is never bypassable. Keeping it outside
the algebra prevents cross-tenant escape via OR-composition — e.g.
`Either(Owner_with_bypass, Tenant)` would collapse to "no filter" for
bypass-role users if Tenant were a peer predicate.

**Layer 1 — mandatory tenant boundary (setting):**

```python
'tenant_field': 'workspace'         # direct column
'tenant_field': 'project__workspace'   # chained — two-hop traversal
```

Every hop must be a forward `ForeignKey` or `OneToOneField`, named by the
field (`workspace`, not `workspace_id`), and the last must point at
`TURBODRF_TENANT_MODEL` (when it's set). A hop through an M2M or reverse
relation is refused at startup.

The framework auto-fills a direct `tenant_field` on create, rejects writes
that try to set it to a different tenant (400), and AND's it onto every
queryset. A chained `tenant_field` isn't auto-filled; instead a create, or an
update that changes the first hop (`project` above), must point that hop at a
row in the caller's tenant. There's no way to compose this away.

**Layer 2 — discretionary within-tenant predicates:**

Stack predicates with AND. Use `Either(...)` for OR. These operate ONLY
within-tenant — even if they all evaluate to "no restriction" for a bypass
user, the tenant boundary still holds.

### `Owner(field, bypass=[...])` — within-tenant ownership

Filters to rows where `<field> = request.user`, unless caller has a bypass
role. On create, a single owner column left out of the request is filled with
the caller; updates never reassign it.

Writes by a caller without a bypass role are checked: they can't set any
owner column to another user, and they can't change an owner column whose
current value is another user. So a co-owner can't take over the other
owner's column, and a collaborator who can see a row can't make it theirs.

```python
Owner('assigned_to')
Owner('assigned_to', bypass=['admin'])
Owner(['author', 'reviewer'])   # multi-owner: any-match OR
```

A multi-column `Owner` (or a list `owner_field`) reads rows where ANY column
matches, and the write checks above cover every column. It isn't auto-filled.
For several owner columns use one `Owner([...])`, not
`Either(Owner(...), Owner(...))`.

### `Either(*predicates)` — OR composition

```python
Either(Owner('owner'), Members('shared_with'))
```

- **Reads** match rows any child matches. A child that places no restriction
  on the caller (an `Owner` whose bypass role they hold, say) makes the whole
  `Either` unrestricted.
- **Writes** must pass every child's write check. `Owner`'s checks apply to
  every writer, including users who see the row through another child; a
  `Custom` adds its `write_validator`; `Members`, `Group` and `Conditional`
  add no write rule (`Conditional` still re-checks the saved row, see
  [Advanced predicates](#advanced-predicates)).
- **Create** fills a single-column `Owner` child's column with the caller when
  the request leaves it out, as a top-level `Owner` does.

In the example, a user in `shared_with` can edit the row's other fields but
can't change `owner`.

⚠ `Tenant(...)` cannot appear inside `Either` — the framework rejects this
configuration at startup. Use the `tenant_field` setting at the top level.

### `Custom(q_func, write_validator=None, auto_filler=None)` — escape hatch

```python
Custom(
    q_func=lambda req, roles: Q(client__assigned_manager=req.user),
    write_validator=lambda data, instance, req: [],
)
```

`q_func(request, user_roles)` returns a `Q` (see
[Step 5](#step-5-write-your-custom-predicates-q_func)); `write_validator` is
required (see [Step 6](#step-6-always-write-a-write_validator));
`auto_filler(validated_data, request)` optionally returns the data to create
with.

A `Custom` predicate that returns `Q()` unconditionally just means "no
within-tenant restriction" — the tenant boundary still applies separately,
so this is safe. TurboDRF logs a warning each time a `Custom` returns an
empty `Q`, to surface accidental `Q()` returns; set
`TURBODRF_LOG_UNRESTRICTED_CUSTOM = False` (default `True`) to silence it.

---

## Sugar form vs predicate list

Sugar (use 90%+ of the time):

```python
return {
    'tenant_field': 'workspace',
    'owner_field': 'assigned_to',
    'bypass_owner_roles': ['admin', 'manager'],
}
```

Several owner columns are still sugar: `'owner_field': ['author', 'reviewer']`
compiles to one `Owner(['author', 'reviewer'])`.

Power form (when you need `Either`, `Members`, `Group`, `Conditional` or
`Custom`):

```python
from turbodrf.predicates import Either, Members, Owner

return {
    'tenant_field': 'workspace',          # mandatory boundary (setting)
    'visibility': [                       # within-tenant predicates
        Either(Owner('author', bypass=['admin']), Members('collaborators')),
    ],
}
```

`tenant_field` is a setting, not a predicate, and is allowed alongside
`visibility`. `owner_field` / `bypass_owner_roles` are sugar that compiles
to predicates — those *do* conflict with `visibility` (pick one or the
other). Tenant inside `visibility` is supported but deprecated; use the
`tenant_field` setting instead.

A misspelt key such as `'owner_feild'` raises `ImproperlyConfigured` at
startup. After changing a model's scoping, run
`python manage.py turbodrf_check`: with `TURBODRF_TENANT_MODEL` set it
prints each model's tenant field and within-tenant predicates.

---


## Hard-fail-at-startup

When `TURBODRF_TENANT_MODEL` is set and `TURBODRF_REQUIRE_TENANCY=True`
(default), every model must declare *one of*:

- `tenant_field=...`
- `'tenancy': 'shared'`
- a `visibility=[...]` list with at least one predicate that restricts every
  caller: an `Owner` without bypass roles, `Members`, `Group` or `Custom` (an
  `Either` counts only if all its children do)

(or have its tenant path auto-detected; see [Settings](#settings)).
`owner_field` alone doesn't count: it scopes rows within a tenant, and its
bypass roles would see every tenant's rows. For the same reason, a
`visibility` list whose predicates can all lift the restriction (an `Owner`
with bypass roles, a `Conditional`) doesn't count without a `tenant_field`.
Models without a tenancy decision raise `ImproperlyConfigured` at boot. This
closes the "developer forgot to scope this model" failure mode.

A model with `'enabled': False` gets no endpoint, but its tenancy is still
registered: other models' FK checks and nested fields that reach it use its
`tenant_field` and predicates. Declare it like any other model's.

Run `python manage.py turbodrf_check` to see what each model resolves to.

Other hard-fails:

- A `tenant_field` that breaks the hop rules in
  [Two-layer model](#two-layer-model), or any `tenant_field` while
  `TURBODRF_TENANT_USER_FIELD` is unset
- An `Owner` / `owner_field` column that isn't a foreign key or one-to-one
  to the user model (with did-you-mean suggestions for typos)
- A `Group(user_via=...)` that doesn't lead to users
- `bypass_owner_roles` listing roles not in `TURBODRF_ROLES` (typo guard)
- A `Custom` predicate without a `write_validator`
- An unknown `turbodrf()` key, a `fields` entry the model doesn't have (or
  that ends on a reverse one-to-one), a `lookup_field` that isn't a unique
  column, and two models routed to the same `endpoint`

These raise whatever the tenancy settings:

- A `tenancy` value other than `'shared'`
- `'tenancy': 'shared'` combined with `tenant_field`, `owner_field` or
  `visibility`
- An empty `visibility` without a `tenant_field`

To opt out of the tenancy gate globally during a migration:

```python
TURBODRF_REQUIRE_TENANCY = False
```

---

## Behavior

**Detail / PATCH / DELETE return 404 (not 403)** when a row is filtered out.
This avoids leaking whether the row exists.

**FK injection on writes is rejected.** When the request body contains a
foreign key or many-to-many values, each target must be visible under the
related model's tenant boundary and predicate stack, as through its own
endpoint. This runs on every model, shared ones included, and an anonymous
writer can't reference a tenant-scoped target. On a model scoped by a
`tenant_field` or predicates, a target that is the tenant model must also be
the caller's tenant, and a target with a `TURBODRF_TENANT_USER_FIELD`
attribute (typically a user) must be in the caller's tenant. The message is
the same whether the target is hidden or doesn't exist.

```
POST /api/comments/ {"document": <foreign>, "amount": 100}
→ 400 {"document": ["Invalid document: not found or not accessible."]}
```

**Tenant reassignment is rejected.** PATCH cannot move a row to another
tenant.

**Multi-role users are OR'd.** A user with both `member` and `manager`
gets the union — more roles never restrict access.

**`@action` routes inherit scoping** if they call `self.get_object()` or
`self.get_queryset()`. Custom actions that go around the standard flow (e.g.
`Model.objects.get(...)` directly) bypass scoping — apply predicates
manually if you do this. Generated viewsets have no `queryset` attribute
(`self.queryset` is `None`). `self.get_serializer()` in an action returns the
same permission-filtered serializer the CRUD routes use.

---

## Exposing the tenant model and the user model

The tenant model and the user model don't reach the tenant through a
foreign key of their own, so they need their own rule. A `tenant_field` must
be a foreign key path, so `'tenant_field': 'id'` on the tenant model is
refused at startup.

**The tenant model.** Scope it to the caller's own tenant with a `Custom`
predicate:

```python
from django.db import models
from django.db.models import Q

from turbodrf.mixins import TurboDRFMixin
from turbodrf.predicates import Custom


def own_workspace(request, user_roles):
    workspace_id = getattr(request.user, 'workspace_id', None)
    if workspace_id is None:
        return Q(pk__in=[])          # no tenant: no rows
    return Q(pk=workspace_id)


class Workspace(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=200)

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['id', 'name'],
            'visibility': [
                Custom(own_workspace, write_validator=lambda d, i, r: []),
            ],
            'http_methods': ['get', 'patch'],   # no create or delete
        }
```

A caller lists and reads only their own workspace (another workspace's id is
a 404), and a role granted `accounts.workspace.update` can rename it.
Creating and deleting tenants usually belongs to a signup or admin flow, not
the API; use `'read_only': True` to drop the PATCH as well.

With a predicate on the tenant model, the startup gate refuses compiled
models that nest its fields (`'workspace__name'`). Read those from
`/api/workspaces/{id}/`, or set `'compiled': False` on the nesting model.

**The user model.** The mixin goes on the model class, so this needs a
custom user model (`AUTH_USER_MODEL`):

```python
from django.contrib.auth.models import AbstractUser


class User(AbstractUser, TurboDRFMixin):
    workspace = models.ForeignKey(Workspace, null=True, on_delete=models.PROTECT)

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['id', 'username', 'first_name', 'last_name'],
            'tenant_field': 'workspace',
            'read_only': True,
        }
```

List `fields` explicitly: `'__all__'` would expose `email`, `is_staff`,
`is_superuser` and more (the sensitive deny-list removes only `password`).
Keep it `read_only` and create users through your own signup flow.

---

## Settings

```python
TURBODRF_TENANT_MODEL = 'accounts.Workspace'   # turns on the startup checks
TURBODRF_TENANT_USER_FIELD = 'workspace'       # needed by every tenant_field

TURBODRF_REQUIRE_TENANCY = True                # default — hard-fail
TURBODRF_AUTODETECT_TENANT = False             # default — be explicit
```

`tenant_field`, `owner_field` and `visibility` are enforced whether or not
`TURBODRF_TENANT_MODEL` is set. Setting it turns on the tenancy gate, the
check that each `tenant_field` ends at the tenant model, and auto-detection.

`TURBODRF_TENANT_USER_FIELD` names the attribute of `request.user` that
holds the user's tenant: a model instance or an integer pk. A direct
foreign key on the user is the usual choice. Any other value counts as no
tenant, and the caller sees no tenant-scoped rows.

When `TURBODRF_AUTODETECT_TENANT=True`, the router walks each model's FK
graph at startup to find the shortest unique path to the tenant model.
Ambiguous paths raise loudly. Off by default — explicit declarations are
easier to reason about.

---

## Advanced predicates

`turbodrf.predicates` also exports these, for use in `'visibility': [...]`
lists (alone or inside `Either`):

**`Members(m2m_field)`** — the caller must be in the row's many-to-many to
users. `Members('participants')` shows a channel to its participants.

**`Group(field, user_via='members')`** — the caller must belong to the
group or team the row points at. `field` is the row's foreign key to that
model; `user_via` is the lookup from that model to users, as you'd write it
in `Team.objects.filter(<user_via>=user)`. For a `Team` with
`members = models.ManyToManyField(User)` that's the default, `'members'`.
For Django's `auth.Group` it's `'user'` (`Group('team', user_via='user')`).
The router checks `user_via` at startup.

**`Conditional(when, require_roles)`** — rows matching the `when` Q are
visible only to callers holding one of `require_roles`; other rows are
unaffected. `Conditional(when=Q(kind='staff_loan'), require_roles=['staff'])`.

- A predicate that crosses an M2M or reverse relation (`Members`, or an
  `Either` that includes one) is applied as a subquery, so it doesn't
  duplicate list rows or break detail lookups.
- `Members` and `Group` scope reads, and so updates and deletes (only
  visible rows can be fetched). They add no write rule: a create isn't
  membership-checked, nor is an update that moves a row to another group.
  Use `Owner`, or `Custom` with a `write_validator`, when writes must be
  restricted.
- `Conditional` writes are checked after the save, in the same transaction:
  a create or update that would leave the row invisible to the writer is
  rolled back with a 400. Inside `Either`, the saved row must be visible
  through the `Either` as a whole.

---

## See also

- [Security](security.md) — threat model, sensitive fields, error responses
- [Migrating to predicates](migration_to_predicates.md) — migrating an
  existing project
