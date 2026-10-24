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

The deny-list applies in every permission mode to list and detail responses
on both read paths (including `fields: "__all__"`), filters, OPTIONS metadata
and the OpenAPI schema. With role-based permissions it also applies to search
and ordering. With `TURBODRF_DISABLE_PERMISSIONS` or
`TURBODRF_USE_DEFAULT_PERMISSIONS`, search uses `searchable_fields` as written
and ordering accepts any concrete model field, deny-listed ones included.

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

With role-based permissions, callers can filter and order only on fields the
model's `fields` config exposes (plus the primary key) that they can read. A
filter on a hidden column would let an attacker infer its value by binary
search (e.g. `?salary__gte=100000`). Filters on other fields are ignored and
logged.

- Nested filters (`?author__name=...`) are permission-checked at every hop,
  and their JOIN only matches targets the caller can see (see
  [URL-driven JOIN scoping](#url-driven-join-scoping)). A related field named
  like a lookup (`folder__date`, where `date` is a field of the folder) is
  treated as that field, so it's checked and scoped too.
- An FK or M2M filter naming a row the caller can't see returns an empty 200,
  the same as a pk that doesn't exist.
- `__regex` / `__iregex` lookups aren't accepted.
- `?ordering=<fk>` orders by the stored FK column, not by the target's
  `Meta.ordering`.

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
`tenant_field`, a non-empty `visibility`, nor `'tenancy': 'shared'` — closing
the "developer forgot to scope this" class of bugs at boot. An `owner_field`
alone doesn't count: it scopes rows within a tenant, and its bypass roles
would see every tenant's rows. Nor does a `visibility` list made only of
`Owner` predicates with bypass roles and `Conditional` predicates.

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
  anonymous writer can't reference a tenant-scoped target. Rejected targets
  return 400 with the same message whether they're hidden or don't exist.
- **Tenant reassignment rejection** — PATCH cannot change a row's tenant FK to
  a different tenant.
- **Owner write check** — non-bypass roles cannot set an owner column to
  another user, or change an owner column whose current value is another
  user. With a multi-column `owner_field` this covers every listed column, so
  a co-owner can't take over the other column. The owner is auto-filled on
  create only; an update leaves it as is.
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
value is withheld. This is the same on detail, uncompiled and compiled lists,
the browsable API, and create/update responses.

For M2M fields, targets the caller can't see are left out of the array, and
a sub-field that crosses a further hop (`tags__folder__name`) is `null` where
that hop's target is out of scope.

Filtering or searching on a nested path can't match an out-of-scope target
either (see [URL-driven JOIN scoping](#url-driven-join-scoping) and
[Search field target bypass](#search-field-target-bypass)).

Only `tenant_field` and predicates scope these JOINs. A default-manager filter
such as soft delete doesn't: like Django's own FK access, a nested FK value
reads the related row through the base manager. (M2M arrays follow the
target's default manager.)

## Compiled M2M target bypass

The compiled read path (default for all models) renders nested many-to-many
arrays via a separate two-query merge: one query for the parent rows, a
second on the M2M through-table joining to the **target** model. If that
join ran unscoped, a model nesting an M2M whose target carries its own
visibility rules (predicates or tenant_field) would render rows the caller
could not see by hitting the target's own endpoint. Two layers prevent it:

1. **Request-time scoping** — the merge's second query is filtered to
   targets visible via the target's own endpoint
   (`scoped_target_queryset` in `views._compiled_list`), and a sub-field
   that crosses a further hop renders `null` where that hop is out of scope.
   This matches the serializer path (`serializers.py`) and holds even when
   the startup gate below is bypassed.
2. **The startup gate** — unsafe parent/target pairings are refused at
   boot, so misconfigurations surface at deploy time.

### Startup gate

The router refuses to boot if any compiled model nests an M2M into a
target that has registered predicates, or into a tenanted target while the
parent itself is shared. Every further hop a sub-field crosses
(`tags__folder__name`) is checked the same way. Detection is fully static —
done at router init in `compiler.validate_compiled_path_safety()` after
every model's predicates are registered. The error message names the
specific parent/target pair and lists the available fixes.

Safe targets (the gate allows them): pure reference / lookup tables with
no predicates and no tenant_field — `Tag`, `Category`, `Status` and the
like. The gate fires only when the target itself has row-level rules.

### Fixes

If the gate fires on your model, choose one:

- **Drop the M2M nesting** from the parent's `turbodrf()` `fields` list.
  The M2M can still be reached via the target's own endpoint, where its
  predicates apply.
- **Set `'compiled': False`** on the parent model's `turbodrf()` config.
  The serializer path applies the same request-time scoping, and the gate
  only checks compiled models.
- **Strip the target's row-level rules** if and only if it is genuinely
  public reference data with no per-row visibility — same intent as
  `'tenancy': 'shared'`.
- **`TURBODRF_ALLOW_UNSAFE_COMPILED_M2M = True`** bypasses the gate
  entirely. Logs a loud warning per offending model. Request-time scoping
  still applies. Intended for migrations; not recommended otherwise.

## Compiled FK annotation bypass

Compiled FK annotations (e.g. `'fields': ['title', 'author__name']`)
emit `F('author__name')` in `.values()`, generating a SQL JOIN to the
target. At request time, each annotation whose path crosses a target with a
`tenant_field` or predicates is wrapped in a `CASE`: the value renders only
where every JOIN target is visible to the caller, and is `null` elsewhere.
The row stays listed (see
[Nested fields across scoped relations](#nested-fields-across-scoped-relations)).

### Startup gate

`compiler.validate_compiled_path_safety()` walks both `m2m_specs` AND
every `fk_annotation` path on each compiled plan. For the FK case it
walks every model along the JOIN chain (not just the leaf) and refuses
to boot if any link has registered predicates or shows tenant drift
from the parent.

### Fixes

- **Drop the FK path** from the parent's `turbodrf()` `fields` list.
  The related field can still be exposed via the target's own
  endpoint.
- **Set `'compiled': False`** on the parent model. The serializer path
  applies the same scoping (out-of-scope nested values render `null`).
- **Strip the target's row-level rules** if and only if it is genuinely
  public reference data.
- **`TURBODRF_ALLOW_UNSAFE_COMPILED_FK = True`** bypasses the gate
  entirely. Logs a loud warning. Request-time scoping still applies.
  Migrations only; not recommended.

## Search field target bypass

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

### Startup gate

The router refuses to boot if any TurboDRF model declares
`searchable_fields` with a `__`-path that walks through a model with
its own predicates, or out of a shared parent into a tenanted target.
Detection runs in the third pass of `discover_models()`, after every
model's predicates have registered. Each step of the traversal chain
is checked, not just the leaf — so `author__profile__bio` blocks if
either Author or Profile has its own rules.

### Fixes

- **Use only flat fields on the parent model** in `searchable_fields`.
  Search by indexed columns on the parent itself; expose related
  search via separate endpoints on the target.
- **Strip the target's row-level rules** if and only if it is genuinely
  public reference data.
- **`TURBODRF_ALLOW_UNSAFE_SEARCH_FIELDS = True`** bypasses the gate
  entirely. Logs a loud warning per offending entry. Request-time scoping
  still applies. Migrations only; not recommended.

## Custom predicate write safety

A `Custom` predicate's write rule is its `write_validator`. Inside
`Either`, a write must pass every child's write check, so a `Custom` can only
add restrictions to the `Owner` rules on another branch; it can't lift them.
(In 0.6.x and earlier, `Either` passed a write when any one child passed,
so a `Custom` that objected to nothing overrode `Owner`.)

A `Custom` without a `write_validator` still says nothing about writes, and
it's easy to forget that a role it admits may later gain write actions. So
every `Custom` must state its write rule.

### Startup gate

`predicates.validate_predicate_write_safety` walks every model's
predicate stack (recursing into `Either`) and refuses to start if any
`Custom` is registered without an explicit `write_validator`.

### Resolution

When the gate fires you have three options:

- **Pass `write_validator=lambda d, i, r: []`** if the predicate adds no
  write rule: the users it admits can edit rows they see, subject to the
  other predicates (inside `Either`, `Owner`'s checks still apply).
- **Pass `write_validator=my_check`** to restrict writes. The function
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

- **Set `TURBODRF_ALLOW_UNSAFE_CUSTOM_WRITE = True`** to bypass the
  gate entirely. Logs a warning. Migrations only; not recommended.

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
not apply the target's own `tenant_field` or registered predicates —
same bug class as the compiled M2M / search-field bypasses, but
URL-driven so it cannot be statically gated at startup.

### Runtime JOIN scoping

`turbodrf.filter_backends.ORFilterBackend` wraps every `__`-path
filter with a target-scoping subquery built by
`validation.build_traversal_scope_q()`. For each model along the JOIN
chain that has registered predicates or a `tenant_field`, an AND
clause of the form
`<prefix>__pk__in=<TargetModel>._base_manager.filter(<target_q>)` is
added to the queryset. The `target_q` mirrors the target view's tenant_q +
predicate_q construction (see
`views.py:_get_tenant_q` / `_get_predicate_q`), so the JOIN can never
return rows outside the target's tenant boundary or predicates.

The subquery uses the target's **base** manager, as Django's own JOINs
and FK descriptors do (the serializer path renders the same related row).
A default-manager filter such as soft delete is not a scoping layer: a row
whose FK points at a soft-deleted target in the caller's scope stays
listed. Hide rows with `tenant_field` or a predicate, not a manager.

For paths through targets with no predicates and no `tenant_field`,
the scope is a no-op `Q()` — no extra subquery cost.

A parent row with an **empty** relation at a scoped step (a NULL FK, or
no rows across a reverse / M2M relation, at that hop or an earlier one)
is kept: each step is `<prefix>__isnull=True OR <prefix>__pk__in=...`
when the relation can be empty. An empty relation joins nothing, so it
can't reveal a target row; a relation pointing at an out-of-scope
target is still excluded. The same scope decides whether a nested FK
value renders on either read path (the value is `null` where the scope
fails; the row stays), and is applied inside search over `__`-path
`searchable_fields`.

`?ordering=fk__field` is handled separately: `ordering_fields` is a flat
list of field names (with role-based permissions, the configured fields
the caller can read), and DRF's `OrderingFilter` silently drops any URL
value that isn't in it. The traversal form is therefore unreachable.
`?ordering=<fk>` orders by the stored FK column; ordering by the field
itself would sort by the target's `Meta.ordering` through an unscoped JOIN.
The primary key is appended as a tie-breaker.

### Kill switch

- **`TURBODRF_ALLOW_UNSAFE_FILTER_TRAVERSAL = True`** disables the
  request-time scoping of filter params. Filter `__`-paths then JOIN
  without target scoping (search and nested values stay scoped).
  Migrations only; not recommended.

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
  `self.get_queryset()`.
- **Bulk permission edits stay cached.** A bulk `RolePermission` QuerySet
  `update()` / `delete()` doesn't invalidate cached permission snapshots;
  call `turbodrf.backends.invalidate_user_permissions()` (see
  [permissions.md](permissions.md#caching)).
- **Bypassed permission modes** (`TURBODRF_DISABLE_PERMISSIONS`,
  `TURBODRF_USE_DEFAULT_PERMISSIONS`) have no field rules, so per-model
  `sensitive_fields` don't apply, and search and ordering don't apply the
  sensitive deny-list.
- **Keycloak roles are captured at login.** See
  [integrations.md](integrations.md#where-the-roles-come-from).
- **Unknown `turbodrf()` keys are ignored.** A misspelled key
  (`'owner_feild'`, `'tenant_feild'`) is dropped without an error, along
  with the scoping it meant to add. Check each model's resolved scoping with
  `python manage.py turbodrf_check`.
- **Startup checks run when the URLconf is imported.** A process that never
  imports it (`get_wsgi_application()` before the first request) starts with
  a broken config and fails on the first request. Run
  `python manage.py check`, which imports the URLconf, in CI and before each
  deploy.
