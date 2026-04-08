"""
Frontend scenario tests — simulates what a real dashboard/SPA would need.

Tests both the compiled and non-compiled paths to verify they work for
common frontend operations: CRUD, search, filter, order, paginate,
related resources, etc.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.models import (
    Category,
    CompiledArticle,
    CompiledSampleModel,
    RelatedModel,
    SampleModel,
)

User = get_user_model()


def make_admin():
    user = User.objects.create_user(username="admin", password="pass")
    user._test_roles = ["admin"]
    return user


class SearchEndpointTests(TestCase):
    """Frontend: search bar hitting the API."""

    def setUp(self):
        self.client = APIClient()
        self.related = RelatedModel.objects.create(name="Author")
        CompiledSampleModel.objects.create(
            title="Django REST Framework Guide",
            price=Decimal("39.99"),
            related=self.related,
        )
        CompiledSampleModel.objects.create(
            title="Python Crash Course",
            price=Decimal("29.99"),
            related=self.related,
        )
        CompiledSampleModel.objects.create(
            title="JavaScript Patterns",
            price=Decimal("24.99"),
            related=self.related,
        )

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_search_by_title(self):
        resp = self.client.get("/api/compiledsamplemodels/?search=Django")
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Django REST Framework Guide")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_search_case_insensitive(self):
        resp = self.client.get("/api/compiledsamplemodels/?search=python")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["data"]), 1)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_search_no_results(self):
        resp = self.client.get("/api/compiledsamplemodels/?search=Rust")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["data"]), 0)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_search_partial_match(self):
        resp = self.client.get("/api/compiledsamplemodels/?search=Guide")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["data"]), 1)


@override_settings(
    TURBODRF_ROLES={
        "admin": [
            "test_app.compiledsamplemodel.read",
            "test_app.compiledsamplemodel.create",
            "test_app.compiledsamplemodel.update",
            "test_app.compiledsamplemodel.delete",
        ]
    }
)
class CRUDResourceTests(TestCase):
    """Frontend: basic CRUD on a single resource."""

    def setUp(self):
        self.user = make_admin()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.related = RelatedModel.objects.create(name="Author")

    def test_list_resources(self):
        CompiledSampleModel.objects.create(
            title="Book A", price=Decimal("10.00"), related=self.related
        )
        CompiledSampleModel.objects.create(
            title="Book B", price=Decimal("20.00"), related=self.related
        )
        resp = self.client.get("/api/compiledsamplemodels/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["data"]), 2)

    def test_retrieve_resource(self):
        book = CompiledSampleModel.objects.create(
            title="Detail Book", price=Decimal("15.00"), related=self.related
        )
        resp = self.client.get(f"/api/compiledsamplemodels/{book.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["title"], "Detail Book")

    def test_update_resource(self):
        book = CompiledSampleModel.objects.create(
            title="Old Title", price=Decimal("10.00"), related=self.related
        )
        resp = self.client.put(
            f"/api/compiledsamplemodels/{book.pk}/",
            {"title": "New Title", "price": "12.00", "related": self.related.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        book.refresh_from_db()
        self.assertEqual(book.title, "New Title")

    def test_partial_update_resource(self):
        book = CompiledSampleModel.objects.create(
            title="Original", price=Decimal("10.00"), related=self.related
        )
        resp = self.client.patch(
            f"/api/compiledsamplemodels/{book.pk}/",
            {"title": "Patched"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        book.refresh_from_db()
        self.assertEqual(book.title, "Patched")
        self.assertEqual(book.price, Decimal("10.00"))  # Unchanged

    def test_delete_resource(self):
        book = CompiledSampleModel.objects.create(
            title="To Delete", price=Decimal("10.00"), related=self.related
        )
        resp = self.client.delete(f"/api/compiledsamplemodels/{book.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(CompiledSampleModel.objects.filter(pk=book.pk).exists())


class RelatedResourceTests(TestCase):
    """Frontend: resources with FK and M2M relations displayed."""

    def setUp(self):
        self.client = APIClient()
        self.author1 = RelatedModel.objects.create(name="Alice", description="Dev")
        self.author2 = RelatedModel.objects.create(name="Bob", description="Writer")
        self.cat_py = Category.objects.create(name="Python")
        self.cat_dj = Category.objects.create(name="Django")
        self.cat_js = Category.objects.create(name="JavaScript")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_list_with_fk_fields(self):
        """List view should inline FK fields (author name)."""
        CompiledSampleModel.objects.create(
            title="Book by Alice", price=Decimal("20.00"), related=self.author1
        )
        CompiledSampleModel.objects.create(
            title="Book by Bob", price=Decimal("30.00"), related=self.author2
        )
        resp = self.client.get("/api/compiledsamplemodels/")
        data = resp.data["data"]
        self.assertEqual(data[0]["related_name"], "Alice")
        self.assertEqual(data[1]["related_name"], "Bob")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_list_with_m2m_fields(self):
        """List view should inline M2M as array of objects."""
        article = CompiledArticle.objects.create(
            title="Multi-cat Article", author=self.author1
        )
        article.categories.add(self.cat_py, self.cat_dj)

        resp = self.client.get("/api/compiledarticles/")
        data = resp.data["data"]
        self.assertEqual(len(data), 1)
        cats = data[0]["categories"]
        self.assertEqual(len(cats), 2)
        self.assertTrue(all("name" in c for c in cats))

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_filter_by_fk_id(self):
        """Frontend: filter dropdown by author."""
        CompiledSampleModel.objects.create(
            title="Alice Book", price=Decimal("10.00"), related=self.author1
        )
        CompiledSampleModel.objects.create(
            title="Bob Book", price=Decimal("20.00"), related=self.author2
        )
        resp = self.client.get(
            f"/api/compiledsamplemodels/?related={self.author1.pk}"
        )
        data = resp.data["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Alice Book")


class ListFilterOrderTests(TestCase):
    """Frontend: data table with sorting, filtering, and pagination."""

    def setUp(self):
        self.client = APIClient()
        self.author = RelatedModel.objects.create(name="Author")
        for i in range(30):
            CompiledSampleModel.objects.create(
                title=f"Item {i:03d}",
                price=Decimal(str(10 + i)),
                is_active=(i % 2 == 0),
                related=self.author,
            )

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_paginated_list(self):
        """Page through results."""
        resp = self.client.get("/api/compiledsamplemodels/?page=1&page_size=10")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["data"]), 10)
        self.assertEqual(resp.data["pagination"]["total_items"], 30)
        self.assertEqual(resp.data["pagination"]["total_pages"], 3)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_order_by_price_ascending(self):
        resp = self.client.get(
            "/api/compiledsamplemodels/?ordering=price&page_size=5"
        )
        data = resp.data["data"]
        prices = [Decimal(d["price"]) for d in data]
        self.assertEqual(prices, sorted(prices))

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_order_by_price_descending(self):
        resp = self.client.get(
            "/api/compiledsamplemodels/?ordering=-price&page_size=5"
        )
        data = resp.data["data"]
        prices = [Decimal(d["price"]) for d in data]
        self.assertEqual(prices, sorted(prices, reverse=True))

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_filter_active_only(self):
        resp = self.client.get("/api/compiledsamplemodels/?is_active=true")
        data = resp.data["data"]
        self.assertTrue(all(d["is_active"] for d in data))
        self.assertEqual(len(data), 15)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_filter_and_order_combined(self):
        """Active items sorted by price descending."""
        resp = self.client.get(
            "/api/compiledsamplemodels/?is_active=true&ordering=-price&page_size=5"
        )
        data = resp.data["data"]
        self.assertTrue(all(d["is_active"] for d in data))
        prices = [Decimal(d["price"]) for d in data]
        self.assertEqual(prices, sorted(prices, reverse=True))

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_search_with_pagination(self):
        """Search + paginate."""
        resp = self.client.get(
            "/api/compiledsamplemodels/?search=Item&page_size=10"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["pagination"]["total_items"], 30)
        self.assertEqual(len(resp.data["data"]), 10)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_filter_search_order_paginate_combined(self):
        """The full combo: active items matching search, ordered, paginated."""
        resp = self.client.get(
            "/api/compiledsamplemodels/"
            "?is_active=true&search=Item&ordering=-price&page_size=5"
        )
        data = resp.data["data"]
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(all(d["is_active"] for d in data))
        prices = [Decimal(d["price"]) for d in data]
        self.assertEqual(prices, sorted(prices, reverse=True))
        self.assertLessEqual(len(data), 5)
