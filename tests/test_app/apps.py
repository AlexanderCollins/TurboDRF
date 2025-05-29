from django.apps import AppConfig


class TestAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tests.test_app"

    def ready(self):
        # Extend User model with roles for testing
        from django.contrib.auth import get_user_model

        User = get_user_model()

        def get_user_roles(self):
            # Simple role assignment for tests
            if hasattr(self, "_test_roles"):
                return self._test_roles
            elif self.is_superuser:
                return ["admin"]
            elif self.is_staff:
                return ["editor"]
            else:
                return ["viewer"]

        if not hasattr(User, "roles"):
            User.add_to_class("roles", property(get_user_roles))
