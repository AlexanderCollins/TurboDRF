# TurboDRF

[![PyPI Version](https://img.shields.io/pypi/v/turbodrf?label=pypi)](https://pypi.org/project/turbodrf/)
[![Tests](https://img.shields.io/github/actions/workflow/status/alexandercollins/turbodrf/tests.yml?branch=main&label=tests)](https://github.com/alexandercollins/turbodrf/actions)
[![Coverage](https://img.shields.io/badge/coverage-69.27%25-yellow)](https://github.com/alexandercollins/turbodrf)
[![License](https://img.shields.io/badge/license-MIT-purple)](LICENSE)

**Dead simple Django REST Framework API generator with role-based permissions.**

Turn your Django models into fully-featured REST APIs with a mixin and a method. Zero boilerplate.

## Install

```bash
pip install turbodrf

# Optional: faster JSON rendering (7x faster than stdlib)
pip install turbodrf[fast]
```

## Quick Start

**1. Add to settings:**

```python
INSTALLED_APPS = [
    'rest_framework',
    'turbodrf',
]
```

**2. Add the mixin to your model:**

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

**3. Add the router:**

```python
from turbodrf.router import TurboDRFRouter

router = TurboDRFRouter()

urlpatterns = [
    path('api/', include(router.urls)),
]
```

**Done.** You now have a full REST API with search, filtering, pagination, and field selection:

```
GET    /api/books/                          # List
GET    /api/books/1/                        # Detail
POST   /api/books/                          # Create
PUT    /api/books/1/                        # Update
DELETE /api/books/1/                        # Delete
GET    /api/books/?search=django            # Search
GET    /api/books/?price__lt=20             # Filter
GET    /api/books/?fields=title,price       # Select fields
```

## Documentation

- [Configuration](docs/configuration.md) -- all `turbodrf()` options, field selection, nested fields
- [Permissions](docs/permissions.md) -- role-based, field-level, and Django default permissions
- [Performance](docs/performance.md) -- compiled read path, fast JSON rendering, benchmarking
- [Filtering & Search](docs/filtering.md) -- filtering, search, ordering, OR queries
- [Integrations](docs/integrations.md) -- allauth, Keycloak, drf-api-tracking
- [Security](docs/security.md) -- sensitive fields, secure defaults, error responses
- [Management Commands](docs/commands.md) -- turbodrf_check, turbodrf_benchmark, turbodrf_explain

## License

MIT License. See [LICENSE](LICENSE) for details.
