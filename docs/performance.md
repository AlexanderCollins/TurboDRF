# Performance

## Compiled read path

TurboDRF compiles list views to use Django's `.values()` + `F()` annotations instead of DRF serializers. This skips model instantiation and field-by-field serialization.

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

### What stays on DRF

- Detail views (single object -- overhead is negligible)
- All write operations (POST, PUT, PATCH, DELETE -- validation is needed)
- Browsable API

## Fast JSON rendering

TurboDRF auto-detects the fastest available JSON library:

| Library | Speed vs stdlib | Install |
|---|---|---|
| msgspec | ~7x faster | `pip install turbodrf[fast]` |
| orjson | ~5x faster | `pip install orjson` |
| stdlib json | baseline | built-in |

No configuration needed -- install and it's used automatically.

## Benchmarking

```bash
python manage.py turbodrf_benchmark Book --requests 500
```

Output:

```
Benchmarking Book (1000 objects, page_size=20)
Requests: 500 (+ 10 warmup)

Path         Avg        p95
----------------------------------
DRF          42.31ms    68.12ms
Compiled      7.22ms    11.04ms

Speedup: 5.9x
```
