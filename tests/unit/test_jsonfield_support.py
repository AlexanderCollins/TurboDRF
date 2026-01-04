"""Test JSONField support in TurboDRF."""

from django.db import models
from django.test import TestCase
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

        # JSONField, BinaryField, FileField, and ImageField should be excluded
        # (django-filter doesn't support these field types)
        self.assertNotIn("json_data", filterset_fields)
        self.assertNotIn("binary_data", filterset_fields)
        self.assertNotIn("file_upload", filterset_fields)
        self.assertNotIn("image_upload", filterset_fields)

    def test_imagefield_with_django_filter_integration(self):
        """Test that ImageField works correctly with django-filter integration."""
        from django_filters import FilterSet

        from turbodrf.mixins import TurboDRFMixin

        class ImageModel(TurboDRFMixin, models.Model):
            name = models.CharField(max_length=100)
            photo = models.ImageField(upload_to="photos/")
            thumbnail = models.ImageField(upload_to="thumbs/", null=True, blank=True)

            class Meta:
                app_label = "tests"

            @classmethod
            def turbodrf(cls):
                return {"fields": ["id", "name", "photo", "thumbnail"]}

        # Test 1: get_filterset_fields should exclude ImageField
        viewset = TurboDRFViewSet()
        viewset.model = ImageModel

        filterset_fields = viewset.get_filterset_fields()

        # ImageField should be excluded (django-filter doesn't support it)
        self.assertNotIn("photo", filterset_fields)
        self.assertNotIn("thumbnail", filterset_fields)

        # But regular fields should be present
        self.assertIn("name", filterset_fields)
        self.assertEqual(
            filterset_fields["name"], ["exact", "icontains", "istartswith", "iendswith"]
        )

        # Test 2: Creating FilterSet should not crash
        # (no ImageFields in filterset_fields)
        try:
            # Create a dynamic FilterSet class like django-filter does
            class ImageModelFilterSet(FilterSet):
                class Meta:
                    model = ImageModel
                    fields = filterset_fields

            # This should not raise an error
            filter_instance = ImageModelFilterSet()

            # Verify name filter was created but not photo
            self.assertIn("name", filter_instance.filters)
            self.assertNotIn("photo", filter_instance.filters)
            self.assertNotIn("thumbnail", filter_instance.filters)

        except Exception as e:
            self.fail(f"Creating FilterSet with ImageModel failed: {e}")

        # Test 3: Using the FilterSet should not crash
        try:
            # Create an empty queryset
            queryset = ImageModel.objects.none()

            # Apply filters (only on supported fields)
            filter_instance = ImageModelFilterSet(
                data={"name__icontains": "test"}, queryset=queryset
            )

            # This should not crash
            filter_instance.qs

        except Exception as e:
            self.fail(f"Using FilterSet with ImageModel failed: {e}")

    def test_imagefield_serialization_in_rest_output(self):
        """Test that ImageField can be serialized in REST output without crashing."""
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory

        from turbodrf.mixins import TurboDRFMixin

        User = get_user_model()

        class ImageModel(TurboDRFMixin, models.Model):
            name = models.CharField(max_length=100)
            photo = models.ImageField(upload_to="photos/")

            class Meta:
                app_label = "tests"

            @classmethod
            def turbodrf(cls):
                return {"fields": ["id", "name", "photo"]}

        # Create a viewset
        factory = APIRequestFactory()
        request = factory.get("/")

        # Create a mock user with admin role
        user = User(username="testuser", is_superuser=True)
        user._test_roles = ["admin"]
        request.user = user

        viewset = TurboDRFViewSet()
        viewset.model = ImageModel
        viewset.request = request
        viewset.action = "list"

        # Test getting serializer class should not crash
        try:
            serializer_class = viewset.get_serializer_class()

            # Check that ImageField is in the serializer
            serializer_instance = serializer_class()
            self.assertIn("photo", serializer_instance.fields)

        except Exception as e:
            self.fail(f"Getting serializer with ImageField failed: {e}")
