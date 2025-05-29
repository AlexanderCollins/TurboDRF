"""
URL configuration for source project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path

from turbodrf.documentation import get_turbodrf_schema_view
from turbodrf.router import TurboDRFRouter

# Create the TurboDRF router
router = TurboDRFRouter()

# Get the schema view for API documentation
schema_view = get_turbodrf_schema_view()

urlpatterns = [
    path("admin/", admin.site.urls),
    # API endpoints
    path("api/", include(router.urls)),
]

# Only add documentation URLs if enabled (default: True)
# Set TURBODRF_ENABLE_DOCS = False in settings to disable
if schema_view:  # Will be None if docs are disabled
    urlpatterns += [
        # API Documentation
        path(
            "swagger/",
            schema_view.with_ui("swagger", cache_timeout=0),
            name="schema-swagger-ui",
        ),
        path(
            "redoc/", schema_view.with_ui("redoc", cache_timeout=0), name="schema-redoc"
        ),
    ]
