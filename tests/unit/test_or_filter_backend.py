"""
Tests for OR Filter Backend.

Tests the ORFilterBackend functionality for OR query support.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from tests.test_app.models import RelatedModel, SampleModel
from turbodrf.filter_backends import ORFilterBackend

User = get_user_model()


class MockView:
    """Mock view for testing filter backend."""

    pass


class TestORFilterBackend(TestCase):
    """Test OR filter backend functionality."""

    def setUp(self):
        """Set up test fixtures."""
        # Create test data
        self.related1 = RelatedModel.objects.create(
            name="Category A", description="First category"
        )
        self.related2 = RelatedModel.objects.create(
            name="Category B", description="Second category"
        )

        self.item1 = SampleModel.objects.create(
            title="Apple Product",
            price=Decimal("100.00"),
            quantity=10,
            related=self.related1,
            is_active=True,
        )

        self.item2 = SampleModel.objects.create(
            title="Banana Product",
            price=Decimal("50.00"),
            quantity=20,
            related=self.related2,
            is_active=False,
        )

        self.item3 = SampleModel.objects.create(
            title="Cherry Product",
            price=Decimal("75.00"),
            quantity=15,
            related=self.related1,
            is_active=True,
        )

        self.item4 = SampleModel.objects.create(
            title="Date Product",
            price=Decimal("100.00"),
            quantity=5,
            related=self.related2,
            is_active=False,
        )

        self.backend = ORFilterBackend()
        self.factory = APIRequestFactory()
        self.view = MockView()

    def test_or_filter_single_field_multiple_values(self):
        """Test OR filtering with multiple values for a single field."""
        django_request = self.factory.get(
            "/?title_or=Apple Product&title_or=Banana Product"
        )
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        self.assertEqual(filtered.count(), 2)
        titles = [item.title for item in filtered]
        self.assertIn("Apple Product", titles)
        self.assertIn("Banana Product", titles)

    def test_or_filter_with_exact_match(self):
        """Test OR filtering with exact value matches."""
        django_request = self.factory.get("/?price_or=100.00&price_or=50.00")
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        # Should match items 1, 2, and 4 (prices 100 and 50)
        self.assertEqual(filtered.count(), 3)

    def test_or_filter_with_lookups(self):
        """Test OR filtering with Django lookups like icontains."""
        django_request = self.factory.get(
            "/?title__icontains_or=Apple&title__icontains_or=Cherry"
        )
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        self.assertEqual(filtered.count(), 2)
        titles = [item.title for item in filtered]
        self.assertIn("Apple Product", titles)
        self.assertIn("Cherry Product", titles)

    def test_or_filter_combined_with_and_filters(self):
        """Test OR filters combined with regular AND filters."""
        django_request = self.factory.get(
            "/?title_or=Apple Product&title_or=Cherry Product&is_active=true"
        )
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        # Both Apple and Cherry are active
        self.assertEqual(filtered.count(), 2)

    def test_or_filter_with_foreign_key(self):
        """Test OR filtering on foreign key fields."""
        django_request = self.factory.get(
            f"/?related_or={self.related1.id}&related_or={self.related2.id}"
        )
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        # All items belong to one of these categories
        self.assertEqual(filtered.count(), 4)

    def test_or_filter_empty_result(self):
        """Test OR filtering that matches no items."""
        django_request = self.factory.get(
            "/?title_or=Nonexistent Product&title_or=Another Missing"
        )
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        self.assertEqual(filtered.count(), 0)

    def test_or_filter_single_value(self):
        """Test OR filtering with a single value (should still work)."""
        django_request = self.factory.get("/", {"title_or": "Apple Product"})
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        self.assertEqual(filtered.count(), 1)
        self.assertEqual(filtered.first().title, "Apple Product")

    def test_or_filter_multiple_fields(self):
        """Test OR filtering across multiple different fields."""
        django_request = self.factory.get(
            "/?title_or=Apple Product&title_or=Banana Product"
            "&price_or=100.00&price_or=50.00"
        )
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        # Apple (100) and Banana (50) match both conditions
        self.assertEqual(filtered.count(), 2)

    def test_or_filter_ignores_pagination_params(self):
        """Test that OR filter doesn't try to filter on pagination params."""
        django_request = self.factory.get(
            "/",
            {
                "title_or": "Apple Product",
                "page": "1",
                "page_size": "10",
            },
        )
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        self.assertEqual(filtered.count(), 1)

    def test_or_filter_with_boolean_field(self):
        """Test OR filtering on boolean fields."""
        django_request = self.factory.get("/?is_active_or=1&is_active_or=0")
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        # Should match all items since we're asking for true OR false
        self.assertEqual(filtered.count(), 4)

    def test_or_filter_with_gte_lookup(self):
        """Test OR filtering with comparison lookups."""
        django_request = self.factory.get("/?quantity__gte_or=15&quantity__gte_or=20")
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        # Items with quantity >= 15: item2 (20) and item3 (15)
        self.assertGreaterEqual(filtered.count(), 2)

    def test_or_filter_no_or_params(self):
        """Test filtering with no OR parameters."""
        django_request = self.factory.get("/?title=Apple Product")
        request = Request(django_request)

        queryset = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, queryset, self.view)

        # Should apply regular filters (backend handles both OR and regular params)
        self.assertEqual(filtered.count(), 1)
