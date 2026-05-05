"""
Unit tests for TurboDRFTenancyMiddleware that work on any backend.

The end-to-end RLS tests live in tests/integration/test_rls.py and require
Postgres. These unit tests verify the middleware's logic with mocks so they
run on every backend including SQLite.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, override_settings

from turbodrf.rls import TurboDRFTenancyMiddleware

User = get_user_model()


class TestRLSMiddlewareNonPostgres(TestCase):
    """On non-Postgres, middleware is a no-op."""

    def test_noop_on_sqlite(self):
        """The default SQLite test backend returns False from _is_postgres."""
        get_response = MagicMock(return_value="response")
        mw = TurboDRFTenancyMiddleware(get_response)
        request = MagicMock()
        request.user = AnonymousUser()

        result = mw(request)
        self.assertEqual(result, "response")
        get_response.assert_called_once_with(request)


class TestRLSMiddlewareOnPostgres(TestCase):
    """Behavior when connection.vendor == 'postgresql'.

    We mock the connection so the test doesn't actually need Postgres
    running. The end-to-end test in tests/integration/test_rls.py covers
    real Postgres behavior."""

    def setUp(self):
        self.get_response = MagicMock(return_value="response")
        self.mw = TurboDRFTenancyMiddleware(self.get_response)

    def _patch_pg(self, cursor_mock):
        """Helper: patch django.db.connection (imported lazily inside the
        middleware) to look like Postgres with a mock cursor."""
        ctx_mgr = MagicMock()
        ctx_mgr.__enter__ = MagicMock(return_value=cursor_mock)
        ctx_mgr.__exit__ = MagicMock(return_value=False)

        mock_connection = MagicMock(vendor="postgresql")
        mock_connection.cursor = MagicMock(return_value=ctx_mgr)
        return patch("django.db.connection", mock_connection)

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_anonymous_user_skips_var_setting(self):
        cursor = MagicMock()
        request = MagicMock()
        request.user = AnonymousUser()

        with self._patch_pg(cursor):
            self.mw(request)

        cursor.execute.assert_not_called()

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_authenticated_user_sets_three_session_vars(self):
        cursor = MagicMock()
        user = User.objects.create_user(username="rls_u", password="x")
        user._test_roles = ["editor"]

        request = MagicMock()
        request.user = user

        with self._patch_pg(cursor):
            self.mw(request)

        # Should have called set_config three times: user_id, tenant_id, roles
        self.assertEqual(cursor.execute.call_count, 3)
        sqls = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("app.user_id" in sql for sql in sqls))
        self.assertTrue(any("app.tenant_id" in sql for sql in sqls))
        self.assertTrue(any("app.user_roles" in sql for sql in sqls))

        # user_id is the user's pk
        user_id_call = next(
            c for c in cursor.execute.call_args_list if "app.user_id" in c.args[0]
        )
        self.assertEqual(user_id_call.args[1], [str(user.pk)])

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_user_with_tenant_attribute(self):
        from tests.test_app.apps import set_test_brokerage
        from tests.test_app.models import Brokerage

        cursor = MagicMock()
        brokerage = Brokerage.objects.create(name="test_pg")
        user = User.objects.create_user(username="rls_u2", password="x")
        user._test_roles = ["admin"]
        set_test_brokerage(user, brokerage)

        request = MagicMock()
        request.user = user

        with self._patch_pg(cursor):
            self.mw(request)

        # tenant_id should be the brokerage's pk
        tenant_call = next(
            c for c in cursor.execute.call_args_list if "app.tenant_id" in c.args[0]
        )
        self.assertEqual(tenant_call.args[1], [str(brokerage.pk)])

    @override_settings(TURBODRF_TENANT_USER_FIELD=None)
    def test_user_with_no_tenant_field_setting_sets_empty_tenant(self):
        cursor = MagicMock()
        user = User.objects.create_user(username="rls_u3", password="x")
        user._test_roles = ["viewer"]

        request = MagicMock()
        request.user = user

        with self._patch_pg(cursor):
            self.mw(request)

        # tenant_id is empty string when user has no tenant
        tenant_call = next(
            c for c in cursor.execute.call_args_list if "app.tenant_id" in c.args[0]
        )
        self.assertEqual(tenant_call.args[1], [""])

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_cursor_failure_logged_does_not_break_request(self):
        """If session-var setting fails, the request continues and the
        failure is logged (rather than raising)."""
        cursor = MagicMock()
        cursor.execute.side_effect = RuntimeError("connection lost")

        user = User.objects.create_user(username="rls_u4", password="x")
        user._test_roles = ["viewer"]
        request = MagicMock()
        request.user = user

        with self._patch_pg(cursor):
            with self.assertLogs("turbodrf.rls.middleware", level="WARNING") as cm:
                result = self.mw(request)

        self.assertEqual(result, "response")
        self.assertTrue(any("Failed to set RLS session vars" in m for m in cm.output))

    def test_no_user_attribute_skips(self):
        """If request has no .user attribute at all, middleware doesn't crash."""
        cursor = MagicMock()
        request = MagicMock(spec=["session"])  # no `user` attribute

        with self._patch_pg(cursor):
            self.mw(request)
        # get_response still called, no execute attempts
        cursor.execute.assert_not_called()
