"""
Integration tests for role-based permissions.

Tests that different user roles have appropriate access to API endpoints
and fields.
"""

from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from tests.test_app.models import SampleModel, RelatedModel


User = get_user_model()


class TestRoleBasedPermissions(TestCase):
    """Test role-based permission enforcement in API."""

    def setUp(self):
        """Set up test fixtures."""
        self.client = APIClient()

        # Create users with different roles
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
        self.related = RelatedModel.objects.create(
            name="Test Category", description="Test description"
        )
        self.item = SampleModel.objects.create(
            title="Test Product",
            description="Test description",
            price=Decimal("100.00"),
            quantity=10,
            related=self.related,
            secret_field="Secret Data",
            is_active=True,
        )

    def test_unauthenticated_permissions(self):
        """Test unauthenticated user permissions."""
        # Can read
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Cannot create
        response = self.client.post(
            "/api/samplemodels/", {"title": "New Item", "price": "50.00"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Cannot update
        response = self.client.put(
            f"/api/samplemodels/{self.item.id}/", {"title": "Updated"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Cannot delete
        response = self.client.delete(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_permissions(self):
        """Test admin user has full permissions."""
        self.client.force_authenticate(user=self.admin_user)

        # Can read
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Can create
        response = self.client.post(
            "/api/samplemodels/",
            {
                "title": "Admin Created",
                "description": "Created by admin",
                "price": "200.00",
                "quantity": 5,
                "related": self.related.id,
                "secret_field": "Admin Secret",
                "is_active": True,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Can update
        response = self.client.put(
            f"/api/samplemodels/{self.item.id}/",
            {
                "title": "Admin Updated",
                "description": "Updated by admin",
                "price": "150.00",
                "quantity": 15,
                "related": self.related.id,
                "secret_field": "Updated Secret",
                "is_active": False,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Can delete
        new_item = SampleModel.objects.create(
            title="To Delete", price=Decimal("10.00"), quantity=1, related=self.related
        )
        response = self.client.delete(f"/api/samplemodels/{new_item.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_editor_permissions(self):
        """Test editor user permissions (read and update only)."""
        self.client.force_authenticate(user=self.editor_user)

        # Can read
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Cannot create
        response = self.client.post(
            "/api/samplemodels/",
            {
                "title": "Editor Created",
                "price": "50.00",
                "quantity": 5,
                "related": self.related.id,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Can update
        response = self.client.patch(
            f"/api/samplemodels/{self.item.id}/", {"title": "Editor Updated"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Cannot delete
        response = self.client.delete(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_viewer_permissions(self):
        """Test viewer user permissions (read only)."""
        self.client.force_authenticate(user=self.viewer_user)

        # Can read
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Cannot create
        response = self.client.post(
            "/api/samplemodels/", {"title": "Viewer Created", "price": "50.00"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Cannot update
        response = self.client.patch(
            f"/api/samplemodels/{self.item.id}/", {"title": "Viewer Updated"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Cannot delete
        response = self.client.delete(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_field_level_permissions_admin(self):
        """Test admin can see and modify all fields."""
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Admin should see all fields including secret_field and price
        self.assertIn("secret_field", response.data)
        self.assertIn("price", response.data)
        self.assertEqual(response.data["secret_field"], "Secret Data")
        self.assertEqual(response.data["price"], "100.00")

        # Admin can update price
        response = self.client.patch(
            f"/api/samplemodels/{self.item.id}/",
            {"price": "200.00", "secret_field": "New Secret"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.item.refresh_from_db()
        self.assertEqual(self.item.price, Decimal("200.00"))
        self.assertEqual(self.item.secret_field, "New Secret")

    def test_field_level_permissions_editor(self):
        """Test editor has limited field access."""
        self.client.force_authenticate(user=self.editor_user)

        response = self.client.get(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Editor should see price but not secret_field
        self.assertIn("price", response.data)
        self.assertNotIn("secret_field", response.data)

        # Editor cannot update price (read-only)
        original_price = self.item.price
        response = self.client.patch(
            f"/api/samplemodels/{self.item.id}/", {"price": "500.00"}
        )

        # The request might succeed but price shouldn't change
        self.item.refresh_from_db()
        self.assertEqual(self.item.price, original_price)

    def test_field_level_permissions_viewer(self):
        """Test viewer has minimal field access."""
        self.client.force_authenticate(user=self.viewer_user)

        # List view - limited fields
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        item_data = response.data["data"][0]
        self.assertIn("title", item_data)
        self.assertNotIn("price", item_data)  # Viewer cannot see price
        self.assertNotIn("secret_field", item_data)

        # Detail view - also limited fields
        response = self.client.get(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("title", response.data)
        self.assertNotIn("price", response.data)
        self.assertNotIn("secret_field", response.data)

    def test_related_model_permissions(self):
        """Test permissions on related models."""
        # Test that permissions work for RelatedModel too
        self.client.force_authenticate(user=self.admin_user)

        # Admin can CRUD related models
        response = self.client.get("/api/relatedmodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.post(
            "/api/relatedmodels/",
            {"name": "New Category", "description": "New description"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Viewer can only read
        self.client.force_authenticate(user=self.viewer_user)

        response = self.client.get("/api/relatedmodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.post("/api/relatedmodels/", {"name": "Viewer Category"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_permission_inheritance_through_relationships(self):
        """Test that nested field access respects permissions."""
        # Create item with related data
        item = SampleModel.objects.create(
            title="Nested Test",
            price=Decimal("50.00"),
            quantity=5,
            related=self.related,
            secret_field="Secret",
        )

        # Admin sees nested fields
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(f"/api/samplemodels/{item.id}/")

        self.assertIn("related_name", response.data)
        self.assertIn("related_description", response.data)

        # Viewer also sees nested fields (if they have permission)
        self.client.force_authenticate(user=self.viewer_user)
        response = self.client.get(f"/api/samplemodels/{item.id}/")

        self.assertIn("related_name", response.data)
        self.assertIn("related_description", response.data)

    def test_multiple_roles_user(self):
        """Test user with multiple roles gets combined permissions."""
        # Create user with custom multiple roles
        multi_role_user = User.objects.create_user(
            username="multirole", password="multi123"
        )
        multi_role_user._test_roles = ["viewer", "editor"]

        self.client.force_authenticate(user=multi_role_user)

        # Should have combined permissions of viewer and editor
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Can update (from editor role)
        response = self.client.patch(
            f"/api/samplemodels/{self.item.id}/", {"title": "Multi-role Updated"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Still cannot delete (neither role has delete permission)
        response = self.client.delete(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
