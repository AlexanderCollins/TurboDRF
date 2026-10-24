### Security

- **A lazy value nested in a `@property` result escaped the sandbox.** A
  queryset or generator inside the dict or list a property returned (or a
  `map` over one) was evaluated by the JSON encoder after the sandbox closed:
  unscoped, with every column. `{"names": Holding.objects.values_list("name",
  flat=True)}` rendered every tenant's names. A property's result is now
  turned into plain data (dicts, lists, scalars) inside the sandbox, so such a
  value fails the property (left out and logged) instead.
- **Columns loaded while a sandbox row was built leaked to the property.** A
  `post_init` handler or `__init__` that read a deferred column (the "remember
  the original value" pattern), or a reverse relation into a `to_field` FK,
  loaded columns the caller can't read. They're dropped again before the
  property runs, a related object a handler cached on the row is reloaded
  within scope, and a relation whose join column the caller can't read isn't
  loaded.
- **A `property_relations` hop didn't need read on its target model.** A role
  with only `holding.read` got `HoldingEvent` rows through `self.events.all()`.
  The caller now needs model-level read on every hop's target.
- **A bare M2M and a reverse relation rendered unscoped on the serializer
  path.** `'linked_deals'` listed the pks of hidden and other-tenant rows, and
  a reverse FK (`'items'`, `'items__label'`) rendered every related row. They
  now render only the rows the caller can see through the target's own
  endpoint, as nested M2M arrays and the compiled path already did.
- **Bypassed permission modes (`TURBODRF_DISABLE_PERMISSIONS`,
  `TURBODRF_USE_DEFAULT_PERMISSIONS`) didn't scope related fields.** The
  browsable API's dropdowns listed other tenants' rows, an M2M write's error
  told a missing pk from another tenant's, and an FK to the tenant model
  wasn't checked. Related-field scoping now applies in every permission mode,
  to every related field type, `to_field` FKs included (which were never
  scoped).
- **An FK to the tenant model must name the caller's tenant** on a
  tenant-scoped host, as its `tenant_field` already did. A target carrying
  `TURBODRF_TENANT_USER_FIELD` (typically a User) with no tenant is rejected
  too.
- **A PATCH / PUT response rendered values the caller can't see.** It reused
  the nested-value visibility computed before the write: pointing a row at a
  same-tenant holding whose portfolio is another tenant's showed that
  portfolio's name in the response (a GET afterwards showed `null`). Each hop
  is re-checked after the write.
- **An M2M update deleted links the caller can't see.** `PATCH
  {"linked_deals": [...]}` replaced every link, including links to other
  tenants' rows and predicate-hidden ones. Those links are now kept.
- **Filters named by an FK's column (`?deal__assigned_broker_id=`) skipped the
  field's read rule and the sensitive deny-list.** Each segment of a filter,
  field or permission path is now checked by its field name, so `token_id` is
  denied like `token`.

### Fixed

- **An integer `TURBODRF_TENANT_USER_FIELD` made every write 500**
  ("must be a Brokerage instance"). An integer tenant pk is assigned to the
  FK's column.
- **A target predicate ORing across a to-many relation (`Either(Owner,
  Members)`) made writes fail.** The target's rows came back duplicated: an FK
  to a visible row was a 500 (`MultipleObjectsReturned`) or a "not found"
  400. Scoped target rows are now unique.
- **A `@property` returning a model instance (or a manager) made the whole
  response 500.** It's left out and logged, like any failing property.
- **Serializer-path M2M arrays ran queries per row and per sub-field hop**
  (17 queries for 3 rows, 67 for 13). Each relation now loads for the whole
  page in one query.
- **Every FK error read "not found or not accessible"**, including "This field
  is required." and wrong-type errors. Only "does not exist" errors are
  unified now, for FKs and M2Ms alike.
- `TurboDRFSerializerFactory` expanded `fields: "__all__"` without M2M fields;
  it now includes them, as the views and the compiled path do.
- A failing to-many render is logged instead of silently rendering `[]`.

### Changed

- A reverse relation listed in `fields` is read-only. A write to it (DRF's
  `.set()`, which moved rows between parents) is ignored.
- Removed `turbodrf.validation.path_traverses_predicate_target`, which nothing
  used.

### Upgrading

- A `@property` that returns a model instance, a manager, or a queryset of
  instances is now left out of the response (it used to 500 at render, or
  return instances to code calling the sandbox directly). Return the fields
  you need: `[e.status for e in self.events.all()]`.
- A `@property` whose result holds a lazy query (a queryset, a generator over
  one) now fails instead of rendering. Build the value from declared
  `property_relations` inside the property.
- A `property_relations` hop needs model-level read on its target; grant
  `app.model.read` to roles whose properties should load it.
- A write naming a user (or another row carrying
  `TURBODRF_TENANT_USER_FIELD`) without a tenant on a tenant-scoped model is
  now a 400 in every permission mode.
- In bypassed permission modes, related fields now accept (and the browsable
  API lists) only rows the caller can see.
- A missing or wrong-type FK value now gets DRF's own error ("This field is
  required.", "Incorrect type. ..."); only a pk that doesn't exist or isn't
  visible reads "Invalid <field>: not found or not accessible.".
