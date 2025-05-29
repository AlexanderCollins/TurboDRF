"""
URL configuration for tests.
"""

from django.urls import path, include
from turbodrf.router import TurboDRFRouter
from turbodrf.documentation import get_turbodrf_schema_view
from turbodrf.swagger_ui import role_selector_view, set_api_role

# Create the TurboDRF router
router = TurboDRFRouter()

# Get the schema view
schema_view = get_turbodrf_schema_view()

urlpatterns = [
    path("api/", include(router.urls)),
    path("api/docs/", role_selector_view, name="turbodrf-docs"),
    path("api/set-role/", set_api_role, name="turbodrf-set-role"),
    path("swagger/", schema_view.with_ui("swagger", cache_timeout=0)),
    path("redoc/", schema_view.with_ui("redoc", cache_timeout=0)),
]
