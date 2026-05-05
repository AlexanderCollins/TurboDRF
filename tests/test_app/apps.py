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

        # Brokerage attribute for tenant tests. We need this to survive a
        # fresh DB fetch (validated_data in DRF refetches the User), so we
        # use a class-level registry keyed by user.pk plus the instance
        # attribute as a fallback.
        def get_user_brokerage(self):
            if self.pk and self.pk in _test_user_brokerages:
                return _test_user_brokerages[self.pk]
            return getattr(self, "_test_brokerage", None)

        if not hasattr(User, "brokerage"):
            User.add_to_class("brokerage", property(get_user_brokerage))


# Class-level registry: user.pk → Brokerage instance.
# Tests populate this in setUp to make `user.brokerage` work after DB re-fetch.
_test_user_brokerages = {}


def set_test_brokerage(user, brokerage):
    """Helper for tests: set the user's brokerage on both the instance and
    the class-level registry so DRF's PrimaryKeyRelatedField re-fetch returns
    a User with the brokerage attribute populated."""
    user._test_brokerage = brokerage
    if user.pk:
        _test_user_brokerages[user.pk] = brokerage
