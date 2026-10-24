# Changelog

## 0.7.0 — Full audit: write rules that hold, tighter filters, faster requests

A second full review (every module, every test, the docs against the code)
found the issues below; each is fixed with regression tests. Many fixes turn a
silent misconfiguration into a startup error or tighten what a caller can do,
so read **Upgrading** first.

### Upgrading

**New startup errors** (`ImproperlyConfigured`; tenancy is checked in
`AppConfig.ready()`, so on every `manage.py` command — run `manage.py check`
in CI):

- Unknown `turbodrf()` keys. A typo such as `owner_feild` used to be ignored,
  leaving rows unscoped. `sensitive_fields` is a class attribute, not a key.
- A `fields` entry that isn't a field, `@property` or `__` path through
  relations, one that ends on a reverse one-to-one (list a field on the
  related model instead), and a `lookup_field` that isn't a unique column.
- A `tenant_field` that doesn't lead to the tenant: every hop must be a
  forward ForeignKey / one-to-one named by the relation (not `<fk>_id`), and
  the last must point at `TURBODRF_TENANT_MODEL`. A `tenant_field` with
  `TURBODRF_TENANT_USER_FIELD` unset, and a `TURBODRF_TENANT_MODEL` that
  doesn't resolve.
- With `TURBODRF_REQUIRE_TENANCY`: a `visibility` list without a
  `tenant_field` whose predicates can all lift the restriction (an `Owner`
  with bypass roles, a `Conditional`, or an `Either` of them). Add the
  `tenant_field`.
- An `Owner` column that isn't a ForeignKey / one-to-one to the user model, a
  `Group` whose `<field>__<user_via>` doesn't reach users (`auth.Group` takes
  `user_via='user'`), and a role value that isn't a string or list.
- Invalid config on a model with `enabled: False` (it used to be skipped), and
  two models routed to the same `endpoint`.

**Behaviour changes:**

- Inside `Either`, every child's write check applies, and `Owner` children
  auto-fill on create. A non-bypass caller can't change or clear an owner
  column that holds another user.
- Generated viewsets have no `queryset` attribute (it held every tenant's
  rows); use `self.get_queryset()` in custom actions.
- Nested filters need their path in `fields`: configuring `author__name` no
  longer makes every column of the author filterable. The only M2M filter is
  `?tags=<pk>`; `__in`, `__isnull`, `__exact` and `_or` on an M2M are ignored.
  A filter value the field can't hold is a 400. `?ordering=` takes concrete,
  single-valued columns only. `?search=` with more than 20 terms, or a term
  over `TURBODRF_MAX_FILTER_VALUE_LENGTH`, matches nothing.
- A `@property` that returns a model instance, a manager or a lazy query is
  left out and logged (it used to 500, or leak). A `property_relations` hop
  needs model-level read on its target.
- A bare M2M or reverse relation in `fields` renders only rows the caller can
  see; reverse relations are read-only. An M2M update keeps the links the
  caller can't see.
- In bypassed permission modes (`TURBODRF_DISABLE_PERMISSIONS`,
  `TURBODRF_USE_DEFAULT_PERMISSIONS`), related fields accept and list only
  rows the caller can see.
- 404 bodies read "Not found." with code `not_found` (no model name). OPTIONS
  has no `tenancy` block and lists only the actions the endpoint routes.
- A create missing a required column the caller can't set (`.create` without
  that field's `.write`) is a 400 naming the field, not a 500.
- `@turbodrf_action` raises `TypeError` for arguments it would ignore
  (`serializer_class=`, `queryset=`). A string `user.roles` is one role.
- `RolePermission.save()` raises `ValidationError` for a rule that can't
  match (wrong-case labels, unknown field or action).
- `AllAuthRoleMiddleware` and `KeycloakRoleMiddleware` are deprecated no-ops:
  remove them from `MIDDLEWARE`. A user model's own `roles` now applies on
  every request.
- `turbodrf_benchmark` requires `--user <username>`.
- Removed: `turbodrf/user_extensions.py`, `TurboDRFMixin.get_field_type()`,
  `turbodrf.exceptions.NoRoleAssigned`,
  `turbodrf.validation.path_traverses_predicate_target`, and the unused
  tenancy names in `turbodrf.settings`.

