"""
Automatic router for TurboDRF.

This module provides the TurboDRFRouter that automatically discovers
and registers all models with TurboDRFMixin.
"""

from django.apps import apps
from django.urls import re_path
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
        - Handles both trailing and non-trailing slash URLs

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
        - /api/books/ and /api/books
        - /api/authors/ and /api/authors
        - /api/categories/ and /api/categories
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

    def get_urls(self):
        """
        Generate URL patterns that work with or without trailing slashes.

        This override ensures that POST requests work regardless of whether
        the client includes a trailing slash, avoiding the common Django
        redirect issue that loses POST data.
        """
        urls = super().get_urls()

        # Create duplicate patterns without trailing slashes
        additional_urls = []
        for url_pattern in urls:
            if hasattr(url_pattern, "pattern") and hasattr(
                url_pattern.pattern, "_regex"
            ):
                # Get the regex pattern
                regex = url_pattern.pattern._regex

                # If it ends with '/$', create a version without it
                if regex.endswith("/$"):
                    new_regex = regex[:-2] + "$"  # Remove / before $

                    # Create new URL pattern without trailing slash
                    new_pattern = re_path(
                        new_regex,
                        url_pattern.callback,
                        url_pattern.default_args,
                        url_pattern.name + "_no_slash" if url_pattern.name else None,
                    )
                    additional_urls.append(new_pattern)

        return urls + additional_urls
