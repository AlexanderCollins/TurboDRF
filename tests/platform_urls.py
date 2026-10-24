"""
URLconf for the platform-scope tests (``ROOT_URLCONF="tests.platform_urls"``):

- ``api/``: the tenant API (tests/urls.py), unchanged;
- ``console-api/``: ``TurboDRFRouter(scope="platform", gate=[IsPlatformStaff])``;
- ``console-audited/``: a router subclass (``scope`` / ``gate`` as class
  attributes) that wraps each generated viewset in a mixin, as downstream
  apps do, here to log platform reads of a raw payload;
- ``console-swagger/``: the platform router's own schema view.
"""

from django.urls import include, path
from rest_framework.permissions import BasePermission

from tests.urls import urlpatterns as tenant_urlpatterns
from turbodrf.router import TurboDRFRouter

# (user pk, model, row pk) for every platform read of a raw payload.
PAYLOAD_READS = []


class IsPlatformStaff(BasePermission):
    """The console gate: authenticated platform staff only."""

    message = "Platform staff only."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.is_staff)


class PayloadAudit:
    """Mixin for generated viewsets: logs who read a payload on the platform
    scope."""

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        if self.turbodrf_scope == "platform" and "payload" in response.data:
            PAYLOAD_READS.append(
                (request.user.pk, self.model.__name__, response.data["id"])
            )
        return response


class AuditedConsoleRouter(TurboDRFRouter):
    scope = "platform"
    gate = [IsPlatformStaff]

    def discover_models(self):
        super().discover_models()
        self.registry = [
            (
                prefix,
                type(view.__name__, (PayloadAudit, view), {"__module__": __name__}),
                basename,
            )
            for prefix, view, basename in self.registry
        ]


platform_router = TurboDRFRouter(scope="platform", gate=[IsPlatformStaff])
audited_router = AuditedConsoleRouter()
platform_schema_view = platform_router.get_schema_view(title="Console API")

urlpatterns = [
    *tenant_urlpatterns,
    path("console-api/", include(platform_router.urls)),
    path("console-audited/", include(audited_router.urls)),
]
if platform_schema_view:
    urlpatterns.append(
        path(
            "console-swagger/",
            platform_schema_view.with_ui("swagger", cache_timeout=0),
        )
    )