### Security

- **`Either` let one child override another's write check.** It passed a
  write as soon as one child accepted it, so `Owner`'s rule never held inside
  `Either`: a member could create rows owned by someone else, or a
  participant reassign `created_by`. Every child's check now applies.
- **A co-owner could take a row over** by setting the other owner column to
  themselves. `Owner` also skipped every check with no authenticated user,
  and its direct columns when another path had `__` in it.
- **Nested filters could probe unexposed columns** (`?target__seen_at__year=`)
  on a first-segment check, through reverse hops too, and any column in
  bypass mode. The whole path must now be configured.
- **A lazy query nested in a `@property` result escaped the sandbox** and was
  evaluated unscoped by the JSON encoder. Results become plain data inside
  the sandbox. Columns loaded by model hooks (`post_init`, a saved original
  value) are dropped before the property runs, and a hop needs read on its
  target model and a readable join column.
- **Bypass roles saw every tenant through `visibility`** without a
  `tenant_field` (the hole 0.6.0 closed for a bare `owner_field`).
- **Disabled models read as unscoped** to FK-injection checks, nested values
  and filters. Tenancy is now registered for every TurboDRF model at startup.
- **A hand-written `TurboDRFViewSet` subclass had no tenant boundary.** It now
  gets its model's, or no rows.
