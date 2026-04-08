"""
Additional coverage tests for TurboDRF integrations and tracking.

Covers uncovered lines in:
- turbodrf/integrations/allauth_roles.py (lines 115-121, 141, 163-172, 190-198)
- turbodrf/integrations/keycloak.py (lines 177-190, 239-250)
- turbodrf/tracking.py (lines 41, 57-62, 79)
- turbodrf/integrations/allauth.py (lines 148, 172)
"""

from unittest.mock import MagicMock, Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.test import RequestFactory, TestCase, override_settings

User = get_user_model()


# ---------------------------------------------------------------------------
# allauth_roles.py — create_role_mapping (lines 115-121)
# ---------------------------------------------------------------------------


class TestCreateRoleMapping(TestCase):
    """Test create_role_mapping including default role_names and length mismatch."""

    def test_create_role_mapping_with_explicit_role_names(self):
        """Lines 115-121: mapping with provided role_names."""
        from turbodrf.integrations.allauth_roles import create_role_mapping

        result = create_role_mapping(["Admins", "Editors"], ["admin", "editor"])
        self.assertEqual(result, {"Admins": "admin", "Editors": "editor"})

    def test_create_role_mapping_default_role_names(self):
        """Line 115-116: role_names defaults to group_names when None."""
        from turbodrf.integrations.allauth_roles import create_role_mapping

        result = create_role_mapping(["admin", "editor"])
        self.assertEqual(result, {"admin": "admin", "editor": "editor"})

    def test_create_role_mapping_length_mismatch(self):
        """Lines 118-119: ValueError when lists differ in length."""
        from turbodrf.integrations.allauth_roles import create_role_mapping

        with self.assertRaises(ValueError):
            create_role_mapping(["a", "b"], ["x"])

    def test_create_role_mapping_empty_lists(self):
        """Edge case: both lists empty."""
        from turbodrf.integrations.allauth_roles import create_role_mapping

        result = create_role_mapping([])
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# allauth_roles.py — get_or_create_role_group (line 141)
# ---------------------------------------------------------------------------


class TestGetOrCreateRoleGroup(TestCase):
    """Test the single-role convenience wrapper."""

    def test_get_or_create_role_group_creates_new(self):
        """Line 141: creates a new group."""
        from turbodrf.integrations.allauth_roles import get_or_create_role_group

        group, created = get_or_create_role_group("brand_new_role")
        self.assertTrue(created)
        self.assertEqual(group.name, "brand_new_role")

    def test_get_or_create_role_group_returns_existing(self):
        """Line 141: returns existing group without creating."""
        from turbodrf.integrations.allauth_roles import get_or_create_role_group

        Group.objects.create(name="existing_role")
        group, created = get_or_create_role_group("existing_role")
        self.assertFalse(created)
        self.assertEqual(group.name, "existing_role")


# ---------------------------------------------------------------------------
# allauth_roles.py — assign_roles_to_user (lines 163-172)
# ---------------------------------------------------------------------------


class TestAssignRolesToUser(TestCase):
    """Test clearing existing groups and assigning new ones."""

    def setUp(self):
        self.user = User.objects.create_user(username="assign_test_user")

    def test_assign_roles_to_user_basic(self):
        """Lines 163-172: clears groups and assigns new ones."""
        from turbodrf.integrations.allauth_roles import assign_roles_to_user

        # Pre-populate with a group that should be cleared
        old_group = Group.objects.create(name="old_role")
        self.user.groups.add(old_group)

        groups = assign_roles_to_user(self.user, ["role_a", "role_b"])

        self.assertEqual(len(groups), 2)
        current_names = set(self.user.groups.values_list("name", flat=True))
        self.assertEqual(current_names, {"role_a", "role_b"})
        self.assertNotIn("old_role", current_names)

    def test_assign_roles_to_user_empty(self):
        """Assigning empty list clears all groups."""
        from turbodrf.integrations.allauth_roles import assign_roles_to_user

        self.user.groups.add(Group.objects.create(name="to_clear"))
        groups = assign_roles_to_user(self.user, [])
        self.assertEqual(groups, [])
        self.assertEqual(self.user.groups.count(), 0)


# ---------------------------------------------------------------------------
# allauth_roles.py — get_users_with_role (lines 190-198)
# ---------------------------------------------------------------------------


class TestGetUsersWithRole(TestCase):
    """Test querying users by role group."""

    def test_get_users_with_role_found(self):
        """Lines 190-192: group exists, return user_set."""
        from turbodrf.integrations.allauth_roles import get_users_with_role

        group = Group.objects.create(name="testers")
        user = User.objects.create_user(username="in_group")
        user.groups.add(group)

        qs = get_users_with_role("testers")
        self.assertIn(user, qs)

    def test_get_users_with_role_group_missing(self):
        """Lines 194-198: group does not exist, return empty queryset."""
        from turbodrf.integrations.allauth_roles import get_users_with_role

        qs = get_users_with_role("nonexistent_group")
        self.assertEqual(qs.count(), 0)


# ---------------------------------------------------------------------------
# keycloak.py — get_user_roles_from_social_auth (lines 177-190)
# ---------------------------------------------------------------------------


