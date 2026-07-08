# Security

## Secure by default

Endpoints require authentication unless `public_access: True` is explicitly set.

## Sensitive fields

Fields like `password`, `token`, and `secret_key` are never exposed in API responses or available for filtering, regardless of permissions.

Default deny-list:

```python
TURBODRF_SENSITIVE_FIELDS = [
    'password', 'password_hash', 'secret_key', 'api_key',
    'token', 'access_token', 'refresh_token', 'session_key',
]
```

Override in your settings to customise.

## No-roles denial

Authenticated users with no assigned roles get 403 on all endpoints. This prevents accidental access when a user account exists but hasn't been configured.

## Filter security

Users can only filter on fields they have read permission for. This prevents binary search attacks where an attacker infers hidden values by filtering (e.g. `?salary__gte=100000`).

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
            'fields': '__all__',
        }
```

When `TURBODRF_TENANT_MODEL` is set in settings and `TURBODRF_REQUIRE_TENANCY=True`
(default), the router refuses to register any model that has neither a tenancy
declaration nor an explicit `'tenancy': 'shared'` opt-out — closing the
"developer forgot to scope this" class of bugs at boot.

See `docs/tenancy.md` for the full predicate vocabulary, sugar form, real-app
coverage table, and configuration reference.

### What the predicate system enforces

- **List/detail/PATCH/DELETE** — every queryset and `get_object()` is AND'd with
  the user's predicate Q. Detail/PATCH/DELETE return 404 (not 403) on filtered
  rows to avoid existence leaks.
- **Tenant auto-fill on create** — the tenant FK is filled from `request.user`,
  client values for the tenant field are overwritten.
- **FK injection defense** — every FK in the request body must resolve to a row
  visible under the related model's predicate stack. Cross-tenant FK targets
  return 400.
- **Tenant reassignment rejection** — PATCH cannot change a row's tenant FK to
  a different tenant.
- **Owner write check** — non-bypass roles cannot assign rows to other users.

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

## Compiled M2M target bypass

The compiled read path (default for all models) renders nested many-to-many
arrays via a separate two-query merge: one query for the parent rows, a
second on the M2M through-table joining to the **target** model. If that
join ran unscoped, a model nesting an M2M whose target carries its own
visibility rules (predicates or tenant_field) would render rows the caller
could not see by hitting the target's own endpoint — a real
cross-permission read leak. Two layers prevent it:

1. **Request-time scoping** — the merge's second query is filtered to
   targets visible via the target's own endpoint
   (`scoped_target_queryset` in `views._compiled_list`), matching what
   the non-compiled DRF serializer path enforces (`serializers.py`).
   This holds even when the startup gate below is bypassed.
2. **The startup gate** — unsafe parent/target pairings are refused at
   boot, so misconfigurations surface at deploy time rather than as
   silently-filtered output.

### Startup gate

The router refuses to boot if any compiled model nests an M2M into a
target that has registered predicates, or into a tenanted target while the
parent itself is shared. Detection is fully static — done at router init
in `compiler.validate_compiled_path_safety()` after every model's
predicates are registered. The error message names the specific
parent/target pair and lists the available fixes.

Safe targets (the gate allows them): pure reference / lookup tables with
no predicates and no tenant_field — `Tag`, `Category`, `Status` and the
like. The gate fires only when the target itself has row-level rules.

### Fixes

If the gate fires on your model, choose one:

- **Drop the M2M nesting** from the parent's `turbodrf()` `fields` list.
  The M2M can still be reached via the target's own endpoint, where its
  predicates apply correctly.
- **Set `'compiled': False`** on the parent model's `turbodrf()` config.
  The DRF serializer path applies target predicates correctly.
- **Strip the target's row-level rules** if and only if it is genuinely
  public reference data with no per-row visibility — same intent as
  `'tenancy': 'shared'`.
- **`TURBODRF_ALLOW_UNSAFE_COMPILED_M2M = True`** bypasses the gate
  entirely. Logs a loud warning per offending model. Intended for
  migrations where you have audited every offending pairing and confirmed
  the leak is acceptable; not recommended otherwise.

## Compiled FK annotation bypass

Compiled FK annotations (e.g. `'fields': ['title', 'author__name']`)
emit `F('author__name')` in `.values()`, generating a SQL JOIN to the
target without applying its `tenant_field` or registered predicates.
Field-level permissions still gate which output keys render, so the
response itself doesn't carry the leaked column by default — but the
JOIN still executes, leaking row existence and timing/query-count side
channels. Same bug class as the compiled M2M target bypass.

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
- **Set `'compiled': False`** on the parent model. The DRF serializer
  path applies target predicates correctly.
- **Strip the target's row-level rules** if and only if it is genuinely
  public reference data.
- **`TURBODRF_ALLOW_UNSAFE_COMPILED_FK = True`** bypasses the gate
  entirely. Logs a loud warning. Migrations only; not recommended.

## Search field target bypass

The same JOIN-without-target-predicates pattern applies to DRF's
`SearchFilter` when `searchable_fields` contains a `__`-traversed path
(e.g. `searchable_fields = ['author__email']`). The search query
generates `WHERE author.email ILIKE ...` joined to the target model.
The parent's tenant + predicate filter still scopes parent rows, but
the target's own visibility rules are not applied to the join — so a
search query can substring-match against rows the caller cannot see
via the target's own endpoint. Even partial matches leak: an attacker
can `?search=` letter-by-letter to enumerate hidden values.

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
  entirely. Logs a loud warning per offending entry. Migrations only;
  not recommended.

## Custom predicate write safety

`Custom(q_func=...)` defaults `validate_write` to `[]` — meaning "this
predicate doesn't object to the write." When a `Custom` is wrapped in
`Either(Owner, Custom)` (the common shape for "owner OR external
grant"), the Either combinator passes if any child returns no errors.
A no-op `Custom` therefore silently overrides `Owner`'s checks: any
caller whose role has the model-level write permission can bypass
owner enforcement via the Custom branch.

The risk is latent — it only becomes exploitable once the role attached
to the Custom predicate is granted a write permission. But the failure
mode is silent and the audit trail is confusing after the fact ("we
always had owner enforcement, why did it stop working?").

### Startup gate

`predicates.validate_predicate_write_safety` walks every model's
predicate stack (recursing into `Either`) and refuses to start if any
`Custom` is registered without an explicit `write_validator`.

### Resolution

When the gate fires you have three options:

- **Pass `write_validator=lambda d, i, r: []`** if the predicate is
  intentionally read-only and writes should pass through. Explicit
  opt-in to the original behavior.
- **Pass `write_validator=my_check`** to enforce writes. The function
  returns a list of error strings (empty = allow, non-empty = block).
  This is the defense-in-depth fix for predicates intended for
  read-only role grants:

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

- Segment count is 3 or 4.
- The `app.model` resolves via Django's app registry.
- The action is a valid model-level action (`read`, `create`,
  `update`, `delete`) or field-level action (`read`, `write`).
- For 4-segment strings, the field exists on the model. Close
  matches are listed in the error message via `difflib`.

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
`<prefix>__pk__in=<TargetModel>.objects.filter(<target_q>)` is added
to the queryset. The `target_q` mirrors the target view's tenant_q +
predicate_q construction (see
`views.py:_get_tenant_q` / `_get_predicate_q`), so the JOIN can never
return rows the caller cannot see via the target's own endpoint.

For paths through targets with no predicates and no `tenant_field`,
the scope is a no-op `Q()` — no extra subquery cost.

`?ordering=fk__field` is handled separately: when role-based
permissions are enabled (the default), `ordering_fields` returns a
flat list of the user's readable fields, and DRF's `OrderingFilter`
silently rejects any URL value that isn't in that list. The traversal
form is therefore unreachable in the default configuration.

### Kill switch

- **`TURBODRF_ALLOW_UNSAFE_FILTER_TRAVERSAL = True`** disables the
  request-time scoping. Filter `__`-paths then JOIN without target
  scoping. Migrations only; not recommended.
