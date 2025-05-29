# TurboDRF Installation Guide

## Requirements

- Python 3.8 or higher
- Django 3.2 or higher
- Django REST Framework 3.12 or higher

## Installation Methods

### Install from PyPI (when published)

```bash
pip install turbodrf
```

### Install from GitHub

```bash
pip install git+https://github.com/yourusername/turbodrf.git
```

### Install for Development

```bash
# Clone the repository
git clone https://github.com/yourusername/turbodrf.git
cd turbodrf

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode
pip install -e ".[dev]"
```

## Django Project Setup

### 1. Add to INSTALLED_APPS

```python
# settings.py
INSTALLED_APPS = [
    # Django apps
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Third-party apps
    'rest_framework',
    'django_filters',
    'drf_yasg',
    
    # TurboDRF
    'turbodrf',
    
    # Your apps
    'myapp',
]
```

### 2. Configure TurboDRF Roles

```python
# settings.py
TURBODRF_ROLES = {
    'admin': [
        # Model-level permissions
        'myapp.book.read',
        'myapp.book.create',
        'myapp.book.update',
        'myapp.book.delete',
        
        # Field-level permissions
        'myapp.book.price.read',
        'myapp.book.price.write',
    ],
    'editor': [
        'myapp.book.read',
        'myapp.book.update',
        'myapp.book.price.read',  # Read-only access to price
    ],
    'viewer': [
        'myapp.book.read',
        # No access to price field
    ]
}
```

### 3. Extend User Model with Roles

```python
# myapp/apps.py
from django.apps import AppConfig

class MyAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'myapp'
    
    def ready(self):
        from django.contrib.auth import get_user_model
        
        User = get_user_model()
        
        def get_user_roles(self):
            # Example: Use Django groups as roles
            return [group.name for group in self.groups.all()]
        
        if not hasattr(User, 'roles'):
            User.add_to_class('roles', property(get_user_roles))
```

### 4. Configure URLs

```python
# urls.py
from django.urls import path, include
from turbodrf.router import TurboDRFRouter
from turbodrf.documentation import get_turbodrf_schema_view
from turbodrf.swagger_ui import role_selector_view, set_api_role

# Create router
router = TurboDRFRouter()

# Get schema view
schema_view = get_turbodrf_schema_view()

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),
    
    # API
    path('api/', include(router.urls)),
    
    # API Documentation
    path('api/docs/', role_selector_view, name='turbodrf-docs'),
    path('api/set-role/', set_api_role, name='turbodrf-set-role'),
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=0)),
    path('redoc/', schema_view.with_ui('redoc', cache_timeout=0)),
]
```

## Model Setup

### Basic Model with TurboDRF

```python
from django.db import models
from turbodrf.mixins import TurboDRFMixin

class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    published_date = models.DateField()
    
    # Define searchable fields
    searchable_fields = ['title', 'author']
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': ['title', 'author', 'price', 'published_date']
        }
```

### Model with Different List/Detail Fields

```python
class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.CharField(max_length=100)
    description = models.TextField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['title', 'author', 'price'],
                'detail': ['title', 'author', 'description', 'price']
            }
        }
```

### Model with Relationships

```python
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
    
    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['title', 'author__name'],
                'detail': ['title', 'author__name', 'author__email']
            }
        }
```

## Running the Application

```bash
# Apply migrations
python manage.py migrate

# Create a superuser
python manage.py createsuperuser

# Run the development server
python manage.py runserver
```

## Testing the API

```bash
# List all books
curl http://localhost:8000/api/books/

# Get a specific book
curl http://localhost:8000/api/books/1/

# Search books
curl http://localhost:8000/api/books/?search=django

# Filter books
curl http://localhost:8000/api/books/?author__name=Smith

# Order books
curl http://localhost:8000/api/books/?ordering=-price

# Paginate results
curl http://localhost:8000/api/books/?page=2&page_size=10
```

## Troubleshooting

### Import Errors

If you get import errors, ensure TurboDRF is properly installed:

```bash
pip list | grep turbodrf
```

### No API Endpoints

If no endpoints appear, check that:
1. Your models inherit from `TurboDRFMixin`
2. Models have a `turbodrf()` classmethod
3. The model is not disabled (`'enabled': False`)

### Permission Denied

If you get 403 errors:
1. Check your user's roles
2. Verify role permissions in `TURBODRF_ROLES`
3. Ensure the User model has a `roles` property

## Uninstallation

```bash
pip uninstall turbodrf
```

Then remove 'turbodrf' from INSTALLED_APPS and remove any TurboDRF imports from your code.