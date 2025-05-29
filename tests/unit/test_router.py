"""
Unit tests for TurboDRF router.

Tests the automatic model discovery and URL registration.
"""

from django.test import TestCase, override_settings
from django.urls import reverse
from turbodrf.router import TurboDRFRouter
from tests.test_app.models import (
    SampleModel,
    RelatedModel,
    NoTurboDRFModel,
    CustomEndpointModel,
    DisabledModel,
)


class TestTurboDRFRouter(TestCase):
    """Test cases for TurboDRF router functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.router = TurboDRFRouter()

    def test_router_initialization(self):
        """Test router is properly initialized."""
        self.assertIsInstance(self.router, TurboDRFRouter)
        # Check that models were discovered
        self.assertTrue(len(self.router.registry) > 0)

    def test_model_discovery(self):
        """Test that models with TurboDRFMixin are discovered."""
        # Get registered model names
        registered_models = [item[1].model for item in self.router.registry]

        # Models with TurboDRFMixin should be registered
        self.assertIn(SampleModel, registered_models)
        self.assertIn(RelatedModel, registered_models)

        # Model without TurboDRFMixin should not be registered
        self.assertNotIn(NoTurboDRFModel, registered_models)

    def test_disabled_model_not_registered(self):
        """Test that disabled models are not registered."""
        registered_models = [item[1].model for item in self.router.registry]
        self.assertNotIn(DisabledModel, registered_models)

    def test_custom_endpoint_registration(self):
        """Test custom endpoint name registration."""
        # Find the custom endpoint model registration
        custom_registration = None
        for url_pattern, viewset, basename in self.router.registry:
            if viewset.model == CustomEndpointModel:
                custom_registration = (url_pattern, viewset, basename)
                break

        self.assertIsNotNone(custom_registration)
        self.assertEqual(custom_registration[0], "custom-items")

    def test_default_endpoint_naming(self):
        """Test default endpoint naming (pluralized model name)."""
        # Find SampleModel registration
        test_model_registration = None
        for url_pattern, viewset, basename in self.router.registry:
            if viewset.model == SampleModel:
                test_model_registration = (url_pattern, viewset, basename)
                break

        self.assertIsNotNone(test_model_registration)
        self.assertEqual(test_model_registration[0], "samplemodels")
        self.assertEqual(test_model_registration[2], "samplemodel")

    def test_viewset_creation(self):
        """Test that viewsets are properly created for models."""
        for url_pattern, viewset_class, basename in self.router.registry:
            # Check viewset has correct base class
            from turbodrf.views import TurboDRFViewSet

            self.assertTrue(issubclass(viewset_class, TurboDRFViewSet))

            # Check viewset has model attribute
            self.assertTrue(hasattr(viewset_class, "model"))

            # Check viewset has queryset
            self.assertTrue(hasattr(viewset_class, "queryset"))

    def test_url_patterns_generated(self):
        """Test that URL patterns are generated."""
        urls = self.router.urls
        self.assertTrue(len(urls) > 0)

        # Check for expected URL patterns
        url_names = [url.name for url in urls if hasattr(url, "name")]

        # Should have list and detail views for each model
        self.assertIn("samplemodel-list", url_names)
        self.assertIn("samplemodel-detail", url_names)
        self.assertIn("relatedmodel-list", url_names)
        self.assertIn("relatedmodel-detail", url_names)
        self.assertIn("api-root", url_names)

    def test_api_root_included(self):
        """Test that API root view is included."""
        urls = self.router.urls
        root_urls = [
            url for url in urls if hasattr(url, "name") and url.name == "api-root"
        ]
        self.assertTrue(len(root_urls) > 0)

    def test_multiple_router_instances(self):
        """Test that multiple router instances work independently."""
        router1 = TurboDRFRouter()
        router2 = TurboDRFRouter()

        # Both should discover the same models
        self.assertEqual(len(router1.registry), len(router2.registry))

    def test_model_with_all_fields(self):
        """Test model registration with '__all__' fields configuration."""
        # This tests that the router doesn't fail when encountering
        # a model with '__all__' fields configuration
        registered = False
        for url_pattern, viewset, basename in self.router.registry:
            if viewset.model in [SampleModel, RelatedModel, CustomEndpointModel]:
                registered = True
                break

        self.assertTrue(registered)
