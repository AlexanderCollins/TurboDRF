# Management Commands

Run Django's `python manage.py check` in CI and before each deploy. It
imports your URLconf, which creates `TurboDRFRouter()` and runs all of
TurboDRF's startup checks. Tenancy errors raise on any `manage.py` command,
but `get_wsgi_application()` doesn't run the router's other checks, so a
broken config would otherwise deploy and fail on the first request.

## turbodrf_check

Report each TurboDRF model's list fields and row scoping:

```bash
python manage.py turbodrf_check
python manage.py turbodrf_check --model Book    # by class name
python manage.py turbodrf_check --scope platform
```

For each enabled model it prints:

- A breakdown of the list fields (DB, FK, M2M, property) and any that don't resolve.
- `public_access`.
- Tenancy, when `TURBODRF_TENANT_MODEL` is set: the resolved `tenant_field` (marked when auto-detected; the command uses the router's `TURBODRF_AUTODETECT_TENANT` default, `False`), the within-tenant predicates, `shared`, or `NOT DECLARED` for a model the router will refuse to register.

With `--scope platform` it reports the models with an enabled `'platform'` block instead, as a platform router serves them: the fields from the block, and the tenant path platform writes keep consistent (see [Platform scope](platform.md)).

It only reports, and exits 0 whatever it finds. The startup checks run in `manage.py check`, as above.

## turbodrf_explain

Say why a user can or can't read a model, a field or a row:

```bash
python manage.py turbodrf_explain --user alice --model shop.Book    # username or pk
python manage.py turbodrf_explain --user alice --model Book --pk 42
python manage.py turbodrf_explain --user alice --model Job --scope platform
```

It prints:

- **Caller:** the user's roles (their platform roles with `--scope platform`) and tenant (`TURBODRF_TENANT_USER_FIELD`). No roles means every request is a 403; no tenant means tenant-scoped models show no rows.
- **Model:** its route, its row scoping (`tenant_field`, predicates or `shared`) and `public_access`.
- **Actions:** read, create, update and delete, each allowed or refused, with the permission string and the roles that grant it.
- **Fields:** for each configured field, whether the user can read and write it, and why: the `TURBODRF_SENSITIVE_FIELDS` deny-list, the model's `sensitive_fields`, a field rule, or the model-level permission. A nested path names the first hop the user can't read.
- **Row** (with `--pk`): whether the default manager, the tenant boundary and each predicate (each child of an `Either`) let the row through, and a verdict naming the first layer that hides it.

```
Row 7: exists (base manager)
  Default manager (Deal.objects): includes it
  Tenant boundary (brokerage): passes (row's tenant 1, caller's 1)
  Owner('assigned_broker', bypass=['admin', 'manager']): EXCLUDES it
  Verdict: NOT visible through the API: Owner('assigned_broker', bypass=['admin', 'manager']) excludes it
```

The answers come from the code a request runs (the permission snapshot, the serializer's field check, `views.Authorization`), run for a request carrying the user. The snapshot is built fresh, not read from the permission cache. It says so when `TURBODRF_DISABLE_PERMISSIONS` or `TURBODRF_USE_DEFAULT_PERMISSIONS` bypasses permissions. It only reads.

## turbodrf_schema

Print the schema the docs view serves a caller holding exactly the given roles, with no database user: for a CI diff or a generated client.

```bash
python manage.py turbodrf_schema --role editor                # JSON on stdout
python manage.py turbodrf_schema --role editor --role viewer --format yaml --output schema.yaml
python manage.py turbodrf_schema --anonymous
python manage.py turbodrf_schema --role support --scope platform
```

Pass `--role` (repeatable) or `--anonymous`; an unknown role is an error. The document is the Swagger 2 one `get_turbodrf_schema_view()` serves: only the operations the caller may perform, with the fields they may read and write. It covers every route of the scope in `ROOT_URLCONF` (a router's `get_schema_view()` covers only that router's). A router's gate counts as passed for a `--role` caller: it checks who the caller is, which a role list doesn't say. The document has no `host` unless `SWAGGER_SETTINGS['DEFAULT_API_URL']` sets one.
