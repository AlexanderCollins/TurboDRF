# TurboDRF

<div align="center">

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![Django Version](https://img.shields.io/badge/django-3.2%2B-green)](https://www.djangoproject.com/)
[![DRF Version](https://img.shields.io/badge/djangorestframework-3.12%2B-red)](https://www.django-rest-framework.org/)
[![License](https://img.shields.io/badge/license-MIT-purple)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/alexandercollins/turbodrf/tests.yml?branch=main&label=tests)](https://github.com/alexandercollins/turbodrf/actions)
[![Coverage](https://img.shields.io/badge/coverage-76.1%25-yellowgreen)](https://github.com/alexandercollins/turbodrf)
[![PyPI Version](https://img.shields.io/pypi/v/turbodrf?label=pypi)](https://pypi.org/project/turbodrf/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/alexandercollins/turbodrf/pulls)
[![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**The dead simple Django REST Framework API generator with role-based permissions**

*New project as of May 2025. Built with assistance from [Claude](https://claude.ai).*

Transform your Django models into fully-featured REST APIs with just a mixin and a method. Zero boilerplate, maximum power.

[Features](#-features) • [Quick Start](#-quick-start) • [Documentation](#-documentation) • [Examples](#-examples) • [Contributing](#-contributing)

</div>

---

## Installation

```bash
pip install turbodrf
```

## Quick Start

**1. Add to settings:**

```python
INSTALLED_APPS = [
    'rest_framework',
    'turbodrf',
]
```

**2. Configure your model:**

```python
from django.db import models
from turbodrf.mixins import TurboDRFMixin

class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    searchable_fields = ['title']

    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['title', 'author__name', 'price'],
                'detail': ['title', 'author__name', 'author__email', 'price']
            }
        }
```

**3. Configure URLs:**

```python
from turbodrf.router import TurboDRFRouter

router = TurboDRFRouter()

urlpatterns = [
    path('api/', include(router.urls)),
]
```

**You now have a complete REST API:**

```bash
GET    /api/books/              # List all books
GET    /api/books/1/            # Get book detail
POST   /api/books/              # Create book
PUT    /api/books/1/            # Update book
DELETE /api/books/1/            # Delete book

# Filtering, search, pagination
GET /api/books/?search=django
GET /api/books/?price__lt=20
GET /api/books/?page=2&page_size=50
```

---

## Features

- **Automatic API generation** from models
- **Role-based permissions** with field-level control
- **Nested field access** (`author__name`, `category__parent__title`)
- **Full-text search** on configured fields
- **Advanced filtering** with Django lookups (`__gte`, `__icontains`, etc.)
- **OR filtering** for complex queries
- **Pagination** with metadata
- **Auto-generated API docs** (Swagger/ReDoc)
- **Query optimization** (automatic `select_related`)

---

## Configuration

### Model Configuration

The `turbodrf()` method configures the API:

```python
@classmethod
def turbodrf(cls):
    return {
        'enabled': True,           # Enable/disable API
        'endpoint': 'books',       # Custom endpoint name (default: pluralized model name)
        'fields': '__all__',       # All fields
        # OR
        'fields': ['title', 'author__name'],  # Specific fields
        # OR
        'fields': {
            'list': ['title'],     # Fields for list view
            'detail': '__all__'    # Fields for detail view
        },
        'public_access': True,     # Allow unauthenticated GET requests
        'lookup_field': 'slug',    # Custom lookup field (default: pk)
    }
```

### Nested Fields

Access related model fields with `__` notation:

```python
'fields': [
    'title',
    'author__name',              # ForeignKey (1 level)
    'author__bio',
    'category__parent__name',    # Multi-level (2 levels)
]
```

Response format (fields are flattened):

```json
{
    "title": "Django for APIs",
    "author": 2,
    "author_name": "William Vincent",
    "author_bio": "Django expert...",
    "category_parent_name": "Programming"
}
```

**Nesting Depth Limit:**

Default maximum nesting depth is **3 levels** (e.g., `author__publisher__parent__name`).

```python
# settings.py
TURBODRF_MAX_NESTING_DEPTH = 3  # Default

# Examples:
'author__name'                    # ✓ Valid (1 level)
'author__publisher__name'         # ✓ Valid (2 levels)
'author__publisher__parent__name' # ✓ Valid (3 levels)
'a__b__c__d__e'                   # ✗ Exceeds limit (4 levels)
```

**⚠️ WARNING:** Increasing `TURBODRF_MAX_NESTING_DEPTH` beyond 3 is **UNSUPPORTED** and may cause:
- Performance degradation
- Security vulnerabilities
- Unexpected behavior
- Increased database queries

**Nested Field Permissions:**

Permissions are checked at **each level** of the relationship chain:

```python
# For field: author__publisher__revenue
# Requires ALL of:
# 1. books.book.author.read (or books.book.read)
# 2. authors.author.publisher.read (or authors.author.read)
# 3. publishers.publisher.revenue.read (or publishers.publisher.read)
```

If permission is denied at any level, the entire field is excluded.

### ManyToMany Fields

TurboDRF fully supports M2M relationships with clean array-of-objects serialization:

**Model Definition:**

```python
class Tag(models.Model):
    name = models.CharField(max_length=50)
    slug = models.SlugField()
    category = models.CharField(max_length=50)

class Article(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    tags = models.ManyToManyField(Tag, related_name='articles')

    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['title', 'tags__name'],
                'detail': [
                    'title',
                    'tags__name',
                    'tags__slug',
                    'tags__category',
                ],
            }
        }
```

**Response Format:**

M2M fields serialize as **arrays of objects** (not flat fields like ForeignKey):

```json
{
    "title": "My Article",
    "tags": [
        {"name": "Python", "slug": "python", "category": "programming"},
        {"name": "Django", "slug": "django", "category": "framework"}
    ]
}
```

**Filtering M2M Fields:**

M2M fields support multiple lookups:

```bash
# Filter by tag ID (exact match)
GET /api/articles/?tags=1

# Filter by multiple tag IDs (OR logic)
GET /api/articles/?tags__in=1,2,3

# Check if article has no tags
GET /api/articles/?tags__isnull=true

# Filter by nested field
GET /api/articles/?tags__name__icontains=python
GET /api/articles/?tags__slug=django
GET /api/articles/?tags__category=programming
```

**M2M Field Permissions:**

Permissions work the same as ForeignKey - checked at each level:

```python
TURBODRF_ROLES = {
    'viewer': [
        'articles.article.read',
        'articles.article.tags.read',  # Permission to see tags field
        'tags.tag.read',                # Permission to access Tag model
        'tags.tag.name.read',           # Permission to see tag.name
        # NO permission on tag.slug or tag.category
    ]
}
```

**Result:**

```json
{
    "title": "My Article",
    "tags": [
        {"name": "Python"},    // Only 'name' field (has permission)
        {"name": "Django"}     // slug and category excluded (no permission)
    ]
}
```

**Nesting Depth for M2M:**

M2M fields respect the same nesting depth limits (default 3 levels):

```python
'tags__name'                    # ✓ Valid (1 level)
'tags__category__parent'        # ✓ Valid (2 levels)
'tags__a__b__c__d'              # ✗ Exceeds limit (4 levels)
```

**Performance:**

- M2M queries use Django's `prefetch_related` for efficiency
- Permission snapshots are O(1) for M2M fields (same as regular fields)
- No N+1 query issues when properly configured

### Search

Define searchable fields:

```python
class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    description = models.TextField()

    searchable_fields = ['title', 'description']
```

Usage:

```bash
GET /api/books/?search=django
```

### Filtering

All model fields are filterable with Django lookups:

```bash
# Exact match
GET /api/books/?author=1

# Comparisons
GET /api/books/?price__gte=10&price__lte=50

# Text search
GET /api/books/?title__icontains=python

# Date filtering
GET /api/books/?published_date__year=2023

# Related fields
GET /api/books/?author__name__istartswith=smith
```

**Filter Permissions:**

Users can only filter on fields they have **read permission** for. Nested filter fields are validated at each level:

```python
# Filter: ?author__publisher__revenue__gte=1000000
# Requires read permission on:
# 1. Book.author
# 2. Author.publisher
# 3. Publisher.revenue
```

Filters on unpermitted fields are silently ignored. This prevents information leakage through filter-based attacks.

### OR Filtering

Use `_or` suffix for OR queries:

```bash
# Title is "Django" OR "Python"
GET /api/books/?title_or=Django&title_or=Python

# With lookups
GET /api/books/?title__icontains_or=django&title__icontains_or=python

# Combined with AND
GET /api/books/?title_or=Django&title_or=Python&price__lt=50
```

---

## Permissions

TurboDRF supports three permission modes:

### 1. No Permissions (Development)

```python
# settings.py
TURBODRF_DISABLE_PERMISSIONS = True
```

### 2. Django Default Permissions

Standard Django permissions (`add_`, `change_`, `delete_`, `view_`):

```python
# settings.py
TURBODRF_USE_DEFAULT_PERMISSIONS = True
```

Grant permissions:

```python
from django.contrib.auth.models import Permission

user.user_permissions.add(
    Permission.objects.get(codename='view_book'),
    Permission.objects.get(codename='change_book')
)
```

### 3. TurboDRF Role-Based Permissions (Default)

Field-level permissions with role management.

**Permission format:**

- Model-level: `app.model.action` (read, create, update, delete)
- Field-level: `app.model.field.permission` (read, write)

#### Static Configuration

```python
# settings.py
TURBODRF_ROLES = {
    'admin': [
        'books.book.read',
        'books.book.create',
        'books.book.update',
        'books.book.delete',
    ],
    'editor': [
        'books.book.read',
        'books.book.update',
        'books.book.price.read',      # Can see price
        # No price.write - can't change price
    ],
    'viewer': [
        'books.book.read',
        'books.book.title.read',
        # No price permission - can't see price
    ],
}
```

Add roles to users:

```python
# Option 1: Property on User model
from django.contrib.auth import get_user_model

User = get_user_model()

def get_user_roles(self):
    return [group.name for group in self.groups.all()]

User.add_to_class('roles', property(get_user_roles))

# Option 2: Custom User model
class User(AbstractUser):
    user_roles = models.JSONField(default=list)

    @property
    def roles(self):
        return self.user_roles
```

#### Database-Backed Permissions

For runtime permission changes without code deployment:

```python
# settings.py
TURBODRF_PERMISSION_MODE = 'database'
TURBODRF_PERMISSION_CACHE_TIMEOUT = 300  # 5 minutes
```

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    'turbodrf',  # Must be in INSTALLED_APPS for migrations
]
```

Run migrations:

```bash
python manage.py migrate turbodrf
```

Create roles and permissions:

```python
from turbodrf.models import TurboDRFRole, RolePermission, UserRole

# Create role
editor = TurboDRFRole.objects.create(name='editor')

# Model-level permissions
RolePermission.objects.create(
    role=editor,
    app_label='books',
    model_name='book',
    action='read'
)

# Field-level permissions
RolePermission.objects.create(
    role=editor,
    app_label='books',
    model_name='book',
    field_name='price',
    permission_type='read'  # Can see but not edit
)

# Assign to user
UserRole.objects.create(user=user, role=editor)
```

Link to Django Groups:

```python
from django.contrib.auth.models import Group

group = Group.objects.create(name='Editors')
editor.django_group = group
editor.save()
```

**Field-level permission logic:**

1. If field has explicit permissions → check those
2. Else → fall back to model-level permissions
3. Read: needs `field.read` OR `model.read`
4. Write: needs `field.write` OR `model.create`/`update`

**Performance:** Uses snapshot-based O(1) permission checking with automatic cache invalidation.

### Guest Role

Unauthenticated users with `public_access: True`:

```python
TURBODRF_ROLES = {
    'guest': [
        'books.book.read',
        'books.book.title.read',
        # Limited field access for unauthenticated users
    ],
}
```

---

## Advanced Usage

### Custom ViewSet

```python
from turbodrf.views import TurboDRFViewSet
from rest_framework.decorators import action

class BookViewSet(TurboDRFViewSet):
    model = Book

    @action(detail=False)
    def trending(self, request):
        queryset = self.get_queryset().filter(views__gte=1000)
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

# Register manually
router.register('books', BookViewSet, basename='book')
```

### User-Based Filtering

Restrict queryset based on current user:

```python
class UserFilteredViewSet(TurboDRFViewSet):
    model = Book

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_authenticated:
            return queryset.filter(owner=self.request.user)
        return queryset.filter(is_public=True)
```

Or via model manager:

```python
class BookManager(models.Manager):
    def for_user(self, user):
        if user.is_superuser:
            return self.all()
        return self.filter(owner=user)

class Book(models.Model, TurboDRFMixin):
    objects = BookManager()

    @classmethod
    def turbodrf(cls):
        return {
            'fields': '__all__',
            'get_queryset': lambda viewset: cls.objects.for_user(viewset.request.user)
        }
```

### Custom Pagination

```python
from turbodrf.views import TurboDRFPagination

class CustomPagination(TurboDRFPagination):
    page_size = 50
    max_page_size = 200

class BookViewSet(TurboDRFViewSet):
    pagination_class = CustomPagination
```

### API Documentation

Swagger UI and ReDoc are enabled by default at:

- `/api/swagger/`
- `/api/redoc/`

Disable in production:

```python
# settings.py
TURBODRF_ENABLE_DOCS = False
```

Customize:

```python
from turbodrf.documentation import get_turbodrf_schema_view

schema_view = get_turbodrf_schema_view(
    title='My API',
    version='v1',
    description='API documentation',
)

urlpatterns = [
    path('docs/', schema_view.with_ui('swagger')),
]
```

#### Swagger Field Visibility

By default, Swagger documentation respects field-level permissions - users only see fields they have access to. For development/testing, you can show all fields regardless of permissions:

```python
# settings.py
# Show all fields in Swagger docs (development only!)
TURBODRF_SWAGGER_SHOW_ALL_FIELDS = True  # Default: False
```

**⚠️ Important Notes:**
- This setting **only affects Swagger/OpenAPI documentation**
- The actual API **still enforces all permission checks** - users cannot access fields they don't have permission for
- Recommended for **development environments only** to see complete API documentation
- In production, keep this `False` (default) so documentation matches actual user permissions

**Use Case:**
- **Development**: Set to `True` to see all available fields while building/testing
- **Production**: Keep `False` so each role sees only their permitted fields in docs

---

## Integrations

### django-allauth

Session-based authentication for SPAs:

```bash
pip install turbodrf[allauth]
```

```python
# settings.py
INSTALLED_APPS = [
    'allauth',
    'allauth.account',
    'allauth.headless',
    'turbodrf',
]

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'turbodrf.integrations.allauth.AllAuthRoleMiddleware',
]

TURBODRF_ALLAUTH_INTEGRATION = True
TURBODRF_ALLAUTH_ROLE_MAPPING = {
    'Administrators': 'admin',
    'Editors': 'editor',
}
```

Frontend usage with httpOnly cookies:

```javascript
// Login (sets httpOnly session cookie)
await fetch('/api/auth/login/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ username, password })
});

// Subsequent API calls (cookie sent automatically)
await fetch('/api/books/', { credentials: 'include' });
```

### drf-api-tracking

Request logging:

```bash
pip install drf-api-tracking
```

```python
INSTALLED_APPS = [
    'rest_framework_tracking',
    'turbodrf',
]
```

Automatically logs all requests. Access via:

```python
from rest_framework_tracking.models import APIRequestLog

APIRequestLog.objects.filter(user=user)
```

### Keycloak / OpenID Connect

Integrate with Keycloak or any OpenID Connect provider for SSO authentication:

```bash
pip install social-auth-app-django
```

**1. Configure Django settings:**

```python
# settings.py
INSTALLED_APPS = [
    'social_django',
    'turbodrf',
]

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'turbodrf.integrations.keycloak.KeycloakRoleMiddleware',
]

# Enable Keycloak integration
TURBODRF_KEYCLOAK_INTEGRATION = True

# Configure role claim path in ID token
TURBODRF_KEYCLOAK_ROLE_CLAIM = 'realm_access.roles'  # or 'roles' for simple claims

# Map Keycloak roles to TurboDRF roles
TURBODRF_KEYCLOAK_ROLE_MAPPING = {
    'realm-admin': 'admin',
    'content-editor': 'editor',
    'basic-user': 'viewer',
}

# Social auth configuration
AUTHENTICATION_BACKENDS = [
    'social_core.backends.keycloak.KeycloakOAuth2',
    'django.contrib.auth.backends.ModelBackend',
]

SOCIAL_AUTH_KEYCLOAK_KEY = 'your-client-id'
SOCIAL_AUTH_KEYCLOAK_SECRET = 'your-client-secret'
SOCIAL_AUTH_KEYCLOAK_PUBLIC_KEY = 'your-realm-public-key'
SOCIAL_AUTH_KEYCLOAK_AUTHORIZATION_URL = 'https://your-keycloak/auth/realms/your-realm/protocol/openid-connect/auth'
SOCIAL_AUTH_KEYCLOAK_ACCESS_TOKEN_URL = 'https://your-keycloak/auth/realms/your-realm/protocol/openid-connect/token'
```

**2. Common role claim paths:**

```python
# Simple roles claim
TURBODRF_KEYCLOAK_ROLE_CLAIM = 'roles'
# Token: {"roles": ["admin", "editor"]}

# Realm roles
TURBODRF_KEYCLOAK_ROLE_CLAIM = 'realm_access.roles'
# Token: {"realm_access": {"roles": ["admin", "editor"]}}

# Client roles
TURBODRF_KEYCLOAK_ROLE_CLAIM = 'resource_access.my-client.roles'
# Token: {"resource_access": {"my-client": {"roles": ["admin"]}}}
```

**3. Role mapping:**

The middleware automatically extracts roles from the ID token and maps them to TurboDRF roles. Unmapped roles pass through unchanged:

```python
# User logs in with Keycloak roles: ['realm-admin', 'developer']
# With mapping: {'realm-admin': 'admin'}
# TurboDRF sees: ['admin', 'developer']
```

**How it works:**
1. User authenticates via Keycloak OAuth2
2. `KeycloakRoleMiddleware` extracts roles from ID token claims
3. Roles are mapped to TurboDRF roles via `TURBODRF_KEYCLOAK_ROLE_MAPPING`
4. Mapped roles are assigned to `request.user.roles`
5. TurboDRF permissions use these roles for access control

---

## Testing

```python
from django.test import TestCase
from rest_framework.test import APIClient

class BookAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user('test', roles=['viewer'])

    def test_list_books(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/books/')
        self.assertEqual(response.status_code, 200)
```

Run tests:

```bash
pytest
pytest --cov=turbodrf
```

---

## Performance Tips

1. **Use different fields for list/detail views**
   ```python
   'fields': {
       'list': ['id', 'title'],      # Minimal
       'detail': '__all__'            # Complete
   }
   ```

2. **Add database indexes** to filtered fields
   ```python
   class Meta:
       indexes = [
           models.Index(fields=['title']),
           models.Index(fields=['published_date']),
       ]
   ```

3. **TurboDRF automatically optimizes queries** with `select_related` for nested fields

---

## Known Limitations

- **Nested writes not supported**: TurboDRF only supports nested **reads** (e.g., `author__name`). Nested resource creation/modification is not supported.
  ```python
  # ✗ NOT supported:
  POST /api/books/
  {
    "title": "New Book",
    "author": {
      "name": "New Author",
      "publisher": {"name": "New Publisher"}
    }
  }

  # ✓ Supported:
  POST /api/books/
  {
    "title": "New Book",
    "author": 1  # Foreign key by ID
  }
  ```
- **JSONField, BinaryField, FilePathField**: Not filterable (included in responses only)
- **Reverse relations**: Not automatically included (use filtering instead)
- **Search on related fields**: Use filter with `__icontains` instead

---

## Migration Guide

### From Static to Database Permissions

```python
from turbodrf.models import TurboDRFRole, RolePermission

def migrate_static_to_database():
    TURBODRF_ROLES = getattr(settings, 'TURBODRF_ROLES', {})

    for role_name, permissions in TURBODRF_ROLES.items():
        role, _ = TurboDRFRole.objects.get_or_create(name=role_name)

        for perm in permissions:
            parts = perm.split('.')
            if len(parts) == 3:  # Model-level
                app_label, model_name, action = parts
                RolePermission.objects.get_or_create(
                    role=role, app_label=app_label,
                    model_name=model_name, action=action
                )
            elif len(parts) == 4:  # Field-level
                app_label, model_name, field_name, ptype = parts
                RolePermission.objects.get_or_create(
                    role=role, app_label=app_label, model_name=model_name,
                    field_name=field_name, permission_type=ptype
                )
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

## Acknowledgments

TurboDRF was inspired by [fast-drf](https://github.com/iashraful/fast-drf) by [https://github.com/iashraful/](https://github.com/iashraful/).

- [Django](https://www.djangoproject.com/)
- [Django REST Framework](https://www.django-rest-framework.org/)
- [drf-yasg](https://github.com/axnsan12/drf-yasg)
- [fast-drf](https://github.com/iashraful/fast-drf)
