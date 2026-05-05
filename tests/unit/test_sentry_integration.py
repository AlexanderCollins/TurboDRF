"""Sentry integration unit tests.

Verifies the no-op behavior when Sentry isn't installed/enabled, plus the
basic shape when it is. Doesn't run against a real Sentry server — that's
the user's job to configure.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, override_settings

from turbodrf.integrations.sentry import (
    SentryContextMiddleware,
    capture_security_message,
    report_security_event,
    set_request_context,
)

User = get_user_model()


class TestSentryNoOpWhenDisabled(TestCase):
    """Default behavior: TURBODRF_ENABLE_SENTRY=False → all functions no-op."""

    def test_set_request_context_noop(self):
        request = MagicMock()
        # No assertion needed — just verifying it doesn't crash
        set_request_context(request)

    def test_report_security_event_noop(self):
        report_security_event("fake_event", "msg", user_id=1)

    def test_capture_security_message_noop(self):
        capture_security_message("msg", user_id=1)

    def test_middleware_passes_through(self):
        get_response = MagicMock(return_value="resp")
        mw = SentryContextMiddleware(get_response)
        request = MagicMock()
        request.user = AnonymousUser()
        result = mw(request)
        self.assertEqual(result, "resp")
        get_response.assert_called_once_with(request)


@override_settings(TURBODRF_ENABLE_SENTRY=True)
class TestSentryWhenEnabled(TestCase):
    """Verify integration attempts to call sentry_sdk when enabled."""

    def _patch_sdk(self):
        """Mock sentry_sdk for the duration of the test."""
        mock_sdk = MagicMock()
        mock_scope = MagicMock()
        mock_sdk.get_current_scope.return_value = mock_scope
        # context manager for push_scope
        mock_sdk.push_scope.return_value.__enter__ = MagicMock(return_value=mock_scope)
        mock_sdk.push_scope.return_value.__exit__ = MagicMock(return_value=False)
        return patch.dict("sys.modules", {"sentry_sdk": mock_sdk}), mock_sdk

    def test_set_request_context_tags_user(self):
        ctx, mock_sdk = self._patch_sdk()
        with ctx:
            user = User.objects.create_user(username="sentry_u", password="x")
            user._test_roles = ["admin"]
            request = MagicMock()
            request.user = user
            set_request_context(request)

            mock_sdk.set_user.assert_called_once()
            call = mock_sdk.set_user.call_args[0][0]
            self.assertEqual(call["id"], user.pk)

    def test_report_security_event_creates_breadcrumb(self):
        ctx, mock_sdk = self._patch_sdk()
        with ctx:
            report_security_event("fk_injection_rejected", "Test", model="Deal")
            mock_sdk.add_breadcrumb.assert_called_once()
            kwargs = mock_sdk.add_breadcrumb.call_args.kwargs
            self.assertEqual(kwargs["category"], "turbodrf.security")
            self.assertEqual(kwargs["data"]["event_type"], "fk_injection_rejected")
            self.assertEqual(kwargs["data"]["model"], "Deal")

    def test_capture_security_message_sends_message(self):
        ctx, mock_sdk = self._patch_sdk()
        with ctx:
            capture_security_message("Suspicious activity", user_id=42)
            mock_sdk.capture_message.assert_called_once()

    def test_anon_user_skips_set_request_context(self):
        ctx, mock_sdk = self._patch_sdk()
        with ctx:
            request = MagicMock()
            request.user = AnonymousUser()
            set_request_context(request)
            mock_sdk.set_user.assert_not_called()

    def test_sentry_failure_does_not_break_request(self):
        ctx, mock_sdk = self._patch_sdk()
        mock_sdk.set_user.side_effect = RuntimeError("Sentry down")
        with ctx:
            user = User.objects.create_user(username="boom", password="x")
            user._test_roles = ["admin"]
            request = MagicMock()
            request.user = user
            # Should not raise
            set_request_context(request)
