# Management Commands

Run Django's `python manage.py check` in CI and before each deploy. It
imports your URLconf, which creates `TurboDRFRouter()` and runs all of
TurboDRF's startup checks. Tenancy errors raise on any `manage.py` command,
but `get_wsgi_application()` doesn't run the router's other checks, so a
broken config would otherwise deploy and fail on the first request.

## turbodrf_check

Report how each TurboDRF model is served and scoped:

```bash
python manage.py turbodrf_check
python manage.py turbodrf_check --model Book    # by class name
```

For each enabled model it prints:

- Whether the list view is compiled. A model that opts in (the default) but can't be compiled is reported with the reason it's served by the serializer path (e.g. a nested `@property` path).
- A breakdown of the list fields (DB, FK, M2M, property) and any that don't resolve.
- `public_access`.
- Tenancy, when `TURBODRF_TENANT_MODEL` is set: the resolved `tenant_field` (marked when auto-detected; the command uses the router's `TURBODRF_AUTODETECT_TENANT` default, `False`), the within-tenant predicates, `shared`, or `NOT DECLARED` for a model the router will refuse to register.

It only reports, and exits 0 whatever it finds. The startup checks run in `manage.py check`, as above.

## turbodrf_benchmark

Time a model's list endpoint on the compiled and the DRF serializer path:

```bash
python manage.py turbodrf_benchmark Book --user alice
python manage.py turbodrf_benchmark Book --user alice --requests 500 --page-size 50 --warmup 20
```

Each timed call is a real list request, sent as `--user` (required, a username) through the model's registered viewset: permissions, row scoping, filters, pagination and rendering included. `--requests` (default 100) timed requests per path, `--page-size` (default 20) rows per request, `--warmup` (default 10) untimed requests first. The user must see some rows. A model the router doesn't compile is timed on the serializer path only.

## turbodrf_explain

Show the compiled query plan for a model:

```bash
python manage.py turbodrf_explain Book
python manage.py turbodrf_explain Book --role viewer
python manage.py turbodrf_explain Book --fields "title,author.name"
python manage.py turbodrf_explain Book --role viewer --sql
```

Shows:
- Simple fields, FK annotations, M2M fields, property fields
- With `--role`, the actions and fields a caller with only that role gets (static permission mode only)
- With `--fields`, what that `?fields=` selection renders, as the API does
- Query complexity (JOINs, total queries)
- With `--sql`, the generated SQL, before the per-request tenant and predicate filters
