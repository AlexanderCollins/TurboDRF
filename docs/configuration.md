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
    }
```

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

## Property fields

Model `@property` methods work in the compiled path:

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

Properties that access related objects (e.g. `self.author.name`) won't work in the compiled path -- use `author__name` in the field config instead.
