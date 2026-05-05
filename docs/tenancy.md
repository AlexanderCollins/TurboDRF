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
| Which orders can this user see? | **Predicates (new)** |
| Can this user assign this order to that customer? | **Predicates (new)** |

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
from turbodrf.predicates import Tenant, Owner, Either, Custom

return {
    'visibility': [
        Tenant('workspace'),
        Either(Owner('author'), Owner('reviewer')),
    ],
}
```

You can't mix sugar + `visibility` on the same model.

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
