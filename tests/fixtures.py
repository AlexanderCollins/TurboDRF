"""
Test fixtures and utilities for TurboDRF tests.

Provides common test data and helper functions.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model

from tests.test_app.models import RelatedModel, SampleModel

User = get_user_model()


class UserFixtures:
    """User fixtures for testing different roles."""

    @staticmethod
    def create_admin_user():
        """Create an admin user."""
        return User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="admin123",
            is_superuser=True,
            is_staff=True,
        )

    @staticmethod
    def create_editor_user():
        """Create an editor user."""
        return User.objects.create_user(
            username="editor",
            email="editor@example.com",
            password="editor123",
            is_staff=True,
        )

    @staticmethod
    def create_viewer_user():
        """Create a viewer user."""
        return User.objects.create_user(
            username="viewer", email="viewer@example.com", password="viewer123"
        )

    @staticmethod
    def create_all_users():
        """Create all test users."""
        return {
            "admin": UserFixtures.create_admin_user(),
            "editor": UserFixtures.create_editor_user(),
            "viewer": UserFixtures.create_viewer_user(),
        }


class ModelFixtures:
    """Model fixtures for testing."""

    @staticmethod
    def create_related_models():
        """Create related model instances."""
        return [
            RelatedModel.objects.create(
                name="Electronics", description="Electronic products and gadgets"
            ),
            RelatedModel.objects.create(
                name="Books", description="Books and publications"
            ),
            RelatedModel.objects.create(
                name="Clothing", description="Apparel and accessories"
            ),
        ]

    @staticmethod
    def create_test_models(related_models):
        """Create test model instances."""
        electronics, books, clothing = related_models

        return [
            SampleModel.objects.create(
                title="Smartphone",
                description="Latest model smartphone with advanced features",
                price=Decimal("799.99"),
                quantity=50,
                related=electronics,
                secret_field="ElectronicsSecret1",
                is_active=True,
            ),
            SampleModel.objects.create(
                title="Laptop",
                description="High-performance laptop for professionals",
                price=Decimal("1299.99"),
                quantity=30,
                related=electronics,
                secret_field="ElectronicsSecret2",
                is_active=True,
            ),
            SampleModel.objects.create(
                title="Python Programming",
                description="Learn Python programming from scratch",
                price=Decimal("39.99"),
                quantity=100,
                related=books,
                secret_field="BooksSecret1",
                is_active=True,
            ),
            SampleModel.objects.create(
                title="Django for Beginners",
                description="Build web applications with Django",
                price=Decimal("44.99"),
                quantity=75,
                related=books,
                secret_field="BooksSecret2",
                is_active=True,
            ),
            SampleModel.objects.create(
                title="Winter Jacket",
                description="Warm winter jacket for cold weather",
                price=Decimal("149.99"),
                quantity=25,
                related=clothing,
                secret_field="ClothingSecret1",
                is_active=True,
            ),
            SampleModel.objects.create(
                title="Running Shoes",
                description="Professional running shoes for athletes",
                price=Decimal("89.99"),
                quantity=60,
                related=clothing,
                secret_field="ClothingSecret2",
                is_active=False,  # Inactive item
            ),
        ]

    @staticmethod
    def create_full_test_data():
        """Create a complete set of test data."""
        users = UserFixtures.create_all_users()
        related_models = ModelFixtures.create_related_models()
        test_models = ModelFixtures.create_test_models(related_models)

        return {
            "users": users,
            "related_models": related_models,
            "test_models": test_models,
        }


class APITestMixin:
    """Mixin providing helper methods for API testing."""

    def assert_has_fields(self, data, fields):
        """Assert that data contains all specified fields."""
        for field in fields:
            self.assertIn(field, data, f"Field '{field}' not found in response")

    def assert_not_has_fields(self, data, fields):
        """Assert that data does not contain specified fields."""
        for field in fields:
            self.assertNotIn(field, data, f"Field '{field}' should not be in response")

    def assert_pagination_structure(self, data):
        """Assert that response has correct pagination structure."""
        self.assertIn("pagination", data)
        self.assertIn("data", data)

        pagination = data["pagination"]
        required_fields = [
            "next",
            "previous",
            "current_page",
            "total_pages",
            "total_items",
        ]
        self.assert_has_fields(pagination, required_fields)

    def assert_can_perform_action(
        self, client, method, url, data=None, expected_status=None
    ):
        """Assert that a user can perform an action."""
        response = getattr(client, method)(url, data=data, format="json")

        if expected_status:
            self.assertEqual(
                response.status_code,
                expected_status,
                f"Expected status {expected_status}, got {response.status_code}",
            )
        else:
            self.assertLess(
                response.status_code,
                400,
                f"Request failed with status {response.status_code}",
            )

        return response

    def assert_cannot_perform_action(self, client, method, url, data=None):
        """Assert that a user cannot perform an action (403 Forbidden)."""
        response = getattr(client, method)(url, data=data, format="json")
        self.assertEqual(
            response.status_code,
            403,
            f"Expected 403 Forbidden, got {response.status_code}",
        )
        return response
