"""Test JSONField support in TurboDRF."""

import json

from django.db import models
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory

from turbodrf.views import TurboDRFViewSet


class JSONFieldTestModel(models.Model):
    """Test model with JSONField."""

    name = models.CharField(max_length=100)
    data = models.JSONField(default=dict)
    metadata = models.JSONField(null=True, blank=True)

    class Meta:
        app_label = "tests"

    @classmethod
    def turbodrf(cls):
        return {"fields": ["id", "name", "data", "metadata"]}


class TestJSONFieldSupport(TestCase):
    """Test that JSONField is properly handled in TurboDRF."""

    def test_jsonfield_excluded_from_filterset(self):
        """Test that JSONField is excluded from automatic filtering."""
        # Create a viewset for the test model
        viewset = TurboDRFViewSet()
        viewset.model = JSONFieldTestModel

        # Get the filterset fields
        filterset_fields = viewset.get_filterset_fields()

        # JSONField fields should not be in the filterset
        self.assertNotIn("data", filterset_fields)
        self.assertNotIn("metadata", filterset_fields)

        # Regular fields should still be there
        self.assertIn("name", filterset_fields)
        self.assertIn("id", filterset_fields)

    def test_jsonfield_in_serializer(self):
        """Test that JSONField is properly included in serializer."""
        # Create a viewset and request
        factory = APIRequestFactory()
        request = factory.get("/")

        viewset = TurboDRFViewSet()
        viewset.model = JSONFieldTestModel
        viewset.request = request
        viewset.action = "list"

        # Get the serializer class
        serializer_class = viewset.get_serializer_class()

        # Check that JSONField is in the serializer fields
        serializer_fields = serializer_class().fields.keys()
        self.assertIn("data", serializer_fields)
        self.assertIn("metadata", serializer_fields)

    def test_filtering_with_jsonfield_model(self):
        """Test that filtering works correctly when model has JSONField."""
        # Create a viewset for the test model
        viewset = TurboDRFViewSet()
        viewset.model = JSONFieldTestModel

        # Create a mock request with filter parameters
        factory = APIRequestFactory()
        request = factory.get("/?name=test")

        viewset.request = request
        viewset.action = "list"

        # Get filterset fields - should not raise an error
        filterset_fields = viewset.get_filterset_fields()

        # Verify that we can filter by regular fields
        self.assertIn("name", filterset_fields)
        self.assertEqual(
            filterset_fields["name"], ["exact", "icontains", "istartswith", "iendswith"]
        )

    def test_other_field_types_still_work(self):
        """Test that other field types are still properly configured."""

        class ComplexModel(models.Model):
            name = models.CharField(max_length=100)
            age = models.IntegerField()
            price = models.DecimalField(max_digits=10, decimal_places=2)
            is_active = models.BooleanField(default=True)
            created_at = models.DateTimeField(auto_now_add=True)
            data = models.JSONField(default=dict)  # This should be skipped

            class Meta:
                app_label = "tests"

        viewset = TurboDRFViewSet()
        viewset.model = ComplexModel

        filterset_fields = viewset.get_filterset_fields()

        # Check that all non-JSON fields are present with correct lookups
        self.assertEqual(
            filterset_fields["name"], ["exact", "icontains", "istartswith", "iendswith"]
        )
        self.assertEqual(filterset_fields["age"], ["exact", "gte", "lte", "gt", "lt"])
        self.assertEqual(filterset_fields["price"], ["exact", "gte", "lte", "gt", "lt"])
        self.assertEqual(filterset_fields["is_active"], ["exact"])
        self.assertEqual(
            filterset_fields["created_at"],
            ["exact", "gte", "lte", "gt", "lt", "year", "month", "day"],
        )

        # JSONField should be excluded
        self.assertNotIn("data", filterset_fields)

    def test_special_field_types_handling(self):
        """Test that special field types are handled correctly."""

        class SpecialFieldsModel(models.Model):
            # Regular fields
            name = models.CharField(max_length=100)

            # Special fields that should be skipped or handled specially
            json_data = models.JSONField(default=dict)
            binary_data = models.BinaryField()
            file_upload = models.FileField(upload_to="uploads/")
            image_upload = models.ImageField(upload_to="images/")

            class Meta:
                app_label = "tests"

        viewset = TurboDRFViewSet()
        viewset.model = SpecialFieldsModel

        filterset_fields = viewset.get_filterset_fields()

        # Regular field should have text lookups
        self.assertEqual(
            filterset_fields["name"], ["exact", "icontains", "istartswith", "iendswith"]
        )

        # JSONField and BinaryField should be excluded
        self.assertNotIn("json_data", filterset_fields)
        self.assertNotIn("binary_data", filterset_fields)

        # File fields should have limited lookups
        self.assertEqual(filterset_fields["file_upload"], ["exact", "isnull"])
        self.assertEqual(filterset_fields["image_upload"], ["exact", "isnull"])
