# Configuration

## The `turbodrf()` method

Every TurboDRF model needs a `turbodrf()` classmethod that returns a config dict:

```python
@classmethod
def turbodrf(cls):
    return {
        'enabled': True,              # Enable/disable API (default: True)
        'endpoint': 'books',          # URL segment (default: lowercase model name + 's')
        'fields': ['title', 'price'], # Fields to expose (default: '__all__'; see below)
        'public_access': False,       # Allow unauthenticated GET (default: False)
        'lookup_field': 'slug',       # URL lookup field (default: pk)
        'compiled': True,             # Use compiled read path (default: True)
        'searchable_fields': [        # Fields enabled for ?search= (default: none)
            'title', 'author__name',
        ],
        'read_only': False,           # Serve list/retrieve (+ declared actions) only; CRUD writes return 405 (default: False)
        'http_methods': ['get', 'post'],  # Explicit HTTP method allow-list (default: all)
        'full_clean': False,          # Run model.clean()/constraints on write (default: False)
        'actions': [],                # Custom endpoints (see below)
    }
```

Row-level access keys (`tenant_field`, `owner_field`, `bypass_owner_roles`,
`visibility`, `tenancy`) are covered in [Tenancy & row-level access](tenancy.md).

- The default endpoint adds an `s` to the lowercase model name, without
  pluralizing: `Category` is served at `/api/categorys/`. Set `endpoint` for
  anything else.
