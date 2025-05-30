

# 🚀 TurboDRF

### DISCLAIMER: TurboDRF is a new project as of 29th May 2025.

<div align="center">

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![Django Version](https://img.shields.io/badge/django-3.2%2B-green)](https://www.djangoproject.com/)
[![DRF Version](https://img.shields.io/badge/djangorestframework-3.12%2B-red)](https://www.django-rest-framework.org/)
[![License](https://img.shields.io/badge/license-MIT-purple)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/alexandercollins/turbodrf/tests.yml?branch=main&label=tests)](https://github.com/alexandercollins/turbodrf/actions)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/alexandercollins/turbodrf)
[![PyPI Version](https://img.shields.io/pypi/v/turbodrf?label=pypi)](https://pypi.org/project/turbodrf/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/alexandercollins/turbodrf/pulls)
[![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**The dead simple Django REST Framework API generator with role-based permissions**

🤖 *This project was structured by [Claude](https://claude.ai), Anthropic's AI assistant which helped with getting the project to a state where it could be shared - the core design however was not AI generated.*

Transform your Django models into fully-featured REST APIs with just a mixin and a method. Zero boilerplate, maximum power.

[Features](#-features) • [Quick Start](#-quick-start) • [Documentation](#-documentation) • [Examples](#-examples) • [Contributing](#-contributing)

</div>

---


## 🎯 Why TurboDRF?

Building REST APIs in Django shouldn't require writing hundreds of lines of boilerplate code. **TurboDRF** revolutionizes Django API development by automatically generating REST APIs from your models with minimal configuration.

### 🚫 The Problem

Traditional Django REST Framework development requires:
- Writing serializers for every model
- Creating ViewSets with repetitive CRUD logic  
- Configuring routers and URL patterns
- Implementing permission classes
- Setting up filters, search, and pagination
- Managing field-level permissions manually

### ✨ The TurboDRF Solution

```python
# This is all you need 👇
class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['title', 'author__name', 'price']
        }

# 💥 Boom! You now have a complete REST API with:
# ✅ All CRUD endpoints     ✅ Smart pagination      ✅ Advanced filtering
# ✅ Full-text search       ✅ Multi-field ordering  ✅ Role-based permissions
# ✅ Nested relationships   ✅ API documentation     ✅ Field-level security
```


## 🚀 Quick Start

### 1. Install TurboDRF

```bash
pip install turbodrf
```

Or with poetry:
```bash
poetry add turbodrf
```

### 2. Add to INSTALLED_APPS

```python
INSTALLED_APPS = [
    # ... your apps
    'rest_framework',
    'drf_yasg',  # for swagger docs
    'turbodrf',
]
```

### 3. Add TurboDRF to Your Model

```python
from django.db import models
from turbodrf.mixins import TurboDRFMixin

class Author(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['name', 'email']
        }

class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    isbn = models.CharField(max_length=13)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Optional: specify searchable fields
    searchable_fields = ['title', 'isbn']
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['title', 'author__name', 'price'],
                'detail': ['title', 'author__name', 'author__email', 'isbn', 'price']
            }
        }
```

### 4. Configure URLs

```python
from django.urls import path, include
from turbodrf.router import TurboDRFRouter

router = TurboDRFRouter()

urlpatterns = [
    path('api/', include(router.urls)),
]
```

### 5. 🎉 That's It! Your API is Ready!

```bash
# List all books
GET /api/books/

# Get specific book
GET /api/books/1/

# Create a book
POST /api/books/

# Update a book  
PUT /api/books/1/

# Search books
GET /api/books/?search=django

# Filter books
GET /api/books/?author__name=Rowling&price__lt=20

# Order books
GET /api/books/?ordering=-price,title

# Paginate results
GET /api/books/?page=2&page_size=10
```

## 📖 Documentation

### 🔧 Model Configuration

The `turbodrf()` classmethod is where the magic happens:

```python
@classmethod
def turbodrf(cls):
    return {
        # Enable/disable API for this model
        'enabled': True,
        
        # Custom endpoint name (default: pluralized model name)
        'endpoint': 'books',
        
        # Simple field list (same for list and detail)
        'fields': ['title', 'author', 'isbn'],
        
        # OR different fields for list vs detail views
        'fields': {
            'list': ['title', 'author__name'],
            'detail': ['title', 'author__name', 'author__email', 'isbn', 'price']
        },
        
        # OR use all fields
        'fields': '__all__'
    }
```

### 🔐 Permissions System

TurboDRF offers two permission modes: Django's default permissions or TurboDRF's advanced role-based permissions.

#### Default Django Permissions (Simple Mode)

Use Django's built-in permission system - perfect for simple use cases:

```python
# settings.py
TURBODRF_USE_DEFAULT_PERMISSIONS = True  # Enable Django's default permissions
```

With default permissions:
- Uses Django's standard `add`, `change`, `delete`, `view` permissions
- Works with Django Admin permissions out of the box
- When a user has write permission for a model, they can write ALL fields
- No field-level permissions (simpler but less granular)

```python
# Grant permissions via Django Admin or programmatically:
from django.contrib.auth.models import User, Permission

user = User.objects.get(username='editor')
permission = Permission.objects.get(codename='change_book')
user.user_permissions.add(permission)

# Or use groups
from django.contrib.auth.models import Group

editors = Group.objects.create(name='Editors')
editors.permissions.add(
    Permission.objects.get(codename='view_book'),
    Permission.objects.get(codename='change_book')
)
user.groups.add(editors)
```

#### TurboDRF Role-Based Permissions (Advanced Mode)

For fine-grained control with field-level permissions:

```python
# settings.py
TURBODRF_USE_DEFAULT_PERMISSIONS = False  # Default - uses TurboDRF permissions
```

Define permissions in your settings:

```python
# settings.py
TURBODRF_ROLES = {
    'admin': [
        # Model-level permissions
        'books.book.read',      # Can view books
        'books.book.create',    # Can create books
        'books.book.update',    # Can update books
        'books.book.delete',    # Can delete books
        
        # Field-level permissions
        'books.book.title.read',     # Can see title field
        'books.book.title.write',    # Can edit title field
        'books.book.price.read',     # Can see price
        'books.book.price.write',    # Can edit price
    ],
    
    'editor': [
        'books.book.read',
        'books.book.update',
        'books.book.title.read',
        'books.book.title.write',
        # Note: no price.write - editors can see but not change prices
        'books.book.price.read',
    ],
    
    'viewer': [
        'books.book.read',
        'books.book.title.read',
        # Note: no price permissions - viewers can't see prices at all
    ]
}
```

#### Future: Dynamic Database Permissions

While currently out of scope, TurboDRF's permission system is designed to support dynamic permissions stored in the database. This would enable:

- Runtime permission changes without code deployment
- User-specific permission overrides
- Permission templates and inheritance
- API-driven permission management

The current static configuration provides excellent performance and simplicity while laying the groundwork for future dynamic permissions.

### 👤 User Roles Setup (for TurboDRF permissions)

Add roles to your User model:

```python
# Option 1: Extend existing User model
from django.contrib.auth import get_user_model

User = get_user_model()

def get_user_roles(self):
    # Example: use Django groups as roles
    return [group.name for group in self.groups.all()]

User.add_to_class('roles', property(get_user_roles))

# Option 2: Custom User model
class User(AbstractUser):
    user_roles = models.JSONField(default=list)
    
    @property
    def roles(self):
        return self.user_roles
```

### 🔍 Searching and Filtering

```python
# Define searchable fields in your model
class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    description = models.TextField()
    
    searchable_fields = ['title', 'description']
```

Now you can search:
```bash
# Simple search across all searchable fields
GET /api/books/?search=python

# Response
{
    "pagination": {
        "next": null,
        "previous": null,
        "current_page": 1,
        "total_pages": 1,
        "total_items": 3
    },
    "data": [
        {
            "id": 1,
            "title": "Python Crash Course",
            "author": 2,
            "author_name": "Eric Matthes",
            "price": "39.99",
            "published_date": "2023-05-01"
        },
        {
            "id": 3,
            "title": "Fluent Python",
            "author": 5,
            "author_name": "Luciano Ramalho",
            "price": "59.99",
            "published_date": "2022-03-15"
        }
    ]
}

# Complex filtering
GET /api/books/?price__gte=10&price__lte=50
GET /api/books/?author__name__icontains=smith
GET /api/books/?published_date__year=2023
```

### 📄 Pagination

Built-in pagination with customizable page size:

```bash
# Default pagination
GET /api/books/?page=2

# Custom page size
GET /api/books/?page=1&page_size=50

# Response format
{
    "pagination": {
        "next": "http://api.example.com/api/books/?page=3",
        "previous": "http://api.example.com/api/books/?page=1",
        "current_page": 2,
        "total_pages": 10,
        "total_items": 193
    },
    "data": [...]
}
```

### 🎯 Field Metadata

Use OPTIONS requests to discover available fields:

```bash
OPTIONS /api/books/

# Response
{
    "name": "Book",
    "model": {
        "name": "Book",
        "app_label": "books",
        "fields": {
            "title": {
                "type": "CharField",
                "required": true,
                "read_only": false,
                "max_length": 200
            },
            "author": {
                "type": "ForeignKey",
                "required": true,
                "read_only": false
            },
            "price": {
                "type": "DecimalField",
                "required": true,
                "read_only": true  # Based on user permissions
            }
        }
    },
    "actions": {
        "list": true,
        "retrieve": true,
        "create": true,
        "update": true,
        "partial_update": true,
        "destroy": false  # Based on user permissions
    }
}
```

## 🎨 Advanced Usage

### 🔗 Nested Relationships

Access related fields using double underscore notation:

```python
@classmethod
def turbodrf(cls):
    return {
        'fields': [
            'title',
            'author__name',           # ForeignKey relation
            'author__email',          # Going deeper
            'category__parent__name', # Multiple levels
            'tags__name',            # Many-to-many
        ]
    }
```

### 🎭 Custom ViewSet Behavior

Extend the auto-generated ViewSet:

```python
from turbodrf.views import TurboDRFViewSet
from rest_framework.decorators import action
from rest_framework.response import Response

class CustomBookViewSet(TurboDRFViewSet):
    model = Book
    
    @action(detail=True, methods=['post'])
    def set_featured(self, request, pk=None):
        book = self.get_object()
        book.is_featured = True
        book.save()
        return Response({'status': 'featured'})
    
    @action(detail=False)
    def trending(self, request):
        queryset = self.get_queryset().filter(
            views__gte=1000
        ).order_by('-views')
        
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

# Register custom viewset
router = TurboDRFRouter()
router.register('books', CustomBookViewSet, basename='book')
```

### 🔍 Custom Querysets (User-based Filtering)

Filter data based on the current user or other request context:

#### Method 1: Override get_queryset in ViewSet

```python
class UserFilteredBookViewSet(TurboDRFViewSet):
    model = Book
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_authenticated:
            # Users only see their own books
            return queryset.filter(owner=user)
        else:
            # Anonymous users only see public books
            return queryset.filter(is_public=True)
```

#### Method 2: Custom Manager with Request Context

```python
class BookManager(models.Manager):
    def for_user(self, user):
        """Filter books based on user permissions."""
        if user.is_superuser:
            return self.all()
        elif user.is_authenticated:
            # Users see their books + public books
            return self.filter(
                models.Q(owner=user) | models.Q(is_public=True)
            )
        else:
            return self.filter(is_public=True)

class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    is_public = models.BooleanField(default=False)
    
    objects = BookManager()
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['title', 'owner__username', 'is_public'],
            # Custom queryset method (optional)
            'get_queryset': lambda viewset: cls.objects.for_user(viewset.request.user)
        }
```

#### Method 3: Dynamic Queryset in turbodrf() Configuration

```python
class Organization(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=100)
    members = models.ManyToManyField(User, through='Membership')
    
    @classmethod
    def turbodrf(cls):
        def get_user_orgs(viewset):
            """Users only see organizations they belong to."""
            user = viewset.request.user
            if user.is_authenticated:
                return cls.objects.filter(members=user)
            return cls.objects.none()
        
        return {
            'fields': ['name', 'created_at'],
            'get_queryset': get_user_orgs
        }
```

#### Method 4: Row-Level Permissions

Combine with Django Guardian or similar for object-level permissions:

```python
from guardian.shortcuts import get_objects_for_user

class DocumentViewSet(TurboDRFViewSet):
    model = Document
    
    def get_queryset(self):
        queryset = super().get_queryset()
        # Only return documents the user has 'view' permission for
        return get_objects_for_user(
            self.request.user, 
            'view_document', 
            queryset
        )
```

#### Advanced: Multi-tenant Filtering

```python
class TenantFilteredMixin:
    """Mixin for multi-tenant applications."""
    
    def get_queryset(self):
        queryset = super().get_queryset()
        # Get tenant from user profile or request
        tenant = getattr(self.request.user, 'tenant', None)
        if tenant:
            return queryset.filter(tenant=tenant)
        return queryset.none()

class ProjectViewSet(TenantFilteredMixin, TurboDRFViewSet):
    model = Project
    
class Project(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=200)
    tenant = models.ForeignKey('Tenant', on_delete=models.CASCADE)
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['name', 'created_at', 'status']
        }
```

> **Note**: When overriding `get_queryset`, ensure you maintain any optimizations (like `select_related`) that TurboDRF applies automatically.

### 🎨 Custom Pagination

Create your own pagination class:

```python
from turbodrf.views import TurboDRFPagination

class CustomPagination(TurboDRFPagination):
    page_size = 50
    page_size_query_param = 'per_page'
    max_page_size = 200
    
    def get_paginated_response(self, data):
        return Response({
            'links': {
                'next': self.get_next_link(),
                'previous': self.get_previous_link()
            },
            'meta': {
                'total': self.page.paginator.count,
                'pages': self.page.paginator.num_pages,
                'page': self.page.number,
                'per_page': self.page_size
            },
            'results': data
        })

# Use in your viewset
class BookViewSet(TurboDRFViewSet):
    model = Book
    pagination_class = CustomPagination
```

### 📝 Custom Metadata

Customize OPTIONS responses:

```python
from rest_framework.metadata import SimpleMetadata

class CustomMetadata(SimpleMetadata):
    def determine_metadata(self, request, view):
        metadata = super().determine_metadata(request, view)
        
        # Add custom metadata
        metadata['api_version'] = 'v1'
        metadata['documentation'] = 'https://docs.example.com'
        
        # Add role-specific information
        if request.user.is_authenticated:
            metadata['user_permissions'] = {
                'can_create': view.model._meta.app_label + '.add_' + view.model._meta.model_name in request.user.get_all_permissions(),
                'can_delete': view.model._meta.app_label + '.delete_' + view.model._meta.model_name in request.user.get_all_permissions(),
            }
        
        return metadata

# Apply globally
REST_FRAMEWORK = {
    'DEFAULT_METADATA_CLASS': 'myapp.metadata.CustomMetadata',
}
```

### 📊 API Documentation

TurboDRF automatically generates interactive API documentation using Swagger UI and ReDoc. The documentation shows all available endpoints and fields based on the current user's permissions.

#### Enabling Documentation

Documentation is enabled by default. The URLs are automatically configured when you include TurboDRF URLs:

```python
# urls.py
from django.urls import path, include
from turbodrf import urls as turbodrf_urls

urlpatterns = [
    path('api/', include(turbodrf_urls)),
]
```

This automatically provides:
- Swagger UI at `/api/swagger/`
- ReDoc at `/api/redoc/`

#### Disabling Documentation in Production

**Important**: API documentation should typically be disabled in production environments for security reasons. To disable documentation:

```python
# settings.py
TURBODRF_ENABLE_DOCS = False  # Default: True
```

When disabled:
- Documentation endpoints return 404
- No schema is generated
- API endpoints continue to work normally

#### How Documentation Works

1. **Automatic Generation**: Documentation is automatically generated from your models and their `turbodrf()` configuration
2. **Permission-Based Filtering**: Users only see endpoints and fields they have permission to access
3. **Real-Time Updates**: Documentation updates automatically as you modify your models
4. **Interactive Testing**: Both Swagger UI and ReDoc allow testing API endpoints directly from the browser

#### Custom Documentation Configuration

You can customize the documentation by creating your own schema view:

```python
# urls.py
from turbodrf.documentation import get_turbodrf_schema_view

# Custom schema configuration
schema_view = get_turbodrf_schema_view(
    title="My API",
    version="v1",
    description="My awesome API powered by TurboDRF",
)

urlpatterns = [
    path('api/', include('turbodrf.urls')),
    # Override default documentation URLs
    path('docs/swagger/', schema_view.with_ui('swagger', cache_timeout=0)),
    path('docs/redoc/', schema_view.with_ui('redoc', cache_timeout=0)),
]
```

### 🔗 Working with Relations

TurboDRF provides powerful support for handling related models with automatic query optimization.

#### Nested Field Retrieval
Access related model fields using double underscore notation:

```python
class Book(TurboDRFMixin, models.Model):
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['id', 'title', 'author__name'],  # Shows author's name
                'detail': ['id', 'title', 'author__name', 'author__email', 'author__bio']
            }
        }
```

Response includes flattened fields:
```json
{
    "id": 1,
    "title": "Django for APIs",
    "author": 2,
    "author_name": "William S. Vincent",
    "author_email": "william@example.com",
    "author_bio": "Django expert and author..."
}
```

#### Foreign Key Updates
Update relations by sending the ID:

```bash
# Change a book's author
PATCH /api/books/1/
Content-Type: application/json
{"author": 3}
```

#### Filtering on Related Fields
Use Django's lookup syntax for filtering:

```bash
# Books by specific author
GET /api/books/?author=1

# Books with price between 20 and 50
GET /api/books/?price__gte=20&price__lte=50

# Books published in 2023
GET /api/books/?published_date__year=2023

# Books with "Python" in title (case-insensitive)
GET /api/books/?title__icontains=python
```

#### Current Limitations

1. **Reverse Relations**: One-to-many fields (like `author.books`) are not automatically included in responses. Use filtering instead:
   ```bash
   # Get all books by author 1
   GET /api/books/?author=1
   ```

2. **Search on Related Fields**: The `search` parameter only searches fields defined in `searchable_fields`. To search related fields, use specific filters:
   ```bash
   # Instead of: ?search=author_name
   # Use: ?author__name__icontains=smith
   ```

### ⚡ Performance

TurboDRF is optimized for speed and efficiency with automatic query optimization.

#### Automatic Query Optimization
- **select_related()** automatically applied for foreign keys to prevent N+1 queries
- Efficient pagination to limit result sets
- Database-level filtering for optimal performance

#### Performance Tips

1. **Use Pagination**: Always paginate large datasets
   ```bash
   GET /api/books/?page_size=50
   ```

2. **Indexed Fields**: Add database indexes to frequently filtered fields
   ```python
   class Book(models.Model):
       isbn = models.CharField(max_length=13, db_index=True)
       published_date = models.DateField(db_index=True)
   ```

3. **Select Only Needed Fields**: Configure different field sets for list/detail views
   ```python
   'fields': {
       'list': ['id', 'title', 'author__name'],  # Minimal fields
       'detail': ['id', 'title', 'description', 'author__name', ...]  # All fields
   }
   ```

## 🧪 Testing

```python
# tests.py
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from myapp.models import Book, Author

User = get_user_model()

class BookAPITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        
        # Create users with different roles
        self.admin = User.objects.create_user('admin', roles=['admin'])
        self.viewer = User.objects.create_user('viewer', roles=['viewer'])
        
        # Create test data
        self.author = Author.objects.create(name='Test Author')
        self.book = Book.objects.create(
            title='Test Book',
            author=self.author,
            price=19.99
        )
    
    def test_admin_can_see_all_fields(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get('/api/books/1/')
        
        self.assertEqual(response.status_code, 200)
        self.assertIn('price', response.data)
    
    def test_viewer_cannot_see_price(self):
        self.client.force_authenticate(user=self.viewer)
        response = self.client.get('/api/books/1/')
        
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('price', response.data)
    
    def test_search_functionality(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get('/api/books/?search=Test')
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['data']), 1)
```

## 🚀 Performance Tips

1. **Use select_related and prefetch_related**: TurboDRF automatically optimizes queries for nested fields

2. **Limit fields in list views**: Return only essential fields in list endpoints
   ```python
   'fields': {
       'list': ['id', 'title'],  # Minimal fields
       'detail': '__all__'        # All fields in detail
   }
   ```

3. **Add database indexes**: Index your searchable and frequently filtered fields
   ```python
   class Meta:
       indexes = [
           models.Index(fields=['title']),
           models.Index(fields=['author', 'published_date']),
       ]
   ```

## 🤝 Contributing

Open to contributors! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

```bash
# Clone the repo
git clone https://github.com/alexandercollins/turbodrf.git

# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest
## 🧪 Testing

TurboDRF comes with a comprehensive test suite covering all features.

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage report
pytest --cov=turbodrf --cov-report=html

# Or use the Makefile
make test-cov

# Run specific test file
pytest tests/test_permissions.py

# Run specific test
pytest tests/test_permissions.py::TestTurboDRFPermission::test_admin_has_read_permission

# Run tests in parallel
pytest -n auto

# Run only unit tests
pytest tests/unit/

# Run only integration tests
pytest tests/integration/
```

### Code Quality

```bash
# Format code
black turbodrf/

# Check code style
flake8 turbodrf/

# Sort imports
isort turbodrf/

# Run all checks
make lint
```

### Test Coverage

Current test coverage: **100%** ✅

View detailed coverage report:
```bash
pytest --cov=turbodrf --cov-report=html
open htmlcov/index.html
```

## 📋 Known Limitations

While TurboDRF handles most Django field types automatically, there are a few limitations to be aware of:

### Field Type Support

- **JSONField**: JSONFields are not filterable through the API due to django-filter limitations. They are included in API responses but cannot be used for filtering queries.
- **BinaryField**: Binary fields are excluded from filtering for security and performance reasons.
- **FilePathField**: File path fields are not filterable to prevent directory traversal attacks.

These fields will still be included in your API responses and can be read/written normally - they just cannot be used as filter parameters in API queries.

## 📝 License

TurboDRF is MIT licensed. See [LICENSE](LICENSE) for details.

## 🙏 Acknowledgments

Built with ❤️ using:
- [Django](https://www.djangoproject.com/) - The web framework for perfectionists with deadlines
- [Django REST Framework](https://www.django-rest-framework.org/) - Powerful and flexible toolkit for building Web APIs
- [drf-yasg](https://github.com/axnsan12/drf-yasg) - Yet another Swagger generator

---


Made with ❤️ by developers who were tired of writing serializers
