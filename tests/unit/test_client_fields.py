"""Tests for the ?fields= client field selection on the compiled read path."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.models import (
    Category,
    CompiledArticle,
    CompiledSampleModel,
    RelatedModel,
)

User = get_user_model()


class ClientFieldSelectionTests(TestCase):
    """Test ?fields= query parameter on compiled list views."""

    def setUp(self):
        self.client = APIClient()
        self.related = RelatedModel.objects.create(
            name="Test Author", description="Author bio"
        )
        self.sample = CompiledSampleModel.objects.create(
            title="Alpha Book",
            price=Decimal("29.99"),
            is_active=True,
            related=self.related,
        )

    def _get_data(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.data["data"]

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_fields_single_field(self):
        """?fields=title returns only the title field."""
        data = self._get_data("/api/compiledsamplemodels/?fields=title")
        row = data[0]
        self.assertIn("title", row)
        self.assertEqual(row["title"], "Alpha Book")
        # Other configured fields should not be present
        self.assertNotIn("price", row)
        self.assertNotIn("is_active", row)
        self.assertNotIn("display_title", row)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_fields_multiple_fields(self):
        """?fields=title,price returns both fields."""
        data = self._get_data("/api/compiledsamplemodels/?fields=title,price")
        row = data[0]
        self.assertIn("title", row)
        self.assertIn("price", row)
        self.assertEqual(row["title"], "Alpha Book")
        self.assertEqual(row["price"], "29.99")
        self.assertNotIn("is_active", row)
        self.assertNotIn("display_title", row)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_fields_fk_dot_notation(self):
        """?fields=title,related.name returns title and FK annotation via dot notation."""
        data = self._get_data("/api/compiledsamplemodels/?fields=title,related.name")
        row = data[0]
        self.assertIn("title", row)
        self.assertIn("related_name", row)
        self.assertEqual(row["related_name"], "Test Author")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_fields_fk_underscore_notation(self):
        """?fields=title,related_name works with underscore notation too."""
        data = self._get_data("/api/compiledsamplemodels/?fields=title,related_name")
        row = data[0]
        self.assertIn("title", row)
        self.assertIn("related_name", row)
        self.assertEqual(row["related_name"], "Test Author")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_fields_property_field(self):
        """?fields=display_title returns the @property field."""
        data = self._get_data("/api/compiledsamplemodels/?fields=display_title")
        row = data[0]
        self.assertIn("display_title", row)
        self.assertEqual(row["display_title"], "ALPHA BOOK")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_fields_nonexistent_ignored(self):
        """?fields=nonexistent returns default fields (no valid fields matched)."""
        data = self._get_data("/api/compiledsamplemodels/?fields=nonexistent")
        row = data[0]
        # _parse_client_fields returns None when no valid fields match,
        # which means all default fields are returned
        self.assertIn("title", row)
        self.assertIn("price", row)
        self.assertIn("is_active", row)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_fields_empty_value(self):
        """?fields= with no value returns all default fields."""
        data = self._get_data("/api/compiledsamplemodels/?fields=")
        row = data[0]
        self.assertIn("title", row)
        self.assertIn("price", row)
        self.assertIn("is_active", row)
        self.assertIn("display_title", row)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_detail_view_ignores_fields_param(self):
        """Detail view should ignore ?fields= (only works on list)."""
        response = self.client.get(
            f"/api/compiledsamplemodels/{self.sample.pk}/?fields=title"
        )
        self.assertEqual(response.status_code, 200)
        # Detail uses DRF serializer path, so it returns all detail fields
        self.assertIn("title", response.data)
        self.assertIn("price", response.data)


class ClientFieldSelectionM2MTests(TestCase):
    """Test ?fields= with M2M relationships on compiled path."""

    def setUp(self):
        self.client = APIClient()
        self.author = RelatedModel.objects.create(name="Author One")
        self.cat1 = Category.objects.create(name="Python", description="Python desc")
        self.cat2 = Category.objects.create(name="Django", description="Django desc")
        self.article = CompiledArticle.objects.create(
            title="Test Article", author=self.author
        )
        self.article.categories.add(self.cat1, self.cat2)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_fields_m2m_field(self):
        """?fields=categories returns the M2M field."""
        response = self.client.get("/api/compiledarticles/?fields=title,categories")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        row = next(d for d in data if d["title"] == "Test Article")
        self.assertIn("categories", row)
        cat_names = {c["name"] for c in row["categories"]}
        self.assertEqual(cat_names, {"Python", "Django"})
        # FK field should not be present since it wasn't requested
        self.assertNotIn("author_name", row)


class ClientFieldSelectionPermissionTests(TestCase):
    """Test that ?fields= respects role-based permissions."""

    def setUp(self):
        self.client = APIClient()
        self.related = RelatedModel.objects.create(name="Author")
        CompiledSampleModel.objects.create(
            title="Book",
            price=Decimal("19.99"),
            is_active=True,
            related=self.related,
        )
        self.user = User.objects.create_user(username="viewer", password="pass")
        self.user._test_roles = ["viewer"]
        self.client.force_authenticate(user=self.user)

    @override_settings(
        TURBODRF_DISABLE_PERMISSIONS=False,
        TURBODRF_ROLES={
            "admin": [
                "test_app.compiledsamplemodel.read",
                "test_app.compiledsamplemodel.title.read",
                "test_app.compiledsamplemodel.price.read",
                "test_app.compiledsamplemodel.is_active.read",
                "test_app.compiledsamplemodel.related.read",
                "test_app.compiledsamplemodel.display_title.read",
            ],
            "viewer": [
                "test_app.compiledsamplemodel.read",
                "test_app.compiledsamplemodel.title.read",
                "test_app.compiledsamplemodel.is_active.read",
                "test_app.compiledsamplemodel.related.read",
                "test_app.compiledsamplemodel.display_title.read",
                # NOTE: price.read intentionally omitted
            ],
        },
    )
    def test_fields_respects_permissions(self):
        """?fields=title,price still excludes price if viewer can't see it."""
        response = self.client.get("/api/compiledsamplemodels/?fields=title,price")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        row = data[0]
        self.assertIn("title", row)
        self.assertNotIn("price", row)
