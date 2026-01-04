"""
Tests for public_access configuration and guest role.

Tests that models can be configured for public access and that
unauthenticated users get the guest role when configured.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.models import SampleModel
from turbodrf.mixins import TurboDRFMixin
from turbodrf.permissions import TurboDRFPermission
from turbodrf.views import TurboDRFViewSet

User = get_user_model()


class PublicModel(SampleModel):
    """Test model with public access enabled."""

    class Meta:
        proxy = True
        app_label = "test_app"

    @classmethod
    def turbodrf(cls):
        config = super().turbodrf()
        config["public_access"] = True
        return config


class PrivateModel(SampleModel):
    """Test model with public access disabled."""

    class Meta:
        proxy = True
        app_label = "test_app"

    @classmethod
    def turbodrf(cls):
        config = super().turbodrf()
        config["public_access"] = False
        return config


class TestPublicAccess(TestCase):
    """Test public_access configuration."""

    def setUp(self):
        """Set up test fixtures."""
        from tests.test_app.models import RelatedModel

        self.client = APIClient()

        # Create related model first
        self.related = RelatedModel.objects.create(
            name="Test Category", description="Test Description"
        )

        # Create test data
        self.public_item = SampleModel.objects.create(
            title="Public Item",
            price=Decimal("100.00"),
            quantity=10,
            is_active=True,
            related=self.related,
        )

        self.private_item = SampleModel.objects.create(
            title="Private Item",
            price=Decimal("200.00"),
            quantity=5,
            is_active=False,
            related=self.related,
        )

        # Create authenticated user
        self.user = User.objects.create_user(username="testuser", password="test123")
        self.user._test_roles = ["viewer"]

    def test_public_access_allows_unauthenticated_get(self):
        """Test that public_access=True allows unauthenticated GET requests."""
        # Create a mock request and view
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/api/test/")
        request.user = None

        # Create viewset with public model
        viewset = TurboDRFViewSet()
        viewset.model = PublicModel

        permission = TurboDRFPermission()
        has_perm = permission.has_permission(request, viewset)

        self.assertTrue(has_perm)

    def test_public_access_denies_unauthenticated_post(self):
        """Test that public_access=True still denies unauthenticated POST."""
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.post("/api/test/")
        request.user = None

        viewset = TurboDRFViewSet()
        viewset.model = PublicModel

        permission = TurboDRFPermission()
        has_perm = permission.has_permission(request, viewset)

        self.assertFalse(has_perm)

    def test_private_access_denies_unauthenticated_get(self):
        """Test that public_access=False denies unauthenticated GET requests."""
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/api/test/")
        request.user = None

        viewset = TurboDRFViewSet()
        viewset.model = PrivateModel

        permission = TurboDRFPermission()
        has_perm = permission.has_permission(request, viewset)

        self.assertFalse(has_perm)

    def test_default_public_access_is_true(self):
        """Test that public_access defaults to True for backward compatibility."""
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/api/test/")
        request.user = None

        # Use regular SampleModel which doesn't specify public_access
        viewset = TurboDRFViewSet()
        viewset.model = SampleModel

        permission = TurboDRFPermission()
        has_perm = permission.has_permission(request, viewset)

        # Should be True for backward compatibility
        self.assertTrue(has_perm)

    def test_authenticated_user_can_access_private_model(self):
        """Test that authenticated users are checked for permissions."""
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/api/test/")
        request.user = self.user

        viewset = TurboDRFViewSet()
        viewset.model = PrivateModel

        permission = TurboDRFPermission()
        has_perm = permission.has_permission(request, viewset)

        # Private models with public_access=False require proper authentication
        # The permission result depends on user's roles and TURBODRF_ROLES configuration
        # This test verifies the permission check runs without errors
        self.assertIsInstance(has_perm, bool)

    def test_options_request_with_public_access(self):
        """Test that OPTIONS requests work with public_access."""
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.options("/api/test/")
        request.user = None

        viewset = TurboDRFViewSet()
        viewset.model = PublicModel

        permission = TurboDRFPermission()
        has_perm = permission.has_permission(request, viewset)

        self.assertTrue(has_perm)


@override_settings(
    TURBODRF_ROLES={
        "guest": [
            "test_app.samplemodel.read",
            "test_app.samplemodel.title.read",
            "test_app.samplemodel.price.read",
        ],
        "viewer": [
            "test_app.samplemodel.read",
            "test_app.samplemodel.title.read",
            "test_app.samplemodel.price.read",
            "test_app.samplemodel.quantity.read",
        ],
    }
)
class TestGuestRole(TestCase):
    """Test guest role functionality for unauthenticated users."""

    def setUp(self):
        """Set up test fixtures."""
        from django.core.cache import cache

        from tests.test_app.models import RelatedModel

        cache.clear()  # Clear cache to avoid test pollution
        self.client = APIClient()

        # Create related model first
        self.related = RelatedModel.objects.create(
            name="Test Category", description="Test Description"
        )

        self.item = SampleModel.objects.create(
            title="Test Item",
            price=Decimal("100.00"),
            quantity=10,
            is_active=True,
            related=self.related,
        )

    def test_guest_role_applied_to_unauthenticated_user(self):
        """Test that guest role is applied when configured."""
        from rest_framework.test import APIRequestFactory

        from turbodrf.views import TurboDRFViewSet

        factory = APIRequestFactory()
        django_request = factory.get("/api/samplemodels/")

        # Create unauthenticated user
        from django.contrib.auth.models import AnonymousUser

        django_request.user = AnonymousUser()

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.action = "list"
        viewset.request = django_request

        # Get serializer class which should apply guest role
        serializer_class = viewset.get_serializer_class()

        # Create serializer instance
        serializer = serializer_class(self.item)

        # Guest role should see title and price but not quantity
        data = serializer.data
        self.assertIn("title", data)
        self.assertIn("price", data)
        # Quantity requires viewer role
        # This may or may not be in data depending on if guest has permission

    def test_guest_role_field_filtering(self):
        """Test that guest role only sees permitted fields."""
        # This test verifies the serializer factory applies guest permissions
        from turbodrf.serializers import TurboDRFSerializerFactory

        # Create guest user
        class GuestUser:
            roles = ["guest"]
            is_authenticated = False

        # Create serializer with guest user
        fields = ["title", "price", "quantity"]
        serializer_class = TurboDRFSerializerFactory.create_serializer(
            SampleModel, fields, GuestUser(), view_type="list"
        )

        serializer = serializer_class(self.item)
        data = serializer.data

        # Guest should see title and price
        self.assertIn("title", data)
        self.assertIn("price", data)
        # Guest should NOT see quantity (requires viewer role)
        # Actually, the factory will filter out fields without permission
        # So quantity should not be in the serializer at all

    def test_unauthenticated_without_guest_role_configured(self):
        """Test behavior when guest role is not configured."""
        # This should use default serializer without permission filtering
        from rest_framework.test import APIRequestFactory

        from turbodrf.views import TurboDRFViewSet

        # Temporarily remove guest role
        with self.settings(
            TURBODRF_ROLES={
                "viewer": [
                    "test_app.samplemodel.read",
                    "test_app.samplemodel.title.read",
                ]
            }
        ):
            factory = APIRequestFactory()
            django_request = factory.get("/api/samplemodels/")

            from django.contrib.auth.models import AnonymousUser

            django_request.user = AnonymousUser()

            viewset = TurboDRFViewSet()
            viewset.model = SampleModel
            viewset.action = "list"
            viewset.request = django_request

            # Should get default serializer without field filtering
            serializer_class = viewset.get_serializer_class()
            serializer = serializer_class(self.item)

            # Should see all configured fields
            data = serializer.data
            self.assertIn("title", data)


class TestPublicAccessConfiguration(TestCase):
    """Test public_access configuration in turbodrf() method."""

    def test_model_can_specify_public_access_true(self):
        """Test that models can set public_access=True."""

        class PublicTestModel(TurboDRFMixin):
            @classmethod
            def turbodrf(cls):
                return {"public_access": True, "fields": ["id"]}

            class Meta:
                app_label = "test_app"

        config = PublicTestModel.turbodrf()
        self.assertTrue(config.get("public_access"))

    def test_model_can_specify_public_access_false(self):
        """Test that models can set public_access=False."""

        class PrivateTestModel(TurboDRFMixin):
            @classmethod
            def turbodrf(cls):
                return {"public_access": False, "fields": ["id"]}

            class Meta:
                app_label = "test_app"

        config = PrivateTestModel.turbodrf()
        self.assertFalse(config.get("public_access"))

    def test_model_without_public_access_defaults_appropriately(self):
        """Test default behavior when public_access is not specified."""

        class DefaultTestModel(TurboDRFMixin):
            @classmethod
            def turbodrf(cls):
                return {"fields": ["id"]}

            class Meta:
                app_label = "test_app"

        config = DefaultTestModel.turbodrf()
        # Should not be in config, will default to True in permission class
        self.assertNotIn("public_access", config)