- **The bare M2M and reverse relations rendered unscoped pks** on the
  serializer path, and bypassed permission modes didn't scope related fields
  at all (browsable-API dropdowns listed other tenants' rows).
- **PATCH / PUT responses rendered nested values the caller can't see**,
  computed for the row before the write.
- **An FK's column name (`?deal__assigned_broker_id=`) skipped the field's
  read rule and the sensitive deny-list.**
- **The co-tenant check** (a write can't name another tenant's user or tenant
  row) now covers FKs to the tenant model and predicate-only hosts.
- **`?ordering=<m2m>` and M2M lookups JOINed hidden targets**, repeating rows
  and leaking hidden links.
- **With `UNAUTHENTICATED_USER = None`**, anonymous callers on a
  `public_access` model got the unfiltered serializer.
- **Permission cache keys:** in database mode the anonymous key ignored role
  versions; keys now cover the model's `sensitive_fields` and
  `public_access`; an evicted invalidation counter no longer revives stale
  snapshots; a stale `TurboDRFRole` can't move its version back.
- **The OpenAPI schema documented columns the API doesn't serve** for a
  single-key `fields` dict.
- **drf-api-tracking logged secrets in clear:** the sensitive-fields setting
  replaced the built-in deny-list, and bodies rejected before parsing were
  logged raw.
- **The role middlewares shadowed the user model's `roles`** on session
  requests only. Keycloak: a non-finite `auth_time` passed the role-age check.

### Fixed

- An integer tenant (`TURBODRF_TENANT_USER_FIELD` returning a pk) made every
  write a 500.
- A target predicate ORing across a to-many relation (`Either(Owner,
  Members)`) duplicated rows: writes naming a visible row failed.
- `?fields=` selects the same keys on the compiled list as on detail.
- 500s: a `Custom` predicate returning `Q(Exists(...))`; a multipart create
  over 2.5 MB; unparseable `_or` filter values and DurationField filters; a
  searchable field ending on a relation; a `@property` returning a model
  instance.
- `__range` was ignored or mis-split; a PUT without the tenant FK was a 400.
- OPTIONS on a detail route described the list fields, and `"__all__"`
  dropped M2M fields in OPTIONS, the schema and the serializer factory.
- `Owner(bypass='admin')` and `Conditional(require_roles='staff')` took a
  string as a set of characters; auto-detection picked one of two equally
  short tenant paths instead of reporting the ambiguity.
- The schema documented every path twice and used OpenAPI 3's `nullable` in a
  Swagger 2 document; `@swagger_auto_schema(request_body=...)` is honoured.
- `turbodrf_check`, `turbodrf_explain` and `turbodrf_benchmark` reported
  things the router and API don't do; they now use the same resolution.
- The allauth role helpers ignored the role mapping; Sentry's middleware ran
  2–3 queries on every request.
- `'searchable_fields': []` in the config now turns search off.

### Performance

- Every request imported `django.contrib.postgres.fields` (a failing import
  without psycopg, retried each time) for every field, three times: about
  two-thirds of a list request. Filter fields are now computed once per
  request.
- Roles and permission snapshots are looked up once per request instead of
  per field, filter hop and search field (database mode: 34 → 6 queries for a
  20-row list). The query count no longer grows with fields, filters or roles.
- The compiled list uses one visibility flag per relation and counts without
  the nested-value JOINs; serializer-path M2M fields load in one query per
  page (17 queries for 3 rows before).
- A 20-row compiled list went from 8.7 ms to 2.5 ms per request (SQLite).

### Docs, CI and packaging

- The docs are published at <https://turbodrf.com/docs/>; installation and
  quick start are Markdown and the single source. The README quick start
  works (it returned 403 on every request).
- `Either`, `Members`, `Group` and `Conditional` are documented, with
  `Owner(['author', 'reviewer'])` for multiple owners; `AGENTS.md` is for
  downstream projects and `CONTRIBUTING.md` for contributors.
- Publishing runs the tests and lint first, checks the tag against the
  version and runs `twine check`. Actions are pinned to commit SHAs; lint
  tools are pinned in the `dev` extra.
- Removed broken tooling (release scripts, `tox.ini`, the badge-committing
  workflow, a stray `pytest.ini `); `example_project/` is one small working
  example.

## 0.6.1

- **Django 6.0+: models with an M2M list field weren't compiled.** Django 6.0
  made `ManyToManyField` non-concrete, and the compiler identified a forward
  M2M by `concrete`; such models silently fell back to the serializer path
  (correct output, scoped, but slower). Forward M2Ms are now detected on
  every Django version.
- **Django 4.2: an out-of-range integer pk or filter value returned 500** on
  SQLite (Django 5.0 made such lookups match nothing). It's now a 404 on
  object routes and a 400 on lists. A request with too many query / form
  fields is a 400 on every version.
- CI installs the matrix Django version together with the dev extras, which
  had upgraded the "4.2" and "6.0" jobs to Django 6.1, so Django 4.2 hadn't
  really been tested.

## 0.6.0 — Security review: field rules everywhere, nested scoping, list/detail parity

A review of the whole framework (row scoping, field RBAC and writes,
integrations, docs against code, and compiled vs. serializer output) found the
issues below; each is fixed with regression tests (`test_review_*.py`,
`test_parity_compiled_vs_serializer.py`, `test_swagger_scoping.py`). Several
fixes tighten behaviour that apps may rely on — read **Upgrading** first.

### Upgrading

- **Field rules bind anonymous callers.** On a `public_access` model an
  anonymous caller has model-level read; a field any role has a read rule for
  now needs a grant, which anonymous callers get only from a `guest` role.
  Before, an anonymous caller with no `guest` role saw (and could filter,
  search, order and OPTIONS) every configured field. Add a `guest` role with
  the field reads public visitors should see.
- **Tenancy gate (`TURBODRF_REQUIRE_TENANCY`).** Only `tenant_field`, a
  non-empty `visibility` or `tenancy: 'shared'` satisfy it; an `owner_field`
  alone no longer does (its bypass roles saw every tenant's rows). Any
  `tenancy` value other than `'shared'`, `'shared'` combined with scoping keys,
  and an empty `visibility` now raise `ImproperlyConfigured` at startup.
- **Nested values from rows outside the caller's scope are `null`.** A row
  whose FK points at another tenant's row (or a predicate-hidden one) stays
  listed, with that target's nested values null — on every path. The compiled
  list used to drop such rows; detail and uncompiled lists rendered the
  values.
- **Filtering and ordering use configured fields only** (plus the pk). A
  column left out of `fields` is no longer filterable or orderable.
  `__regex` / `__iregex` lookups are no longer accepted.
- **Owner auto-fill runs on create only**; a PATCH that omits the owner keeps
  it. **The primary key is read-only on update.**
- **`TURBODRF_SENSITIVE_FIELDS` adds to the defaults** instead of replacing
  them.
- **Integrations:** allauth drops unmapped groups when a mapping is
  configured (`TURBODRF_ALLAUTH_STRICT_ROLES=False` restores pass-through);
  Keycloak reads roles only from `TURBODRF_KEYCLOAK_PROVIDER` (default
  `"keycloak"`); the Keycloak / allauth middleware do nothing when their
  `TURBODRF_*_INTEGRATION` flag is False; drf-api-tracking skips anonymous
  requests unless `TURBODRF_TRACKING_ANONYMOUS=True`.
- **`@property` fields run in a sandbox** (see Security): only the caller's
  readable columns, no queries, and only the related objects declared in the
  new `property_relations` key, each hop loaded within the caller's scope. A
  property that reads an undeclared relation, an unreadable column or runs a
  query is left out of the response, with a logged warning. Declare its
  relations (`'property_relations': {'label': ['portfolio']}`), iterate
  `self.items.all()` rather than filtering, or, for properties derived from
  data every reader may see, list it in `unscoped_properties`.
- **Compiled lists match detail exactly** (formatting, keys, M2M shape and
  order); a nested Decimal on detail now renders as a string. See Fixed.

### Security

- **`@property` fields read data outside the caller's scope and field
  rules.** A property ran on the full instance: one reading
  `self.holding.name` rendered another tenant's holding (compiled list,
  detail, and with permissions bypassed), and one reading a column the caller
  had no read rule for rendered it. Properties now run on a copy of the row
  holding only readable columns, with declared relations loaded within the
  caller's scope and every query blocked; a lazy QuerySet a property returns
  is evaluated inside the sandbox.
- **Database permission mode ignored field rules defined on other roles.** A
  role with model-level read/update and no field rules read and wrote every
  field another role had restricted. Field rules from any role now bind all
  roles, as in static mode.
- **Anonymous callers bypassed field rules and the sensitive deny-list** on
  `public_access` models (detail, compiled list, filters, search, ordering,
  OPTIONS), including `fields: "__all__"` and bypassed-permission modes.
- **Nested FK fields rendered rows outside the caller's scope** on detail,
  uncompiled lists, the browsable API and detail-only fields (only the
  compiled list scoped them, by dropping the row). Every JOIN target along a
  nested path is now checked against its own tenant boundary and predicates;
  M2M sub-fields crossing a further hop too.
- **Chained `tenant_field` (`ledger__firm`) allowed cross-tenant writes**: a
  create or PATCH could attach the row to another tenant's ledger. The first
  hop's target must now be in the caller's tenant.
- **FK / M2M injection checks skipped hosts with no `tenant_field` or
  predicates** (including `tenancy: 'shared'`), skipped M2M fields entirely,
  and skipped anonymous writers. They now run for every host, every FK and
  M2M field, and fail closed for anonymous writers on scoped targets.
- **Multi-column `owner_field` let users assign rows to others**: it compiled
  to `Either(Owner(a), Owner(b))`, which passed whenever one column was left
  out. It is now one `Owner` checking every column.
- **Custom actions got an unfiltered serializer**: `self.get_serializer()` in
  a `@turbodrf_action` rendered every configured field.
- **A `fields` dict with only `list` exposed every column on detail** (and
  accepted writes to them): the missing view fell back to `"__all__"`.
- **Owner auto-fill on update reassigned ownership** to whoever edited the row
  last.
- **A writable primary key on update saved a copy of the row** under the new
  key (a create through an update-only grant).
- **A related field named like a lookup bypassed filter scoping**
  (`?folder__date=…` was read as the `date` lookup on `folder`); filters are
  now resolved against the model before any lookup is split off.
- **Search over a reverse / M2M path was scoped in a separate JOIN**, so a
  visible sibling let a term match a hidden row.
- **FK filters were a cross-tenant pk existence oracle** (another tenant's pk
  returned 200, a missing pk 400); both now return an empty 200.
  `?ordering=<fk>` no longer sorts by the hidden target's `Meta.ordering`.
- **Tenancy gate loopholes** (see Upgrading) and a permission-string gate that
  accepted strings the snapshot never matched (`App.Model`, `<fk>_id`).
- **Per-model `sensitive_fields`** (documented in AGENTS.md) was never read. It
  now makes the listed fields readable only through an explicit field read
  grant.
- **OpenAPI schema leaked every model's fields**, to anonymous callers too:
  only `paths` were role-filtered. Operations, request / response bodies and
  definitions are now built for the caller, unreachable definitions pruned and
  sensitive names never published.
- **Keycloak trusted role claims from any linked social provider**; new
  opt-in `TURBODRF_KEYCLOAK_MAX_ROLE_AGE` drops roles captured too long ago.
- **allauth turned any unmapped Django Group into a role** of the same name.
- **Sentry received usernames**; it now gets only the user id, with
  request-scoped context cleared for anonymous requests.
- **The fast renderer rendered `str()` of any object** it couldn't encode
  (e.g. a property returning a QuerySet of unscoped rows); it now encodes
  only what DRF's encoder does.
- **drf-api-tracking** now masks `TURBODRF_SENSITIVE_FIELDS` in logged bodies.

### Fixed

- **Compiled lists rendered values differently from detail** — Decimals with
  `str()` (`"0E-8"` for zero at `decimal_places=8`), datetimes in UTC rather
  than the current timezone, durations, times, dates, files (the stored name,
  not the URL) and binary data. Values now go through the DRF field
  ModelSerializer builds for the model field, so `COERCE_DECIMAL_TO_STRING`,
  `DATETIME_FORMAT` etc. apply on both paths.
- **Rows whose nested FK pointed at a soft-deleted row vanished** from
  compiled lists, counts and nested search: traversal scoping used the
  target's default manager. It uses the base manager now (as Django's JOINs
  do); tenant and predicate scoping still apply.
