# Management Commands

## turbodrf_check

Check which models are eligible for the compiled read path:

```bash
python manage.py turbodrf_check
python manage.py turbodrf_check --model Book
```

Shows field breakdown, compilation status, and any issues.

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
