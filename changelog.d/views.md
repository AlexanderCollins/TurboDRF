### Upgrading

- **A nested filter needs its path configured.** `?author__email=` filters
  only when `author__email` is itself in the model's `fields` (list or
  detail); configuring `author__name` no longer makes every column of the
  author filterable. Other nested filters are ignored and logged, in every
  permission mode.
- **M2M filters:** `?tags=3` is the only M2M lookup. `?tags__in=`,
  `?tags__isnull=`, `?tags__exact=` and `?tags_or=` are ignored (they never
  matched, or matched targets the caller can't see). A filter across an M2M
  (`?tags__name=`) matches each row once.
- **A filter value the field can't hold is a 400** (`?price_or=abc`,
  `?created_at__year=x`, `?id__in=1,x`), not a 500 or an ignored filter.
  `__range` takes exactly two comma-separated values, `__isnull` true or
  false. An empty value is no filter.
- **Ordering takes concrete, single-valued columns only**: an M2M, a reverse
  relation or a `@property` in `?ordering=` is ignored.
- **`?search=` matches nothing** with more than 20 distinct terms or a term
  longer than `TURBODRF_MAX_FILTER_VALUE_LENGTH`, instead of a 500 or every
  row. A `searchable_fields` entry ending on a relation is dropped, and the
  `^` / `=` / `@` prefixes now work (`$` isn't accepted).
- **OPTIONS** no longer has a `tenancy` block, and its `actions` are only
  those the endpoint routes (none of the writes on a `read_only` resource;
  Django's model permissions with `TURBODRF_USE_DEFAULT_PERMISSIONS`).
- **404s** carry the code `not_found` and the message "Not found." — the
  model name is no longer in it.
- **`@turbodrf_action`** raises `TypeError` for `serializer_class=`,
  `queryset=` and other arguments TurboDRF ignored; `detail` still defaults
  to `False`.
- **`user.roles` as a string** is one role name (it was a set of characters);
  a related manager of role objects gives their `name` field.
- `turbodrf.exceptions.NoRoleAssigned` (never raised) is removed.

### Security

- A nested filter could probe any column of the related model — including
  through a reverse hop, and in bypass mode any column at all — on a
  first-segment check only. The whole resolved path must now be configured.
- With DRF's `UNAUTHENTICATED_USER = None`, an anonymous caller on a
  `public_access` model got the unfiltered serializer (sensitive and
  role-gated fields included).
- `?ordering=<m2m>` and the M2M `in` / `isnull` / `exact` / `_or` lookups
  JOINed hidden targets: rows repeated (and the paginator dropped others) and
  hidden links' existence and order leaked.
- In database permission mode, the anonymous snapshot's cache key ignored
  every role's version: a new field rule didn't hide the field from anonymous
  callers until the cache TTL.
- The snapshot cache key now covers the model's `sensitive_fields` and
  `public_access`, so a deploy changing them can't reuse snapshots from a
  shared cache.
- An evicted invalidation counter brought back keys of stale snapshots; the
  generation is now a random token, seeded when missing.
  `invalidate_user_permissions()` now accepts an anonymous user.
- A hand-written `TurboDRFViewSet` subclass had no tenant boundary or
  predicates (only router-built viewsets got them); it now gets its model's,
  and no rows when they can't be resolved.
- The OPTIONS `tenancy` block named the tenant path and predicate classes to
  any reader, anonymous callers included.

### Fixed

- `?fields=` selects the same keys on the compiled list as on detail:
  `?fields=author_name` no longer adds the author's other nested values,
  `?fields=id` with an unconfigured pk no longer renders `{}`, and
  `?fields=tags.name` selects an M2M sub-field.
- OPTIONS on a detail route describes the detail fields; with
  `fields: "__all__"` it includes M2M fields.
- A `Custom` predicate returning `Q(Exists(...))` was a 500 on every request.
- A multipart create with a file over 2.5 MB was a 500.
- A tuple `fields` config skipped the sensitive deny-list with permissions
  bypassed, and a nested entry in it was a 500.
- A PUT that left out the tenant FK was a 400 ("Invalid brokerage"); it's
  pre-filled, as on create.
- `?integer_or=abc`, `?uid_or=zzz`, `?flag_or=maybe`, DurationField filters
  and other unparseable OR-filter values were 500s; `?integer__range=` was
  ignored or mis-split; `?fields=<long>` emptied the list.
- On the compiled path, a field type whose raw value DRF renders differently
  (choices, field classes from outside Django, `ArrayField`) renders through
  its DRF field; one with no DRF field isn't compiled.
- The fast renderers' docstring no longer claims parity with DRF for raw
  values (bytes, timedeltas, NaN, Enum and dataclass values) from properties
  and actions.

### Performance

- `get_filterset_fields` imported `django.contrib.postgres.fields` for every
  field, three times per request (re-executing the failing import without
  psycopg): ~65% of a compiled list's time. It's computed once per request.
- Roles and permission snapshots are looked up once per request instead of
  once per field, filter hop and search field. A warm 20-row compiled list:
  52 → 5 cache reads; in database mode 34 → 6 queries (one role lookup).
  The query count no longer grows with fields, filters or roles.
- The compiled list selects one visibility flag per relation path (not per
  nested field), counts rows without the nested-value JOINs, and skips the
  serializer path's `select_related` and annotations (so does a delete).
- Measured (SQLite, 20-row page): compiled list 8.7 → 2.5 ms per request.
