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


class TestTrackingMixinOrdering(TestCase):
    """Test that tracking mixin is added in correct order."""

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_tracking_mixin_comes_before_model_viewset(self):
        """Test that tracking mixin is added before ModelViewSet in MRO."""
        bases = get_viewset_base_classes()
        if len(bases) > 1:
            # If tracking is installed AND enabled, mixin should come first
            self.assertEqual(bases[-1], viewsets.ModelViewSet)


class _FakeLoggingMixin:
    """Stand-in for rest_framework_tracking.mixins.LoggingMixin (not
    installed in the test env). ``_clean_data`` mirrors drf-api-tracking's
    masking: built-in key names plus the view's ``sensitive_fields``."""

    sensitive_fields = {}
    CLEANED_SUBSTITUTE = "********************"
    parent_should_log = True

    def should_log(self, request, response):
        return self.parent_should_log

    def _clean_data(self, data):
        sensitive = {"api", "token", "key", "secret", "password", "signature"}
        if self.sensitive_fields:
            sensitive |= {field.lower() for field in self.sensitive_fields}
        return {
            k: (self.CLEANED_SUBSTITUTE if k.lower() in sensitive else v)
            for k, v in data.items()
        }


class TestTurboDRFTrackingMixin(TestCase):
    """TURBODRF_TRACKING_ANONYMOUS and sensitive-field masking."""

    def _mixin(self):
        from turbodrf.tracking import build_tracking_mixin

        return build_tracking_mixin(_FakeLoggingMixin)()

    def test_get_tracking_mixin_wraps_logging_mixin(self):
        import sys
        from unittest.mock import MagicMock, patch

        mixins_module = MagicMock(LoggingMixin=_FakeLoggingMixin)
        fake_modules = {
            "rest_framework_tracking": MagicMock(),
            "rest_framework_tracking.mixins": mixins_module,
        }
        with override_settings(TURBODRF_ENABLE_TRACKING=True):
            with patch.dict(sys.modules, fake_modules):
                mixin = get_tracking_mixin()
        self.assertTrue(issubclass(mixin, _FakeLoggingMixin))
        self.assertIs(mixin, self._mixin().__class__)

    def test_turbodrf_sensitive_fields_are_masked(self):
        cleaned = self._mixin()._clean_data(
            {
                "api_key": "k-123",
                "access_token": "t-123",
                "refresh_token": "r-123",
                "password_hash": "h",
                "session_key": "s",
                "title": "visible",
            }
        )
        self.assertEqual(cleaned["title"], "visible")
        for key in (
            "api_key",
            "access_token",
            "refresh_token",
            "password_hash",
            "session_key",
        ):
            self.assertEqual(cleaned[key], _FakeLoggingMixin.CLEANED_SUBSTITUTE, key)

    @override_settings(TURBODRF_SENSITIVE_FIELDS=["ssn"])
    def test_sensitive_fields_follow_settings(self):
        self.assertIn("ssn", self._mixin().sensitive_fields)
        self.assertEqual(
            self._mixin()._clean_data({"ssn": "1"})["ssn"],
            _FakeLoggingMixin.CLEANED_SUBSTITUTE,
        )

    def test_anonymous_requests_not_logged_by_default(self):
        from unittest.mock import Mock

        from django.contrib.auth.models import AnonymousUser

        request = Mock(user=AnonymousUser())
        self.assertFalse(self._mixin().should_log(request, Mock()))

    @override_settings(TURBODRF_TRACKING_ANONYMOUS=True)
    def test_anonymous_requests_logged_when_enabled(self):
        from unittest.mock import Mock

        from django.contrib.auth.models import AnonymousUser

        request = Mock(user=AnonymousUser())
        self.assertTrue(self._mixin().should_log(request, Mock()))

    def test_authenticated_requests_logged_and_parent_rule_kept(self):
        from unittest.mock import Mock

        request = Mock(user=Mock(is_authenticated=True))
        mixin = self._mixin()
        self.assertTrue(mixin.should_log(request, Mock()))
        mixin.parent_should_log = False
        self.assertFalse(mixin.should_log(request, Mock()))


# Note: removed 5 docstring-presence tests + 4 "doesn't raise" placeholders
# from earlier versions of this file. They asserted nothing functional
# (e.g. `self.assertIsNotNone(fn.__doc__)`). The remaining tests in this
# file actually exercise behavior.
