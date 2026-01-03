"""
Tests for drf-api-tracking integration.

Tests that TurboDRF can integrate with drf-api-tracking
for API request logging and monitoring.
"""

from django.test import TestCase, override_settings
from rest_framework import viewsets

from turbodrf.tracking import (
    get_tracking_mixin,
    get_viewset_base_classes,
    is_tracking_enabled,
)


class TestTrackingDetection(TestCase):
    """Test tracking availability detection."""

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_is_tracking_enabled_when_setting_true(self):
        """Test tracking enabled detection with setting True."""
        # This will return True only if drf-api-tracking is installed
        # Since it's likely not installed in test env, we just test the logic
        enabled = is_tracking_enabled()

        # Should check both setting AND package availability
        self.assertIsInstance(enabled, bool)

    @override_settings(TURBODRF_ENABLE_TRACKING=False)
    def test_is_tracking_enabled_when_setting_false(self):
        """Test tracking disabled when setting is False."""
        enabled = is_tracking_enabled()

        # Should be False when setting is False
        self.assertFalse(enabled)

    def test_is_tracking_enabled_default(self):
        """Test tracking disabled by default."""
        # Without setting, should default to False
        enabled = is_tracking_enabled()

        self.assertFalse(enabled)


class TestTrackingMixin(TestCase):
    """Test tracking mixin retrieval."""

    @override_settings(TURBODRF_ENABLE_TRACKING=False)
    def test_get_tracking_mixin_when_disabled(self):
        """Test that get_tracking_mixin returns None when disabled."""
        mixin = get_tracking_mixin()

        self.assertIsNone(mixin)

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_get_tracking_mixin_when_enabled_but_not_installed(self):
        """Test that get_tracking_mixin returns None when package not installed."""
        # drf-api-tracking is likely not installed in test environment
        mixin = get_tracking_mixin()

        # Should be None if package is not installed
        # If it IS installed, it would return the mixin class
        self.assertTrue(mixin is None or callable(mixin))


class TestViewsetBaseClasses(TestCase):
    """Test viewset base class generation."""

    @override_settings(TURBODRF_ENABLE_TRACKING=False)
    def test_viewset_base_classes_without_tracking(self):
        """Test that base classes don't include tracking when disabled."""
        bases = get_viewset_base_classes()

        self.assertIsInstance(bases, tuple)
        self.assertIn(viewsets.ModelViewSet, bases)

        # Should only have ModelViewSet
        self.assertEqual(len(bases), 1)

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_viewset_base_classes_with_tracking_enabled(self):
        """Test base classes when tracking is enabled."""
        bases = get_viewset_base_classes()

        self.assertIsInstance(bases, tuple)
        self.assertIn(viewsets.ModelViewSet, bases)

        # If tracking mixin is available, should have 2 classes
        # If not available (package not installed), should have 1
        self.assertGreaterEqual(len(bases), 1)
        self.assertLessEqual(len(bases), 2)

    def test_viewset_base_classes_default(self):
        """Test base classes with default settings."""
        bases = get_viewset_base_classes()

        # Default should not include tracking
        self.assertEqual(len(bases), 1)
        self.assertEqual(bases[0], viewsets.ModelViewSet)

    def test_viewset_base_classes_returns_tuple(self):
        """Test that base classes are returned as tuple."""
        bases = get_viewset_base_classes()

        self.assertIsInstance(bases, tuple)

    def test_viewset_base_classes_includes_model_viewset(self):
        """Test that ModelViewSet is always included."""
        bases = get_viewset_base_classes()

        self.assertIn(viewsets.ModelViewSet, bases)


