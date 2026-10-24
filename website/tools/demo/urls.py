from django.urls import include, path

from turbodrf.router import TurboDRFRouter

router = TurboDRFRouter()

urlpatterns = [path("api/", include(router.urls))]
