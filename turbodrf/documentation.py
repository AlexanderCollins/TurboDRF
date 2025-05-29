"""
TurboDRF API Documentation Module

This module provides automatic API documentation generation for
TurboDRF-enabled applications. It integrates with drf-yasg to create
OpenAPI/Swagger documentation
that respects TurboDRF's role-based access control system.

The documentation is dynamically filtered based on user roles, showing only
the endpoints and fields that each role has permission to access.
"""

from django.conf import settings
from drf_yasg import openapi
from drf_yasg.views import get_schema_view
from rest_framework import permissions


def get_turbodrf_schema_view():
    """
    Create and configure the schema view for TurboDRF API documentation.

    This function sets up a drf-yasg schema view with TurboDRF's custom
    schema generator that filters API documentation based on user roles
    and permissions. The generated documentation will only show endpoints
    and fields that the viewing user has permission to access.

    Features:
        - Role-based filtering of API endpoints
        - Dynamic field visibility based on permissions
        - Automatic schema generation from model configurations
        - Support for both Swagger UI and ReDoc interfaces

    Returns:
        SchemaView: A configured drf-yasg SchemaView instance that can be
                   used to generate multiple documentation formats:
                   - Swagger UI (interactive HTML)
                   - ReDoc (alternative HTML documentation)
                   - OpenAPI JSON/YAML schemas

        Returns None if TURBODRF_ENABLE_DOCS is set to False.

    Usage:
        # In your urls.py
        from turbodrf.documentation import get_turbodrf_schema_view

        schema_view = get_turbodrf_schema_view()

        if schema_view:  # Only add URLs if docs are enabled
            urlpatterns += [
                path('swagger/',
                     schema_view.with_ui('swagger', cache_timeout=0)),
                path('redoc/', schema_view.with_ui('redoc', cache_timeout=0)),
                path('swagger.json', schema_view.without_ui(cache_timeout=0)),
            ]

    Configuration:
        The schema view uses the following configuration:
        - Title: "TurboDRF API" (customizable)
        - Version: "v1"
        - Generator: RoleBasedSchemaGenerator for permission filtering
        - Permissions: AllowAny (filtering happens at schema level)

        Set TURBODRF_ENABLE_DOCS = False in settings to disable documentation.

    Note:
        While the view itself allows any user to access the documentation,
        the RoleBasedSchemaGenerator ensures that users only see the parts
        of the API they have permission to use based on their roles.
    """
    # Check if documentation is enabled
    if not getattr(settings, "TURBODRF_ENABLE_DOCS", True):
        return None

    from .swagger import RoleBasedSchemaGenerator

    schema_view = get_schema_view(
        openapi.Info(
            title="TurboDRF API",
            default_version="v1",
            description=(
                "Auto-generated API with role-based access control "
                "powered by TurboDRF"
            ),
            terms_of_service="https://www.example.com/terms/",
            contact=openapi.Contact(email="contact@example.com"),
            license=openapi.License(name="MIT License"),
        ),
        public=True,
        permission_classes=[permissions.AllowAny],
        generator_class=RoleBasedSchemaGenerator,
    )

    return schema_view
