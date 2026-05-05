"""Tests for `get_turbodrf_schema_view` configuration.

Trimmed: previously had 26 `assertIsNotNone()` presence-checks and 6
docstring-introspection tests that verified zero behavior. Replaced with
focused tests that actually exercise the configuration path.
"""

from django.test import TestCase, override_settings

from turbodrf.documentation import get_turbodrf_schema_view


@override_settings(TURBODRF_ENABLE_DOCS=True)
class TestSchemaViewParameters(TestCase):
    """Verify get_turbodrf_schema_view passes custom params through to the
    underlying drf-yasg schema_view."""

    def test_default_returns_schema_view(self):
        sv = get_turbodrf_schema_view()
        self.assertIsNotNone(sv)
        # drf-yasg's schema_view exposes with_ui / without_ui / as_view
        self.assertTrue(callable(getattr(sv, "with_ui", None)))
        self.assertTrue(callable(getattr(sv, "without_ui", None)))

    def test_custom_params_accepted_without_error(self):
        """drf-yasg returns a configured view class. We can't easily
        introspect title/version through the public interface (drf-yasg
        bakes them into the renderer at request time), but the accepted
        kwargs should round-trip without error and the returned view
        should be usable."""
        sv = get_turbodrf_schema_view(
            title="Custom",
            version="v9.9",
            description="Custom description",
            license_name="Apache 2.0",
        )
        self.assertIsNotNone(sv)
        # The configured view should still be usable
        self.assertTrue(callable(getattr(sv, "as_view", None)))


@override_settings(TURBODRF_ENABLE_DOCS=False)
class TestSchemaViewDisabled(TestCase):
    """When TURBODRF_ENABLE_DOCS is False, schema view returns None."""

    def test_returns_none_when_disabled(self):
        self.assertIsNone(get_turbodrf_schema_view())