- **Detail and uncompiled lists returned 500 for any model with an M2M
  sub-field** (`select_related` on an M2M). Only single-valued chains are
  `select_related` now.
- **`Either()` dropped a bypass child** (`Q() | Q(x)` is `Q(x)`), so the
  bypass role saw nothing; a predicate crossing an M2M duplicated rows and
  made detail raise `MultipleObjectsReturned`. **`Members` / `Group` /
  `Conditional` writes returned 500**; `Conditional` writes are now verified
  after the save and rolled back if the row would be invisible to the writer.
- **Compiled-path parity**: @property fields are computed on model instances
  in the same sandbox as on detail (`getattr` defaults and `type(self)`
  behave the same; `DictProxy` is gone); a bare M2M renders its pk list; M2M items follow the
  target's `Meta.ordering` and default manager; `target__id` / `tags__id` no
  longer 500; the pk renders only when configured; a nested path ending on a
  `to_field` FK renders the stored column on both paths; M2M sub-field keys
  use the full sub-path on both paths. Configs the compiled path can't render
  faithfully (a nested @property, a path through a reverse FK) are served by
  the serializer path; `turbodrf_check` says why.
- **`?fields=`** now also applies to detail and uncompiled lists, and no
  longer selects every key sharing a prefix (`char` matched `char_null`).
