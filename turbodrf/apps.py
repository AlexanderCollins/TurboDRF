from django.apps import AppConfig


class TurboDRFConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "turbodrf"
    verbose_name = "TurboDRF"

    def ready(self):
        """
        Perform initialization when Django starts.
        """
        # This ensures models are discovered when Django starts
        # You can add any other initialization code here
        pass
