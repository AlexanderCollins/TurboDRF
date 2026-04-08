# Filtering & Search

## Search

Define searchable fields on the model:

```python
class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    description = models.TextField()

    searchable_fields = ['title', 'description']
```

```
GET /api/books/?search=django
```

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