- **Ordering** gets a pk tie-breaker, so pages are stable.
- **Models without a manager named `objects`** crashed the router.
- **`TURBODRF_MAX_NESTING_DEPTH`** is enforced on the compiled path.
- **`turbodrf_check`** used a different tenant-autodetect default from the
  router.
- **`invalidate_user_permissions()`**, documented in AGENTS.md, now exists.
- **`COERCE_DECIMAL_TO_STRING = False` was ignored** with msgspec or orjson
  installed: Decimals rendered as strings. They render as numbers now, as
  with DRF's renderer.
- **Schema**: logged-in users get their schema without `?role=`; no 500
  without `SessionMiddleware`; list responses document the pagination
  envelope; `TURBODRF_SWAGGER_SHOW_ALL_FIELDS` (dead code) is removed.
- **Integration roles reach token/JWT-authenticated users** (resolved lazily
  from the user, not only by middleware that runs before DRF auth).
- **Listing the app as `turbodrf.apps.TurboDRFConfig` crashed startup**; a
  missing `drf_yasg` is now system-check warning `turbodrf.W001`.
- **Lint**: the tree passes black 26, isort and flake8 again.

## 0.5.2 — Compiled-path fixes: NULL FKs, properties, `?fields=`

### Security

- **Compiled `?fields=` could render every column (field-level RBAC
  bypass).** When permission filtering or a `?fields=` selection left the
  compiled list with no plain or FK fields selected (e.g.
  `?fields=<property>`, or `?fields=<field the role can't read>`), the plan
  called `.values()` with no arguments, which returns *every* concrete
  column, including ones the caller's role can't read and sensitive-named
  ones. The compiled plan now always selects the pk explicitly and strips it
  when it wasn't requested. Row scoping was unaffected.

