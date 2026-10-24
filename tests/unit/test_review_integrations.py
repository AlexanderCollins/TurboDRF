"""
Regression tests for the optional integrations (tracking, allauth, Keycloak,
Sentry). Each test failed before its fix:

- Tracking: ``TURBODRF_SENSITIVE_FIELDS = ['ssn']`` replaced the default
  deny-list, so ``api_key`` / ``refresh_token`` were logged in clear; a
  request rejected before DRF parsed it (403, malformed JSON) was logged with
  its raw body unmasked; and tracking turned on whenever the package was
  importable, though its mixins raise unless the app is installed.
- allauth / Keycloak middleware wrote roles into ``request.user.__dict__``,
  so a session request got different roles than a token request for the
  same user, and queried on every session request.
- Keycloak: ``auth_time`` of ``nan`` / ``inf`` passed the max-age check, and
  a non-string role in the claim raised TypeError (a 500 for that user).
- allauth role helpers ignored the mapping and strict mode.
- Sentry middleware ran user / role / tenant queries on every request, and
  ``capture_security_message`` used the deprecated ``push_scope``.
- Importing any ``turbodrf.integrations.*`` module imported the allauth
  helpers and Django's auth models.
"""

import json
import os
import subprocess
import sys
import time
import types
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.functional import SimpleLazyObject
from rest_framework.test import APIRequestFactory, force_authenticate

from tests.test_app.models import RelatedModel, SampleModel

User = get_user_model()

CLEANED = "********************"


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------


class _TrackingBase:
    """Stand-in for drf-api-tracking 1.8.4's BaseLoggingMixin (not installed
    in the test env), reproducing its log flow: the raw ``request.body`` is
    stored before DRF authenticates and parses the request, replaced by the
    masked parsed data only once ``initial()`` succeeds, and handed to
    ``handle_log`` in ``finalize_response``."""

    CLEANED_SUBSTITUTE = CLEANED
    sensitive_fields = {}
    logs = []

    def initial(self, request, *args, **kwargs):
        self.log = {"data": self._clean_data(request.body)}
        super().initial(request, *args, **kwargs)
        try:
            data = self.request.data.dict()
        except AttributeError:
            data = self.request.data
        self.log["data"] = self._clean_data(data)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if self.should_log(request, response):
            query = self._clean_data(request.query_params.dict())
            self.log["query_params"] = query if query != {} else self.log["data"]
            self.handle_log()
        return response

    def should_log(self, request, response):
        return True

    def handle_log(self):
        type(self).logs.append(dict(self.log))

    def _clean_data(self, data):
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        if isinstance(data, list):
            return [self._clean_data(d) for d in data]
        if isinstance(data, dict):
            sensitive = {"api", "token", "key", "secret", "password", "signature"}
            sensitive |= {f.lower() for f in self.sensitive_fields}
            data = dict(data)
            for key, value in data.items():
                if isinstance(value, (list, dict)):
                    data[key] = self._clean_data(value)
                if key.lower() in sensitive:
                    data[key] = self.CLEANED_SUBSTITUTE
        return data


SECRETS = {"api_key": "AK-SECRET", "refresh_token": "RT-SECRET"}


class TestTrackingMasking(TestCase):
    def setUp(self):
        from turbodrf.tracking import build_tracking_mixin

        cache.clear()
        _TrackingBase.logs = []
        import tests.urls

        viewset = next(
            v for _, v, _ in tests.urls.router.registry if v.model is SampleModel
        )
        tracked = type(
            "TrackedViewSet", (build_tracking_mixin(_TrackingBase), viewset), {}
        )
        self.view = tracked.as_view({"post": "create"})
        self.related = RelatedModel.objects.create(name="r")

    def _post(self, roles, body):
        user = User.objects.create_user(f"track_{len(_TrackingBase.logs)}_{roles}")
        user._test_roles = roles
        request = APIRequestFactory().post(
            "/api/samplemodels/", body, content_type="application/json"
        )
        force_authenticate(request, user=user)
        response = self.view(request)
        return response.status_code, _TrackingBase.logs[-1]

    def _assert_masked(self, log):
        blob = json.dumps(log, default=str)
        for secret in SECRETS.values():
            self.assertNotIn(secret, blob)

    def test_rejected_request_body_is_masked(self):
        body = json.dumps({"title": "t", "price": "1", **SECRETS})
        status, log = self._post(["viewer"], body)  # viewer can't create
        self.assertEqual(status, 403)
        self._assert_masked(log)
        self.assertEqual(json.loads(log["data"])["title"], "t")

    def test_malformed_body_is_masked(self):
        status, log = self._post(["admin"], '{"api_key": "AK-SECRET", ')
        self.assertEqual(status, 400)
        self._assert_masked(log)

    def test_body_without_sensitive_names_is_kept(self):
        status, log = self._post(["viewer"], '{"title": "t"')
        self.assertEqual(log["data"], '{"title": "t"')

    @override_settings(TURBODRF_SENSITIVE_FIELDS=["ssn"])
    def test_custom_setting_adds_to_the_defaults(self):
        from turbodrf.tracking import get_sensitive_field_names

        names = get_sensitive_field_names()
        self.assertTrue({"ssn", "api_key", "refresh_token", "password_hash"} <= names)
        body = json.dumps({"title": "t", "price": "1", **SECRETS, "ssn": "123"})
        status, log = self._post(["viewer"], body)
        self._assert_masked(log)
        self.assertNotIn('"123"', log["data"])

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_not_enabled_unless_the_app_is_installed(self):
        from turbodrf.tracking import get_viewset_base_classes, is_tracking_enabled

        with mock.patch.dict(sys.modules, {"rest_framework_tracking": mock.Mock()}):
            with self.assertLogs("turbodrf.tracking", "WARNING"):
                self.assertFalse(is_tracking_enabled())
            self.assertEqual(len(get_viewset_base_classes()), 1)