class TestGetUserRolesFromSocialAuth(TestCase):
    """Test role extraction via social_auth associations."""

    def test_returns_mapped_roles_from_social_auth(self):
        """Lines 177-188: iterates social_auths and maps roles."""
        user = User.objects.create_user(username="kc_user")

        mock_social = Mock()
        mock_social.extra_data = {"roles": ["realm-admin", "viewer"]}

        mock_manager = MagicMock()
        mock_manager.all.return_value = [mock_social]
        user.social_auth = mock_manager

        with override_settings(TURBODRF_KEYCLOAK_ROLE_MAPPING={"realm-admin": "admin"}):
            from turbodrf.integrations.keycloak import get_user_roles_from_social_auth

            roles = get_user_roles_from_social_auth(user)

        self.assertEqual(roles, ["admin", "viewer"])

    def test_returns_empty_when_no_roles_in_any_social_auth(self):
        """Line 190: no social_auth has roles, returns []."""
        user = User.objects.create_user(username="kc_empty")

        mock_social = Mock()
        mock_social.extra_data = {"sub": "123"}  # no roles claim

        mock_manager = MagicMock()
        mock_manager.all.return_value = [mock_social]
        user.social_auth = mock_manager

        from turbodrf.integrations.keycloak import get_user_roles_from_social_auth

        roles = get_user_roles_from_social_auth(user)
        self.assertEqual(roles, [])

    @override_settings(TURBODRF_KEYCLOAK_ROLE_CLAIM="realm_access.roles")
    def test_extracts_nested_roles_from_social_auth(self):
        """Nested claim path via social auth extra_data."""
        user = User.objects.create_user(username="kc_nested")

        mock_social = Mock()
        mock_social.extra_data = {"realm_access": {"roles": ["editor"]}}

        mock_manager = MagicMock()
        mock_manager.all.return_value = [mock_social]
        user.social_auth = mock_manager

        from turbodrf.integrations.keycloak import get_user_roles_from_social_auth

        roles = get_user_roles_from_social_auth(user)
        self.assertEqual(roles, ["editor"])


# ---------------------------------------------------------------------------
# keycloak.py — KeycloakRoleMiddleware.__call__ (lines 239-250)
# ---------------------------------------------------------------------------


