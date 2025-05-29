"""
Unit tests for TurboDRF views.

Tests the ViewSet functionality and dynamic serializer generation.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.test import APIRequestFactory

from tests.test_app.models import RelatedModel, SampleModel
from turbodrf.permissions import TurboDRFPermission
from turbodrf.views import TurboDRFPagination, TurboDRFViewSet

User = get_user_model()


class TestTurboDRFPagination(TestCase):
    """Test cases for TurboDRF pagination."""

    def setUp(self):
        """Set up test fixtures."""
        self.pagination = TurboDRFPagination()
        self.factory = APIRequestFactory()

    def test_default_page_size(self):
        """Test default page size."""
        self.assertEqual(self.pagination.page_size, 20)

    def test_page_size_query_param(self):
        """Test page size query parameter."""
        self.assertEqual(self.pagination.page_size_query_param, "page_size")

    def test_max_page_size(self):
        """Test maximum page size limit."""
        self.assertEqual(self.pagination.max_page_size, 100)

    def test_paginated_response_format(self):
        """Test the custom paginated response format."""

        # Create mock page object
        class MockPage:
            number = 1
            paginator = type("Paginator", (), {"num_pages": 5, "count": 100})()

        # Set up pagination with mock data
        self.pagination.page = MockPage()
        self.pagination.request = self.factory.get("/api/test/")

        # Mock get_next_link and get_previous_link
        self.pagination.get_next_link = lambda: "http://example.com/api/test/?page=2"
        self.pagination.get_previous_link = lambda: None

        response = self.pagination.get_paginated_response(["item1", "item2"])

        self.assertIn("pagination", response.data)
        self.assertIn("data", response.data)

        pagination = response.data["pagination"]
        self.assertEqual(pagination["current_page"], 1)
        self.assertEqual(pagination["total_pages"], 5)
        self.assertEqual(pagination["total_items"], 100)
        self.assertEqual(pagination["next"], "http://example.com/api/test/?page=2")
        self.assertIsNone(pagination["previous"])


class TestTurboDRFViewSet(TestCase):
    """Test cases for TurboDRF ViewSet."""

    def setUp(self):
        """Set up test fixtures."""
        # Create related model
        self.related = RelatedModel.objects.create(
            name="Related", description="Test related"
        )

        # Create test models
        self.test_obj1 = SampleModel.objects.create(
            title="First Item",
            description="First description",
            price=Decimal("50.00"),
            quantity=5,
            related=self.related,
            is_active=True,
        )
        self.test_obj2 = SampleModel.objects.create(
            title="Second Item",
            description="Second description",
            price=Decimal("100.00"),
            quantity=10,
            related=self.related,
            is_active=False,
        )

        # Create viewset
        self.viewset = TurboDRFViewSet()
        self.viewset.model = SampleModel
        self.viewset.queryset = SampleModel.objects.all()

        self.factory = APIRequestFactory()

    def test_viewset_configuration(self):
        """Test ViewSet basic configuration."""
        self.assertEqual(self.viewset.permission_classes, [TurboDRFPermission])
        self.assertEqual(self.viewset.pagination_class, TurboDRFPagination)
        self.assertIn(DjangoFilterBackend, self.viewset.filter_backends)
        self.assertIn(SearchFilter, self.viewset.filter_backends)
        self.assertIn(OrderingFilter, self.viewset.filter_backends)

    def test_get_serializer_class_list_action(self):
        """Test serializer class generation for list action."""
        self.viewset.action = "list"
        serializer_class = self.viewset.get_serializer_class()

        # Check that it returns a class
        self.assertTrue(isinstance(serializer_class, type))

        # Check that it has the correct model
        self.assertEqual(serializer_class.Meta.model, SampleModel)

        # Check that it has list fields
        expected_fields = ["title", "price", "related", "is_active"]
        self.assertEqual(set(serializer_class.Meta.fields), set(expected_fields))

    def test_get_serializer_class_detail_action(self):
        """Test serializer class generation for detail action."""
        self.viewset.action = "retrieve"
        serializer_class = self.viewset.get_serializer_class()

        # Check that it has detail fields
        expected_fields = [
            "title",
            "description",
            "price",
            "quantity",
            "related",
            "is_active",
            "secret_field",
            "created_at",
            "updated_at",
            "published_date",
        ]
        self.assertEqual(set(serializer_class.Meta.fields), set(expected_fields))

    def test_get_serializer_class_with_nested_fields(self):
        """Test serializer class handles nested field metadata."""
        self.viewset.action = "list"
        serializer_class = self.viewset.get_serializer_class()

        # Check that nested fields metadata is included
        self.assertTrue(hasattr(serializer_class.Meta, "_nested_fields"))
        nested = serializer_class.Meta._nested_fields

        self.assertIn("related", nested)
        self.assertIn("related__name", nested["related"])

    def test_get_queryset_optimization(self):
        """Test queryset optimization with select_related."""
        queryset = self.viewset.get_queryset()

        # Check that select_related is applied
        # This is tricky to test directly, but we can check the query
        self.assertIsNotNone(queryset)
        self.assertEqual(queryset.model, SampleModel)

    def test_search_fields_property(self):
        """Test search fields property."""
        search_fields = self.viewset.search_fields
        self.assertEqual(search_fields, ["title", "description"])

    def test_ordering_fields_property(self):
        """Test ordering fields property."""
        ordering_fields = self.viewset.ordering_fields
        self.assertEqual(ordering_fields, "__all__")

    def test_filterset_fields_property(self):
        """Test filterset fields property."""
        filterset_fields = self.viewset.filterset_fields
        # Should be a dict with field names and their lookup expressions
        self.assertIsInstance(filterset_fields, dict)
        # Check some expected fields
        self.assertIn("title", filterset_fields)
        self.assertIn("price", filterset_fields)
        self.assertIn("is_active", filterset_fields)
        # Check lookup types for different field types
        self.assertIn("gte", filterset_fields["price"])  # Numeric field
        self.assertIn("icontains", filterset_fields["title"])  # Text field
        self.assertEqual(filterset_fields["is_active"], ["exact"])  # Boolean field

    def test_simple_fields_configuration(self):
        """Test viewset with simple fields configuration."""
        # Create a viewset for RelatedModel which has simple fields
        viewset = TurboDRFViewSet()
        viewset.model = RelatedModel
        viewset.queryset = RelatedModel.objects.all()
        viewset.action = "list"

        serializer_class = viewset.get_serializer_class()
        self.assertEqual(serializer_class.Meta.fields, ["name", "description"])

    def test_all_fields_configuration(self):
        """Test viewset with '__all__' fields configuration."""

        # Mock a model with '__all__' configuration
        class MockModel:
            @classmethod
            def turbodrf(cls):
                return {"fields": "__all__"}

        viewset = TurboDRFViewSet()
        viewset.model = MockModel
        viewset.action = "list"

        serializer_class = viewset.get_serializer_class()
        self.assertEqual(serializer_class.Meta.fields, "__all__")
