# Management Commands

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

It only reports. The router's startup gates (compiled-path and search-field safety, `Custom` write validators, permission strings) run when `TurboDRFRouter()` is created, usually when your URLconf is imported, not here. The command exits 0 whatever it finds.

## turbodrf_benchmark

Compare compiled vs DRF serializer performance:

```bash
python manage.py turbodrf_benchmark Book
python manage.py turbodrf_benchmark Book --requests 500 --page-size 50
```

Requires data in the database to benchmark against.

## turbodrf_explain

Show the compiled query plan for a model:

```bash
python manage.py turbodrf_explain Book
python manage.py turbodrf_explain Book --role viewer
python manage.py turbodrf_explain Book --role viewer --sql
```

Shows:
- Simple fields, FK annotations, M2M fields, property fields
- Permission pruning per role
- Query complexity (JOINs, total queries)
- Generated SQL
