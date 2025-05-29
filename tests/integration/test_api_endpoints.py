"""
Integration tests for TurboDRF API endpoints.

Tests the complete API functionality including CRUD operations,
search, filtering, ordering, and pagination.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from tests.test_app.models import RelatedModel, SampleModel

User = get_user_model()


class TestAPIEndpoints(TestCase):
    """Integration tests for API endpoints."""

    def setUp(self):
        """Set up test fixtures."""
        self.client = APIClient()

        # Create users
        self.admin_user = User.objects.create_user(
            username="admin", password="admin123", is_superuser=True
        )
        self.admin_user._test_roles = ["admin"]

        self.editor_user = User.objects.create_user(
            username="editor", password="editor123", is_staff=True
        )
        self.editor_user._test_roles = ["editor"]

        self.viewer_user = User.objects.create_user(
            username="viewer", password="viewer123"
        )
        self.viewer_user._test_roles = ["viewer"]

        # Create test data
        self.related1 = RelatedModel.objects.create(
            name="Category A", description="First category"
        )
        self.related2 = RelatedModel.objects.create(
            name="Category B", description="Second category"
        )

        self.item1 = SampleModel.objects.create(
            title="Apple Product",
            description="A great apple product",
            price=Decimal("999.99"),
            quantity=10,
            related=self.related1,
            secret_field="Secret1",
            is_active=True,
        )
        self.item2 = SampleModel.objects.create(
            title="Banana Product",
            description="A yellow banana product",
            price=Decimal("49.99"),
            quantity=100,
            related=self.related2,
            secret_field="Secret2",
            is_active=True,
        )
        self.item3 = SampleModel.objects.create(
            title="Cherry Product",
            description="A red cherry product",
            price=Decimal("199.99"),
            quantity=50,
            related=self.related1,
            secret_field="Secret3",
            is_active=False,
        )

    def test_api_root(self):
        """Test API root endpoint."""
        response = self.client.get("/api/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that endpoints are listed
        self.assertIn("samplemodels", response.data)
        self.assertIn("relatedmodels", response.data)
        self.assertIn("custom-items", response.data)

    def test_list_endpoint_unauthenticated(self):
        """Test list endpoint for unauthenticated users."""
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check response structure
        self.assertIn("pagination", response.data)
        self.assertIn("data", response.data)

        # Check pagination structure
        pagination = response.data["pagination"]
        self.assertIn("current_page", pagination)
        self.assertIn("total_pages", pagination)
        self.assertIn("total_items", pagination)
        self.assertEqual(pagination["total_items"], 3)

        # Check data
        items = response.data["data"]
        self.assertEqual(len(items), 3)

        # Check that nested fields are included
        first_item = items[0]
        self.assertIn("title", first_item)
        self.assertIn("price", first_item)
        self.assertIn("related_name", first_item)
        self.assertNotIn(
            "secret_field", first_item
        )  # Should not be visible to unauthenticated

    def test_detail_endpoint_unauthenticated(self):
        """Test detail endpoint for unauthenticated users."""
        response = self.client.get(f"/api/samplemodels/{self.item1.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check fields
        self.assertEqual(response.data["title"], "Apple Product")
        self.assertIn("description", response.data)
        self.assertIn("related_name", response.data)
        self.assertIn("related_description", response.data)
        self.assertEqual(response.data["related_name"], "Category A")

    def test_create_endpoint_authenticated(self):
        """Test create endpoint with authentication."""
        self.client.force_authenticate(user=self.admin_user)

        data = {
            "title": "New Product",
            "description": "A new product",
            "price": "299.99",
            "quantity": 25,
            "related": self.related1.id,
            "secret_field": "NewSecret",
            "is_active": True,
        }

        response = self.client.post("/api/samplemodels/", data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify created object
        self.assertEqual(SampleModel.objects.count(), 4)
        new_item = SampleModel.objects.get(title="New Product")
        self.assertEqual(new_item.price, Decimal("299.99"))

    def test_update_endpoint_authenticated(self):
        """Test update endpoint with authentication."""
        self.client.force_authenticate(user=self.admin_user)

        data = {
            "title": "Updated Apple Product",
            "description": "Updated description",
            "price": "1099.99",
            "quantity": 5,
            "related": self.related2.id,
            "secret_field": "UpdatedSecret",
            "is_active": False,
        }

        response = self.client.put(
            f"/api/samplemodels/{self.item1.id}/", data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify updated object
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.title, "Updated Apple Product")
        self.assertEqual(self.item1.price, Decimal("1099.99"))
        self.assertEqual(self.item1.related, self.related2)

    def test_partial_update_endpoint(self):
        """Test partial update (PATCH) endpoint."""
        self.client.force_authenticate(user=self.admin_user)

        data = {"price": "1299.99"}

        response = self.client.patch(
            f"/api/samplemodels/{self.item1.id}/", data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify only price was updated
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.price, Decimal("1299.99"))
        self.assertEqual(self.item1.title, "Apple Product")  # Unchanged

    def test_delete_endpoint_authenticated(self):
        """Test delete endpoint with authentication."""
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.delete(f"/api/samplemodels/{self.item1.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify deletion
        self.assertEqual(SampleModel.objects.count(), 2)
        self.assertFalse(SampleModel.objects.filter(id=self.item1.id).exists())

    def test_search_functionality(self):
        """Test search functionality."""
        # Search for 'apple'
        response = self.client.get("/api/samplemodels/", {"search": "apple"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data["data"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Apple Product")

        # Search for 'product' should return all
        response = self.client.get("/api/samplemodels/", {"search": "product"})
        items = response.data["data"]
        self.assertEqual(len(items), 3)

        # Search in description
        response = self.client.get("/api/samplemodels/", {"search": "yellow"})
        items = response.data["data"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Banana Product")

    def test_filtering_functionality(self):
        """Test filtering functionality."""
        # Filter by is_active
        response = self.client.get("/api/samplemodels/", {"is_active": "true"})
        items = response.data["data"]
        self.assertEqual(len(items), 2)

        # Filter by related model
        response = self.client.get("/api/samplemodels/", {"related": self.related1.id})
        items = response.data["data"]
        self.assertEqual(len(items), 2)

        # Filter by price range
        response = self.client.get(
            "/api/samplemodels/", {"price__gte": "100", "price__lte": "500"}
        )
        items = response.data["data"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Cherry Product")

    def test_ordering_functionality(self):
        """Test ordering functionality."""
        # Order by price ascending
        response = self.client.get("/api/samplemodels/", {"ordering": "price"})
        items = response.data["data"]
        self.assertEqual(items[0]["title"], "Banana Product")  # Cheapest
        self.assertEqual(items[2]["title"], "Apple Product")  # Most expensive

        # Order by price descending
        response = self.client.get("/api/samplemodels/", {"ordering": "-price"})
        items = response.data["data"]
        self.assertEqual(items[0]["title"], "Apple Product")  # Most expensive
        self.assertEqual(items[2]["title"], "Banana Product")  # Cheapest

        # Order by multiple fields
        response = self.client.get(
            "/api/samplemodels/", {"ordering": "is_active,-price"}
        )
        items = response.data["data"]
        # Inactive items first, then by price descending
        self.assertEqual(items[0]["title"], "Cherry Product")  # Inactive

    def test_pagination_functionality(self):
        """Test pagination functionality."""
        # Test custom page size
        response = self.client.get("/api/samplemodels/", {"page_size": "2"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data["data"]
        self.assertEqual(len(items), 2)

        pagination = response.data["pagination"]
        self.assertEqual(pagination["current_page"], 1)
        self.assertEqual(pagination["total_pages"], 2)
        self.assertIsNotNone(pagination["next"])
        self.assertIsNone(pagination["previous"])

        # Test second page
        response = self.client.get(
            "/api/samplemodels/", {"page": "2", "page_size": "2"}
        )
        items = response.data["data"]
        self.assertEqual(len(items), 1)

        pagination = response.data["pagination"]
        self.assertEqual(pagination["current_page"], 2)
        self.assertIsNone(pagination["next"])
        self.assertIsNotNone(pagination["previous"])

    def test_combined_query_parameters(self):
        """Test combining search, filter, and ordering."""
        # Search for 'product', filter by active, order by price
        response = self.client.get(
            "/api/samplemodels/",
            {"search": "product", "is_active": "true", "ordering": "-price"},
        )

        items = response.data["data"]
        self.assertEqual(len(items), 2)  # Only active products
        self.assertEqual(items[0]["title"], "Apple Product")  # Higher price
        self.assertEqual(items[1]["title"], "Banana Product")  # Lower price

    def test_custom_endpoint_model(self):
        """Test model with custom endpoint name."""
        # Create a custom endpoint model instance
        from tests.test_app.models import CustomEndpointModel

        CustomEndpointModel.objects.create(name="Custom Item")

        # Test the custom endpoint
        response = self.client.get("/api/custom-items/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        items = response.data["data"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Custom Item")

    def test_options_request(self):
        """Test OPTIONS request for metadata."""
        response = self.client.options("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that it returns allowed methods
        self.assertIn("name", response.data)
        self.assertIn("description", response.data)