class TestKeycloakRoleMiddlewareCall(TestCase):
    """Test the actual __call__ path of KeycloakRoleMiddleware."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_middleware_sets_roles_on_authenticated_user(self):
        """Lines 239-248: authenticated user gets roles from social_auth."""
        from turbodrf.integrations.keycloak import KeycloakRoleMiddleware

        user = User.objects.create_user(username="kc_mw_user")
        # Make the existing roles property return [] (falsy) so middleware
        # enters the block that fetches roles from social_auth.
        user._test_roles = []

        mock_social = Mock()
        mock_social.extra_data = {"roles": ["admin"]}
        mock_manager = MagicMock()
        mock_manager.all.return_value = [mock_social]
        user.social_auth = mock_manager

        request = self.factory.get("/")
        request.user = user

        get_response = Mock(return_value="ok")
        middleware = KeycloakRoleMiddleware(get_response)
        response = middleware(request)

        self.assertEqual(response, "ok")
        self.assertEqual(request.user.__dict__.get("roles"), ["admin"])

    def test_middleware_skips_anonymous_user(self):
        """Lines 239-249: anonymous user is not modified."""
        from turbodrf.integrations.keycloak import KeycloakRoleMiddleware

        request = self.factory.get("/")
        request.user = AnonymousUser()

        get_response = Mock(return_value="ok")
        middleware = KeycloakRoleMiddleware(get_response)
        response = middleware(request)

        self.assertEqual(response, "ok")

    def test_middleware_skips_user_with_existing_roles(self):
        """Line 241: user already has roles, middleware does not override."""
        from turbodrf.integrations.keycloak import KeycloakRoleMiddleware

        user = User.objects.create_user(username="kc_existing_roles")
        user.__dict__["roles"] = ["existing"]

        request = self.factory.get("/")
        request.user = user

        get_response = Mock(return_value="ok")
        middleware = KeycloakRoleMiddleware(get_response)
        middleware(request)

        self.assertEqual(request.user.__dict__["roles"], ["existing"])

    def test_middleware_no_roles_found(self):
        """Lines 245-248: social_auth present but no roles extracted."""
        from turbodrf.integrations.keycloak import KeycloakRoleMiddleware

        user = User.objects.create_user(username="kc_no_roles")
        # Make existing roles property return [] so middleware enters the block
        user._test_roles = []

        mock_social = Mock()
        mock_social.extra_data = {}
        mock_manager = MagicMock()
        mock_manager.all.return_value = [mock_social]
        user.social_auth = mock_manager

        request = self.factory.get("/")
        request.user = user

        get_response = Mock(return_value="ok")
        middleware = KeycloakRoleMiddleware(get_response)
        middleware(request)

        # roles should not be set in __dict__ because no roles were found
        self.assertNotIn("roles", request.user.__dict__)


# ---------------------------------------------------------------------------
# tracking.py — lines 41, 57-62, 79
# ---------------------------------------------------------------------------


class TestTrackingWithMockedPackage(TestCase):
    """Test tracking paths that require drf-api-tracking to be 'installed'."""

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_is_tracking_enabled_true_when_package_available(self):
        """Line 41: returns True when setting is True and package importable."""
        mock_module = MagicMock()
        with patch.dict("sys.modules", {"rest_framework_tracking": mock_module}):
            from turbodrf.tracking import is_tracking_enabled

            self.assertTrue(is_tracking_enabled())

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_get_tracking_mixin_returns_class(self):
        """Lines 57-60: returns LoggingMixin when available."""

        class FakeLoggingMixin:
            pass

        mock_mixins = MagicMock()
        mock_mixins.LoggingMixin = FakeLoggingMixin

        mock_pkg = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "rest_framework_tracking": mock_pkg,
                "rest_framework_tracking.mixins": mock_mixins,
            },
        ):
            from turbodrf.tracking import get_tracking_mixin

            result = get_tracking_mixin()
            self.assertIs(result, FakeLoggingMixin)

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_get_tracking_mixin_import_error(self):
        """Lines 61-62: returns None when mixins import fails."""
        mock_pkg = MagicMock()
        # Make the tracking package importable but mixins import fail
        with patch.dict("sys.modules", {"rest_framework_tracking": mock_pkg}):
            with patch("turbodrf.tracking.is_tracking_enabled", return_value=True):
                # Patch the import inside get_tracking_mixin to raise
                original_import = (
                    __builtins__.__import__
                    if hasattr(__builtins__, "__import__")
                    else __import__
                )

                def failing_import(name, *args, **kwargs):
                    if name == "rest_framework_tracking.mixins":
                        raise ImportError("no mixins")
                    return original_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=failing_import):
                    from turbodrf.tracking import get_tracking_mixin

                    result = get_tracking_mixin()
                    self.assertIsNone(result)

    @override_settings(TURBODRF_ENABLE_TRACKING=True)
    def test_get_viewset_base_classes_with_tracking(self):
        """Line 79: tracking mixin inserted before ModelViewSet."""
        from rest_framework import viewsets

        class FakeLoggingMixin:
            pass

        with patch(
            "turbodrf.tracking.get_tracking_mixin", return_value=FakeLoggingMixin
        ):
            from turbodrf.tracking import get_viewset_base_classes

            bases = get_viewset_base_classes()

        self.assertEqual(len(bases), 2)
        self.assertIs(bases[0], FakeLoggingMixin)
        self.assertIs(bases[1], viewsets.ModelViewSet)


# ---------------------------------------------------------------------------
# allauth.py — lines 148, 172
# ---------------------------------------------------------------------------


class _PlainUser:
    """Minimal user-like object without a roles property on the class."""

    is_authenticated = True

    def __init__(self):
        self.groups = MagicMock()
        self.groups.all.return_value = []


class TestAllAuthMiddlewareDictRoles(TestCase):
    """Test the __dict__['roles'] branch (line 148) of AllAuthRoleMiddleware."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_middleware_sets_roles_via_dict_for_plain_user(self):
        """Line 148: user without a roles property gets roles via __dict__."""
        from turbodrf.integrations.allauth import AllAuthRoleMiddleware

        user = _PlainUser()
        group_mock = Mock()
        group_mock.name = "editor"
        user.groups.all.return_value = [group_mock]

        request = self.factory.get("/")
        request.user = user

        get_response = Mock(return_value="ok")
        middleware = AllAuthRoleMiddleware(get_response)

        with override_settings(TURBODRF_ALLAUTH_ROLE_MAPPING={}):
            middleware(request)

        self.assertEqual(request.user.__dict__["roles"], ["editor"])


class TestAllAuthSetupIntegration(TestCase):
    """Test setup_allauth_integration return dict (line 172)."""

    def test_setup_allauth_integration_returns_dict(self):
        """Line 172: verify the full return dict."""
        from turbodrf.integrations.allauth import setup_allauth_integration

        with patch(
            "turbodrf.integrations.allauth.is_allauth_installed", return_value=True
        ):
            with override_settings(
                TURBODRF_ALLAUTH_INTEGRATION=True,
                TURBODRF_ALLAUTH_ROLE_MAPPING={"A": "a"},
            ):
                result = setup_allauth_integration()

        self.assertTrue(result["allauth_installed"])
        self.assertTrue(result["integration_enabled"])
        self.assertEqual(result["role_mapping"], {"A": "a"})
        self.assertTrue(result["has_custom_mapping"])

    def test_setup_allauth_integration_defaults(self):
        """Line 172: default values when nothing configured."""
        from turbodrf.integrations.allauth import setup_allauth_integration

        with patch(
            "turbodrf.integrations.allauth.is_allauth_installed", return_value=False
        ):
            result = setup_allauth_integration()

        self.assertFalse(result["allauth_installed"])
        self.assertFalse(result["integration_enabled"])
        self.assertEqual(result["role_mapping"], {})
        self.assertFalse(result["has_custom_mapping"])
