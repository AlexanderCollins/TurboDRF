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


def check_permissions_enabled(app_configs=None, **kwargs):
    """Warn when TURBODRF_DISABLE_PERMISSIONS is on outside DEBUG: it lets
    anonymous callers create, update and delete through every generated
    endpoint, and is for local development only."""
    if settings.DEBUG or not getattr(settings, "TURBODRF_DISABLE_PERMISSIONS", False):
        return []
    return [
        checks.Warning(
            "TURBODRF_DISABLE_PERMISSIONS is True with DEBUG off: anonymous "
            "callers can create, update and delete through every TurboDRF "
            "endpoint.",
            hint=(
                "It's for local development only. Remove it from deployed "
                "settings and give users roles (docs/permissions.md)."
            ),
            id="turbodrf.W002",
        )
    ]


def check_roles_configured(app_configs=None, **kwargs):
    """Warn when static mode has no roles: every authenticated request to a
    TurboDRF endpoint is then a 403, with nothing saying why."""
    if (
        getattr(settings, "TURBODRF_PERMISSION_MODE", "static") != "static"
        or getattr(settings, "TURBODRF_DISABLE_PERMISSIONS", False)
        or getattr(settings, "TURBODRF_USE_DEFAULT_PERMISSIONS", False)
        or getattr(settings, "TURBODRF_ROLES", None)
    ):
        return []
    return [
        checks.Warning(
            "TURBODRF_ROLES is empty: no role grants anything, so every "
            "authenticated request to a TurboDRF endpoint is a 403.",
            hint=(
                "Define TURBODRF_ROLES (docs/permissions.md), or set "
                "TURBODRF_PERMISSION_MODE = 'database' to keep roles in the "
                "database."
            ),
            id="turbodrf.W003",
        )
    ]


def check_role_source(app_configs=None, **kwargs):
    """Warn when TURBODRF_ROLE_GETTER is set alongside the allauth or Keycloak
    integration: the getter decides the roles, so the integration's ``roles``
    are never read."""
    integration = [
        name
        for name in ("TURBODRF_ALLAUTH_INTEGRATION", "TURBODRF_KEYCLOAK_INTEGRATION")
        if getattr(settings, name, False)
    ]
    if not integration or getattr(settings, "TURBODRF_ROLE_GETTER", None) is None:
        return []
    return [
        checks.Warning(
            f"TURBODRF_ROLE_GETTER is set, so {', '.join(integration)} has no "
            f"effect on roles: the getter decides them.",
            hint="Remove one of them, or have the getter read the integration's roles.",
            id="turbodrf.W005",
        )
    ]


def check_explicit_fields(app_configs=None, **kwargs):
    """Warn for each TurboDRF model whose ``fields`` is ``'__all__'`` (the
    default): every column is exposed, a column added later included. A model
    that inherits the mixin from an abstract base without its own
    ``turbodrf()`` gets that default without anyone choosing it."""
    from .mixins import turbodrf_config, turbodrf_models

    messages = []
    for model in turbodrf_models():
        try:
            config = turbodrf_config(model)
        except Exception:
            continue  # the router reports a broken config
        if not isinstance(config, dict) or not config.get("enabled", True):
            continue
        fields = config.get("fields", "__all__")
        if fields == "__all__" or (
            isinstance(fields, dict) and "__all__" in fields.values()
        ):
            messages.append(
                checks.Warning(
                    f"{model._meta.label} exposes every field ('__all__'), "
                    f"including any column added later.",
                    hint="List the fields the API should expose in 'fields'.",
                    obj=model,
                    id="turbodrf.W004",
                )
            )
    return messages


class TurboDRFConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "turbodrf"
    verbose_name = "TurboDRF"

    def ready(self):
        """
        Perform initialization when Django starts.
        """
        checks.register(check_docs_dependencies)
        checks.register(check_permissions_enabled, checks.Tags.security)
        checks.register(check_roles_configured)
        checks.register(check_explicit_fields)
        checks.register(check_role_source)

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
