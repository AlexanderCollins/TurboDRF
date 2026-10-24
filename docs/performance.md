# Performance

## Compiled read path

TurboDRF serves list views through a compiled path that uses Django's `.values()` + `F()` annotations instead of DRF serializers. The database returns each row as a dict, so no model instances are built and no serializer runs field by field. The gain grows with the page size.

Enabled by default for all models. Opt out with `'compiled': False` in your `turbodrf()` config.

### What it does

Instead of fetching full model objects and serializing them through DRF:

```python
# DRF path: fetch objects → serialize field by field
books = Book.objects.select_related('author').all()
serializer = BookSerializer(books, many=True)
return Response(serializer.data)
```

It tells the database to return exactly the fields needed as dicts:

```python
# Compiled path: database returns dicts directly
books = Book.objects.values('title', 'price', author_name=F('author__name'))
return Response(list(books))
```

A compiled row reads exactly as the serializer path renders it: values go through the DRF field for their model field (decimals, dates, times, durations), file fields render absolute URLs, binary fields base64, and permissions and nested-value scoping are the same.

The number of queries per page doesn't depend on how many rows the page has: a `COUNT`, the `SELECT`, and one query per M2M field. `@property` fields are the exception to "no model instances": they're computed on sandbox instances (see [the property sandbox](configuration.md#the-property-sandbox)), loaded in one extra query per page plus one per declared relation hop.

### What stays on DRF

- Detail views (single object -- overhead is negligible)
- All write operations (POST, PUT, PATCH, DELETE -- validation is needed)
- Browsable API
- Models whose list fields the compiled path can't render the way the serializer does: a reverse relation, a nested path through a reverse FK (or through an M2M below the first hop), a nested `@property` path, or a field type with no DRF field to render it. `python manage.py turbodrf_check` reports which field and why.

## Fast JSON rendering

TurboDRF auto-detects the fastest available JSON library:

| Library | Speed vs stdlib | Install |
|---|---|---|
| msgspec | ~7x faster | `pip install "turbodrf[fast]"` |
| orjson | ~5x faster | `pip install orjson` |
| stdlib json | baseline | built-in |

No configuration needed -- install and it's used automatically.

## Benchmarking

How much the compiled path saves depends on your models, page size and database. Measure it with `turbodrf_benchmark`, which times real list requests as a given user on both paths:

```bash
python manage.py turbodrf_benchmark Book --user alice --requests 500
```

It prints the average and p95 time per request for each path, and the speedup. See [Management Commands](commands.md#turbodrf_benchmark).
