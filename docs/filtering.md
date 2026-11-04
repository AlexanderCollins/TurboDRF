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

The config key wins whenever it's present: `'searchable_fields': []` turns search off, whatever the class attribute says.

```
GET /api/books/?search=django
```

Each entry is a column or a `__`-path that ends on one (`author__name`). It may start with one of DRF's prefixes: `^` (starts with), `=` (exact match) or `@` (full-text search, Postgres). `$` (regex) isn't accepted. An entry that doesn't resolve, ends on a relation or starts with `$` raises `ImproperlyConfigured` at startup.

- A caller who can't read a field can't search by it, and sensitive fields are never searched.
- A `__`-path term only matches related rows the caller can see through that model's own endpoint (its tenant and predicates).
- A search with more than 20 distinct terms, or whose terms together are longer than `TURBODRF_MAX_FILTER_VALUE_LENGTH` (default 200), returns nothing.

## Filtering

The fields in the model's `fields` config (plus the primary key) are filterable with Django lookups; with role-based permissions, only those the caller can read:

```
GET /api/books/?author=1                           # Exact match
GET /api/books/?price__gte=10&price__lte=50        # Range
GET /api/books/?title__icontains=python            # Text search
GET /api/books/?published_date__year=2024          # Date
GET /api/books/?author__name__istartswith=smith    # Related field: 'author__name' must be in fields
```

- A nested filter needs its own path in `fields` (list or detail). Configuring `author__name` makes `?author__name=` filterable, not every column of the author.
- Filters on any other field are ignored (and logged), so a hidden column can't be probed through filters.
- A value the field can't hold is a 400 (`?price__gte=abc`). An empty value (`?price=`) is no filter.
- `__in` takes comma-separated values, `__range` exactly two (`?price__range=10,50`), `__isnull` true or false. `__isnull` filters the model's own fields only (an FK included): through a relation (`?author__name__isnull=`) it's ignored, since it would tell a related row the caller can't see from none.
- A `__year` outside 1–9999, or a value with a NUL character, is a 400.
- `__regex` / `__iregex` lookups aren't accepted.
- An M2M filters by one target pk (`?tags=3`); `exact` is its only lookup. `?tags__in=`, `?tags__isnull=` and `?tags_or=` are ignored. A filter across an M2M or reverse relation (`?tags__name=`) matches each row once.
- A related-field filter is permission-checked at every hop (a reverse relation by the related model's FK, see [Security](security.md#reverse-relations)) and only matches related rows the caller can see through that model's own endpoint. A related field named like a lookup (`folder__date`, where `date` is a field of the folder) is treated as that field.
- An FK or M2M filter naming a row the caller can't see returns an empty `200`, exactly as a pk that doesn't exist does. When another filter in the request is invalid, the 400 lists only that filter's error.
- A filter value longer than `TURBODRF_MAX_FILTER_VALUE_LENGTH` (default 200), text filters (`__contains`, `__icontains`, ...) longer than that together, an `_or` filter with more than 20 values, or a parameter repeated more than 20 times, returns nothing.
- JSON containment (`?data__contains=`) is ignored on a database without it (SQLite).

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

`_or` on an M2M or reverse relation is ignored.

## Ordering

```
GET /api/books/?ordering=price         # Ascending
GET /api/books/?ordering=-price        # Descending
GET /api/books/?ordering=author        # By the stored author id
```

`?ordering=` takes the filterable fields that are concrete, single-valued columns. An M2M, a reverse relation, a `@property`, a nested path (`author__name`) or a sensitive field is ignored. An FK orders by its stored column, not by the related model's `Meta.ordering`. The primary key is appended as a tie-breaker, in the direction of the last term, so rows don't repeat or go missing across pages.

Without `?ordering=`, a list follows the model's `Meta.ordering` only if the caller could pass each of its terms as `?ordering=`; otherwise it comes in primary-key order. An M2M or reverse array follows its target's `Meta.ordering` only when the array shows those columns and the caller can read them.

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

The browsable API shows no filter form: its choice lists would name rows of
the related models (another tenant's users included). Filters work the same
from the URL.

To use your own pagination class, and so your API's own response envelope,
set `TURBODRF_PAGINATION_CLASS` to it (a dotted path or the class). A
`pagination_class` set on a `TurboDRFViewSet` subclass or a custom action is
kept. Give your class a `max_page_size`: without one a client can ask for
every row in one page. A cursor class (`CursorPagination`) orders by its
`ordering` only for a caller who may order by that column, since its links
carry the column's values; anyone else's pages go in pk order.
