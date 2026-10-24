### Security

- **`Either` no longer lets one child override another's write check.** It ran
  its children's checks until one passed, and `Members`, `Group` and a
  `Custom` returning `[]` always pass, so inside `Either` the `Owner` rule
  ("can't set the owner to another user") never held: a plain member could
  create a row owned by someone else, or a participant could reassign
  `created_by`. `Either` now runs every child's check and any error blocks
  the write. It also auto-fills through its children, so an `Owner` child
  sets its column to the creator (the documented `Either(Owner, Members)`
  recipe created rows with no owner, which the creator then couldn't see).
- **A co-owner can't take a row over.** On update, `Owner` only checked the
  new value. With `owner_field: ['owner', 'reviewer']`, the reviewer could set
  `owner` to themselves and lock the owner out. A non-bypass caller can no
  longer change an owner column that holds another user.
- **`Owner` checks writes without an authenticated user**, and its direct
  columns when another of its paths has `__` in it. Both used to skip every
  check: an `AllowAny` action could create a row owned by anyone.
- **Bypass roles no longer see every tenant through `visibility`.** Without a
  `tenant_field`, a `visibility` list counted as a tenancy declaration even
  when its predicates could lift the restriction, so
  `'visibility': [Owner('owner', bypass=['admin'])]` let admins list every
  tenant's rows (the hole 0.6.0 closed for a bare `owner_field`). Such a list
  now counts only if one of its predicates restricts every caller.
- **A `tenant_field` must lead to the tenant.** Every hop must be a forward
  ForeignKey / one-to-one, named by the relation (not `<fk>_id`), and the last
  must point at `TURBODRF_TENANT_MODEL`. `tenant_field: 'portfolio'` was
  accepted and, with an integer tenant pk, matched another tenant's rows; a
  hop through an M2M or reverse relation duplicated list rows and broke detail
  lookups.
- **Disabled models are still scoped as targets.** Tenancy and predicates were
  registered only for enabled models, and only when a router was built. A
  model with `enabled: False` read as unscoped to FK-injection checks, nested
  values and filters (a user could attach another user's private row and read
  its name back), and so did every model in a process that never built a
  router. Every TurboDRF model is now registered at startup
  (`AppConfig.ready()`), enabled or not.
- **Generated viewsets have no class-level `queryset`.** It held every
  tenant's rows, so a custom action reading `self.queryset` bypassed scoping.
  Use `self.get_queryset()`.
- **Database-mode rules that match nothing are refused.** `RolePermission`
  accepted a wrong-case app label or model name, a misspelt field name or an
  unknown action, and the rule silently matched nothing, leaving the field it
  meant to restrict readable. Saving one now raises `ValidationError`, as the
  same string fails static mode's startup check.
- **A stale `TurboDRFRole` can't bring back a revoked permission.** Saving a
  stale instance wrote its old version + 1, which could move the version back
  onto a cached snapshot's key. The version is now bumped in the database.

### Fixed

- `Owner(bypass='admin')` and `Conditional(require_roles='staff')` took a bare
  string as a set of characters (role `'a'` bypassed). A string is one role.
- A `Group` whose `user_via` doesn't resolve (a typo, or an accessor name such
  as `user_set` instead of the lookup `user`) raised `FieldError` on every
  request. It's now checked at startup, as is that each `Owner` column is a
  ForeignKey / one-to-one to the user model.
- `TURBODRF_AUTODETECT_TENANT` picked the first of two equally short paths
  (`source__firm` / `dest__firm`) instead of reporting them as ambiguous, and
  followed reverse one-to-one relations.
- A misspelt `TURBODRF_TENANT_MODEL` was only noticed when auto-detection was
  on; it's now resolved at startup.
- A user tenant attribute holding an unsaved instance or a bool is treated as
  "no tenant" (it raised, or matched tenant pk 1).
- A typo in `fields` or `lookup_field` only logged a warning (nothing with
  `compiled: False`) and then every request failed with a 500.
- Two TurboDRF models with the same name in different apps crashed at startup
  (duplicate route name). Give one a distinct `endpoint`; its routes are then
  named after it.

### Changed

- The `Custom`-without-`write_validator` startup error explains what's at
  stake now that `Either` runs every child's check: a `Custom`'s `q_func`
  filters reads only, so declare its write rule (`write_validator=my_check`,
  or `lambda d, i, r: []` when it shouldn't restrict writes).
- The tenancy startup error mentions auto-detection only when it ran, and no
  longer suggests `visibility` as a way to declare tenancy.
- An `Owner` write that clears its column reads "Cannot clear owner", not
  "Cannot set owner to a different user".

### Upgrading

These now raise `ImproperlyConfigured` at startup (from `AppConfig.ready()`
for tenancy, so on every `manage.py` command, and when the router is built):

- A `tenant_field` through an M2M or reverse relation, named by its column
  (`workspace_id`), ending at a model other than `TURBODRF_TENANT_MODEL`, or
  on a non-relation column. On the tenant model itself, scope with a `Custom`
  predicate instead.
- A `tenant_field` (declared or auto-detected) with `TURBODRF_TENANT_USER_FIELD`
  unset, and a `TURBODRF_TENANT_MODEL` that doesn't resolve.
- With `TURBODRF_REQUIRE_TENANCY`: a `visibility` list without a
  `tenant_field` whose predicates can all lift the restriction (an `Owner`
  with bypass roles, a `Conditional`, or an `Either` with such a child). Add
  the `tenant_field`.
- An `Owner` column that isn't a ForeignKey / one-to-one to the user model, and
  a `Group` whose `<field>__<user_via>` doesn't reach the user model.
- Invalid tenancy config on a model with `enabled: False` (it used to be
  skipped).
- A `fields` entry that isn't a field, `@property` or `__` path through
  relations; a `lookup_field` that isn't a unique column; two models routed
  to the same `endpoint`.
- A bypass-role or `require_roles` value that isn't a string or a list.

Behaviour changes:

- Inside `Either`, every child's write check applies, and `Owner` children
  auto-fill on create (`Either(Owner('author'), Owner('reviewer'))` sets both
  columns to the creator).
- A non-bypass caller can't change, or clear, an owner column that holds
  another user, nor clear their own.
- `self.queryset` on a generated viewset is `None`.
- `RolePermission.save()` raises `ValidationError` for a rule naming a model
  that isn't installed; set `TURBODRF_ALLOW_UNKNOWN_PERMISSIONS = True` (as in
  static mode) if you store rules for models loaded later. `bulk_create()`
  skips `save()` and isn't checked.
