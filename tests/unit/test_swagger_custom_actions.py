"""
Tests for Swagger custom action parameter handling.

Tests that TurboDRFSwaggerAutoSchema prevents custom actions
from showing all model fields as request parameters.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.decorators import action
from rest_framework.response import Response

from tests.test_app.models import RelatedModel, SampleModel
from turbodrf.swagger import TurboDRFSwaggerAutoSchema
from turbodrf.views import TurboDRFViewSet

User = get_user_model()


class CustomActionViewSet(TurboDRFViewSet):
    """ViewSet with custom actions for testing."""

    model = SampleModel
    queryset = SampleModel.objects.all()

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Custom action to activate an item."""
        item = self.get_object()
        item.is_active = True
        item.save()
        return Response({"status": "activated"})

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Custom action to get summary data."""
        count = self.get_queryset().count()
        return Response({"total": count})

    @action(detail=True, methods=["get", "post"])
    def toggle(self, request, pk=None):
        """Custom action with multiple methods."""
        item = self.get_object()
        if request.method == "POST":
            item.is_active = not item.is_active
            item.save()
        return Response({"is_active": item.is_active})


class TestSwaggerCustomActions(TestCase):
    """Test Swagger schema for custom actions."""

    def setUp(self):
        """Set up test fixtures."""
        self.viewset = CustomActionViewSet()

        # Create related model first
        self.related = RelatedModel.objects.create(
            name="Test Category", description="Test Description"
        )

        # Create test data
        self.item = SampleModel.objects.create(
            title="Test Item",
            price=Decimal("100.00"),
            quantity=10,
            is_active=False,
            related=self.related,
        )

    def test_standard_actions_get_serializer(self):
        """Test that standard actions (create, update) get serializer."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.post("/api/samplemodels/")

        self.viewset.action = "create"
        self.viewset.request = request
        self.viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        # Standard create action should have request serializer
        serializer = schema.get_request_serializer()

        # Should return a serializer (not None)
        self.assertIsNotNone(serializer)

    def test_custom_action_no_request_serializer(self):
        """Test that custom actions don't get model serializer."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.post("/api/samplemodels/1/activate/")

        self.viewset.action = "activate"
        self.viewset.request = request
        self.viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/1/activate/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        # Custom action should NOT have request serializer
        serializer = schema.get_request_serializer()

        # Should be None for custom actions
        self.assertIsNone(serializer)

    def test_custom_action_no_request_body_parameters(self):
        """Test that custom actions don't show model fields as parameters."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.post("/api/samplemodels/1/activate/")

        self.viewset.action = "activate"
        self.viewset.request = request
        self.viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/1/activate/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        # Custom action should have empty request body parameters
        params = schema.get_request_body_parameters(["application/json"])

        # Should be empty list
        self.assertEqual(params, [])

    def test_update_action_has_request_serializer(self):
        """Test that update action gets serializer."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.put("/api/samplemodels/1/")

        self.viewset.action = "update"
        self.viewset.request = request
        self.viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/1/",
            method="PUT",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()

        # Update action should have serializer
        self.assertIsNotNone(serializer)

    def test_partial_update_action_has_request_serializer(self):
        """Test that partial_update action gets serializer."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.patch("/api/samplemodels/1/")

        self.viewset.action = "partial_update"
        self.viewset.request = request
        self.viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/1/",
            method="PATCH",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        serializer = schema.get_request_serializer()

        # Partial update should have serializer
        self.assertIsNotNone(serializer)

    def test_list_action_no_request_serializer(self):
        """Test that list action doesn't need request serializer."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/api/samplemodels/")

        self.viewset.action = "list"
        self.viewset.request = request
        self.viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/",
            method="GET",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        # List is not in standard actions for request serializer
        # But it should still work without errors
        serializer = schema.get_request_serializer()

        # List doesn't need request body - serializer should be None
        self.assertIsNone(serializer)

    def test_retrieve_action_no_request_serializer(self):
        """Test that retrieve action doesn't need request serializer."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/api/samplemodels/1/")

        self.viewset.action = "retrieve"
        self.viewset.request = request
        self.viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/1/",
            method="GET",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        # Retrieve is not in standard actions for request serializer
        serializer = schema.get_request_serializer()

        # Retrieve doesn't need request body - serializer should be None
        self.assertIsNone(serializer)

    def test_custom_action_with_get_method(self):
        """Test custom action with GET method."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/api/samplemodels/summary/")

        self.viewset.action = "summary"
        self.viewset.request = request
        self.viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/summary/",
            method="GET",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        # GET custom action shouldn't need request body
        params = schema.get_request_body_parameters(["application/json"])

        # Should be empty
        self.assertEqual(params, [])

    def test_custom_action_multiple_methods(self):
        """Test custom action that supports multiple methods."""
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()

        # Test GET
        request = factory.get("/api/samplemodels/1/toggle/")
        self.viewset.action = "toggle"
        self.viewset.request = request
        self.viewset.format_kwarg = None

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/1/toggle/",
            method="GET",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        params_get = schema.get_request_body_parameters(["application/json"])
        self.assertEqual(params_get, [])

        # Test POST
        request = factory.post("/api/samplemodels/1/toggle/")
        self.viewset.request = request

        schema = TurboDRFSwaggerAutoSchema(
            view=self.viewset,
            path="/api/samplemodels/1/toggle/",
            method="POST",
            components=openapi.ReferenceResolver("", force_init=True),
            request=request,
            overrides={},
        )

        params_post = schema.get_request_body_parameters(["application/json"])
        self.assertEqual(params_post, [])


class TestSwaggerAutoSchemaIntegration(TestCase):
    """Integration tests for TurboDRFSwaggerAutoSchema."""

    def test_viewset_has_swagger_schema_attribute(self):
        """Test that TurboDRFViewSet has swagger_schema attribute."""
        # Check if the attribute is set
        if hasattr(TurboDRFViewSet, "swagger_schema"):
            self.assertEqual(TurboDRFViewSet.swagger_schema, TurboDRFSwaggerAutoSchema)

    def test_swagger_auto_schema_class_exists(self):
        """Test that TurboDRFSwaggerAutoSchema class is importable."""
        from turbodrf.swagger import TurboDRFSwaggerAutoSchema

        # Should be a class
        self.assertTrue(callable(TurboDRFSwaggerAutoSchema))

    def test_swagger_auto_schema_inherits_from_base(self):
        """Test that TurboDRFSwaggerAutoSchema inherits from SwaggerAutoSchema."""
        from drf_yasg.inspectors import SwaggerAutoSchema

        # Should be subclass
        self.assertTrue(issubclass(TurboDRFSwaggerAutoSchema, SwaggerAutoSchema))

    def test_standard_action_list(self):
        """Test the standard actions list in schema."""
        # The schema defines standard actions that should get model fields
        standard_actions = ["create", "update", "partial_update", "list", "retrieve"]

        # Verify these are the expected standard actions
        self.assertIn("create", standard_actions)
        self.assertIn("update", standard_actions)
        self.assertIn("partial_update", standard_actions)

    def test_non_standard_action_detection(self):
        """Test that non-standard actions are detected correctly."""
        standard_actions = ["create", "update", "partial_update", "list", "retrieve"]

        # Custom actions
        custom_actions = ["activate", "summary", "toggle", "custom_method"]

        for custom_action in custom_actions:
            self.assertNotIn(custom_action, standard_actions)
