# TurboDRF API Reference

This document provides a comprehensive API reference for TurboDRF.

## Table of Contents

- [Core Components](#core-components)
  - [TurboDRFMixin](#turbodrfmixin)
  - [TurboDRFRouter](#turbodrfrouter)
  - [TurboDRFViewSet](#turbodrfviewset)
  - [TurboDRFPermission](#turbodrfpermission)
  - [TurboDRFSerializer](#turbodrfserializer)
- [Configuration](#configuration)
  - [Model Configuration](#model-configuration)
  - [Settings](#settings)
- [API Endpoints](#api-endpoints)
- [Query Parameters](#query-parameters)
- [Response Formats](#response-formats)
- [Error Handling](#error-handling)

## Core Components

### TurboDRFMixin

The core mixin that enables automatic API generation for Django models.

```python
from turbodrf import TurboDRFMixin

class MyModel(models.Model, TurboDRFMixin):
    # Model fields
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['field1', 'field2'],
            'endpoint': 'custom-endpoint',  # Optional
            'enabled': True  # Optional
        }
```

#### Methods

##### `turbodrf()`

**Type**: `@classmethod`

**Returns**: `dict`

Configuration method that defines how the model should be exposed in the API.

**Configuration Options**:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `fields` | `list`, `dict`, or `str` | `'__all__'` | Fields to expose in API |
| `endpoint` | `str` | `'{model_name}s'` | Custom endpoint name |
| `enabled` | `bool` | `True` | Whether to enable API for this model |

**Field Configuration Examples**:

```python
# Simple list (same fields for list and detail)
'fields': ['title', 'author', 'price']

# Different fields for list vs detail
'fields': {
    'list': ['title', 'author'],
    'detail': ['title', 'author', 'description', 'price']
}

# All fields
'fields': '__all__'

# Nested fields
'fields': ['title', 'author__name', 'category__parent__name']
```

##### `get_api_fields(view_type='list')`

**Type**: `@classmethod`

**Parameters**:
- `view_type` (str): Either 'list' or 'detail'

**Returns**: `list`

Get the fields for a specific view type based on the model's configuration.

##### `get_field_type(field_path)`

**Type**: `@classmethod`

**Parameters**:
- `field_path` (str): Field path using double-underscore notation

**Returns**: `Field` or `None`

Resolve field type for nested fields.

### TurboDRFRouter

Automatic router that discovers and registers all TurboDRF-enabled models.

```python
from turbodrf import TurboDRFRouter

router = TurboDRFRouter()
# No manual registration needed!
```

#### Methods

##### `__init__(*args, **kwargs)`

Initialize the router and automatically discover all models.

**Parameters**: Same as DRF's `DefaultRouter`

##### `discover_models()`

Automatically discover and register all models with TurboDRFMixin.

Called automatically during initialization.

### TurboDRFViewSet

Base ViewSet class for all auto-generated APIs.

#### Features

- Automatic serializer generation
- Built-in pagination
- Search, filter, and ordering support
- Query optimization
- Permission handling

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `model` | `Model` | The Django model class |
| `permission_classes` | `list` | `[TurboDRFPermission]` |
| `pagination_class` | `class` | `TurboDRFPagination` |
| `filter_backends` | `list` | DjangoFilter, Search, Ordering |
| `search_fields` | `list` | From model's `searchable_fields` |
| `ordering_fields` | `str` | `'__all__'` |
| `filterset_fields` | `str` | `'__all__'` |

### TurboDRFPermission

Role-based permission class.

#### Permission Format

```
app_label.model_name.action
app_label.model_name.field_name.read
app_label.model_name.field_name.write
```

#### Methods

##### `has_permission(request, view)`

Check if user has permission to perform the requested action.

**Returns**: `bool`

### TurboDRFSerializer

Base serializer with support for nested field notation.

#### Features

- Automatic handling of nested fields
- Dynamic field inclusion based on configuration
- Support for `field__subfield` notation

## Configuration

### Model Configuration

#### Basic Example

```python
class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Enable search
    searchable_fields = ['title', 'author__name']
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['title', 'author__name', 'price']
        }
```

#### Advanced Example

```python
class Product(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=200)
    description = models.TextField()
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    
    searchable_fields = ['name', 'description']
    
    @classmethod
    def turbodrf(cls):
        return {
            'endpoint': 'products',  # Custom endpoint
            'fields': {
                'list': ['name', 'category__name', 'price'],
                'detail': [
                    'name', 'description', 'price',
                    'category__name', 'category__description'
                ]
            }
        }
```

### Settings

Add to your Django settings:

```python
# Required apps
INSTALLED_APPS = [
    # ...
    'rest_framework',
    'django_filters',
    'drf_yasg',
    'turbodrf',
]

# Role-based permissions
TURBODRF_ROLES = {
    'admin': [
        # Model permissions
        'app.model.read',
        'app.model.create',
        'app.model.update',
        'app.model.delete',
        # Field permissions
        'app.model.field.read',
        'app.model.field.write',
    ],
    'editor': [
        'app.model.read',
        'app.model.update',
        'app.model.field.read',
    ],
    'viewer': [
        'app.model.read',
    ]
}
```

## API Endpoints

For each model, TurboDRF generates:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/{model}s/` | List all objects |
| POST | `/api/{model}s/` | Create new object |
| GET | `/api/{model}s/{id}/` | Retrieve specific object |
| PUT | `/api/{model}s/{id}/` | Update object (full) |
| PATCH | `/api/{model}s/{id}/` | Update object (partial) |
| DELETE | `/api/{model}s/{id}/` | Delete object |
| OPTIONS | `/api/{model}s/` | Get metadata |

## Query Parameters

### Pagination

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | int | 1 | Page number |
| `page_size` | int | 20 | Items per page (max 100) |

**Example**: `/api/books/?page=2&page_size=50`

### Search

| Parameter | Type | Description |
|-----------|------|-------------|
| `search` | string | Search across `searchable_fields` |

**Example**: `/api/books/?search=django`

### Filtering

Use Django's field lookups:

```
/api/books/?price__gte=10&price__lte=50
/api/books/?author__name__icontains=smith
/api/books/?published_date__year=2023
/api/books/?is_active=true
```

### Ordering

| Parameter | Type | Description |
|-----------|------|-------------|
| `ordering` | string | Field(s) to order by |

**Examples**:
- `/api/books/?ordering=price` (ascending)
- `/api/books/?ordering=-price` (descending)
- `/api/books/?ordering=-price,title` (multiple fields)

## Response Formats

### List Response

```json
{
    "pagination": {
        "next": "http://example.com/api/books/?page=2",
        "previous": null,
        "current_page": 1,
        "total_pages": 5,
        "total_items": 100
    },
    "data": [
        {
            "id": 1,
            "title": "Django for Beginners",
            "author_name": "John Smith",
            "price": "29.99"
        }
        // ... more items
    ]
}
```

### Detail Response

```json
{
    "id": 1,
    "title": "Django for Beginners",
    "description": "Learn Django step by step",
    "author_name": "John Smith",
    "author_email": "john@example.com",
    "price": "29.99",
    "created_at": "2024-01-01T00:00:00Z"
}
```

### Error Response

```json
{
    "detail": "Not found."
}
```

```json
{
    "field_name": [
        "This field is required."
    ]
}
```

## Error Handling

### HTTP Status Codes

| Code | Description | When Used |
|------|-------------|-----------|
| 200 | OK | Successful GET, PUT, PATCH |
| 201 | Created | Successful POST |
| 204 | No Content | Successful DELETE |
| 400 | Bad Request | Invalid data |
| 401 | Unauthorized | No authentication |
| 403 | Forbidden | No permission |
| 404 | Not Found | Object doesn't exist |
| 405 | Method Not Allowed | Invalid HTTP method |
| 500 | Server Error | Internal error |

### Common Errors

#### Permission Denied

```json
{
    "detail": "You do not have permission to perform this action."
}
```

**Solution**: Check user roles and permissions in `TURBODRF_ROLES`.

#### Field Not Found

```json
{
    "detail": "Field 'invalid_field' does not exist on model 'Book'."
}
```

**Solution**: Check field names in model's `turbodrf()` configuration.

#### Invalid Filter

```json
{
    "detail": "Invalid filter: 'invalid_lookup'."
}
```

**Solution**: Use valid Django field lookups.

## Advanced Usage

### Custom ViewSet Methods

```python
from turbodrf import TurboDRFViewSet
from rest_framework.decorators import action
from rest_framework.response import Response

class CustomBookViewSet(TurboDRFViewSet):
    model = Book
    
    @action(detail=True, methods=['post'])
    def set_favorite(self, request, pk=None):
        book = self.get_object()
        # Custom logic
        return Response({'status': 'favorite set'})
```

### Extending Serializers

```python
from turbodrf import TurboDRFSerializer

class CustomBookSerializer(TurboDRFSerializer):
    # Add custom fields or methods
    
    class Meta:
        model = Book
        fields = ['title', 'author', 'custom_field']
```

### Performance Optimization

TurboDRF automatically optimizes queries, but you can enhance it:

```python
class Book(models.Model, TurboDRFMixin):
    # Model fields
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['title', 'author__name'],  # Minimal fields
                'detail': '__all__'  # All fields in detail
            }
        }
```

This reduces data transfer in list views while providing complete information in detail views.