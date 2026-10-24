from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Every TurboDRF model, plus /api/swagger/ and /api/redoc/.
    path("api/", include("turbodrf.urls")),
]
