"""Test that POST requests work with and without trailing slashes."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from tests.test_app.models import RelatedModel, SampleModel

User = get_user_model()


class TestTrailingSlash(TestCase):
    """Test API endpoints work with and without trailing slashes."""

    def setUp(self):
        """Set up test fixtures."""
        self.client = APIClient()
        
        # Create test user
        self.user = User.objects.create_superuser(
            username="admin", password="admin123"
        )
        self.user._test_roles = ["admin"]
        
        # Create related model
        self.related = RelatedModel.objects.create(
            name="Test Related", description="Test"
        )
        
        # Authenticate
        self.client.force_authenticate(user=self.user)

    def test_post_with_trailing_slash(self):
        """Test POST request with trailing slash works."""
        data = {
            "title": "Test Item 1",
            "description": "Test description",
            "price": "99.99",
            "quantity": 10,
            "related": self.related.id,
            "is_active": True,
            "secret_field": "secret",
        }
        
        response = self.client.post("/api/samplemodels/", data, format="json")
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["title"], "Test Item 1")
        # Ensure response is not paginated
        self.assertNotIn("pagination", response.data)
        self.assertNotIn("data", response.data)

    def test_post_without_trailing_slash(self):
        """Test POST request without trailing slash works."""
        data = {
            "title": "Test Item 2",
            "description": "Test description",
            "price": "99.99",
            "quantity": 10,
            "related": self.related.id,
            "is_active": True,
            "secret_field": "secret",
        }
        
        response = self.client.post("/api/samplemodels", data, format="json")
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["title"], "Test Item 2")
        # Ensure response is not paginated
        self.assertNotIn("pagination", response.data)
        self.assertNotIn("data", response.data)

    def test_get_with_and_without_slash(self):
        """Test GET requests work with and without trailing slashes."""
        # Create a test item
        SampleModel.objects.create(
            title="Test Item",
            price=Decimal("99.99"),
            related=self.related,
        )
        
        # Test with trailing slash
        response1 = self.client.get("/api/samplemodels/")
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertIn("pagination", response1.data)
        self.assertIn("data", response1.data)
        self.assertEqual(len(response1.data["data"]), 1)
        
        # Test without trailing slash
        response2 = self.client.get("/api/samplemodels")
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertIn("pagination", response2.data)
        self.assertIn("data", response2.data)
        self.assertEqual(len(response2.data["data"]), 1)