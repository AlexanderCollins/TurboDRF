"""
Tests for get_turbodrf_schema_view parameters.

Tests that get_turbodrf_schema_view accepts custom parameters
for title, version, description, etc.
"""

from django.test import TestCase, override_settings

from turbodrf.documentation import get_turbodrf_schema_view


@override_settings(TURBODRF_ENABLE_DOCS=True)
class TestSchemaViewParameters(TestCase):
    """Test schema view parameter customization."""

    def test_get_turbodrf_schema_view_default_parameters(self):
        """Test schema view with default parameters."""
        schema_view = get_turbodrf_schema_view()

        self.assertIsNotNone(schema_view)

    def test_get_turbodrf_schema_view_custom_title(self):
        """Test schema view with custom title."""
        schema_view = get_turbodrf_schema_view(title="Custom API Title")

        self.assertIsNotNone(schema_view)
        # The title is stored in the schema_view's info
        # We can't easily access it without generating the schema,
        # but we can verify the function accepts the parameter

    def test_get_turbodrf_schema_view_custom_version(self):
        """Test schema view with custom version."""
        schema_view = get_turbodrf_schema_view(version="v2.0")

        self.assertIsNotNone(schema_view)

    def test_get_turbodrf_schema_view_custom_description(self):
        """Test schema view with custom description."""
        schema_view = get_turbodrf_schema_view(
            description="This is a custom API description"
        )

        self.assertIsNotNone(schema_view)

    def test_get_turbodrf_schema_view_custom_terms(self):
        """Test schema view with custom terms of service."""
        schema_view = get_turbodrf_schema_view(
            terms_of_service="https://myapi.com/terms/"
        )

        self.assertIsNotNone(schema_view)

    def test_get_turbodrf_schema_view_custom_contact_email(self):
        """Test schema view with custom contact email."""
        schema_view = get_turbodrf_schema_view(contact_email="support@myapi.com")

        self.assertIsNotNone(schema_view)

    def test_get_turbodrf_schema_view_custom_license(self):
        """Test schema view with custom license name."""
        schema_view = get_turbodrf_schema_view(license_name="Apache 2.0")

        self.assertIsNotNone(schema_view)

    def test_get_turbodrf_schema_view_all_custom_parameters(self):
        """Test schema view with all custom parameters."""
        schema_view = get_turbodrf_schema_view(
            title="My Custom API",
            version="v2.5",
            description="A comprehensive API for managing resources",
            terms_of_service="https://myapi.com/tos/",
            contact_email="api-support@mycompany.com",
            license_name="GPL v3",
        )

        self.assertIsNotNone(schema_view)

    @override_settings(TURBODRF_ENABLE_DOCS=False)
    def test_get_turbodrf_schema_view_disabled(self):
        """Test that schema view returns None when docs are disabled."""
        schema_view = get_turbodrf_schema_view()

        self.assertIsNone(schema_view)

    @override_settings(TURBODRF_ENABLE_DOCS=False)
    def test_get_turbodrf_schema_view_disabled_with_custom_params(self):
        """Test that schema view returns None even with custom params when disabled."""
        schema_view = get_turbodrf_schema_view(title="Custom Title", version="v2.0")

        self.assertIsNone(schema_view)

    def test_schema_view_function_signature(self):
        """Test that function has correct signature."""
        import inspect

        sig = inspect.signature(get_turbodrf_schema_view)
        params = list(sig.parameters.keys())

        # Should have these parameters
        expected_params = [
            "title",
            "version",
            "description",
            "terms_of_service",
            "contact_email",
            "license_name",
        ]

        for param in expected_params:
            self.assertIn(param, params)

    def test_schema_view_parameters_have_defaults(self):
        """Test that all parameters have default values."""
        import inspect

        sig = inspect.signature(get_turbodrf_schema_view)

        for param_name, param in sig.parameters.items():
            # All parameters should have defaults
            self.assertIsNot(param.default, inspect.Parameter.empty)

    def test_schema_view_default_title(self):
        """Test the default title value."""
        import inspect

        sig = inspect.signature(get_turbodrf_schema_view)
        title_default = sig.parameters["title"].default

        self.assertEqual(title_default, "TurboDRF API")

    def test_schema_view_default_version(self):
        """Test the default version value."""
        import inspect

        sig = inspect.signature(get_turbodrf_schema_view)
        version_default = sig.parameters["version"].default

        self.assertEqual(version_default, "v1")

    def test_schema_view_default_description(self):
        """Test the default description value."""
        import inspect

        sig = inspect.signature(get_turbodrf_schema_view)
        description_default = sig.parameters["description"].default

        self.assertIn("TurboDRF", description_default)
        self.assertIn("role-based", description_default.lower())


