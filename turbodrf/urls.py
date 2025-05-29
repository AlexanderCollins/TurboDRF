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
