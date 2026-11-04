# Design principles

TurboDRF's security doesn't come from each endpoint getting its checks
right. It comes from a few rules the framework holds on every path, so a
generated endpoint can't forget one. This page lists those rules, the
guarantees they add up to, and how they're tested. [Security](security.md)
has the threat model and the known limitations.

## The principles

### 1. Nothing is exposed until you say so

Every default denies:

- A model is served only when it has the mixin or is passed to
  [`turbodrf.register()`](configuration.md#registering-a-model-without-the-mixin).
- A role grants nothing it doesn't list, and a user with no role gets a 403
  on every endpoint.
- Anonymous callers get nothing, unless a model sets `public_access`, and
  then they only read.
- A field with a read rule is hidden from every role that isn't granted it,
  and names like `password` and `token` are never served at all.

The one default that exposes more, `fields: '__all__'`, makes
`manage.py check` warn (`turbodrf.W004`).

### 2. One chokepoint for rows

Every way a request can touch rows gets them through the same scope: the
model's tenant boundary AND its predicates, applied by one function
(`views.Authorization.scope`). That covers list, detail, update, delete,
custom actions (through `self.get_object()` / `self.get_queryset()`), nested
fields, to-many arrays, filters, search, ordering, `@property` relations and
the targets of FK and M2M writes. A related model's rows are scoped by that
model's own rules, the same as through its own endpoint
(`validation.scoped_target_queryset`). There is no side door that skips it.

### 3. The tenant wall sits outside the logic

The tenant boundary (`tenant_field`) is a setting, not a predicate. It's
applied as its own filter, before the predicates and apart from them, so no
`Either`, bypass role or `Conditional` can OR it away. Predicates only narrow
what a caller sees inside their tenant. Every model must make this decision
(`tenant_field`, a restricting `visibility`, or `'tenancy': 'shared'`), or
the app doesn't start.

Writes hold it twice. Before a write, its FK and M2M values are checked
against the rows the caller may reference, through the scoped querysets.
Then, before the write commits, every row it named is checked again from the
rows' own tenant columns, by separate code: a gap in the first check can't
alone put a reference to another tenant's row in the database.

### 4. The predicate algebra: reads are OR, writes are AND

Row rules are a small logic:

- **The `visibility` list is AND:** every predicate must match.
- **`Either` is OR for reads:** a row is visible if any branch matches.
- **Writes are AND:** a write must pass the write check of every predicate,
  including every branch of an `Either`. Seeing a row through one branch
  (`Members`) never lifts another branch's rule (`Owner`).
- **Some rules can only be judged on the saved row:** a `Conditional`, an
  `Owner` over a `__` path, and an `Owner` with several columns. The row is
  checked after the save, inside the same transaction, and the write is
  rolled back if the writer could no longer see it.

Six predicates (`Owner`, `Members`, `Group`, `Either`, `Conditional`,
`Custom`) and the tenant setting cover the rules. They compile to Django `Q`
objects, so they filter lists in the database, not row by row in Python.

### 5. The same rules on every path

A field the caller can't read is missing everywhere it could show up or be
inferred from:

- responses, at every `__` segment of a nested path;
- filters and search (`?salary__gte=` would binary-search it);
- `?ordering=` and the model's default order;
- the reverse side of a relation;
- OPTIONS, the Swagger and OpenAPI 3 schemas, and the browsable API.

There is one read path, the serializer, so two implementations can't drift
apart.

### 6. Fail closed

When something is missing or wrong, the answer is "nothing", never
"everything":

- no request, no user, no tenant, or a tenant attribute of the wrong type
  gives no rows;
- an error inside a permission check is a denial;
- a property that fails in the sandbox is left out of the row.

### 7. Fail loud at startup

A misconfiguration that would quietly weaken scoping refuses to start, in
`manage.py check` and when the URLconf loads. That includes:

- a model with no tenancy decision;
- an unknown config key;
- a misspelt permission string, field or search field;
- a broken `tenant_field` path;
- a `Custom` predicate without a `write_validator`;
- an unknown permission mode, or a role getter that can't be imported.

Legal but risky settings warn instead:

- `turbodrf.W002`: permissions disabled with `DEBUG` off;
- `turbodrf.W003`: no roles defined;
- `turbodrf.W004`: a model exposing every field.

### 8. Answers don't leak

What a caller can't see looks the same as what doesn't exist:

- a hidden row is a 404, not a 403;
- a hidden FK target gets the same 400 message as a missing one;
- a filter naming a hidden row returns an empty list, as a missing one does;
- the schema and OPTIONS describe only what the caller can do;
- a 403 says why only when `DEBUG` is on.

### 9. Staff access is separate and gated

Your own staff work across tenants through a separate
[platform router](platform.md). It needs a gate on every route, uses its own
roles, and serves only the models that opt in. Tenant roles grant nothing
there, and platform roles grant nothing on the tenant API.

### 10. Small enough to audit

Configuration is data that sits next to the model. There's one read path, a
six-word predicate vocabulary, and one function where rows are scoped. A new
capability earns its place when it narrows or replaces something, not when
it adds another way to do the same thing.

## The guarantees

Taken together, the principles promise these invariants for any request to a
generated endpoint:

1. A caller never receives a row outside their tenant. The only exception is
   a platform router, which is behind its gate.
2. A caller never receives a row the model's predicates exclude, whether
   directly or through a relation on another model.
3. A caller never receives, or infers through filters, search or ordering, a
   field value they have no read grant for.
4. A write never adds a reference to a row the caller can't see, and never
   moves a row out of the caller's tenant.
5. A write never sets a field the caller has no write grant for.
6. A configuration that would break 1–5 doesn't start.

## How they're tested

- **A conformance monitor** (`tests/conformance/`) recomputes, from raw
  database facts and the role config, what each caller should receive. It
  doesn't call TurboDRF's own scoping code. It then checks real API responses
  against that across callers, roles, models and access paths, including
  property-based (Hypothesis) runs.
- **A differential fuzzer** (`tests/conformance/test_rewrite_equivalence.py`)
  generates random rules and data and checks that the faster query shapes
  TurboDRF builds match the rows Django's own reading of each rule does.
- **A write-rule fuzzer** (`tests/conformance/test_write_rule_equivalence.py`)
  checks TurboDRF against an executable copy of these docs
  (`tests/conformance/write_spec.py`, written from them, not from the code):
  random predicate combinations (up to four, `Either` nested twice), callers,
  rows and creates, updates, takeovers and deletes. Each must be allowed or
  refused, and stored, as the docs say.
- **Noninterference fuzzers** (`tests/conformance/test_noninterference_reads.py`,
  `test_noninterference_writes.py`) send random requests to every endpoint
  twice: once on a database, once with hidden data added (other tenants' rows,
  rows a rule hides, values in columns the caller can't read). A read's
  response must not change. A write must leave the hidden data as it was,
  reference only rows the caller may reference, and answer a hidden row as
  it answers a missing one.
- Each fuzzer runs a small number of examples in CI. Set
  `TURBODRF_REWRITE_EXAMPLES`, `TURBODRF_WRITE_RULE_EXAMPLES` or
  `TURBODRF_NONINTERFERENCE_EXAMPLES` for a longer campaign: 0.8.5 was
  checked with about 330,000, 64,000 and 75,000 examples respectively.
- **Attack tests** (`tests/integration/test_security_*.py`,
  `test_audit_084.py`, `test_audit_085.py`, `test_audit_third*.py` and
  others) each send a request that broke an invariant in an earlier release,
  and assert that it no longer does.
- **Backstop tests** (`tests/integration/test_backstops.py`) switch off a
  first check, as a bug in it would, and assert that the backstop behind it
  still refuses the request.
- **`manage.py turbodrf_explain`** shows how the rules apply to one user, row
  and field, using the same code paths as a request.
