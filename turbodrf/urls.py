from importlib.util import find_spec

from django.urls import include, path

from .documentation import get_turbodrf_schema_view
from .router import TurboDRFRouter

# Auto-discover and register all models
router = TurboDRFRouter()

# Get schema view
schema_view = get_turbodrf_schema_view()

# TurboDRF URL patterns
urlpatterns = [
    # API endpoints
    path("", include(router.urls)),
]

# Only add documentation URLs if enabled
if schema_view:
    urlpatterns += [
        path(
            "swagger/",
            schema_view.with_ui("swagger", cache_timeout=0),
            name="turbodrf-swagger",
        ),
        path(
            "redoc/",
            schema_view.with_ui("redoc", cache_timeout=0),
            name="turbodrf-redoc",
        ),
    ]
    # The OpenAPI 3 schema, with the openapi3 extra (drf-spectacular).
    if find_spec("drf_spectacular"):
        from .openapi import get_openapi3_view

        urlpatterns.append(
            path(
                "openapi/",
                get_openapi3_view(title="TurboDRF API"),
                name="turbodrf-openapi",
            )
        )