### Fixed

- **Rows with a NULL FK disappeared from compiled lists, counts and
  filters.** Traversal scoping (`build_traversal_scope_q`) emitted
  `<fk> IN (<scoped targets>)` for each tenant- or predicate-scoped JOIN
  target, which is never true for a NULL FK. Any compiled model with a
  nested field across a nullable FK (`asset__name`) dropped those rows, and
  so did search over a nested `searchable_fields` entry, on both paths. A
  row whose relation is empty at a scoped step (at that hop or an earlier
  one) is now kept; a relation pointing at an out-of-scope target is still
  excluded.
- **Compiled list left out `@property` fields for role-scoped users.**
  Properties were filtered by the permission snapshot, which only knows
  model fields. They're now gated as on the serializer (detail) path, so
  list and detail render the same keys.
- **Compiled properties that read anything outside the row raised (500).**
  A property that reads a related object, a reverse relation or an unselected
  column is now computed on model instances, loaded in one extra query for
  the affected rows. A property that also fails on the instance is omitted
  and logged, as on the serializer path. The row proxy no longer hands a
  property the raw FK id where the instance would return the related object.
  It serves `<fk>_id` and `pk` from the row instead. Properties now run
  before Decimal→str coercion, so they see model value types.
- **A nested field ending on an FK rendered the model instance** (e.g.
  `asset__fund`), so create/update/retrieve responses failed with
  `Object of type Fund is not JSON serializable`. It now renders the
  related pk (or `None`), as the compiled list already did. The same
  applies to M2M sub-fields that are FKs.
- **OPTIONS raised `KeyError: 'fields'`** when `fields` listed an FK and a
  nested field of it (`message`, `message__subject`). The FK's entry now
  describes both, in either order.
- **`read_only: True` also blocked POST custom actions (405).** CRUD write
  routes stay unrouted (405), but declared `actions` keep their own HTTP
  methods.
- **`permission_classes` on `@turbodrf_action` were ignored.** They now
  replace the role check for that action, as with DRF's `@action`, and
  apply even with `TURBODRF_DISABLE_PERMISSIONS`. Row scoping through
  `get_object()` / `get_queryset()` is unchanged.
- **`full_clean: True` discarded values normalised by `clean()`.** The
  normalised values of the provided fields are now saved. Validation runs on
  a copy, so a failed `PATCH` no longer leaves `serializer.instance` partly
  updated.
- **`turbodrf_check` treated models without a `compiled` key as not
  compiled.** It now uses the router's default (compiled).

## 0.5.1 — Permission-snapshot cache isolation fix

### Security

- **Fixed a permission-snapshot cache collision on user models with a
  non-`id` primary key (HIGH, broken access control / OWASP A01).** The
  cache key derived the user segment from `getattr(user, "id", "mock")`.
  A custom user model whose primary key is not named `id` (e.g. a UUID pk)
  has no `.id` attribute, so *every* such user collapsed to the single
  literal `"mock"` — one identity's cached action/field-permission snapshot
  was then served to all other users until the cache TTL expired
  (`TURBODRF_PERMISSION_CACHE_TIMEOUT`, default 300s). Row-level visibility
  predicates are not cached, so cross-row *reads* were unaffected; the
  exposure was action/field-level write escalation.

  The key now derives from `user.pk`, which resolves the real primary key
  on every Django user model regardless of field name. An authenticated
  user with no persistent pk (unsaved / mock) is treated as **uncacheable**
  (the snapshot is rebuilt per request) rather than falling back to a
  shared sentinel — a permissions snapshot is never shared across distinct
  identities. Anonymous callers continue to share one key by design (same
  guest/none role; predicates uncached).

  Reported against the 0.4.x line; on 0.5.0 the specific escalation was
  already mitigated because the user's own resolved roles were folded into
  the key hash (0.5.0), but the broken user segment was still a latent
  isolation defect and is now fixed outright.

## 0.5.0 — Security hardening, custom actions, conformance suite

### Security

Fixes from a full adversarial security audit. No cross-tenant isolation
break was found; these close field-level confidentiality gaps within a
tenant and robustness issues.

