"""
TurboDRF - Dead simple Django REST API generator with role-based permissions.

TurboDRF automatically generates a complete REST API from your Django models
with just
a mixin and a configuration method. It provides:

- Automatic CRUD endpoints for all models
- Role-based permissions at model and field level
- Built-in search, filtering, ordering, and pagination
- Nested field support for relationships
- Auto-generated API documentation with Swagger/ReDoc
- Zero boilerplate code required

Quick Start:
    >>> from django.db import models
    >>> from turbodrf import TurboDRFMixin
    >>>
    >>> class Book(models.Model, TurboDRFMixin):
    ...     title = models.CharField(max_length=200)
    ...     author = models.CharField(max_length=100)
    ...
    ...     @classmethod
    ...     def turbodrf(cls):
    ...         return {'fields': ['title', 'author']}

This automatically creates:
- GET /api/books/ - List all books
- POST /api/books/ - Create a new book
- GET /api/books/{id}/ - Get a specific book
- PUT /api/books/{id}/ - Update a book
- DELETE /api/books/{id}/ - Delete a book

For more information, see: https://github.com/alexandercollins/turbodrf
"""

__version__ = "0.1.10"
__author__ = "Alexander Collins"
__email__ = "your.email@example.com"
__license__ = "MIT"


# Lazy imports to avoid Django configuration issues during package installation
def __getattr__(name):
    """Lazy import for Django-dependent modules."""
    if name == "TurboDRFMixin":
        from .mixins import TurboDRFMixin

        return TurboDRFMixin
    elif name == "TurboDRFPermission":
        from .permissions import TurboDRFPermission

        return TurboDRFPermission
    elif name == "TurboDRFRouter":
        from .router import TurboDRFRouter

        return TurboDRFRouter
    elif name == "TurboDRFSerializer":
        from .serializers import TurboDRFSerializer

        return TurboDRFSerializer
    elif name == "TurboDRFSerializerFactory":
        from .serializers import TurboDRFSerializerFactory

        return TurboDRFSerializerFactory
    elif name == "TurboDRFViewSet":
        from .views import TurboDRFViewSet

        return TurboDRFViewSet
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    "TurboDRFMixin",
    "TurboDRFRouter",
    "TurboDRFViewSet",
    "TurboDRFPermission",
    "TurboDRFSerializer",
    "TurboDRFSerializerFactory",
]
