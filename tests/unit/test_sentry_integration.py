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

    def test_only_user_id_is_sent_never_username(self):
        ctx, mock_sdk = self._patch_sdk()
        with ctx:
            user = User.objects.create_user(username="pii_name", password="x")
            user._test_roles = ["viewer"]
            request = MagicMock()
            request.user = user
            set_request_context(request)

            mock_sdk.set_user.assert_called_once_with({"id": user.pk})
            self.assertNotIn("pii_name", str(mock_sdk.mock_calls))

    def test_anon_user_clears_user_and_tags(self):
        """An anonymous request must not keep the previous request's user and
        tags on a shared scope."""
        ctx, mock_sdk = self._patch_sdk()
        with ctx:
            request = MagicMock()
            request.user = AnonymousUser()
            set_request_context(request)
            mock_sdk.set_user.assert_called_once_with(None)
            scope = mock_sdk.get_current_scope.return_value
            scope.remove_tag.assert_any_call("tenant_id")
            scope.remove_tag.assert_any_call("user_roles")
            scope.set_tag.assert_not_called()

    def test_user_without_roles_clears_stale_roles_tag(self):
        ctx, mock_sdk = self._patch_sdk()
        with ctx:
            user = User.objects.create_user(username="no_roles", password="x")
            user._test_roles = []
            request = MagicMock()
            request.user = user
            set_request_context(request)
            scope = mock_sdk.get_current_scope.return_value
            scope.remove_tag.assert_any_call("user_roles")

    def test_middleware_tags_a_request_local_scope(self):
        ctx, mock_sdk = self._patch_sdk()
        request_scope = MagicMock()
        mock_sdk.new_scope.return_value.__enter__ = MagicMock(
            return_value=request_scope
        )
        mock_sdk.new_scope.return_value.__exit__ = MagicMock(return_value=False)
        with ctx:
            user = User.objects.create_user(username="mw_user", password="x")
            user._test_roles = ["admin"]
            request = MagicMock()
            request.user = user
            get_response = MagicMock(return_value="resp")

            self.assertEqual(SentryContextMiddleware(get_response)(request), "resp")

            # Nothing is resolved or written eagerly: the event processor
            # describes the user when (if) an event is captured.
            request_scope.set_user.assert_not_called()
            request_scope.set_tag.assert_not_called()
            request_scope.add_event_processor.assert_called_once()
            # Nothing written to the process-wide API.
            mock_sdk.set_user.assert_not_called()
            mock_sdk.new_scope.return_value.__exit__.assert_called_once()

            processor = request_scope.add_event_processor.call_args[0][0]
            event = processor({"user": {"username": "mw_user"}}, {})
            self.assertEqual(event["user"], {"id": user.pk})
            self.assertEqual(event["tags"]["user_roles"], "admin")

    def test_event_processor_sees_user_authenticated_after_middleware(self):
        """Token/JWT users are authenticated by DRF after the middleware ran;
        events captured later still carry their id and tags."""
        ctx, mock_sdk = self._patch_sdk()
        request_scope = MagicMock()
        mock_sdk.new_scope.return_value.__enter__ = MagicMock(
            return_value=request_scope
        )
        mock_sdk.new_scope.return_value.__exit__ = MagicMock(return_value=False)
        with ctx:
            user = User.objects.create_user(username="late_user", password="x")
            user._test_roles = ["editor"]
            request = MagicMock()
            request.user = AnonymousUser()

            def view(req):
                req.user = user  # what DRF does after authenticating
                return "resp"

            SentryContextMiddleware(view)(request)
            request_scope.set_user.assert_not_called()

            processor = request_scope.add_event_processor.call_args[0][0]
            event = processor({"tags": {}}, {})
            self.assertEqual(event["user"], {"id": user.pk})
            self.assertEqual(event["tags"]["user_roles"], "editor")

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
