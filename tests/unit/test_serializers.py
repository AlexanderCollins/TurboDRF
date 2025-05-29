"""
Unit tests for TurboDRF serializers.

Tests the dynamic serializer generation and nested field handling.
"""

from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory
from turbodrf.serializers import TurboDRFSerializer, TurboDRFSerializerFactory
from tests.test_app.models import SampleModel, RelatedModel


User = get_user_model()


class TestTurboDRFSerializer(TestCase):
    """Test cases for TurboDRF serializer functionality."""

    def setUp(self):
        """Set up test fixtures."""
        # Create related model instance
        self.related = RelatedModel.objects.create(
            name="Related Item", description="A related item for testing"
        )

        # Create test model instance
        self.test_obj = SampleModel.objects.create(
            title="Test Item",
            description="Test description",
            price=Decimal("99.99"),
            quantity=10,
            related=self.related,
            secret_field="Secret data",
            is_active=True,
        )

    def test_basic_serialization(self):
        """Test basic serialization without nested fields."""
        # Create a simple serializer
        SimpleSerializer = type(
            "SimpleSerializer",
            (TurboDRFSerializer,),
            {
                "Meta": type(
                    "Meta",
                    (),
                    {"model": SampleModel, "fields": ["title", "price", "is_active"]},
                )
            },
        )

        serializer = SimpleSerializer(self.test_obj)
        data = serializer.data

        self.assertEqual(data["title"], "Test Item")
        self.assertEqual(data["price"], "99.99")
        self.assertTrue(data["is_active"])
        self.assertNotIn("description", data)
        self.assertNotIn("secret_field", data)

    def test_nested_field_serialization(self):
        """Test serialization with nested fields."""
        # Create serializer with nested fields
        NestedSerializer = type(
            "NestedSerializer",
            (TurboDRFSerializer,),
            {
                "Meta": type(
                    "Meta",
                    (),
                    {
                        "model": SampleModel,
                        "fields": ["title", "related"],
                        "_nested_fields": {
                            "related": ["related__name", "related__description"]
                        },
                    },
                )
            },
        )

        serializer = NestedSerializer(self.test_obj)
        data = serializer.data

        self.assertEqual(data["title"], "Test Item")
        self.assertIn("related", data)
        self.assertEqual(data["related"], self.related.id)
        self.assertEqual(data["related_name"], "Related Item")
        self.assertEqual(data["related_description"], "A related item for testing")

    def test_to_representation_with_nested_fields(self):
        """Test the to_representation method handles nested fields correctly."""

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = SampleModel
                fields = ["title", "related"]
                _nested_fields = {"related": ["related__name"]}

        serializer = TestSerializer(self.test_obj)
        data = serializer.data

        self.assertIn("related_name", data)
        self.assertEqual(data["related_name"], "Related Item")

    def test_none_handling_in_nested_fields(self):
        """Test that None values are handled properly in nested fields."""

        # Test with a nullable field (published_date can be None)
        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = SampleModel
                fields = ["title", "published_date"]
                _nested_fields = {
                    "published_date": [
                        "year",
                        "month",
                    ]  # Accessing attributes of a None date
                }

        # Create object with None published_date
        test_obj = SampleModel.objects.create(
            title="No Date",
            price=Decimal("50.00"),
            quantity=5,
            related=self.related,
            published_date=None,
        )

        serializer = TestSerializer(test_obj)
        data = serializer.data

        # Should not raise an error and should handle None gracefully
        self.assertIn("title", data)
        self.assertEqual(data["title"], "No Date")
        # These should be None, not cause an error
        self.assertIn("published_date_year", data)
        self.assertIsNone(data["published_date_year"])
        self.assertIn("published_date_month", data)
        self.assertIsNone(data["published_date_month"])


class TestTurboDRFSerializerFactory(TestCase):
    """Test cases for TurboDRF serializer factory."""

    def setUp(self):
        """Set up test fixtures."""
        # Create users with different roles
        self.admin_user = User.objects.create_user(username="admin", is_superuser=True)
        self.admin_user._test_roles = ["admin"]
        self.editor_user = User.objects.create_user(username="editor", is_staff=True)
        self.editor_user._test_roles = ["editor"]
        self.viewer_user = User.objects.create_user(username="viewer")
        self.viewer_user._test_roles = ["viewer"]

        # Create test data
        self.related = RelatedModel.objects.create(
            name="Related", description="Description"
        )
        self.test_obj = SampleModel.objects.create(
            title="Test",
            price=Decimal("100.00"),
            quantity=1,
            related=self.related,
            secret_field="Secret",
        )

    def test_create_serializer_with_permissions(self):
        """Test creating serializer with field permissions."""
        fields = ["title", "price", "secret_field"]

        # Admin should see all fields
        AdminSerializer = TurboDRFSerializerFactory.create_serializer(
            SampleModel, fields, self.admin_user
        )
        serializer = AdminSerializer(self.test_obj)
        data = serializer.data

        self.assertIn("title", data)
        self.assertIn("price", data)
        self.assertIn("secret_field", data)

    def test_get_permitted_fields(self):
        """Test field permission filtering."""
        fields = ["title", "price", "secret_field"]

        # Admin should get all fields
        admin_fields = TurboDRFSerializerFactory._get_permitted_fields(
            SampleModel, fields, self.admin_user
        )
        self.assertEqual(set(admin_fields), set(fields))

        # Editor should get title and price (read permission)
        editor_fields = TurboDRFSerializerFactory._get_permitted_fields(
            SampleModel, fields, self.editor_user
        )
        self.assertIn("title", editor_fields)
        self.assertIn("price", editor_fields)
        self.assertNotIn("secret_field", editor_fields)

        # Viewer should only get title
        viewer_fields = TurboDRFSerializerFactory._get_permitted_fields(
            SampleModel, fields, self.viewer_user
        )
        self.assertIn("title", viewer_fields)
        self.assertNotIn("price", viewer_fields)
        self.assertNotIn("secret_field", viewer_fields)

    def test_get_read_only_fields(self):
        """Test read-only field determination."""
        fields = ["title", "price"]

        # Admin can write all fields
        admin_readonly = TurboDRFSerializerFactory._get_read_only_fields(
            SampleModel, fields, self.admin_user
        )
        self.assertNotIn("price", admin_readonly)

        # Editor cannot write price
        editor_readonly = TurboDRFSerializerFactory._get_read_only_fields(
            SampleModel, fields, self.editor_user
        )
        self.assertIn("price", editor_readonly)

        # Viewer cannot write anything
        viewer_readonly = TurboDRFSerializerFactory._get_read_only_fields(
            SampleModel, fields, self.viewer_user
        )
        self.assertIn("title", viewer_readonly)
        self.assertIn("price", viewer_readonly)

    def test_create_nested_serializer(self):
        """Test nested serializer creation."""
        NestedSerializer = TurboDRFSerializerFactory._create_nested_serializer(
            RelatedModel, ["name", "description"], self.admin_user
        )

        serializer = NestedSerializer(self.related)
        data = serializer.data

        self.assertEqual(data["name"], "Related")
        self.assertEqual(data["description"], "Description")

    def test_handle_nested_fields_in_factory(self):
        """Test factory handling of nested field notation."""
        fields = ["title", "related__name", "related__description"]

        # This should create appropriate nested serializers
        SerializerClass = TurboDRFSerializerFactory.create_serializer(
            SampleModel, fields, self.admin_user
        )

        # Check that the serializer works
        serializer = SerializerClass(self.test_obj)
        data = serializer.data

        self.assertIn("title", data)
        # The factory should handle nested fields appropriately
        # (exact behavior depends on implementation)