- **Sensitive fields are never filterable.** Fields matching the
  sensitive-name deny-list (`api_key`, `password`, …) were stripped from
  responses but still accepted as filter params, giving a blind
  value-confirmation / substring oracle for the hidden value
  (e.g. `?api_key__istartswith=sk-a`). Filters on them are now dropped.
- **Swagger schema no longer discloses to anonymous / unauthorized
  callers.** Previously an anonymous request received the full unfiltered
  OpenAPI schema, and any caller could pass `?role=admin` to preview a
  privileged role's schema. A role (query param or session) is now honored
  only when the caller actually holds it; with no held role the schema
  filters to empty.
- **Compiled read path denies instead of falling through when a role has
  zero readable fields.** The internal helpers returned `None` ("no
  snapshot — use full config") instead of an empty set, exposing every
  configured field to roles that should see none.
- **Compiled M2M merge is scoped at request time.** The merge's second
  query is now filtered to target rows visible via the target's own
  tenant + predicates (`scoped_target_queryset`), matching the DRF
  serializer path — defense-in-depth on top of the existing boot-time
  gate, and it holds even under `TURBODRF_ALLOW_UNSAFE_COMPILED_M2M`.
- **`__`-path filters scope the JOIN target.** Filtering across a
  relation (`?fk__field=x`) now ANDs in the target model's own
  tenant/predicate rules, closing a row-existence oracle. Opt out with
  `TURBODRF_ALLOW_UNSAFE_FILTER_TRAVERSAL` (not recommended).
- **Cross-tenant FK writes no longer confirm row existence.** The error
  for an inaccessible FK target is now indistinguishable from a
  nonexistent one ("not found or not accessible").
- **Model `@property` fields with sensitive names are excluded from
  API output**, mirroring the concrete-field deny-list.
- **`?search=` terms are length-capped** (`TURBODRF_MAX_FILTER_VALUE_LENGTH`,
  default 1000) via a new `TurboDRFSearchFilter`; oversized terms
  previously drove unbounded multi-field ILIKE scans (SQLite 500s,
  Postgres CPU burn). Filter values were already capped; search/ordering
  params now are too.
- **Fast JSON renderers can no longer 500 on non-native types.** msgspec /
  orjson encoders now coerce `Decimal`, `UUID`, lazy strings, paths, etc.
  via an `enc_hook` (Decimal stays a string for DRF parity — no float
  precision loss), with a last-resort fallback to DRF's `JSONRenderer`.
  DRF `ErrorDetail` in error responses encodes correctly with no
  exception-handler wiring required.
- **Custom action names that collide with ViewSet internals are rejected
  at boot** (`ImproperlyConfigured`) instead of silently shadowing
  methods like `get_queryset`.
- **Permission-snapshot cache keys use `hashlib`, not builtin `hash()`.**
  `hash()` is randomized per process, so multi-worker deployments sharing
  Redis/memcached silently computed different keys per worker.

### Added

- **Custom actions:** `@turbodrf_action` decorator
  (`turbodrf.decorators`) plus an `"actions"` list in the `turbodrf()`
  config. Actions attach to the generated viewset and inherit
  tenant/predicate scoping when they use `self.get_object()` /
  `self.get_queryset()`.
- **`"read_only": True`** model config — restricts the endpoint to
  GET/HEAD/OPTIONS (writes return 405). **`"http_methods": [...]`** for
  an explicit method allow-list.
- **`"full_clean": True`** model config — runs model `full_clean()`
  (validators, `clean()`, constraints) on API writes.
- **Model `@property` / computed fields in `fields`** render on both
  read paths (compiled and DRF serializer), with sensitive-named
  properties excluded.
- **`"searchable_fields"` in the `turbodrf()` config dict** — previously
  only the class-attribute form was read and the config-dict form
  silently did nothing.
- **Conformance test suite** (`tests/conformance/`): an independent
  oracle that recomputes each user's authorized view from raw DB facts
  (never calling the enforcement code) and asserts API output matches,
  plus Hypothesis fuzzing over hostile query params.

### Changed

- `TURBODRF_DISABLE_PERMISSIONS` and related settings are resolved
  per-request (`get_permissions()`) instead of frozen at import — the
  kill-switch and `override_settings` now take effect without a process
  restart.
- Renderers resolve per-request via `get_renderers()`, upgrading the
  stock `JSONRenderer` to the fast msgspec/orjson renderer in place. The
  project's `DEFAULT_RENDERER_CLASSES` is respected — e.g. removing the
  browsable API in production now works; previously a class-level
  override ignored it.
- Packaging: `setup.py` removed — `pyproject.toml` is authoritative;
  `tox.ini` matrix aligned with the supported Python (3.10+) / Django
  (4.2–6.0) range.

### Removed

- **Postgres RLS module (`turbodrf.rls`) and `turbodrf_emit_rls`
  command.** Keeping RLS policies in sync with the Python predicate
  config was hard to get right and a stale or misconfigured policy is
  a worse failure mode than not having one. TurboDRF now enforces
  row-level access at the application layer only. Teams that need
  DB-layer defense in depth should author and version Postgres RLS
  policies in Django `RunSQL` migrations directly. See `docs/security.md`
  for the rationale.
- `to_rls_using_clause()` / `to_rls_policy()` methods on `Predicate`,
  `Tenant`, `Owner`, `Either`.

## 0.4.0 — Predicate-based row-level access control

### Added

- **Django 6.0 support.** CI matrix tests Django 4.2, 5.2, and 6.0
  across Python 3.10–3.14 (with appropriate version exclusions per
  Django's official Python support matrix).
- **Parallel test execution by default** via `pytest-xdist` (`-n auto`
  in the pytest config). Full suite runs in ~12s on a 14-core machine
  vs 71s serial. Disable for debugging with `-p no:xdist`.
- Predicate system (`turbodrf.predicates`) for declarative row-level access
  control. Core primitives: `Tenant`, `Owner`, `Either`, `Custom`. Advanced
  primitives (importable but not surfaced in the main docs): `Members`,
  `Group`, `Conditional`. See `docs/tenancy.md`.
- Sugar form: `tenant_field`, `owner_field`, `bypass_owner_roles` in
  `turbodrf()` config — compiles to a predicate list internally.
- Hard-fail-at-startup when a model has no tenancy declaration and
  `TURBODRF_TENANT_MODEL` is set. Catches "forgot to scope this model" bugs
  at boot.
- Field-path validation at startup with did-you-mean suggestions.
- Bypass-role validation against `TURBODRF_ROLES` at startup (typo guard).
- FK injection defense: every FK in create/update is validated against the
  related model's predicate stack.
- Tenant reassignment rejection: PATCH cannot move a row to another tenant.
- Detail / PATCH / DELETE return 404 (not 403) on filtered rows — no
  existence leak.
- Optional Postgres RLS module (`turbodrf.rls`):
  - `TurboDRFTenancyMiddleware` sets `app.user_id`, `app.tenant_id`,
    `app.user_roles` per request.
  - `to_rls_using_clause()` / `to_rls_policy()` on predicates.
  - `turbodrf_emit_rls` management command.
- `turbodrf_check` now reports tenancy / predicates per model.
- New settings: `TURBODRF_TENANT_MODEL`, `TURBODRF_TENANT_USER_FIELD`,
  `TURBODRF_REQUIRE_TENANCY` (default `True`),
  `TURBODRF_AUTODETECT_TENANT` (default `False`).

### Fixed

- Exception handler now coerces DRF `ErrorDetail` to plain strings before the
  fast JSON renderer (msgspec/orjson) encodes them.
- `get_queryset()` now fails closed when called without a request (schema
  generation, programmatic use) instead of silently returning all rows.

### Security

- Closes the IDOR/BOLA, FK injection, and FK ownership validation gaps
  identified in the security audit. Multi-tenant isolation is now
  declarative on the model.
- Promotes tenant boundary to a first-class setting separate from the
  predicate algebra. The framework rejects `Tenant()` inside `Either`
  at startup — keeps OR-composition from escaping the tenant boundary.
- Removed `Custom(unrestricted_ok=...)` flag — no longer needed under the
  two-layer design (tenant boundary always enforced separately).

### Migration

Existing projects with no `TURBODRF_TENANT_MODEL` configured see no behavior
change. Multi-tenant projects: see `docs/migration_to_predicates.md`.
