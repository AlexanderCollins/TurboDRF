# Configuration

## The `turbodrf()` method

Every TurboDRF model needs a `turbodrf()` classmethod that returns a config dict:

```python
@classmethod
def turbodrf(cls):
    return {
        'enabled': True,              # Enable/disable API (default: True)
        'endpoint': 'books',          # Custom endpoint name (default: pluralized model name)
        'fields': '__all__',          # Fields to expose (see below)
        'public_access': False,       # Allow unauthenticated GET (default: False)
        'lookup_field': 'slug',       # URL lookup field (default: pk)
        'compiled': True,             # Use compiled read path (default: True)
        'searchable_fields': [        # Fields enabled for ?search= (default: none)
            'title', 'author__name',
        ],
        'read_only': False,           # Serve list/retrieve only; writes return 405 (default: False)
        'http_methods': ['get', 'post'],  # Explicit HTTP method allow-list (default: all)
        'full_clean': False,          # Run model.clean()/constraints on write (default: False)
        'actions': [],                # Custom endpoints (see below)
    }
```

Row-level access keys (`tenant_field`, `owner_field`, `bypass_owner_roles`,
`visibility`, `tenancy`) are covered in [Tenancy & row-level access](tenancy.md).

## Fields

Three formats:

```python
# All database fields
'fields': '__all__'

# Specific fields (same for list and detail)
'fields': ['title', 'author__name', 'price']

# Different fields for list vs detail views
'fields': {
    'list': ['title', 'author__name', 'price'],
    'detail': ['title', 'description', 'author__name', 'author__email', 'price']
}
```

### Nested fields

Access related model fields with `__` notation:

```python
'fields': [
    'title',
    'author__name',              # ForeignKey (1 level)
    'author__publisher__name',   # Multi-level (2 levels)
    'tags__name',                # ManyToMany
]
```

FK fields are flattened in responses (`author__name` becomes `author_name`). M2M fields are arrays of objects:

```json
{
    "title": "Django for APIs",
    "author_name": "William Vincent",
    "tags": [{"name": "Python"}, {"name": "Django"}]
}
```

Maximum nesting depth is 3 by default. Change with `TURBODRF_MAX_NESTING_DEPTH` in settings.

### Client field selection

Clients can request specific fields via `?fields=`:

```
GET /api/books/?fields=title,price
GET /api/books/?fields=title,author.name
```

Only fields configured in `turbodrf()` are available. Dot or underscore notation both work. Permission filtering still applies.

## Property / computed read fields

A model `@property` (or a zero-argument method) can be listed in `fields`. It renders **read-only** on both read paths -- the compiled `.values()` path and the DRF serializer path -- and is ignored on writes:

```python
class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    @property
    def display_title(self):
        return self.title.upper()

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['title', 'price', 'display_title']
        }
```

`display_title` appears in every response alongside the real fields.

On the **compiled path** a property can only reference columns fetched into the row (e.g. `self.title`), not traverse relationships. For related data, use `__` field notation (`author__name`) instead of a property that does `self.author.name`.

## Searchable fields

List the fields available for `?search=` either in the config dict (preferred) or as a `searchable_fields` class attribute:

```python
class Book(models.Model, TurboDRFMixin):
    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['title', 'author__name'],
            'searchable_fields': ['title', 'author__name'],
        }
```

```
GET /api/books/?search=django
```

Entries may be plain field names or `__`-paths. Search is gated by field-level read permissions -- a caller who cannot read a field cannot search by it -- and the list is validated at startup. See [Filtering & Search](filtering.md#search) for details.

## Read-only endpoints

Set `read_only: True` to serve only `list` / `retrieve`. Writes (POST/PUT/PATCH/DELETE) return `405 Method Not Allowed`. Good for audit logs and reference data:

```python
class AuditLog(models.Model, TurboDRFMixin):
    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['id', 'action', 'actor', 'created_at'],
            'read_only': True,
        }
```

## Restricting HTTP methods

For finer control, `http_methods` is an explicit allow-list of HTTP verbs. Disallowed methods return `405`. (`read_only` is the common shorthand for a GET-only endpoint.)

```python
@classmethod
def turbodrf(cls):
    return {
        'fields': ['id', 'email'],
        'http_methods': ['get', 'post'],   # list, retrieve, create — no update/delete
    }
```

`head` and `options` are always allowed and don't need to be listed. Method names are case-insensitive.

## Model validation (`full_clean`)

By default TurboDRF runs DRF's field-level validation only. Set `full_clean: True` to also run Django model validation (`Model.clean()` and model-level constraints) during serializer validation. Business-rule violations then surface as clean `400` responses instead of leaking through as `500`s from the database layer:

```python
from django.core.exceptions import ValidationError

class Booking(models.Model, TurboDRFMixin):
    start = models.DateField()
    end = models.DateField()

    def clean(self):
        if self.end < self.start:
            raise ValidationError({'end': 'End date must be after start date.'})

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['start', 'end'],
            'full_clean': True,
        }
```

A `POST` with `end` before `start` returns:

```json
{ "end": ["End date must be after start date."] }
```

`full_clean` is opt-in and off by default. On partial updates (`PATCH`), only the fields actually provided are validated. Uniqueness is not re-checked here (DRF already handles it), so it won't double-report unique errors.

## Custom endpoints (`actions`)

Add custom verbs to a model's generated viewset with `actions` -- a list of handler functions each decorated with `@turbodrf_action(...)`. Because the router attaches them to the same viewset as the CRUD routes, they inherit `get_object()` / `get_queryset()`, so they get the **same tenant + predicate scoping** as everything else. You don't re-implement access control by hand.

`turbodrf_action` mirrors DRF's `@action`:

```python
turbodrf_action(detail=False, methods=None, url_path=None, url_name=None, **kwargs)
```

- `detail=True` → operates on a single object (`/api/<endpoint>/{pk}/<url_path>/`)
- `detail=False` → operates on the collection (`/api/<endpoint>/<url_path>/`)
- `methods` defaults to `["get"]`
- `url_path` / `url_name` default to the handler's function name

```python
from rest_framework.response import Response
from turbodrf import turbodrf_action

@turbodrf_action(detail=True, methods=['post'], url_path='resend')
def resend(self, request, pk=None):
    obj = self.get_object()          # already tenant + predicate scoped
    obj.resend()
    return Response({'status': 'sent'})

class Invite(models.Model, TurboDRFMixin):
    email = models.EmailField()

    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['id', 'email'],
            'actions': [resend],
        }
```

This exposes `POST /api/invites/{pk}/resend/`. Because the handler calls `self.get_object()`, a caller can only resend an invite they can already see -- cross-tenant PKs 404 exactly like a normal detail GET.

A collection action looks the same with `detail=False` and no `pk`:

```python
@turbodrf_action(detail=False, methods=['get'], url_path='pending')
def pending(self, request):
    qs = self.get_queryset().filter(status='pending')   # already scoped
    data = [{'id': o.id, 'email': o.email} for o in qs]
    return Response(data)
```

> **Note:** the scoping is inherited only because the handler goes through `self.get_object()` / `self.get_queryset()`. If you query the model directly (`self.model.objects.get(...)`), you bypass tenant and predicate filtering and must apply it yourself.
