from django.apps import AppConfig
from django.conf import settings


class TurboDRFConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "turbodrf"
    verbose_name = "TurboDRF"

    def ready(self):
        """
        Perform initialization when Django starts.
        """
        # Auto-configure drf_yasg if documentation is enabled
        if getattr(settings, "TURBODRF_ENABLE_DOCS", True):
            self._ensure_drf_yasg_installed()

    def _ensure_drf_yasg_installed(self):
        """
        Ensure drf_yasg is in INSTALLED_APPS for template loading.

        This method checks if drf_yasg is already in INSTALLED_APPS and adds it
        if necessary. This prevents the TemplateDoesNotExist error when accessing
        API documentation endpoints.
        """
        if "drf_yasg" not in settings.INSTALLED_APPS:
            # Convert to list if it's a tuple
            if isinstance(settings.INSTALLED_APPS, tuple):
                settings.INSTALLED_APPS = list(settings.INSTALLED_APPS)

            # Add drf_yasg before turbodrf to ensure proper loading order
            turbodrf_index = settings.INSTALLED_APPS.index("turbodrf")
            settings.INSTALLED_APPS.insert(turbodrf_index, "drf_yasg")