# ---------------------------------------------------------------------------
# allauth / Keycloak middleware
# ---------------------------------------------------------------------------


class TestRoleMiddlewaresAreNoOps(TestCase):
    def _session_request(self, user):
        request = RequestFactory().get("/healthz")
        request.user = SimpleLazyObject(lambda: user)  # as AuthenticationMiddleware
        return request

    @override_settings(TURBODRF_ALLAUTH_INTEGRATION=True)
    def test_allauth_session_roles_match_token_roles(self):
        from turbodrf.backends import get_user_roles
        from turbodrf.integrations.allauth import AllAuthRoleMiddleware

        user = User.objects.create_user("mw_allauth")
        Group.objects.create(name="admin").user_set.add(user)
        token_roles = get_user_roles(User.objects.get(pk=user.pk))
        fresh = User.objects.get(pk=user.pk)
        seen = {}

        def view(request):
            seen["roles"] = get_user_roles(request.user)
            return "ok"

        with self.assertWarns(DeprecationWarning):
            middleware = AllAuthRoleMiddleware(view)
        request = self._session_request(fresh)
        with CaptureQueriesContext(connection) as queries:
            middleware(request)
        self.assertEqual(seen["roles"], token_roles)
        self.assertEqual(token_roles, ["viewer"])  # the model's own roles
        self.assertNotIn("roles", fresh.__dict__)
        self.assertEqual(len(queries.captured_queries), 0)

    @override_settings(TURBODRF_KEYCLOAK_INTEGRATION=True)
    def test_keycloak_middleware_writes_and_queries_nothing(self):
        from turbodrf.integrations.keycloak import KeycloakRoleMiddleware

        user = User.objects.create_user("mw_keycloak")
        user._test_roles = []
        user.social_auth = mock.MagicMock()
        with self.assertWarns(DeprecationWarning):
            middleware = KeycloakRoleMiddleware(lambda request: "ok")
        self.assertEqual(middleware(self._session_request(user)), "ok")
        user.social_auth.filter.assert_not_called()
        self.assertNotIn("roles", user.__dict__)


# ---------------------------------------------------------------------------
# Keycloak hardening
# ---------------------------------------------------------------------------


class TestKeycloakHardening(TestCase):
    @override_settings(TURBODRF_KEYCLOAK_MAX_ROLE_AGE=60)
    def test_non_finite_auth_time_fails_closed(self):
        from turbodrf.integrations.keycloak import _roles_too_old

        for value in ("nan", "inf", "-inf", float("nan"), float("inf")):
            self.assertTrue(_roles_too_old({"auth_time": value}), value)
        self.assertFalse(_roles_too_old({"auth_time": time.time()}))

    @override_settings(TURBODRF_KEYCLOAK_ROLE_MAPPING={"a": "admin"})
    def test_non_string_roles_are_ignored(self):
        from turbodrf.integrations.keycloak import (
            extract_roles_from_token,
            map_keycloak_roles_to_turbodrf,
        )

        self.assertEqual(map_keycloak_roles_to_turbodrf(["a", {"x": 1}, 3]), ["admin"])
        with override_settings(TURBODRF_KEYCLOAK_ROLE_MAPPING={}):
            self.assertEqual(
                extract_roles_from_token({"roles": ["a", {"x": 1}, None]}), ["a"]
            )


# ---------------------------------------------------------------------------
# allauth role helpers
# ---------------------------------------------------------------------------


