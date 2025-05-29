"""
Automatic router for TurboDRF.

This module provides the TurboDRFRouter that automatically discovers
and registers all models with TurboDRFMixin.
"""

from django.apps import apps
from rest_framework.routers import DefaultRouter

from .mixins import TurboDRFMixin
from .views import TurboDRFViewSet


class TurboDRFRouter(DefaultRouter):
    """
    Router that auto-discovers and registers TurboDRF models.

    This router extends DRF's DefaultRouter to automatically discover all
    Django models that inherit from TurboDRFMixin and register them as
    API endpoints. No manual registration is required.

    Features:
        - Automatic model discovery on initialization
        - Dynamic ViewSet generation for each model
        - Respects model configuration (enabled/disabled, custom endpoints)
        - Inherits all DefaultRouter functionality (browsable API, format suffixes)

    Example:
        >>> # In your urls.py
        >>> from django.urls import path, include
        >>> from turbodrf.router import TurboDRFRouter
        >>>
        >>> router = TurboDRFRouter()
        >>>
        >>> urlpatterns = [
        ...     path('api/', include(router.urls)),
        ... ]

    This will automatically create endpoints for all TurboDRF-enabled models:
        - /api/books/
        - /api/authors/
        - /api/categories/
        etc.
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize the router and discover models.

        Args:
            *args: Positional arguments passed to DefaultRouter.
            **kwargs: Keyword arguments passed to DefaultRouter.
        """
        super().__init__(*args, **kwargs)
        self.discover_models()

    def discover_models(self):
        """
        Discover all models with TurboDRFMixin and register them.

        This method iterates through all registered Django models and
        automatically registers those that inherit from TurboDRFMixin
        and are enabled in their configuration.

        The method:
        1. Finds all models inheriting from TurboDRFMixin
        2. Checks if the model is enabled (via turbodrf() config)
        3. Creates a dynamic ViewSet for the model
        4. Registers the ViewSet with the appropriate endpoint

        Models can customize their endpoint name via the 'endpoint' key
        in their turbodrf() configuration. If not specified, the endpoint
        defaults to the pluralized model name.
        """
        for model in apps.get_models():
            if issubclass(model, TurboDRFMixin):
                config = model.turbodrf()

                if config.get("enabled", True):
                    # Get custom endpoint or use default
                    endpoint = config.get("endpoint", f"{model._meta.model_name}s")

                    # Create a custom viewset for this model
                    viewset_class = type(
                        f"{model.__name__}ViewSet",
                        (TurboDRFViewSet,),
                        {
                            "model": model,
                            "queryset": model.objects.all(),
                            "__module__": model.__module__,
                            "__doc__": (
                                f"Auto-generated ViewSet for {model.__name__} model."
                            ),
                        },
                    )

                    # Register the viewset
                    self.register(
                        endpoint, viewset_class, basename=model._meta.model_name
                    )
