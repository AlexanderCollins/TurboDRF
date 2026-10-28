# Security

## Secure by default

Endpoints require authentication unless `public_access: True` is explicitly set.

> **`TURBODRF_DISABLE_PERMISSIONS = True` is for local development only.**
> It removes every permission check, authentication included: anonymous
> callers can create, update and delete through the generated endpoints,
> and read any model that isn't row-scoped. Never set it in a deployed
> environment.

`public_access: True` gives anonymous callers model-level read (GET, HEAD,
OPTIONS) and nothing more. Field rules still bind them: a field that any role
has a read rule for needs an explicit grant, and anonymous callers get grants
only through a role named `guest`. Without one they see, filter, search and
order on only the fields no role has a rule for. See
[permissions.md](permissions.md#anonymous-callers-and-the-guest-role).

## Sensitive fields

Fields like `password`, `token`, and `secret_key` are never exposed in API
responses or available for filtering, regardless of permissions. The check
runs at every `__` segment of a path, so `author__api_key` is denied too.

Default deny-list:

```python
TURBODRF_SENSITIVE_FIELDS = [
    'password', 'password_hash', 'secret_key', 'api_key',
    'token', 'access_token', 'refresh_token', 'session_key',
]
```

Setting `TURBODRF_SENSITIVE_FIELDS` adds your names to this list. It can't
remove the defaults.

The deny-list applies in every permission mode, `TURBODRF_DISABLE_PERMISSIONS`
and `TURBODRF_USE_DEFAULT_PERMISSIONS` included: to list and detail responses
on both read paths (including `fields: "__all__"`), filters, search, ordering,
OPTIONS metadata and the OpenAPI schema.

### Per-model `sensitive_fields`

A model can also list fields that are readable only through an explicit
field grant (`app.model.field.read`), never through model-level read:

```python
class Project(models.Model, TurboDRFMixin):
    sensitive_fields = ['price']   # readable only with 'myapp.project.price.read'
```

Like any field rule it gates responses, filters, search, ordering and
OPTIONS. It restricts reads only: add a `.write` rule to restrict writes. It's
a role rule, so it has no effect when permissions are bypassed.

## No-roles denial

Authenticated users with no assigned roles get 403 on all endpoints. This prevents accidental access when a user account exists but hasn't been configured.

## Filter security

Callers can filter and order only on fields the model's `fields` config
exposes (plus the primary key), and, with role-based permissions, only on
those they can read. A filter on a hidden column would let an attacker infer
its value by binary search (e.g. `?salary__gte=100000`). Filters on other
fields are ignored and logged.

- A nested filter (`?author__name=...`) needs that path in `fields`, is
  permission-checked at every hop, and its JOIN only matches targets the
  caller can see (see [URL-driven JOIN scoping](#url-driven-join-scoping)). A
  related field named like a lookup (`folder__date`, where `date` is a field
  of the folder) is treated as that field, so it's checked and scoped too.
- An M2M filters only by `exact` (`?tags=3`), matched against the targets the
  caller can see. Its other lookups and `_or` are ignored.
- An FK or M2M filter naming a row the caller can't see returns an empty 200,
  the same as a pk that doesn't exist.
- `__regex` / `__iregex` lookups aren't accepted.
- `?ordering=` takes concrete, single-valued columns only, and `<fk>` orders
  by the stored FK column, not by the target's `Meta.ordering`.

## OPTIONS metadata

`OPTIONS` describes only what the caller can use: the fields they can read,
the actions the endpoint routes that they may perform, and their search,
ordering and filter fields. It says nothing about the model's tenancy or
predicates.

## Error responses

All errors follow a consistent format. Enable with:

```python
REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'turbodrf.exceptions.turbodrf_exception_handler',
}
```

```json
{
    "error": {
        "status": 403,
        "code": "permission_denied",
        "message": "You do not have permission to perform this action."
    }
}
```

A 404 never names the model:

```json
{"error": {"status": 404, "code": "not_found", "message": "Not found."}}
```

## Fail-closed design

If a permission check fails due to an error (database issue, malformed data), access is denied. TurboDRF never grants access on exception.

## Row-level access control (predicates)

TurboDRF enforces row-level access through *predicates* declared per model.
The system answers all four standard authorization questions:

| Concern | What it answers | TurboDRF handles it? |
|---------|----------------|---------------------|
| **RBAC** | "Can this user read orders?" | Yes |
| **Field-level** | "Can this user see the `price` field?" | Yes |
| **Row-level scoping** | "Which orders can this user see?" | Yes (predicates) |
| **Write validation** | "Can this user assign this order to that customer?" | Yes (FK-injection defense) |

A typical configuration looks like:

```python
class Order(models.Model, TurboDRFMixin):
    @classmethod
    def turbodrf(cls):
        return {
            'tenant_field': 'store',
            'owner_field': 'customer',
            'bypass_owner_roles': ['staff', 'admin'],
            'fields': ['id', 'store', 'customer', 'total'],
        }
```

When `TURBODRF_TENANT_MODEL` is set in settings and `TURBODRF_REQUIRE_TENANCY=True`
(default), the router refuses to register any model that declares neither a
`tenant_field`, a `visibility` list, nor `'tenancy': 'shared'` — closing
the "developer forgot to scope this" class of bugs at boot. An `owner_field`
alone doesn't count: it scopes rows within a tenant, and its bypass roles
would see every tenant's rows. For the same reason, a `visibility` list
without a `tenant_field` counts only if one of its predicates restricts every
caller (see [Hard-fail at startup](tenancy.md#hard-fail-at-startup)).

See [Tenancy](tenancy.md) for the full predicate vocabulary, sugar form,
and configuration reference.

### What the predicate system enforces

- **List/detail/PATCH/DELETE** — every queryset and `get_object()` is AND'd with
  the user's predicate Q. Detail/PATCH/DELETE return 404 (not 403) on filtered
  rows to avoid existence leaks. A predicate that crosses an M2M or reverse
  relation is applied as a subquery, so it can't duplicate rows.
- **Tenant on writes** — a direct `tenant_field` missing from the request is
  filled from `request.user`. A client-supplied value for a different tenant
  is rejected with a 400. With a chained `tenant_field` (`ledger__firm`),
  a create, or an update that changes the first hop (`ledger`), must point
  it at a row in the caller's tenant.
- **FK injection defense** — every FK and M2M value in the request body must
  resolve to a row visible under the related model's tenant boundary and
  predicates. This runs on every model, shared ones included, and an
  anonymous writer can't reference a tenant-scoped target. On a model scoped
  by a `tenant_field` or predicates, a target that is the tenant model must be
  the caller's tenant, and a target with a `TURBODRF_TENANT_USER_FIELD`
  attribute (typically a user) must be in the caller's tenant. Rejected
  targets return 400 with the same message whether they're hidden or don't
  exist.
- **M2M writes** — a write replaces only the links the caller can see; links
  to rows they can't reference are kept.
- **Tenant reassignment rejection** — PATCH cannot change a row's tenant FK to
  a different tenant.
- **Owner write check** — non-bypass roles cannot set an owner column to
  another user, or change an owner column whose current value is another
  user. With a multi-column `owner_field` this covers every listed column, so
  a co-owner can't take over the other column. The owner is auto-filled on
  create only; an update leaves it as is. An owner reached through a `__`
  path (`Owner('project__owner')`) is checked after the save, in the same
  transaction: a write that would leave the row invisible to the writer (a
  task moved onto someone else's project) is rolled back with a 400.
- **`Either` writes** — a write must pass every child's write check, so the
  `Owner` check above holds for users who see a row through another branch
  (`Members`, `Group`, `Custom`).
- **`Conditional` write check** — the row is re-checked after the save, in the
  same transaction. A write that would leave it invisible to the writer is
  rolled back with a 400.
- **Primary key** — read-only on update.

### Defense-in-depth options

TurboDRF enforces row-level access at the application layer. For Postgres
deployments that need DB-layer enforcement on top of that — specific
compliance findings, ad-hoc operator SQL against production, a database
shared with other applications — Postgres Row Level Security is the
standard mechanism. TurboDRF does not ship tooling to generate or
manage RLS policies: keeping them in sync with the predicate config is
hard to get right, and a stale or misconfigured policy is a worse
failure mode than not having one. Teams that need DB-layer enforcement
should author and version policies in Django `RunSQL` migrations
directly against the Postgres docs, and verify drift with a
custom check in CI.

## Nested fields across scoped relations

A nested field (`author__name`) reads a column of another model. Its value
renders only when every JOIN target along the path is visible to the caller
through that target's own endpoint (its `tenant_field` and predicates).
Otherwise the value is `null`. The parent row stays listed: only the nested
value is withheld. This is the same on lists, detail, the browsable API, and
create/update responses.

For M2M fields, targets the caller can't see are left out of the array, and
a sub-field that crosses a further hop (`tags__folder__name`) is `null` where
that hop's target is out of scope.

Filtering or searching on a nested path can't match an out-of-scope target
either (see [URL-driven JOIN scoping](#url-driven-join-scoping) and
[Search over related fields](#search-over-related-fields)).

Only `tenant_field` and predicates scope these JOINs. A default-manager filter
such as soft delete doesn't: like Django's own FK access, a nested FK value
reads the related row through the base manager. (M2M arrays follow the
target's default manager.)

## Search over related fields

The same JOIN pattern applies to DRF's `SearchFilter` when
`searchable_fields` contains a `__`-traversed path (e.g.
`searchable_fields = ['author__email']`). The search query generates
`WHERE author.email ILIKE ...` joined to the target model. Unscoped, a
search could substring-match rows the caller cannot see via the target's
own endpoint, and `?search=` letter by letter would enumerate hidden values.

TurboDRF's search filter ANDs each `__`-path term with the traversal scope of
its JOIN targets inside the same `filter()` call, so a term only matches
targets the caller can see. Keeping it in one call matters for M2M and
reverse relations: a scope in a separate `filter()` is a separate JOIN, where
one visible sibling would satisfy the scope while the term matched a hidden
row.

## Custom predicate write safety

A `Custom` predicate's write rule is its `write_validator`. Inside
`Either`, a write must pass every child's write check, so a `Custom` can only
add restrictions to the `Owner` rules on another branch; it can't lift them.
(In 0.6.x and earlier, `Either` passed a write when any one child passed,
so a `Custom` that objected to nothing overrode `Owner`.)

A `Custom` without a `write_validator` would say nothing about writes, and
it's easy to forget that a role it admits may later gain write actions. So
`Custom(...)` raises `ImproperlyConfigured` when it's created without one.
Pass one of:

- **`write_validator=lambda d, i, r: []`** if the predicate adds no
  write rule: the users it admits can edit rows they see, subject to the
  other predicates (inside `Either`, `Owner`'s checks still apply).
- **`write_validator=my_check`** to restrict writes. The function
  receives `(validated_data, instance, request)` and returns a list of error
  strings (empty = allow, non-empty = block). Use it for predicates meant
  for read-only role grants:

  ```python
  def block_role_writes(validated_data, instance, request):
      from turbodrf.backends import get_user_roles
      if "legacy_contact" in set(get_user_roles(request.user)):
          return ["legacy_contact cannot write."]
      return []

  Custom(q_func=my_q, write_validator=block_role_writes)
  ```

## Permission string typo gate

`TURBODRF_ROLES` permission strings are unstructured text. A typo at any
segment — wrong app label, wrong model name, wrong field name, wrong
action — silently grants nothing. The role appears configured but the
permission never fires. Bugs of this shape look like "this user
suddenly can't see X" with no error to point at.

### Startup gate

`predicates.validate_permission_strings` walks every entry in
`TURBODRF_ROLES`, parses each as `app.model.action` or
`app.model.field.action`, and verifies:

- Segment count is 3 or 4. There are no wildcards: `'*'` is rejected.
- The `app.model` resolves via Django's app registry and is written
  exactly as the model's lowercase `app_label.model_name`
  (`books.Book.read` is rejected, because permission checks compare
  strings exactly).
- The action is a valid model-level action (`read`, `create`,
  `update`, `delete`) or field-level action (`read`, `write`).
- For 4-segment strings, the field is a concrete or many-to-many field of
  the model, by name. An FK's attname (`author_id`), a reverse relation or
  a property is rejected. Close matches are listed in the error message via
  `difflib`.

### Resolution

Fix the typo. The error message names the offending role, the bad
permission string, and (for field typos) close matches.

For deployments where some models load dynamically (plugin systems,
lazy apps), set `TURBODRF_ALLOW_UNKNOWN_PERMISSIONS = True` to skip
checks for permissions whose `app.model` isn't yet registered.
Permissions on registered models are still validated even with the
kill-switch on, so typos in field names aren't silenced.

## URL-driven JOIN scoping

Filter URL params that traverse `__`-paths (e.g.
`?author__email=foo`, `?bank_account__deal__brokerage=42`) generate
SQL JOINs to the target model. The parent's tenant + predicate scope
already restricts which parent rows return, but the JOIN itself does
not apply the target's own `tenant_field` or registered predicates.

### Runtime JOIN scoping

`turbodrf.filter_backends.ORFilterBackend` wraps every `__`-path
filter with a target-scoping subquery built by
`validation.build_traversal_scope_q()`. For each model along the JOIN
chain that has registered predicates or a `tenant_field`, an AND
clause of the form
`<prefix>__in=<visible target pks>` is added to the queryset, the visible
rows being the target's tenant boundary and predicates as its own endpoint
applies them (`validation.scoped_target_queryset`), so the JOIN can never
return rows outside them.

The subquery uses the target's **base** manager, as Django's own JOINs
and FK descriptors do (the serializer path renders the same related row).
A default-manager filter such as soft delete is not a scoping layer: a row
whose FK points at a soft-deleted target in the caller's scope stays
listed. Hide rows with `tenant_field` or a predicate, not a manager.

For paths through targets with no predicates and no `tenant_field`,
the scope is a no-op `Q()` — no extra subquery cost.

A parent row with an **empty** relation at a scoped step (a NULL FK, or
no rows across a reverse / M2M relation, at that hop or an earlier one)
is kept: each step is `<prefix>__isnull=True OR <prefix>__in=...`
when the relation can be empty. An empty relation joins nothing, so it
can't reveal a target row; a relation pointing at an out-of-scope
target is still excluded. The same scope decides whether a nested FK
value renders (the value is `null` where the scope fails; the row stays),
and is applied inside search over `__`-path `searchable_fields`.

`?ordering=fk__field` is handled separately: `ordering_fields` is a flat
list of concrete, single-valued columns from the configured fields (with
role-based permissions, those the caller can read), and DRF's
`OrderingFilter` silently drops any URL value that isn't in it. The traversal form is therefore unreachable.
`?ordering=<fk>` orders by the stored FK column; ordering by the field
itself would sort by the target's `Meta.ordering` through an unscoped JOIN.
The primary key is appended as a tie-breaker.

## Platform scope

A `TurboDRFRouter(scope="platform", gate=[...])` serves every tenant's rows to
an operator's staff. What keeps it contained (details in
[platform.md](platform.md)):

- **Opt-in per model.** Only models with a `'platform'` block are served, and
  a relation can reach only models with a block, the tenant model, or models
  that aren't TurboDRF models. A path into any other model is refused at
  startup, and its rows read as none at request time.
- **A gate on every route.** The router refuses to build without one; it runs
  before the role check on every route and action, OPTIONS and the schema
  view, and a viewset's `get_permissions()` can't remove it.
- **Separate roles.** Platform roles come from
  `TURBODRF_PLATFORM_ROLE_GETTER` and permissions from
  `TURBODRF_PLATFORM_ROLES`; tenant roles (static or database) grant nothing
  there, and platform roles nothing on the tenant router. Snapshots are cached
  per scope. No anonymous access, and the permission bypass settings don't
  apply.
- **Writes stay in one tenant.** A create names its tenant (nothing is filled
  in from the staff member), and FK / M2M values must belong to the row's
  tenant.

## Known limitations

- **`@property` fields are gated by model-level read.** A property can't
  carry its own field rule (sensitive-named properties are dropped). What it
  can read is limited by the [property sandbox](configuration.md#the-property-sandbox):
  the caller's readable columns, the relations declared in
  `property_relations` (each hop scoped), and no queries. A property listed
  in `unscoped_properties` runs outside the sandbox and returns whatever it
  reads, whatever the caller's tenant, predicates or field rules.
- **Default managers aren't a security layer.** A manager that hides rows
  (soft delete) hides them from the model's own endpoint, not from nested
  FK values or filters on other models. Scope with `tenant_field` or a
  predicate.
- **`Members` and `Group` don't check creates.** They scope reads, and so
  updates and deletes, but a create (or a `Group` update that moves the row
  to another group) isn't membership-checked. Use `Owner`, or `Custom` with
  a `write_validator`, when writes must be restricted.
- **Custom code that queries the model directly bypasses scoping.** Custom
  actions are scoped only through `self.get_object()` /
  `self.get_queryset()`. (Generated viewsets have no `queryset` attribute:
  `self.queryset` is `None`.)
- **Bulk permission edits stay cached.** A bulk `RolePermission` QuerySet
  `update()` / `delete()` doesn't invalidate cached permission snapshots;
  call `turbodrf.backends.invalidate_user_permissions()` (see
  [permissions.md](permissions.md#caching)).
- **Bypassed permission modes** (`TURBODRF_DISABLE_PERMISSIONS`,
  `TURBODRF_USE_DEFAULT_PERMISSIONS`) have no field rules, so per-model
  `sensitive_fields` don't apply.
- **Keycloak roles are captured at login.** See
  [integrations.md](integrations.md#where-the-roles-come-from).
- **Most startup checks run when the URLconf is imported.** Tenancy errors
  raise when Django starts (`AppConfig.ready()`), but the router's other
  checks run only when the URLconf is first imported. A process that doesn't
  import it (`get_wsgi_application()` before the first request) starts with
  a broken config and fails on the first request. Run
  `python manage.py check`, which imports the URLconf, in CI and before each
  deploy.