@override_settings(
    TURBODRF_ALLAUTH_INTEGRATION=True,
    TURBODRF_ALLAUTH_ROLE_MAPPING={"Administrators": "admin"},
)
class TestAllauthRoleHelpersFollowTheMapping(TestCase):
    def setUp(self):
        self.mapped = User.objects.create_user("mapped_admin")
        Group.objects.create(name="Administrators").user_set.add(self.mapped)
        self.plain = User.objects.create_user("plain_admin_group")
        Group.objects.create(name="admin").user_set.add(self.plain)

    def test_get_users_with_role(self):
        from turbodrf.integrations.allauth_roles import get_users_with_role

        self.assertEqual(list(get_users_with_role("admin")), [self.mapped])

    def test_sync_groups_to_roles(self):
        from turbodrf.integrations.allauth_roles import sync_groups_to_roles

        self.assertEqual(sync_groups_to_roles(self.mapped), ["admin"])
        self.assertEqual(sync_groups_to_roles(self.plain), [])

    def test_assign_roles_to_user(self):
        from turbodrf.integrations.allauth import get_user_roles_from_groups
        from turbodrf.integrations.allauth_roles import assign_roles_to_user

        user = User.objects.create_user("assigned")
        assign_roles_to_user(user, ["admin"])
        self.assertEqual(get_user_roles_from_groups(user), ["admin"])
        self.assertEqual(
            list(user.groups.values_list("name", flat=True)), ["Administrators"]
        )

    def test_unmappable_role_raises_before_touching_groups(self):
        from turbodrf.integrations.allauth_roles import assign_roles_to_user

        with self.assertRaises(ValueError):
            assign_roles_to_user(self.mapped, ["admin", "editor"])
        self.assertEqual(
            list(self.mapped.groups.values_list("name", flat=True)),
            ["Administrators"],
        )

    @override_settings(TURBODRF_ALLAUTH_STRICT_ROLES=False)
    def test_passthrough_mode(self):
        from turbodrf.integrations.allauth_roles import get_users_with_role

        editor = User.objects.create_user("editor_group")
        Group.objects.create(name="editor").user_set.add(editor)
        self.assertEqual(list(get_users_with_role("editor")), [editor])
        # 'admin' is unmapped too, so the plain 'admin' group now passes.
        self.assertEqual(set(get_users_with_role("admin")), {self.mapped, self.plain})


# ---------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------


def _fake_sentry():
    sdk = types.ModuleType("sentry_sdk")
    scope = mock.MagicMock()
    context = mock.MagicMock()
    context.__enter__ = mock.MagicMock(return_value=scope)
    context.__exit__ = mock.MagicMock(return_value=False)
    sdk.new_scope = mock.MagicMock(return_value=context)
    sdk.push_scope = mock.MagicMock(side_effect=AssertionError("deprecated"))
    sdk.capture_message = mock.MagicMock()
    sdk.set_user = mock.MagicMock()
    return sdk, scope


@override_settings(TURBODRF_ENABLE_SENTRY=True, TURBODRF_PERMISSION_MODE="database")
class TestSentryIsLazy(TestCase):
    def test_middleware_runs_no_queries(self):
        from turbodrf.integrations.sentry import SentryContextMiddleware

        sdk, scope = _fake_sentry()
        user = User.objects.create_user("sentry_lazy")
        request = RequestFactory().get("/healthz")
        request.user = SimpleLazyObject(lambda: User.objects.get(pk=user.pk))
        with mock.patch.dict(sys.modules, {"sentry_sdk": sdk}):
            with CaptureQueriesContext(connection) as queries:
                SentryContextMiddleware(lambda r: "ok")(request)
        self.assertEqual(len(queries.captured_queries), 0)
        scope.add_event_processor.assert_called_once()

    def test_capture_security_message_without_push_scope(self):
        from turbodrf.integrations.sentry import capture_security_message

        sdk, _ = _fake_sentry()
        with mock.patch.dict(sys.modules, {"sentry_sdk": sdk}):
            capture_security_message("cross-tenant", user_id=7)
        sdk.push_scope.assert_not_called()
        kwargs = sdk.capture_message.call_args.kwargs
        self.assertEqual(kwargs["tags"], {"turbodrf.security_event": "true"})
        self.assertEqual(kwargs["extras"], {"user_id": 7})


# ---------------------------------------------------------------------------
# Lazy package imports
# ---------------------------------------------------------------------------


class TestIntegrationsImportLazily(TestCase):
    def test_submodule_import_needs_no_auth_models(self):
        # Without configured settings, importing django.contrib.auth.models
        # raises: importing the sentry module must not pull it in.
        code = (
            "import sys; import turbodrf.integrations.sentry; "
            "print('turbodrf.integrations.allauth' in sys.modules, "
            "'django.contrib.auth.models' in sys.modules)"
        )
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False False")

    def test_package_names_still_resolve(self):
        from turbodrf.integrations import (
            AllAuthRoleMiddleware,
            assign_roles_to_user,
            get_user_roles_from_groups,
        )
        from turbodrf.integrations.allauth import get_user_roles_from_groups as direct

        self.assertIs(get_user_roles_from_groups, direct)
        self.assertTrue(callable(assign_roles_to_user))
        self.assertTrue(callable(AllAuthRoleMiddleware))
