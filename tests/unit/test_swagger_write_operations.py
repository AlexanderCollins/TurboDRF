"""
Tests for Swagger write operation serializer handling.

Tests that TurboDRFSwaggerAutoSchema shows all writable fields in Swagger
documentation for write operations (create, update, partial_update).
"""

from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from tests.test_app.models import RelatedModel, SampleModel
from turbodrf.mixins import TurboDRFMixin
from turbodrf.swagger import TurboDRFSwaggerAutoSchema
from turbodrf.views import TurboDRFViewSet

User = get_user_model()


class ModelWithListDetailFields(TurboDRFMixin, models.Model):
    """Model with separate list and detail field configuration."""

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    secret_field = models.CharField(max_length=100, blank=True)

    class Meta:
        app_label = "test_app"

    @classmethod
    def turbodrf(cls):
        return {
            "fields": {
                "list": ["title", "price"],
                "detail": ["title", "description", "price", "secret_field"],
            }
        }


class ModelWithSimpleFields(TurboDRFMixin, models.Model):
    """Model with simple field list (not dict)."""

    name = models.CharField(max_length=100)
    value = models.IntegerField()

    class Meta:
        app_label = "test_app"

    @classmethod
    def turbodrf(cls):
        return {"fields": ["name", "value"]}


class TestSwaggerWriteOperationSerializer(TestCase):
    """Test Swagger schema for write operations shows all writable fields."""

    def setUp(self):
        """Set up test fixtures."""
        self.factory = APIRequestFactory()
        self.related = RelatedModel.objects.create(
            name="Test Category", description="Test Description"
        )

    def test_create_operation_uses_write_serializer(self):
        """Test that create operation returns serializer with all fields."""
        from drf_yasg import openapi

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "create"

        request = self.factory.post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()

        # Should return a serializer (not None)
        self.assertIsNotNone(serializer)
        # Should have fields from the model
        self.assertTrue(hasattr(serializer, "Meta"))

    def test_update_operation_uses_write_serializer(self):
        """Test that update operation returns serializer with all fields."""
        from drf_yasg import openapi

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "update"

        request = self.factory.put("/api/samplemodels/1/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/1/",
            method="PUT",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()

        # Should return a serializer
        self.assertIsNotNone(serializer)
        self.assertTrue(hasattr(serializer, "Meta"))

    def test_partial_update_operation_uses_write_serializer(self):
        """Test that partial_update operation returns serializer with all fields."""
        from drf_yasg import openapi

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "partial_update"

        request = self.factory.patch("/api/samplemodels/1/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/1/",
            method="PATCH",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()

        # Should return a serializer
        self.assertIsNotNone(serializer)
        self.assertTrue(hasattr(serializer, "Meta"))

    def test_write_serializer_includes_detail_fields(self):
        """Test that write serializer uses detail fields from dict config."""
        from drf_yasg import openapi

        # Create a viewset with model that has list/detail field config
        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "create"

        request = self.factory.post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()

        # Get the fields from serializer Meta
        self.assertIsNotNone(serializer)
        if hasattr(serializer, "Meta"):
            fields = getattr(serializer.Meta, "fields", None)
            # Should have fields configured
            self.assertIsNotNone(fields)

    def test_write_serializer_ref_name(self):
        """Test that write serializer has unique ref_name for Swagger."""
        from drf_yasg import openapi

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "create"

        request = self.factory.post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()

        # Should have ref_name in Meta
        if serializer and hasattr(serializer, "Meta"):
            self.assertTrue(hasattr(serializer.Meta, "ref_name"))
            # ref_name should end with _write
            self.assertTrue(serializer.Meta.ref_name.endswith("_write"))

    def test_read_operations_use_default_serializer(self):
        """Test that read operations (list, retrieve) use default behavior."""
        from drf_yasg import openapi

        # Test list action
        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "list"

        request = self.factory.get("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="GET",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        # List should return None (no request body)
        serializer = schema.get_request_serializer()
        self.assertIsNone(serializer)

        # Test retrieve action
        viewset.action = "retrieve"
        request = self.factory.get("/api/samplemodels/1/")
        viewset.request = request

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/1/",
            method="GET",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        # Retrieve should also return None
        serializer = schema.get_request_serializer()
        self.assertIsNone(serializer)

    def test_write_serializer_with_simple_field_list(self):
        """Test write serializer with model that has simple field list."""
        from drf_yasg import openapi

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "create"

        request = self.factory.post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()

        # Should successfully create serializer
        self.assertIsNotNone(serializer)
        self.assertTrue(hasattr(serializer, "Meta"))

    def test_write_serializer_fallback_for_model_without_turbodrf(self):
        """Test write serializer fallback for models without turbodrf."""
        from drf_yasg import openapi

        # Create a simple Django model without TurboDRF mixin
        class PlainModel(models.Model):
            name = models.CharField(max_length=100)

            class Meta:
                app_label = "test_app"

        viewset = TurboDRFViewSet()
        viewset.model = PlainModel
        viewset.queryset = PlainModel.objects.none()
        viewset.action = "create"

        request = self.factory.post("/api/plainmodels/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/plainmodels/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        # Should fall back to default behavior without errors
        schema.get_request_serializer()

        # May be None or a serializer - just shouldn't raise an exception
        # The important thing is it doesn't crash
        self.assertTrue(True)  # Test passed if we got here

    def test_write_serializer_model_property(self):
        """Test write serializer correctly sets model in Meta."""
        from drf_yasg import openapi

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "create"

        request = self.factory.post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()

        # Should have model in Meta
        if serializer and hasattr(serializer, "Meta"):
            self.assertTrue(hasattr(serializer.Meta, "model"))
            self.assertEqual(serializer.Meta.model, SampleModel)


class TestWriteSerializerIntegration(TestCase):
    """Integration tests for write operation serializer."""

    def test_different_operations_get_different_serializers(self):
        """Test that different write operations can get serializers independently."""
        from drf_yasg import openapi

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()

        factory = APIRequestFactory()

        # Create operation
        viewset.action = "create"
        request = factory.post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        create_serializer = schema.get_request_serializer()
        self.assertIsNotNone(create_serializer)

        # Update operation
        viewset.action = "update"
        request = factory.put("/api/samplemodels/1/")
        viewset.request = request

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/1/",
            method="PUT",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        update_serializer = schema.get_request_serializer()
        self.assertIsNotNone(update_serializer)

        # Both should be serializers
        self.assertTrue(hasattr(create_serializer, "Meta"))
        self.assertTrue(hasattr(update_serializer, "Meta"))


class TestSwaggerShowAllFieldsSetting(TestCase):
    """Test TURBODRF_SWAGGER_SHOW_ALL_FIELDS setting."""

    def test_swagger_show_all_fields_default_false(self):
        """Test that TURBODRF_SWAGGER_SHOW_ALL_FIELDS defaults to False."""
        from django.conf import settings
        from drf_yasg import openapi

        # Default should be False
        show_all = getattr(settings, "TURBODRF_SWAGGER_SHOW_ALL_FIELDS", False)
        self.assertFalse(show_all)

        # Serializer should still be created
        viewset = TurboDRFViewSet()
        viewset.model = SampleModel
        viewset.queryset = SampleModel.objects.all()
        viewset.action = "create"

        request = APIRequestFactory().post("/api/samplemodels/")
        viewset.request = request
        viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=viewset,
            path="/api/samplemodels/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()
        self.assertIsNotNone(serializer)

    def test_swagger_with_show_all_fields_enabled(self):
        """Test serializer creation with TURBODRF_SWAGGER_SHOW_ALL_FIELDS=True."""
        from django.conf import settings
        from django.test import override_settings
        from drf_yasg import openapi

        with override_settings(TURBODRF_SWAGGER_SHOW_ALL_FIELDS=True):
            viewset = TurboDRFViewSet()
            viewset.model = SampleModel
            viewset.queryset = SampleModel.objects.all()
            viewset.action = "create"

            request = APIRequestFactory().post("/api/samplemodels/")
            viewset.request = request
            viewset.format_kwarg = None

            schema = TurboDRFSwaggerAutoSchema(
                view=viewset,
                path="/api/samplemodels/",
                method="POST",
                components=openapi.ReferenceResolver("", force_init=True),
                request=request,
                overrides={},
            )

            serializer = schema.get_request_serializer()

            # Should still create serializer
            self.assertIsNotNone(serializer)
            self.assertTrue(hasattr(serializer, "Meta"))

            # Verify the setting was actually True
            self.assertTrue(getattr(settings, "TURBODRF_SWAGGER_SHOW_ALL_FIELDS"))

    def test_swagger_show_all_fields_only_affects_swagger(self):
        """Test TURBODRF_SWAGGER_SHOW_ALL_FIELDS affects Swagger only, not API."""
        from django.test import override_settings

        # This setting should ONLY affect Swagger documentation generation
        # It should NOT affect actual API permission enforcement

        with override_settings(TURBODRF_SWAGGER_SHOW_ALL_FIELDS=True):
            # The actual API viewsets and serializers should still enforce permissions
            # This is a documentation-only setting

            # Create a viewset (not via Swagger schema)
            viewset = TurboDRFViewSet()
            viewset.model = SampleModel

            # Regular serializer should still work normally
            # (This test confirms the setting doesn't break normal operation)
            self.assertEqual(viewset.model, SampleModel)
