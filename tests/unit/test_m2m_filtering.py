"""
Tests for ManyToMany field filtering.

Tests that ManyToMany fields are included in filterset_fields
and can be used for filtering.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase
from rest_framework.test import APIClient

from turbodrf.mixins import TurboDRFMixin
from turbodrf.views import TurboDRFViewSet

User = get_user_model()


class ProductCategory(models.Model):
    """Category model for Product ManyToMany relationship testing."""

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)

    class Meta:
        app_label = "test_app"
        db_table = "test_product_category"  # Avoid conflicts


class Product(models.Model, TurboDRFMixin):
    """Product model with ManyToMany categories."""

    title = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    categories = models.ManyToManyField(ProductCategory, related_name="products")

    @classmethod
    def turbodrf(cls):
        return {
            "enabled": True,
            "endpoint": "products",
            "fields": ["title", "price", "categories"],
        }

    class Meta:
        app_label = "test_app"


class TestManyToManyFiltering(TestCase):
    """Test ManyToMany field filtering."""

    def setUp(self):
        """Set up test fixtures."""
        self.client = APIClient()

        # Create admin user
        self.admin = User.objects.create_user(username="admin", password="admin123")
        self.admin._test_roles = ["admin"]

    def test_m2m_fields_in_filterset_fields(self):
        """Test that ManyToMany fields are included in filterset_fields."""
        from tests.test_app.models import SampleModel

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel

        filterset_fields = viewset.get_filterset_fields()

        # Check if the method includes m2m fields
        # SampleModel doesn't have m2m fields, so let's check the Product model
        viewset.model = Product
        filterset_fields = viewset.get_filterset_fields()

        # Should include categories (m2m field)
        self.assertIn("categories", filterset_fields)

    def test_m2m_field_has_lookups(self):
        """Test that ManyToMany fields get appropriate lookups."""
        viewset = TurboDRFViewSet()
        viewset.model = Product

        filterset_fields = viewset.get_filterset_fields()

        # Categories should have exact, in, and isnull lookups
        self.assertEqual(filterset_fields.get("categories"), ["exact", "in", "isnull"])

    def test_get_filterset_fields_includes_regular_and_m2m(self):
        """Test that filterset includes both regular and M2M fields."""
        viewset = TurboDRFViewSet()
        viewset.model = Product

        filterset_fields = viewset.get_filterset_fields()

        # Should have regular fields
        self.assertIn("title", filterset_fields)
        self.assertIn("price", filterset_fields)

        # Should also have m2m fields
        self.assertIn("categories", filterset_fields)

    def test_m2m_fields_excluded_unsupported_types(self):
        """Test that unsupported field types are still excluded."""
        from django.db import models

        class TestModel(models.Model, TurboDRFMixin):
            title = models.CharField(max_length=100)
            data = models.JSONField(default=dict)
            categories = models.ManyToManyField(ProductCategory)

            @classmethod
            def turbodrf(cls):
                return {"fields": "__all__"}

            class Meta:
                app_label = "test_app"

        viewset = TurboDRFViewSet()
        viewset.model = TestModel

        filterset_fields = viewset.get_filterset_fields()

        # Categories (m2m) should be included
        self.assertIn("categories", filterset_fields)

        # JSONField should be excluded
        self.assertNotIn("data", filterset_fields)

    def test_filterset_fields_property_includes_m2m(self):
        """Test that filterset_fields property includes M2M fields."""
        viewset = TurboDRFViewSet()
        viewset.model = Product

        # Use the property
        filterset_fields = viewset.filterset_fields

        self.assertIn("categories", filterset_fields)


@pytest.mark.skip(
    reason=(
        "ProductCategory table not in migrations - "
        "use test_nesting_validation.py::TestManyToManyFieldNesting instead"
    )
)
class TestManyToManyFilteringIntegration(TestCase):
    """Integration tests for M2M filtering with actual filtering."""

    def setUp(self):
        """Set up test data."""
        # Create categories
        self.electronics = ProductCategory.objects.create(
            name="Electronics", slug="electronics"
        )
        self.books = ProductCategory.objects.create(name="Books", slug="books")
        self.clothing = ProductCategory.objects.create(name="Clothing", slug="clothing")

        # Create products with categories
        self.laptop = Product.objects.create(title="Laptop", price=Decimal("999.99"))
        self.laptop.categories.add(self.electronics)

        self.novel = Product.objects.create(title="Novel", price=Decimal("19.99"))
        self.novel.categories.add(self.books)

        self.tablet = Product.objects.create(title="Tablet", price=Decimal("499.99"))
        self.tablet.categories.add(self.electronics, self.books)

        self.shirt = Product.objects.create(title="Shirt", price=Decimal("29.99"))
        self.shirt.categories.add(self.clothing)

    def test_filter_by_m2m_field_id(self):
        """Test filtering by ManyToMany field ID."""
        # This would be tested via API if we had the endpoint registered
        # For now, test that the filterset configuration is correct

        viewset = TurboDRFViewSet()
        viewset.model = Product

        filterset_fields = viewset.get_filterset_fields()

        # Verify categories can be filtered
        self.assertIn("categories", filterset_fields)
        self.assertIn("exact", filterset_fields["categories"])

    def test_m2m_filtering_with_queryset(self):
        """Test M2M filtering works with queryset."""
        # Test direct queryset filtering (not through DRF)
        electronics_products = Product.objects.filter(categories=self.electronics)

        # Should return laptop and tablet
        self.assertEqual(electronics_products.count(), 2)
        titles = [p.title for p in electronics_products]
        self.assertIn("Laptop", titles)
        self.assertIn("Tablet", titles)

    def test_multiple_m2m_values_filter(self):
        """Test filtering with multiple M2M values."""
        # Products in both electronics AND books categories
        multi_category_products = Product.objects.filter(
            categories=self.electronics
        ).filter(categories=self.books)

        # Should only return tablet
        self.assertEqual(multi_category_products.count(), 1)
        self.assertEqual(multi_category_products.first().title, "Tablet")


class TestManyToManyFieldDetection(TestCase):
    """Test detection of ManyToMany fields in get_filterset_fields."""

    def test_model_with_no_m2m_fields(self):
        """Test model without M2M fields."""
        from tests.test_app.models import SampleModel

        viewset = TurboDRFViewSet()
        viewset.model = SampleModel

        filterset_fields = viewset.get_filterset_fields()

        # SampleModel has no M2M fields
        # Check that regular fields are present
        self.assertIn("title", filterset_fields)
        self.assertIn("price", filterset_fields)

    def test_model_with_m2m_fields(self):
        """Test model with M2M fields."""
        viewset = TurboDRFViewSet()
        viewset.model = Product

        filterset_fields = viewset.get_filterset_fields()

        # Product has M2M field
        self.assertIn("categories", filterset_fields)

    def test_m2m_fields_iteration(self):
        """Test that we iterate through model._meta.many_to_many."""
        # Verify Product has M2M fields
        m2m_fields = Product._meta.many_to_many

        self.assertTrue(len(m2m_fields) > 0)
        field_names = [f.name for f in m2m_fields]
        self.assertIn("categories", field_names)

    def test_regular_and_m2m_fields_combined(self):
        """Test that regular and M2M fields are both included."""
        viewset = TurboDRFViewSet()
        viewset.model = Product

        filterset_fields = viewset.get_filterset_fields()

        # Regular fields
        self.assertIn("title", filterset_fields)
        self.assertIn("price", filterset_fields)

        # M2M fields
        self.assertIn("categories", filterset_fields)

        # Verify they have appropriate lookups
        self.assertIn("icontains", filterset_fields["title"])
        self.assertIn("gte", filterset_fields["price"])
        self.assertEqual(filterset_fields["categories"], ["exact", "in", "isnull"])
