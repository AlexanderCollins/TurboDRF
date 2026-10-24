from django.apps import AppConfig, apps
from django.conf import settings
from django.core import checks


def check_docs_dependencies(app_configs=None, **kwargs):
    """Warn when the API docs are enabled but drf_yasg isn't an installed app.

    The Swagger / ReDoc pages render drf_yasg's templates, which Django only
    finds when 'drf_yasg' is in INSTALLED_APPS. TurboDRF can't add it for you:
    by the time an app's ready() runs, the app registry is already populated,
    so editing settings.INSTALLED_APPS there has no effect.
    """
    if not getattr(settings, "TURBODRF_ENABLE_DOCS", True):
        return []
    if apps.is_installed("drf_yasg"):
        return []
    return [
        checks.Warning(
            "TURBODRF_ENABLE_DOCS is True but 'drf_yasg' is not in "
            "INSTALLED_APPS, so the Swagger and ReDoc pages will fail with "
            "TemplateDoesNotExist.",
            hint=(
                "Add 'drf_yasg' to INSTALLED_APPS, or set "
                "TURBODRF_ENABLE_DOCS = False if you don't serve the docs."
            ),
            id="turbodrf.W001",
        )
    ]


class TurboDRFConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "turbodrf"
    verbose_name = "TurboDRF"

    def ready(self):
        """
        Perform initialization when Django starts.
        """
        checks.register(check_docs_dependencies)

        # Register every TurboDRF model's tenant boundary and predicates now,
        # not when the URLconf first builds a router: FK-injection checks and
        # nested-value / filter scoping read a target model's scoping from
        # this registry, and a process that hasn't built a router (a Celery
        # worker, a WSGI worker before its first request) would otherwise
        # treat every model as unscoped. A tenancy misconfiguration raises
        # here. Each router build re-registers and runs the startup gates.
        from .tenancy import _register_tenancy

        _register_tenancy()

        # Resolve Keycloak / allauth roles from the user object so they also
        # reach token/JWT-authenticated users (the integrations' middleware
        # runs before DRF authentication).
        if getattr(settings, "TURBODRF_KEYCLOAK_INTEGRATION", False) or getattr(
            settings, "TURBODRF_ALLAUTH_INTEGRATION", False
        ):
            from .integrations.roles import install_integration_role_resolver

            install_integration_role_resolver()