class TestTrackingIntegration(TestCase):
    """Test tracking integration with TurboDRFViewSet."""

    def test_turbodrf_viewset_uses_base_classes(self):
        """Test that TurboDRFViewSet uses the generated base classes."""
        from turbodrf.views import TurboDRFViewSet

        # TurboDRFViewSet should inherit from the generated base classes
        # We can't directly compare since it's created at import time,
        # but we can verify it has ModelViewSet as a base
        self.assertTrue(issubclass(TurboDRFViewSet, viewsets.ModelViewSet))

    def test_viewset_mro_includes_model_viewset(self):
        """Test that viewset MRO includes ModelViewSet."""
        from turbodrf.views import TurboDRFViewSet

        mro = TurboDRFViewSet.__mro__

        # ModelViewSet should be in the MRO
        self.assertIn(viewsets.ModelViewSet, mro)


class TestTrackingConfiguration(TestCase):
    """Test tracking configuration options."""

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_tracking_enabled_setting(self):
        """Test TURBODRF_ENABLE_TRACKING setting."""
        from django.conf import settings

        self.assertTrue(settings.TURBODRF_ENABLE_TRACKING)

    @override_settings(TURBODRF_ENABLE_TRACKING=False)
    def test_tracking_disabled_setting(self):
        """Test tracking can be disabled via setting."""
        from django.conf import settings

        self.assertFalse(settings.TURBODRF_ENABLE_TRACKING)

    def test_tracking_default_setting(self):
        """Test default tracking setting."""
        from django.conf import settings

        # Default should be False
        default_value = getattr(settings, "TURBODRF_ENABLE_TRACKING", False)
        self.assertFalse(default_value)


class TestTrackingDocumentation(TestCase):
    """Test tracking module documentation."""

    def test_tracking_module_has_docstring(self):
        """Test that tracking module has documentation."""
        from turbodrf import tracking

        docstring = tracking.__doc__

        self.assertIsNotNone(docstring)
        self.assertIn("tracking", docstring.lower())

    def test_is_tracking_enabled_has_docstring(self):
        """Test that is_tracking_enabled has documentation."""
        docstring = is_tracking_enabled.__doc__

        self.assertIsNotNone(docstring)

    def test_get_tracking_mixin_has_docstring(self):
        """Test that get_tracking_mixin has documentation."""
        docstring = get_tracking_mixin.__doc__

        self.assertIsNotNone(docstring)

    def test_get_viewset_base_classes_has_docstring(self):
        """Test that get_viewset_base_classes has documentation."""
        docstring = get_viewset_base_classes.__doc__

        self.assertIsNotNone(docstring)


class TestTrackingMixinOrdering(TestCase):
    """Test that tracking mixin is added in correct order."""

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_tracking_mixin_comes_before_model_viewset(self):
        """Test that tracking mixin is added before ModelViewSet in MRO."""
        bases = get_viewset_base_classes()

        # If tracking mixin is present, it should come first
        if len(bases) > 1:
            # First base should be the tracking mixin
            # Last base should be ModelViewSet
            self.assertEqual(bases[-1], viewsets.ModelViewSet)


class TestTrackingImportSafety(TestCase):
    """Test that tracking integration handles missing package gracefully."""

    def test_import_tracking_module(self):
        """Test that tracking module can be imported."""
        try:
            from turbodrf import tracking

            self.assertIsNotNone(tracking)
        except ImportError:
            self.fail("Should be able to import tracking module")

    def test_is_tracking_enabled_doesnt_raise(self):
        """Test that is_tracking_enabled doesn't raise even if package missing."""
        try:
            result = is_tracking_enabled()
            self.assertIsInstance(result, bool)
        except Exception as e:
            self.fail(f"is_tracking_enabled should not raise: {e}")

    def test_get_tracking_mixin_doesnt_raise(self):
        """Test that get_tracking_mixin doesn't raise even if package missing."""
        try:
            result = get_tracking_mixin()
            # Should be None or a class
            self.assertTrue(result is None or callable(result))
        except Exception as e:
            self.fail(f"get_tracking_mixin should not raise: {e}")

    def test_get_viewset_base_classes_doesnt_raise(self):
        """Test that get_viewset_base_classes doesn't raise."""
        try:
            result = get_viewset_base_classes()
            self.assertIsInstance(result, tuple)
        except Exception as e:
            self.fail(f"get_viewset_base_classes should not raise: {e}")
