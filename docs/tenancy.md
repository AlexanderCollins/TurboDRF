# Row-Level Access Control (Tenancy & Predicates)

TurboDRF enforces row-level access control through **predicates** declared on
each model. Every list / detail / PATCH / DELETE is filtered by the user's
predicate Q. Writes are validated against the predicate stack: tenant
reassignment is rejected and FK targets must resolve to rows the user can see.

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
tenant?** There are exactly three answers:

| Answer | Config |
|---|---|
| It has a direct FK to the tenant model | `"tenant_field": "workspace"` |
| It chains through another model to the tenant | `"tenant_field": "project__workspace"` |
| It's reference data, not tenant-scoped | `"tenancy": "shared"` |

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
explicitly blocked."

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

### Step 6: Always write `write_validator` if your model is writable

If your `Custom` predicate is on a writable model, **the framework
will refuse to start without an explicit `write_validator`**. This is
intentional — `Custom`'s default `validate_write` returns no errors,
which inside `Either(Owner, Custom)` silently lets writes bypass
`Owner`'s checks.

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
# Or explicitly opt in to "writes pass through this predicate":
Custom(q_func=read_only_q, write_validator=lambda d, i, r: [])
```

Which to use:

- If the role the predicate matches is *read-only by intent*: use the
  blocking validator. That way, even if the role grants change later
  to include write actions, the predicate still blocks writes.
- If the role *should* be able to write and `Owner` will catch the
  ownership check: use the explicit no-op `lambda d, i, r: []`.

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

The router runs five startup passes. If your config has a problem,
you'll get a directed error before you can serve a request:

1. Tenancy validation (every model has a tenancy decision).
2. Compiled-path safety (M2M and FK joins to predicate-bearing targets).
3. Searchable-fields safety (`__`-paths through predicate-bearing
   targets).
4. Predicate write safety (`Custom` without `write_validator`).
5. Permission-string validation (`TURBODRF_ROLES` typos).

If the app boots, the static checks have passed. Run the sanity-check
project at `~/github/turbodrf-sanity-check/` to verify the runtime
behaviour matches what you expect.

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

The framework auto-fills `tenant_field` on create, rejects writes that try
to set it to a different tenant, and AND's it onto every queryset. There's
no way to compose this away.

**Layer 2 — discretionary within-tenant predicates:**

Stack predicates with AND. Use `Either(...)` for OR. These operate ONLY
within-tenant — even if they all evaluate to "no restriction" for a bypass
user, the tenant boundary still holds.

### `Owner(field, bypass=[...])` — within-tenant ownership

Filters to rows where `<field> = request.user`, unless caller has a bypass
role. Auto-fills owner FK on create. Rejects non-bypass users from assigning
rows to other users.

```python
Owner('assigned_to')
Owner('assigned_to', bypass=['admin'])
Owner(['author', 'editor'])     # multi-owner: any-match OR
```

### `Either(*predicates)` — OR composition

```python
Either(Owner('owner'), Owner('reviewer'))
```

⚠ `Tenant(...)` cannot appear inside `Either` — the framework rejects this
configuration at startup. Use the `tenant_field` setting at the top level.

### `Custom(q_func, ...)` — escape hatch

```python
Custom(q_func=lambda req, roles: Q(client__assigned_manager=req.user))
```

A `Custom` predicate that returns `Q()` unconditionally just means "no
within-tenant restriction" — the tenant boundary still applies separately,
so this is safe. Set `TURBODRF_LOG_UNRESTRICTED_CUSTOM=True` to log a
warning if you want visibility into accidental Q() returns.

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

Power form (when you need `Either` or `Custom`):

```python
from turbodrf.predicates import Owner, Either, Custom

return {
    'tenant_field': 'workspace',          # mandatory boundary (setting)
    'visibility': [                       # within-tenant predicates
        Either(Owner('author'), Owner('reviewer')),
    ],
}
```

`tenant_field` is a setting, not a predicate, and is allowed alongside
`visibility`. `owner_field` / `bypass_owner_roles` are sugar that compiles
to predicates — those *do* conflict with `visibility` (pick one or the
other). Tenant inside `visibility` is supported but deprecated; use the
`tenant_field` setting instead.

---

## Hard-fail-at-startup

When `TURBODRF_TENANT_MODEL` is set and `TURBODRF_REQUIRE_TENANCY=True`
(default), every model must declare *one of*:

- `tenant_field=...`
- `visibility=[...]`
- `'tenancy': 'shared'`

Models without a tenancy decision raise `ImproperlyConfigured` at boot. This
closes the "developer forgot to scope this model" failure mode.

Run `python manage.py turbodrf_check` to see what each model resolves to.

Other hard-fails the router catches:

- Invalid `tenant_field`/`owner_field` paths (with did-you-mean suggestions)
- `bypass_owner_roles` listing roles not in `TURBODRF_ROLES` (typo guard)

To opt out globally during a migration:

```python
TURBODRF_REQUIRE_TENANCY = False
```

---

## Behavior

**Detail / PATCH / DELETE return 404 (not 403)** when a row is filtered out.
This avoids leaking whether the row exists.

**FK injection on writes is rejected.** When the request body contains a
foreign key, the FK target must be visible under the related model's
predicate stack.

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
manually if you do this.

---

## Settings

```python
TURBODRF_TENANT_MODEL = 'accounts.Workspace'   # required for row scoping
TURBODRF_TENANT_USER_FIELD = 'workspace'       # required

TURBODRF_REQUIRE_TENANCY = True                # default — hard-fail
TURBODRF_AUTODETECT_TENANT = False             # default — be explicit
```

When `TURBODRF_AUTODETECT_TENANT=True`, the router walks each model's FK
graph at startup to find the shortest unique path to the tenant model.
Ambiguous paths raise loudly. Off by default — explicit declarations are
easier to reason about.

---

## Advanced predicates

For app shapes that don't fit `Tenant + Owner + Either + Custom`, the
`turbodrf.predicates` module also exports `Members` (M2M-to-User collections),
`Group` (FK to a team that has M2M members), and `Conditional` (rows matching
a Q clause are restricted to specified roles). These are usable in
`'visibility': [...]` lists. Read the source for details.

---

## See also

- `docs/security.md` — threat model and security guarantees
- `docs/rls.md` — optional Postgres RLS as defense-in-depth
- `docs/migration_to_predicates.md` — migrating an existing project
- `SECURITY_AUDIT.md` — audit history
