"""
Regression tests: integration roles reach token/JWT-authenticated users.

The Keycloak / allauth middleware runs before DRF authentication, so a user
authenticated by a DRF authentication class (token, JWT, ...) never got
integration roles. TurboDRF now installs a lazy ``roles`` resolver on the
user model at startup when an integration is enabled.
"""

from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from tests.test_app.models import SampleModel
from turbodrf.backends import get_user_roles
from turbodrf.integrations.roles import (
    install_integration_role_resolver,
    resolve_integration_roles,
)


def _make_user_class():
    """A fresh user-like class (no class-level ``roles``) per test."""

    class ResolverUser:
        is_authenticated = True
        is_anonymous = False

        def __init__(self, pk, group_names=(), associations=()):
            self.pk = pk
            self.id = pk
            self.groups = Mock()
            groups = []
            for name in group_names:
                group = Mock()
                group.name = name
                groups.append(group)
            self.groups.all.return_value = groups
            self.social_auth = Mock()
            self.social_auth.filter.side_effect = lambda **kw: [
                a for a in associations if a.provider == kw.get("provider")
            ]

    return ResolverUser


class _Association:
    def __init__(self, provider, extra_data):
        self.provider = provider
        self.extra_data = extra_data


class TestInstallResolver(TestCase):
    def setUp(self):
        cache.clear()

    def test_not_installed_when_integrations_disabled(self):
        cls = _make_user_class()
        self.assertFalse(install_integration_role_resolver(cls))
        self.assertFalse(hasattr(cls, "roles"))

    @override_settings(TURBODRF_ALLAUTH_INTEGRATION=True)
    def test_existing_roles_attribute_is_never_replaced(self):
        cls = _make_user_class()
        own = property(lambda self: ["mine"])
        cls.roles = own
        self.assertFalse(install_integration_role_resolver(cls))
        self.assertIs(cls.roles, own)

    @override_settings(TURBODRF_ALLAUTH_INTEGRATION=True)
    def test_install_is_idempotent(self):
        cls = _make_user_class()
        self.assertTrue(install_integration_role_resolver(cls))
        self.assertTrue(install_integration_role_resolver(cls))

    @override_settings(
        TURBODRF_ALLAUTH_INTEGRATION=True, TURBODRF_ALLAUTH_ROLE_MAPPING={}
    )
    def test_get_user_roles_resolves_allauth_roles_without_middleware(self):
        cls = _make_user_class()
        install_integration_role_resolver(cls)
        user = cls(pk=910001, group_names=["viewer"])
        self.assertEqual(get_user_roles(user), ["viewer"])
        # Resolved once per instance.
        get_user_roles(user)
        self.assertEqual(user.groups.all.call_count, 1)

    @override_settings(
        TURBODRF_ALLAUTH_INTEGRATION=True,
        TURBODRF_KEYCLOAK_INTEGRATION=True,
        TURBODRF_ALLAUTH_ROLE_MAPPING={},
        TURBODRF_KEYCLOAK_ROLE_MAPPING={},
    )
    def test_both_integrations_are_combined_without_duplicates(self):
        cls = _make_user_class()
        install_integration_role_resolver(cls)
        user = cls(
            pk=910002,
            group_names=["viewer", "editor"],
            associations=[
                _Association("github", {"roles": ["admin"]}),
                _Association("keycloak", {"roles": ["editor"]}),
            ],
        )
        self.assertEqual(resolve_integration_roles(user), ["editor", "viewer"])

    @override_settings(
        TURBODRF_ALLAUTH_INTEGRATION=True, TURBODRF_ALLAUTH_ROLE_MAPPING={}
    )
    def test_drf_token_style_authentication_gets_integration_roles(self):
        """A user authenticated by a DRF authentication class (no middleware
        ran) passes TurboDRF's permission check with its group role."""
        from turbodrf.permissions import TurboDRFPermission
        from turbodrf.views import TurboDRFViewSet

        cls = _make_user_class()
        install_integration_role_resolver(cls)
        user = cls(pk=910003, group_names=["viewer"])

        class TokenLikeAuthentication(BaseAuthentication):
            def authenticate(self, request):
                return (user, None)

        class SampleViewSet(TurboDRFViewSet):
            model = SampleModel

        request = Request(
            APIRequestFactory().get("/api/samplemodels/"),
            authenticators=[TokenLikeAuthentication()],
        )
        self.assertTrue(TurboDRFPermission().has_permission(request, SampleViewSet()))


class TestAppReadyInstallsResolver(TestCase):
    def _ready(self):
        from django.apps import apps

        apps.get_app_config("turbodrf").ready()

    def test_ready_installs_when_an_integration_is_enabled(self):
        with patch(
            "turbodrf.integrations.roles.install_integration_role_resolver"
        ) as install:
            with override_settings(TURBODRF_KEYCLOAK_INTEGRATION=True):
                self._ready()
            install.assert_called_once_with()

    def test_ready_skips_when_integrations_disabled(self):
        with patch(
            "turbodrf.integrations.roles.install_integration_role_resolver"
        ) as install:
            self._ready()
            install.assert_not_called()
