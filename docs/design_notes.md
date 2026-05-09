# Design notes

A short summary of what the framework's row-scoping system is designed
to do and what it doesn't try to do. Refer to it if you're working
out how TurboDRF fits into your security model; it's not a promise
about behaviour.

## What the design addresses

Row-level access control for authenticated users in multi-tenant
deployments — list, detail, search, ordering, filter, OPTIONS,
browsable API, M2M renders, and writes (POST / PATCH / PUT / DELETE).

The architecture has two layers. The **tenant boundary** is a setting
(`tenant_field`), applied as an AND outside the predicate algebra so
`Either(...)` OR-composition can't escape it. **Within-tenant rules**
are predicates (`Owner`, `Custom`, `Either`) that compose freely.
Cross-tenant detail / PATCH / DELETE returns 404 rather than 403 to
avoid leaking row existence. Writes pass through three checks: tenant
auto-fill, predicate `validate_write`, and an FK-injection guard that
verifies every FK in the request body resolves to a row visible under
the related model's predicate stack.

Five startup passes refuse to register a config that's likely wrong:
tenancy declaration, compiled-path safety (M2M and FK joins to
predicate-bearing targets), `searchable_fields` traversals,
`Custom`-without-`write_validator`, and `TURBODRF_ROLES` permission
strings that don't resolve to real models / fields / actions.

For Postgres deployments, the same rules can be emitted as RLS
policies as defense in depth (`docs/rls.md`).

## What's outside the design

A few things are intentional opt-outs or developer-side concerns. The
framework can't enforce them:

- Models marked `'tenancy': 'shared'` are not tenant-scoped — that's
  the point of the marker. `public_access: True` and the
  `TURBODRF_DISABLE_PERMISSIONS` / `TURBODRF_ALLOW_UNSAFE_*` settings
  are similar opt-ins.
- The sensitive-field deny-list matches field names, not content.
  Putting secrets inside a `JSONField` won't be caught.
- Custom `@action` methods on `TurboDRFViewSet` subclasses that don't
  call `get_queryset()` bypass the access layer.
- `Custom` predicate `q_func` correctness is the developer's
  responsibility — the framework AND's whatever `Q` the function
  returns into the queryset.
- Adjacent permission classes (MFA, subscription, IP gates) aren't
  applied to TurboDRF-generated viewsets. Add them at a layer in
  front (middleware, custom auth backend).
- Migration scripts, signal handlers, raw SQL, and other code paths
  outside the ORM aren't reached by the framework. RLS is the
  defense-in-depth option for those.

## Verification

The framework ships ~1,500 unit and integration tests, including a
suite under `tests/integration/test_security_*` that exercises
cross-tenant attack shapes (FK injection, search inference, ordering-
by-hidden-field, filter traversal, cross-tenant PATCH/DELETE). A
recipe for adapting the same approach to your own models is in
[`docs/sanity_check.md`](sanity_check.md).

The framework is not audited by a third party and is not certified
for any compliance regime. See the [LICENSE](../LICENSE) for the
warranty terms.
