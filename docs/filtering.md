# Filtering & Search

## Search

Define searchable fields in the `turbodrf()` config dict (preferred) or as a `searchable_fields` class attribute:

```python
class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    description = models.TextField()

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['title', 'description', 'author__name'],
            'searchable_fields': ['title', 'description', 'author__name'],
        }
```

The legacy class-attribute form still works and is used as a fallback:

```python
class Book(models.Model, TurboDRFMixin):
    searchable_fields = ['title', 'description']
```

```
GET /api/books/?search=django
```

Entries may be plain field names or `__`-paths (`author__name`). Search is gated by field-level read permissions -- a caller without read access to a field cannot search by it -- and the whole list is validated at startup (unresolvable paths are dropped, and paths that would join into a predicate-bearing target are refused unless `TURBODRF_ALLOW_UNSAFE_SEARCH_FIELDS` is set).

## Filtering

All model fields are filterable with Django lookups:

```
GET /api/books/?author=1                           # Exact match
GET /api/books/?price__gte=10&price__lte=50        # Range
GET /api/books/?title__icontains=python             # Text search
GET /api/books/?published_date__year=2024           # Date
GET /api/books/?author__name__istartswith=smith      # Related field
```

### OR filtering

Use `_or` suffix for OR queries:

```
GET /api/books/?title_or=Django&title_or=Python
GET /api/books/?title__icontains_or=django&title__icontains_or=python
```

Different `_or` fields are combined with AND:

```
GET /api/books/?title_or=Django&title_or=Python&price__lt=50
# → (title="Django" OR title="Python") AND price < 50
```

## Ordering

```
GET /api/books/?ordering=price         # Ascending
GET /api/books/?ordering=-price        # Descending
```

## Pagination

```
GET /api/books/?page=2&page_size=50
```

Response format:

```json
{
    "pagination": {
        "next": "http://api.example.com/books/?page=3",
        "previous": "http://api.example.com/books/?page=1",
        "current_page": 2,
        "total_pages": 10,
        "total_items": 200
    },
    "data": [...]
}
```

Default page size: 20. Max: 100.
