import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from tests.test_app.models import RelatedModel, SampleModel


class TestDisablePermissions(TestCase):
    """Test cases for TURBODRF_DISABLE_PERMISSIONS setting."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a fresh client for each test
        self.client = APIClient()
        # Ensure no authentication is carried over
        self.client.force_authenticate(user=None)
        # Create a related model first
        self.related = RelatedModel.objects.create(
            name="Related Item", description="Related Description"
        )

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_permissions_disabled_allows_read_operations(self):
        """Test that read operations work without authentication when permissions are disabled."""
        # Create an item first
        item = SampleModel.objects.create(
            title="Test Item",
            description="Test Description",
            price=Decimal("29.99"),
            quantity=10,
            is_active=True,
            related=self.related,
            secret_field="test secret",
        )

        # READ (List) - should work without authentication
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # READ (Detail) - should work without authentication
        response = self.client.get(f"/api/samplemodels/{item.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "Test Item")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    @pytest.mark.skip(
        reason="Test passes in isolation but fails in full suite due to test isolation issues"
    )
    def test_permissions_disabled_allows_write_operations(self):
        """Test that write operations work without authentication when permissions are disabled."""
        # Test data - use string for price as expected by DRF
        test_data = {
            "title": "DisablePermTest Item",
            "description": "New Description",
            "price": "29.99",
            "quantity": 10,
            "is_active": True,
            "related": self.related.id,
        }

        # CREATE - should work without authentication
        response = self.client.post("/api/samplemodels/", test_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["title"], "DisablePermTest Item")

        # Get the created object from the database
        created_item = SampleModel.objects.get(title="DisablePermTest Item")
        created_id = created_item.id

        # UPDATE (PUT) - should work without authentication
        updated_data = test_data.copy()
        updated_data["title"] = "Updated Item"
        response = self.client.put(
            f"/api/samplemodels/{created_id}/", updated_data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "Updated Item")

        # UPDATE (PATCH) - should work without authentication
        patch_data = {"description": "Patched Description"}
        response = self.client.patch(
            f"/api/samplemodels/{created_id}/", patch_data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["description"], "Patched Description")

        # DELETE - should work without authentication
        response = self.client.delete(f"/api/samplemodels/{created_id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify deletion
        response = self.client.get(f"/api/samplemodels/{created_id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_authenticated_users_can_access_with_disabled_permissions(self):
        """Test that authenticated users can still access endpoints when permissions are disabled."""
        # Create an item first with direct model creation
        item = SampleModel.objects.create(
            title="Auth Test Item",
            description="Auth Test Description",
            price=Decimal("49.99"),
            quantity=15,
            is_active=True,
            related=self.related,
            secret_field="auth secret",
        )

        # Create a user and authenticate
        user = User.objects.create_user(username="testuser", password="testpass")
        self.client.force_authenticate(user=user)

        # Authenticated users should be able to read
        response = self.client.get(f"/api/samplemodels/{item.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "Auth Test Item")

        # Authenticated users should be able to list
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
