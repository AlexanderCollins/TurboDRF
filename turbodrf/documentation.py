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
from django.core.exceptions import ImproperlyConfigured
from drf_yasg import openapi
from drf_yasg.views import get_schema_view
from rest_framework import permissions


def get_turbodrf_schema_view(
    title="TurboDRF API",
    version="v1",
    description="Auto-generated API with role-based access control powered by TurboDRF",
    terms_of_service=None,
    contact_email=None,
    license_name="MIT License",
    scope="tenant",
    gate=None,
    views=None,
):
    """
    Create and configure the schema view for TurboDRF API documentation.

    This function sets up a drf-yasg schema view with TurboDRF's custom
    schema generator that filters API documentation based on user roles
    and permissions. The generated documentation will only show endpoints
    and fields that the viewing user has permission to access.

    Args:
        title (str): The API title. Defaults to "TurboDRF API".
        version (str): The API version. Defaults to "v1".
        description (str): The API description. Defaults to auto-generated text.
        terms_of_service (str): URL to terms of service. Left out of the
            schema unless given.
        contact_email (str): Contact email for API support. Left out of the
            schema unless given.
        license_name (str): License name. Defaults to "MIT License".
        scope (str): ``"tenant"`` (default) documents the tenant scope's
            endpoints; ``"platform"`` documents the platform router's only,
            with platform roles. The two are separate documents.
        gate (list): Permission classes the schema view checks before
            anything else (the router's gate). Required for the platform
            scope; without one the tenant schema view allows any caller.
        views (callable): Returns the view classes to document (a
            router's registry); every view of ``scope`` when None.
            ``TurboDRFRouter.get_schema_view()`` passes all three.

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

        schema_view = get_turbodrf_schema_view(
            title="My API",
            version="v1",
            description="My awesome API powered by TurboDRF",
        )

        if schema_view:  # Only add URLs if docs are enabled
            urlpatterns += [
                path('swagger/',
                     schema_view.with_ui('swagger', cache_timeout=0)),
                path('redoc/', schema_view.with_ui('redoc', cache_timeout=0)),
                path('swagger.json', schema_view.without_ui(cache_timeout=0)),
            ]

    Configuration:
        The schema view uses the following configuration:
        - Title: Customizable via title parameter
        - Version: Customizable via version parameter
        - Generator: RoleBasedSchemaGenerator for permission filtering
        - public=False: each endpoint's permission check runs for the caller
        - Permissions: the router's gate, else AllowAny (filtering happens at
          schema level)

        Set TURBODRF_ENABLE_DOCS = False in settings to disable documentation.

    Note:
        While the view itself allows any user to fetch the documentation,
        the RoleBasedSchemaGenerator scopes the schema to the caller: only
        the TurboDRF operations they may perform, bodies built for their own
        field permissions, and only the definitions those operations use. An
        anonymous caller sees only reads of ``public_access`` models, without
        fields a role has a read rule for (unless a ``guest`` role grants them).
    """
    # Check if documentation is enabled
    if not getattr(settings, "TURBODRF_ENABLE_DOCS", True):
        return None

    from .scope import PLATFORM, SCOPES
    from .swagger import PlatformSchemaGenerator, RoleBasedSchemaGenerator

    if scope not in SCOPES:
        raise ImproperlyConfigured(
            f"get_turbodrf_schema_view scope must be one of {SCOPES}; got {scope!r}."
        )
    gate = list(gate or ())
    if scope == PLATFORM and not gate:
        raise ImproperlyConfigured(
            "The platform schema view requires a gate: pass the platform "
            "router's (router.get_schema_view() does)."
        )
    generator_class = (
        PlatformSchemaGenerator if scope == PLATFORM else RoleBasedSchemaGenerator
    )
    if views is not None:
        enumerator = type(
            "RouterEndpointEnumerator",
            (generator_class.endpoint_enumerator_class,),
            {"view_classes": staticmethod(views)},
        )
        generator_class = type(
            f"Router{generator_class.__name__}",
            (generator_class,),
            {"endpoint_enumerator_class": enumerator},
        )

    schema_view = get_schema_view(
        openapi.Info(
            title=title,
            default_version=version,
            description=description,
            terms_of_service=terms_of_service,
            contact=openapi.Contact(email=contact_email) if contact_email else None,
            license=openapi.License(name=license_name),
        ),
        # public=False: drf-yasg runs each endpoint's permission classes for
        # the caller instead of documenting every endpoint.
        public=False,
        permission_classes=gate or [permissions.AllowAny],
        generator_class=generator_class,
    )
    if scope == PLATFORM:
        schema_view = _platform_schema_view(schema_view)

    return schema_view


def _platform_schema_view(schema_view):
    """``schema_view`` dispatched (gate included) and rendered in the platform
    scope, as a platform viewset is: a gate that reads roles gets the
    caller's platform roles."""
    from .backends import _permission_memo
    from .scope import PLATFORM, use_scope

    class PlatformSchemaView(schema_view):
        turbodrf_scope = PLATFORM

        def dispatch(self, request, *args, **kwargs):
            with use_scope(PLATFORM), _permission_memo():
                response = super().dispatch(request, *args, **kwargs)
                render = getattr(response, "render", None)
                if callable(render) and not getattr(response, "is_rendered", True):
                    render()
                return response

    return PlatformSchemaView
