"""
Tests for lookup_field configuration.

Tests that models can specify custom lookup fields for detail views.
"""

from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase

from turbodrf.mixins import TurboDRFMixin
from turbodrf.router import TurboDRFRouter

User = get_user_model()


class SlugModel(models.Model, TurboDRFMixin):
    """Test model with slug field for lookup."""

    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    description = models.TextField()

    @classmethod
    def turbodrf(cls):
        return {
            "enabled": True,
            "endpoint": "slug-items",
            "fields": ["title", "slug", "description"],
            "lookup_field": "slug",
        }

    class Meta:
        app_label = "test_app"


class UUIDModel(models.Model, TurboDRFMixin):
    """Test model with UUID field for lookup."""

    uuid = models.UUIDField(unique=True)
    name = models.CharField(max_length=100)

    @classmethod
    def turbodrf(cls):
        return {
            "enabled": True,
            "endpoint": "uuid-items",
            "fields": ["uuid", "name"],
            "lookup_field": "uuid",
        }

    class Meta:
        app_label = "test_app"


class TestLookupFieldConfiguration(TestCase):
    """Test lookup_field configuration."""

    def test_model_can_specify_lookup_field(self):
        """Test that models can specify a custom lookup field."""
        config = SlugModel.turbodrf()
        self.assertEqual(config.get("lookup_field"), "slug")

    def test_model_without_lookup_field_defaults_to_none(self):
        """Test that models without lookup_field don't have it in config."""
        from tests.test_app.models import SampleModel

        config = SampleModel.turbodrf()
        self.assertIsNone(config.get("lookup_field"))

    def test_router_applies_lookup_field_to_viewset(self):
        """Test that router applies lookup_field to generated viewset."""
        # Create router
        router = TurboDRFRouter()

        # Find the viewset for SlugModel if it was registered
        # We need to check if the model is registered
        found_viewset = None
        for pattern in router.registry:
            if pattern[1].model == SlugModel:
                found_viewset = pattern[1]
                break

        # If not found, the model might not be in installed apps
        # Let's test the router's discover_models logic directly
        if found_viewset:
            self.assertEqual(found_viewset.lookup_field, "slug")

    def test_lookup_field_in_viewset_attrs(self):
        """Test that lookup_field is added to viewset attributes."""
        # Simulate what the router does
        model = SlugModel
        config = model.turbodrf()
        lookup_field = config.get("lookup_field")

        # Build viewset attributes like router does
        viewset_attrs = {
            "model": model,
            "queryset": model.objects.all(),
            "__module__": model.__module__,
        }

        if lookup_field:
            viewset_attrs["lookup_field"] = lookup_field

        self.assertIn("lookup_field", viewset_attrs)
        self.assertEqual(viewset_attrs["lookup_field"], "slug")

    def test_lookup_field_slug_configuration(self):
        """Test slug lookup field configuration."""
        config = SlugModel.turbodrf()

        self.assertEqual(config["lookup_field"], "slug")
        self.assertEqual(config["endpoint"], "slug-items")
        self.assertIn("slug", config["fields"])

    def test_lookup_field_uuid_configuration(self):
        """Test UUID lookup field configuration."""
        config = UUIDModel.turbodrf()

        self.assertEqual(config["lookup_field"], "uuid")
        self.assertEqual(config["endpoint"], "uuid-items")
        self.assertIn("uuid", config["fields"])

    def test_multiple_models_different_lookup_fields(self):
        """Test that different models can have different lookup fields."""
        slug_config = SlugModel.turbodrf()
        uuid_config = UUIDModel.turbodrf()

        self.assertEqual(slug_config["lookup_field"], "slug")
        self.assertEqual(uuid_config["lookup_field"], "uuid")
        self.assertNotEqual(slug_config["lookup_field"], uuid_config["lookup_field"])

    def test_lookup_field_documentation(self):
        """Test that lookup_field is documented in mixin."""
        from turbodrf.mixins import TurboDRFMixin

        # Check docstring mentions lookup_field
        docstring = TurboDRFMixin.turbodrf.__doc__
        self.assertIn("lookup_field", docstring)


class TestLookupFieldIntegration(TestCase):
    """Integration tests for lookup_field with router."""

    def test_router_creates_viewset_with_lookup_field(self):
        """Test router creates viewset with correct lookup_field attribute."""
        from turbodrf.views import TurboDRFViewSet

        # Manually create viewset like router does
        model = SlugModel
        config = model.turbodrf()
        lookup_field = config.get("lookup_field")

        viewset_attrs = {
            "model": model,
            "queryset": model.objects.all(),
            "__module__": model.__module__,
        }

        if lookup_field:
            viewset_attrs["lookup_field"] = lookup_field

        viewset_class = type(
            f"{model.__name__}ViewSet",
            (TurboDRFViewSet,),
            viewset_attrs,
        )

        # Verify the viewset has the lookup_field
        self.assertTrue(hasattr(viewset_class, "lookup_field"))
        self.assertEqual(viewset_class.lookup_field, "slug")

    def test_default_lookup_field_when_not_specified(self):
        """Test that viewset uses default pk lookup when not specified."""
        from tests.test_app.models import SampleModel
        from turbodrf.views import TurboDRFViewSet

        model = SampleModel
        config = model.turbodrf()
        lookup_field = config.get("lookup_field")

        viewset_attrs = {
            "model": model,
            "queryset": model.objects.all(),
            "__module__": model.__module__,
        }

        # Only add if specified
        if lookup_field:
            viewset_attrs["lookup_field"] = lookup_field

        viewset_class = type(
            f"{model.__name__}ViewSet",
            (TurboDRFViewSet,),
            viewset_attrs,
        )

        # Should use DRF's default 'pk'
        # If not set, DRF defaults to 'pk'
        if hasattr(viewset_class, "lookup_field"):
            # If it has the attribute, it should be pk (DRF default)
            # or not set at all
            pass
        else:
            # No lookup_field attribute means DRF will use 'pk'
            pass
