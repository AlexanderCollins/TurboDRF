"""Integration tests for the compiled read path."""

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


class CompiledPathBasicTests(TestCase):
    """Test the compiled read path via HTTP."""

    def setUp(self):
        self.client = APIClient()
        self.related = RelatedModel.objects.create(
            name="Test Author", description="desc"
        )
        self.sample1 = CompiledSampleModel.objects.create(
            title="Alpha Book",
            price=Decimal("29.99"),
            is_active=True,
            related=self.related,
        )
        self.sample2 = CompiledSampleModel.objects.create(
            title="Beta Book",
            price=Decimal("49.50"),
            is_active=False,
            related=self.related,
        )

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_list_returns_200(self):
        response = self.client.get("/api/compiledsamplemodels/")
        self.assertEqual(response.status_code, 200)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_list_has_correct_data(self):
        response = self.client.get("/api/compiledsamplemodels/")
        data = response.data["data"]
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["title"], "Alpha Book")
        self.assertEqual(data[1]["title"], "Beta Book")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_list_decimal_as_string(self):
        response = self.client.get("/api/compiledsamplemodels/")
        data = response.data["data"]
        self.assertEqual(data[0]["price"], "29.99")
        self.assertIsInstance(data[0]["price"], str)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_list_fk_fields(self):
        response = self.client.get("/api/compiledsamplemodels/")
        data = response.data["data"]
        # FK annotation
        self.assertEqual(data[0]["related_name"], "Test Author")
        # Raw FK ID
        self.assertEqual(data[0]["related"], self.related.pk)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_list_property_fields(self):
        response = self.client.get("/api/compiledsamplemodels/")
        data = response.data["data"]
        self.assertEqual(data[0]["display_title"], "ALPHA BOOK")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_list_has_pagination(self):
        response = self.client.get("/api/compiledsamplemodels/")
        self.assertIn("pagination", response.data)
        self.assertIn("data", response.data)
        self.assertEqual(response.data["pagination"]["total_items"], 2)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_detail_uses_drf(self):
        """Detail view should still use DRF serializer path."""
        response = self.client.get(f"/api/compiledsamplemodels/{self.sample1.pk}/")
        self.assertEqual(response.status_code, 200)
        # Detail should still work (via DRF path)
        self.assertEqual(response.data["title"], "Alpha Book")

    @override_settings(
        TURBODRF_DISABLE_PERMISSIONS=True,
        TURBODRF_ROLES={
            "admin": [
                "test_app.compiledsamplemodel.read",
                "test_app.compiledsamplemodel.create",
                "test_app.compiledsamplemodel.update",
                "test_app.compiledsamplemodel.delete",
            ]
        },
    )
    def test_compiled_create_uses_drf(self):
        """Write operations should still use DRF serializer path."""
        user = User.objects.create_user(username="testuser", password="testpass")
        user._test_roles = ["admin"]
        self.client.force_authenticate(user=user)
        response = self.client.post(
            "/api/compiledsamplemodels/",
            {
                "title": "New Book",
                "price": "19.99",
                "is_active": True,
                "related": self.related.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(CompiledSampleModel.objects.count(), 3)


class CompiledPathSearchTests(TestCase):
    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_with_search(self):
        related = RelatedModel.objects.create(name="Author")
        CompiledSampleModel.objects.create(
            title="Django Guide", price=Decimal("20.00"), related=related
        )
        CompiledSampleModel.objects.create(
            title="Python Guide", price=Decimal("25.00"), related=related
        )

        client = APIClient()
        response = client.get("/api/compiledsamplemodels/?search=Django")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Django Guide")


class CompiledPathFilterTests(TestCase):
    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_with_filtering(self):
        related = RelatedModel.objects.create(name="Author")
        CompiledSampleModel.objects.create(
            title="Book A", price=Decimal("10.00"), is_active=True, related=related
        )
        CompiledSampleModel.objects.create(
            title="Book B", price=Decimal("50.00"), is_active=False, related=related
        )

        client = APIClient()
        response = client.get("/api/compiledsamplemodels/?is_active=true")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Book A")


class CompiledPathOrderingTests(TestCase):
    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_with_ordering(self):
        related = RelatedModel.objects.create(name="Author")
        CompiledSampleModel.objects.create(
            title="Zebra", price=Decimal("10.00"), related=related
        )
        CompiledSampleModel.objects.create(
            title="Alpha", price=Decimal("50.00"), related=related
        )

        client = APIClient()
        response = client.get("/api/compiledsamplemodels/?ordering=title")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data[0]["title"], "Alpha")
        self.assertEqual(data[1]["title"], "Zebra")


class CompiledPathM2MTests(TestCase):
    """Test compiled path with M2M relationships."""

    def setUp(self):
        self.client = APIClient()
        self.author = RelatedModel.objects.create(name="Author One")
        self.cat1 = Category.objects.create(name="Python", description="Python desc")
        self.cat2 = Category.objects.create(name="Django", description="Django desc")

        self.article = CompiledArticle.objects.create(
            title="Test Article", author=self.author
        )
        self.article.categories.add(self.cat1, self.cat2)

        self.empty_article = CompiledArticle.objects.create(
            title="Empty Article", author=self.author
        )

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_m2m_list(self):
        response = self.client.get("/api/compiledarticles/")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(len(data), 2)

        # Article with categories
        art1 = next(d for d in data if d["title"] == "Test Article")
        self.assertIsInstance(art1["categories"], list)
        cat_names = {c["name"] for c in art1["categories"]}
        self.assertEqual(cat_names, {"Python", "Django"})

        # Article without categories
        art2 = next(d for d in data if d["title"] == "Empty Article")
        self.assertEqual(art2["categories"], [])

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_m2m_with_fk(self):
        """FK and M2M should work together."""
        response = self.client.get("/api/compiledarticles/")
        data = response.data["data"]
        art1 = next(d for d in data if d["title"] == "Test Article")
        self.assertEqual(art1["author_name"], "Author One")
        self.assertIsInstance(art1["categories"], list)


class CompiledPathPaginationTests(TestCase):
    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_compiled_pagination(self):
        related = RelatedModel.objects.create(name="Author")
        for i in range(25):
            CompiledSampleModel.objects.create(
                title=f"Book {i:03d}",
                price=Decimal("10.00"),
                related=related,
            )

        client = APIClient()

        # Page 1 (default page size is 20)
        response = client.get("/api/compiledsamplemodels/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["data"]), 20)
        self.assertEqual(response.data["pagination"]["total_items"], 25)
        self.assertEqual(response.data["pagination"]["total_pages"], 2)

        # Page 2
        response = client.get("/api/compiledsamplemodels/?page=2")
        self.assertEqual(len(response.data["data"]), 5)