- `enabled: False` removes the endpoint. The model's tenancy is still
  registered, so declare it as for any other model (see
  [Tenancy](tenancy.md#hard-fail-at-startup)).

The router checks each enabled model's config at startup and raises
`ImproperlyConfigured` on:

- an unknown key, such as a misspelt `'owner_feild'` (the error lists the
  valid keys; `sensitive_fields` is a class attribute on the model, not a
  key);
- a `fields` entry that isn't a model field, a `@property` or a `__` path
  through relations, or one that ends on a reverse one-to-one (list a field
  of the related model instead, e.g. `'profile__bio'`);
- a `lookup_field` that isn't a unique column of the model.

## Fields

Always set `fields` explicitly. It defaults to `'__all__'`, which exposes
every concrete and many-to-many field, including ones added to the model
later. Three formats:

```python
# All concrete and many-to-many fields
'fields': '__all__'

# Specific fields (same for list and detail)
'fields': ['title', 'author__name', 'price']

# Different fields for list vs detail views
'fields': {
    'list': ['title', 'author__name', 'price'],
    'detail': ['title', 'description', 'author__name', 'author__email', 'price']
}
```

A dict with only `list` or only `detail` uses that list for both views. In every format, names on the sensitive deny-list are dropped (see [Security](security.md#sensitive-fields)), and each caller gets only the fields they can read. The primary key renders only if it's listed.

A create that leaves out a required column the caller can't set (one missing from `fields`, or one their role can't write) is a 400 naming that field.

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

FK fields are flattened in responses (`author__name` becomes `author_name`), and the FK itself renders as its raw id. M2M fields are arrays of objects keyed by the path below the M2M (`tags__group__name` gives `{"group__name": ...}`), in the target's `Meta.ordering`:

```json
{
    "title": "Django for APIs",
    "author": 12,
    "author_name": "William Vincent",
    "tags": [{"name": "Python"}, {"name": "Django"}]
}
```

An M2M listed on its own (`'tags'`) renders the pks of the targets the caller can see. A reverse relation (`'items'`, `'items__label'`) renders like an M2M, scoped the same way, and is read-only. Nested and M2M values are formatted by the same DRF field a top-level field of that type uses (a Decimal renders as a string by default, `DATETIME_FORMAT` applies, files render as absolute URLs). A path ending on an FK renders the value the parent stores: the related pk, or the `to_field` value.

A nested value renders only when the caller can see the related row through that model's own endpoint; otherwise it's `null`, and M2M targets they can't see are left out. See [Security](security.md#nested-fields-across-scoped-relations).

Maximum nesting depth is 3 by default (`a__b__c__d`). Change with `TURBODRF_MAX_NESTING_DEPTH` in settings. Deeper fields are skipped with a logged warning.

### Client field selection

Clients can narrow a list or detail response with `?fields=`:

```
GET /api/books/?fields=title,price
GET /api/books/1/?fields=title,author.name
GET /api/books/?fields=author          # the FK and all its configured nested values
```

Names are output keys; dot (`author.name`) or underscore (`author_name`) notation both work. An FK name selects every configured nested value under it, an M2M name selects the whole array, and selecting a nested value also renders its FK's raw id. Only fields configured in `turbodrf()` are available: unknown names are ignored, and if none match, the full field set is returned. Permission filtering still applies.

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

Both paths render the same property keys: a property is readable by any role that can read the model (it can't carry its own field-level rule), and sensitive-named properties are dropped.

### The property sandbox

A property is computed on a **sandbox** copy of the row, the same way on both paths:

- Only the columns the caller may read are loaded. Reading any other column (a field a role has no read rule for, a `sensitive_fields` entry, `password`) would need a query, so it fails. A column a model hook loads while the row is built (a `post_init` handler, `__init__`) is dropped again before the property runs.
- Related objects are available only if the model declares them in `property_relations`. Each hop is loaded within the caller's scope (the target's own `tenant_field` and predicates, as for `author__name`) and with only the columns the caller may read on that model. A FK whose target is out of scope reads as `None`; out-of-scope rows are missing from a reverse or M2M relation. A hop is loaded only if the caller can read the relation, has model-level read on its target model, and can read the column it joins on; otherwise a property that follows it fails.
- The property can't query the database. `Book.objects.count()`, `self.tags.filter(...)` and `self.tags.values_list(...)` all fail; `self.tags.all()` on a declared relation works.
- The result becomes plain data (dicts, lists, scalars) before the sandbox closes. A property that returns a model instance, a manager or a queryset fails: return the values instead (`[t.name for t in self.tags.all()]`).

A property that fails is left out of the response and logged (a warning the first time, then at debug level). The page's rows are loaded in one query, plus one per declared relation hop.

```python
class Holding(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=100)
    portfolio = models.ForeignKey(Portfolio, null=True, on_delete=models.SET_NULL)

    @property
    def label(self):
        portfolio = self.portfolio
        return f"{self.name} ({portfolio.name})" if portfolio else self.name

    @property
    def valuation_dates(self):
        return [v.date for v in self.valuations.all()]   # reverse FK

    @classmethod
    def turbodrf(cls):
        return {
            'tenant_field': 'brokerage',
            'fields': ['name', 'label', 'valuation_dates'],
            'property_relations': {
                'label': ['portfolio'],             # or 'portfolio__name'
                'valuation_dates': ['valuations'],
            },
        }
```

A path may end on a plain field (`portfolio__name`), and may be as deep as `TURBODRF_MAX_NESTING_DEPTH`. Forward and reverse FKs, one-to-ones and M2Ms can be loaded; a generic relation can't.

`unscoped_properties` opts a property out: it runs on the full instance, with every column and unrestricted queries, whatever the caller's tenant, predicates or field rules. Use it only for properties derived from data every reader of the model may see:

```python
'unscoped_properties': ['exchange_rate'],   # reads shared reference data
```

A nested property path (`author__display_name`) isn't compiled: a model that lists one is served by the serializer path (`turbodrf_check` says why).

## Searchable fields

List the fields available for `?search=` either in the config dict (preferred) or as a `searchable_fields` class attribute. The config key wins whenever it's present, even as an empty list:

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

Entries may be plain field names or `__`-paths ending on a column, optionally with a `^`, `=` or `@` prefix. Search is gated by field-level read permissions -- a caller who cannot read a field cannot search by it. See [Filtering & Search](filtering.md#search) for details.

## Read-only endpoints

Set `read_only: True` to serve only `list` / `retrieve`. CRUD writes (POST/PUT/PATCH/DELETE) return `405 Method Not Allowed`. Custom [`actions`](#custom-endpoints-actions) keep their own methods, so a read-only resource can still expose e.g. a `POST .../reprocess/` action. Good for audit logs and reference data:

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

Values that `clean()` normalises (e.g. lower-casing an email) are saved for the fields the request provided. Fields that `clean()` sets but the request didn't send are not saved -- derive those in `save()`.

## Custom endpoints (`actions`)

Add custom verbs to a model's generated viewset with `actions` -- a list of handler functions each decorated with `@turbodrf_action(...)`. Because the router attaches them to the same viewset as the CRUD routes, they inherit `get_object()` / `get_queryset()`, so they get the **same tenant + predicate scoping** as everything else. You don't re-implement access control by hand.

`turbodrf_action` mirrors DRF's `@action`:

```python
turbodrf_action(detail=False, methods=None, url_path=None, url_name=None, **kwargs)
```

- `detail=True` → operates on a single object (`/api/<endpoint>/{pk}/<url_path>/`)
- `detail=False` (the default) → operates on the collection (`/api/<endpoint>/<url_path>/`)
- `methods` defaults to `["get"]`
- `url_path` / `url_name` default to the handler's function name
- Other keyword arguments are DRF's per-action view attributes (`permission_classes`, `throttle_classes`, ...). `serializer_class`, `queryset` and any other argument TurboDRF would ignore raise `TypeError`: TurboDRF builds the action's serializer and queryset from the model's config.

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

> **Note:** the scoping is inherited only because the handler goes through `self.get_object()` / `self.get_queryset()`. If you query the model directly (`self.model.objects.get(...)`), you bypass tenant and predicate filtering and must apply it yourself. Generated viewsets have no `queryset` attribute: `self.queryset` is `None`.

`self.get_serializer()` in an action returns the same permission-filtered serializer the CRUD routes use: it renders only the configured detail fields the caller can read and accepts only the ones they can write.

By default an action uses TurboDRF's role check, mapped from the HTTP method (`GET` → `read`, `POST` → `create`, ...). Pass `permission_classes` to replace it for that action, as with DRF's `@action`:

```python
from rest_framework.permissions import IsAuthenticated

@turbodrf_action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
def reprocess(self, request, pk=None):
    obj = self.get_object()          # still tenant + predicate scoped
    ...
```

The declared classes are the action's whole permission check: they apply even with `TURBODRF_DISABLE_PERMISSIONS`, and the role check doesn't run. Row scoping is unaffected -- `get_object()` / `get_queryset()` still apply the tenant boundary and predicates.

A hand-written `TurboDRFViewSet` subclass gets its `model`'s tenant boundary and predicates from the model's registration, as a generated viewset does, and serves no rows if they can't be resolved.
