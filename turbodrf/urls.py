from django.urls import path, include
from .router import TurboDRFRouter
from .documentation import get_turbodrf_schema_view
from .swagger_ui import role_selector_view, set_api_role

# Auto-discover and register all models
router = TurboDRFRouter()

# Get schema view
schema_view = get_turbodrf_schema_view()

# TurboDRF URL patterns
urlpatterns = [
    # API endpoints
    path("", include(router.urls)),
    # Documentation
    path("docs/", role_selector_view, name="turbodrf-docs"),
    path("set-role/", set_api_role, name="turbodrf-set-role"),
    path(
        "swagger/",
        schema_view.with_ui("swagger", cache_timeout=0),
        name="turbodrf-swagger",
    ),
    path(
        "redoc/", schema_view.with_ui("redoc", cache_timeout=0), name="turbodrf-redoc"
    ),
]
