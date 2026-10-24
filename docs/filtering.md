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

Entries may be plain field names or `__`-paths (`author__name`). Search is gated by field-level read permissions -- a caller without read access to a field cannot search by it -- and the whole list is validated at startup (unresolvable paths are dropped, and paths that would join into a predicate-bearing target are refused unless `TURBODRF_ALLOW_UNSAFE_SEARCH_FIELDS` is set). A `__`-path term only matches related rows the caller can see through that model's own endpoint.

## Filtering

The fields in the model's `fields` config (plus the primary key) that the caller can read are filterable with Django lookups:

```
GET /api/books/?author=1                           # Exact match
GET /api/books/?price__gte=10&price__lte=50        # Range
GET /api/books/?title__icontains=python             # Text search
GET /api/books/?published_date__year=2024           # Date
GET /api/books/?author__name__istartswith=smith      # Related field
```

- Filters on any other field are ignored (and logged), so a hidden column can't be probed through filters.
- `__regex` / `__iregex` lookups aren't accepted.
- A related-field filter is permission-checked at every hop and only matches related rows the caller can see through that model's own endpoint. A related field named like a lookup (`folder__date`, where `date` is a field of the folder) is treated as that field.
- An FK or M2M filter naming a row the caller can't see returns an empty `200`, exactly as a pk that doesn't exist does.

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
GET /api/books/?ordering=author        # By the stored author id
```

The same configured, readable fields are orderable; nested paths (`author__name`) aren't. An FK orders by its stored column, not by the related model's `Meta.ordering`. The primary key is appended as a tie-breaker, so rows don't repeat or go missing across pages.

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
    "data": [
        {"id": 21, "title": "Django for APIs", "price": "39.00"}
    ]
}
```

Default page size: 20. Max: 100.
