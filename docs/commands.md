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