class TestSchemaViewIntegration(TestCase):
    """Integration tests for schema view configuration."""

    @override_settings(TURBODRF_ENABLE_DOCS=True)
    def test_schema_view_can_be_used_in_urls(self):
        """Test that schema view can be used to generate URL patterns."""
        schema_view = get_turbodrf_schema_view(title="Test API")

        # Schema view should be callable/usable
        self.assertIsNotNone(schema_view)
        self.assertTrue(hasattr(schema_view, "with_ui"))
        self.assertTrue(hasattr(schema_view, "without_ui"))

    @override_settings(TURBODRF_ENABLE_DOCS=True)
    def test_schema_view_with_ui_method(self):
        """Test that schema view has with_ui method for Swagger/ReDoc."""
        schema_view = get_turbodrf_schema_view()

        # Should have with_ui method
        self.assertTrue(callable(schema_view.with_ui))

        # Should be able to call with 'swagger' or 'redoc'
        swagger_view = schema_view.with_ui("swagger", cache_timeout=0)
        self.assertIsNotNone(swagger_view)

        redoc_view = schema_view.with_ui("redoc", cache_timeout=0)
        self.assertIsNotNone(redoc_view)

    @override_settings(TURBODRF_ENABLE_DOCS=True)
    def test_schema_view_without_ui_method(self):
        """Test that schema view has without_ui method for JSON/YAML."""
        schema_view = get_turbodrf_schema_view()

        # Should have without_ui method
        self.assertTrue(callable(schema_view.without_ui))

        # Should be able to get raw schema
        raw_view = schema_view.without_ui(cache_timeout=0)
        self.assertIsNotNone(raw_view)


class TestSchemaViewDocumentation(TestCase):
    """Test schema view function documentation."""

    def test_function_has_docstring(self):
        """Test that get_turbodrf_schema_view has docstring."""
        docstring = get_turbodrf_schema_view.__doc__

        self.assertIsNotNone(docstring)
        self.assertGreater(len(docstring), 0)

    def test_docstring_mentions_parameters(self):
        """Test that docstring documents the parameters."""
        docstring = get_turbodrf_schema_view.__doc__

        # Should mention the parameters
        self.assertIn("title", docstring)
        self.assertIn("version", docstring)
        self.assertIn("description", docstring)

    def test_docstring_has_usage_example(self):
        """Test that docstring includes usage example."""
        docstring = get_turbodrf_schema_view.__doc__

        # Should have usage section
        self.assertIn("Usage", docstring)

    def test_docstring_mentions_args(self):
        """Test that docstring has Args section."""
        docstring = get_turbodrf_schema_view.__doc__

        # Should document arguments
        self.assertIn("Args:", docstring)


class TestSchemaViewBackwardCompatibility(TestCase):
    """Test backward compatibility of schema view."""

    @override_settings(TURBODRF_ENABLE_DOCS=True)
    def test_can_call_without_parameters(self):
        """Test that function works when called without any parameters."""
        # Should work with no parameters (backward compatible)
        schema_view = get_turbodrf_schema_view()

        self.assertIsNotNone(schema_view)

    @override_settings(TURBODRF_ENABLE_DOCS=True)
    def test_positional_parameters_work(self):
        """Test that positional parameters work."""
        # Should work with positional args
        schema_view = get_turbodrf_schema_view("My API", "v2")

        self.assertIsNotNone(schema_view)

    @override_settings(TURBODRF_ENABLE_DOCS=True)
    def test_keyword_parameters_work(self):
        """Test that keyword parameters work."""
        # Should work with keyword args
        schema_view = get_turbodrf_schema_view(title="My API", version="v2")

        self.assertIsNotNone(schema_view)

    @override_settings(TURBODRF_ENABLE_DOCS=True)
    def test_mixed_positional_keyword_parameters(self):
        """Test that mixed positional and keyword parameters work."""
        # Should work with mixed args
        schema_view = get_turbodrf_schema_view(
            "My API", "v2", description="Custom description"
        )

        self.assertIsNotNone(schema_view)
