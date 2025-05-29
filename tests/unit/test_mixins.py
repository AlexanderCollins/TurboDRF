"""
Unit tests for TurboDRF mixins.

Tests the core functionality of the TurboDRFMixin class.
"""

import pytest
from django.test import TestCase
from tests.test_app.models import (
    SampleModel,
    RelatedModel,
    NoTurboDRFModel,
    CustomEndpointModel,
    DisabledModel,
)
from turbodrf.mixins import TurboDRFMixin


class TestTurboDRFMixin(TestCase):
    """Test cases for TurboDRFMixin functionality."""

    def test_mixin_inheritance(self):
        """Test that models properly inherit from TurboDRFMixin."""
        self.assertTrue(issubclass(SampleModel, TurboDRFMixin))
        self.assertTrue(issubclass(RelatedModel, TurboDRFMixin))
        self.assertFalse(issubclass(NoTurboDRFModel, TurboDRFMixin))

    def test_turbodrf_method_exists(self):
        """Test that models with mixin have turbodrf classmethod."""
        self.assertTrue(hasattr(SampleModel, "turbodrf"))
        self.assertTrue(callable(getattr(SampleModel, "turbodrf")))
        self.assertFalse(hasattr(NoTurboDRFModel, "turbodrf"))

    def test_turbodrf_returns_dict(self):
        """Test that turbodrf method returns a dictionary."""
        config = SampleModel.turbodrf()
        self.assertIsInstance(config, dict)

        config = RelatedModel.turbodrf()
        self.assertIsInstance(config, dict)

    def test_simple_fields_configuration(self):
        """Test simple fields configuration."""
        config = RelatedModel.turbodrf()
        self.assertIn("fields", config)
        self.assertEqual(config["fields"], ["name", "description"])

    def test_complex_fields_configuration(self):
        """Test complex fields configuration with list/detail views."""
        config = SampleModel.turbodrf()
        self.assertIn("fields", config)
        self.assertIsInstance(config["fields"], dict)
        self.assertIn("list", config["fields"])
        self.assertIn("detail", config["fields"])

        # Check list fields
        list_fields = config["fields"]["list"]
        self.assertIn("title", list_fields)
        self.assertIn("price", list_fields)
        self.assertIn("related__name", list_fields)

        # Check detail fields
        detail_fields = config["fields"]["detail"]
        self.assertIn("description", detail_fields)
        self.assertIn("secret_field", detail_fields)
        self.assertIn("related__description", detail_fields)

    def test_custom_endpoint_configuration(self):
        """Test custom endpoint name configuration."""
        config = CustomEndpointModel.turbodrf()
        self.assertIn("endpoint", config)
        self.assertEqual(config["endpoint"], "custom-items")

    def test_disabled_model_configuration(self):
        """Test disabled model configuration."""
        config = DisabledModel.turbodrf()
        self.assertIn("enabled", config)
        self.assertFalse(config["enabled"])

    def test_searchable_fields_attribute(self):
        """Test searchable_fields attribute on models."""
        self.assertTrue(hasattr(SampleModel, "searchable_fields"))
        self.assertEqual(SampleModel.searchable_fields, ["title", "description"])
        self.assertFalse(hasattr(RelatedModel, "searchable_fields"))

    def test_nested_field_notation(self):
        """Test that nested field notation is properly configured."""
        config = SampleModel.turbodrf()
        list_fields = config["fields"]["list"]
        detail_fields = config["fields"]["detail"]

        # Check nested fields use double underscore notation
        self.assertIn("related__name", list_fields)
        self.assertIn("related__name", detail_fields)
        self.assertIn("related__description", detail_fields)

    def test_all_field_types_included(self):
        """Test that various field types are included in configuration."""
        config = SampleModel.turbodrf()
        detail_fields = config["fields"]["detail"]

        # Check different field types
        field_types = {
            "title": "CharField",
            "description": "TextField",
            "price": "DecimalField",
            "quantity": "IntegerField",
            "is_active": "BooleanField",
            "created_at": "DateTimeField",
            "published_date": "DateField",
        }

        for field_name in field_types:
            self.assertIn(
                field_name,
                detail_fields,
                f"Field {field_name} should be in detail fields",
            )
