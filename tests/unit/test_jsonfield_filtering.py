import pytest
from django.db import models
from django.test import RequestFactory
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.test import APIRequestFactory

from turbodrf.views import TurboDRFViewSet


class TestJSONFieldModel(models.Model):
    """Test model with JSONField and other field types."""

    name = models.CharField(max_length=100)
    data = models.JSONField(default=dict)
    vendor_data_json = models.JSONField(default=dict, blank=True, null=True)
    config = models.JSONField(default=dict)
    metadata = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    uuid_field = models.UUIDField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        app_label = "tests"

    @classmethod
    def turbodrf(cls):
        return {
            "fields": [
                "id",
                "name",
                "data",
                "vendor_data_json",
                "config",
                "metadata",
                "created_at",
            ]
        }


class TestJSONFieldFiltering:
    """Test JSONField handling in TurboDRF filtering."""

    def test_jsonfield_excluded_from_filters(self):
        """Test that JSONField is properly excluded from filterset fields."""
        # Create viewset
        viewset = TurboDRFViewSet()
        viewset.model = TestJSONFieldModel

        # Get filterset fields
        filterset_fields = viewset.get_filterset_fields()

        # JSONFields should not be in filterset_fields
        assert "data" not in filterset_fields
        assert "vendor_data_json" not in filterset_fields
        assert "config" not in filterset_fields
        assert "metadata" not in filterset_fields

        # Other fields should be present
        assert "name" in filterset_fields
        assert "created_at" in filterset_fields

        # Check that text fields have correct lookups
        assert set(filterset_fields["name"]) == {
            "exact",
            "icontains",
            "istartswith",
            "iendswith",
        }

        # Check that datetime fields have correct lookups
        assert "exact" in filterset_fields["created_at"]
        assert "gte" in filterset_fields["created_at"]
        assert "lte" in filterset_fields["created_at"]

        # Check UUID and IP fields
        assert "uuid_field" in filterset_fields
        assert set(filterset_fields["uuid_field"]) == {"exact", "isnull"}
        assert "ip_address" in filterset_fields
        assert set(filterset_fields["ip_address"]) == {"exact", "istartswith"}

    def test_list_endpoint_with_jsonfield_model(self):
        """Test that list endpoint works correctly with models containing JSONField."""
        # Create viewset instance
        viewset = TurboDRFViewSet()
        viewset.model = TestJSONFieldModel
        viewset.request = APIRequestFactory().get("/test/")
        viewset.action = "list"

        # This should not raise an AssertionError
        viewset.filter_backends = [DjangoFilterBackend]

        # Get filterset fields - this is what triggers the error
        filterset_fields = viewset.filterset_fields

        # Verify JSONFields are excluded
        assert "data" not in filterset_fields
        assert "vendor_data_json" not in filterset_fields
        assert "config" not in filterset_fields
        assert "metadata" not in filterset_fields

    def test_jsonfield_in_serializer(self):
        """Test that JSONField is included in serializer fields."""
        viewset = TurboDRFViewSet()
        viewset.model = TestJSONFieldModel
        viewset.action = "list"

        serializer_class = viewset.get_serializer_class()
        serializer = serializer_class()

        # JSONFields should be in serializer fields
        assert "data" in serializer.fields
        assert "vendor_data_json" in serializer.fields
        assert "config" in serializer.fields
        assert "metadata" in serializer.fields
