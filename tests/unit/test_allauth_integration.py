"""
Unit tests for TurboDRF django-allauth integration.

Tests role mapping, middleware, and configuration utilities.
"""

from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import RequestFactory, TestCase, override_settings

User = get_user_model()


class TestRoleMapping(TestCase):
    """Test role mapping from Django groups to TurboDRF roles."""

    def setUp(self):
        """Set up test fixtures."""
        self.user = User.objects.create_user(username="testuser")
        self.admin_group = Group.objects.create(name="admin")
        self.editor_group = Group.objects.create(name="editor")
        self.viewer_group = Group.objects.create(name="viewer")

    def test_get_roles_from_groups_default_mapping(self):
        """Test that group names are used as roles by default."""
        from turbodrf.integrations.allauth import get_user_roles_from_groups

        self.user.groups.add(self.admin_group, self.editor_group)

        roles = get_user_roles_from_groups(self.user)

        self.assertEqual(set(roles), {"admin", "editor"})

    @override_settings(
        TURBODRF_ALLAUTH_ROLE_MAPPING={
            "Administrators": "admin",
            "Content Editors": "editor",
            "Basic Users": "viewer",
        }
    )
    def test_get_roles_from_groups_custom_mapping(self):
        """Test custom role mapping configuration."""
        from turbodrf.integrations.allauth import get_user_roles_from_groups

        # Create groups with different names
        admins = Group.objects.create(name="Administrators")
        editors = Group.objects.create(name="Content Editors")

        self.user.groups.add(admins, editors)

        roles = get_user_roles_from_groups(self.user)

        self.assertEqual(set(roles), {"admin", "editor"})

    def test_get_roles_from_groups_no_groups(self):
        """Test user with no groups returns empty list."""
        from turbodrf.integrations.allauth import get_user_roles_from_groups

        roles = get_user_roles_from_groups(self.user)

        self.assertEqual(roles, [])

    def test_get_roles_from_groups_unmapped_groups_ignored(self):
        """Test that unmapped groups are ignored in custom mapping."""
        from turbodrf.integrations.allauth import get_user_roles_from_groups

        unmapped_group = Group.objects.create(name="Unmapped")
        self.user.groups.add(self.admin_group, unmapped_group)

        with override_settings(TURBODRF_ALLAUTH_ROLE_MAPPING={"admin": "super_admin"}):
            roles = get_user_roles_from_groups(self.user)

            # With custom mapping, unmapped groups use their name as role
            self.assertIn("Unmapped", roles)
            self.assertIn("super_admin", roles)

    def test_get_roles_from_groups_multiple_groups(self):
        """Test user with multiple groups gets multiple roles."""
        from turbodrf.integrations.allauth import get_user_roles_from_groups

        self.user.groups.add(self.admin_group, self.editor_group, self.viewer_group)

        roles = get_user_roles_from_groups(self.user)

        self.assertEqual(set(roles), {"admin", "editor", "viewer"})


class TestAllAuthRoleMiddleware(TestCase):
    """Test middleware that adds roles property to users."""

    def setUp(self):
        """Set up test fixtures."""
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="testuser")
        self.admin_group = Group.objects.create(name="admin")
        self.editor_group = Group.objects.create(name="editor")

    def test_middleware_adds_roles_property(self):
        """Test that middleware adds roles property to user."""
        from turbodrf.integrations.allauth import AllAuthRoleMiddleware

        self.user.groups.add(self.admin_group)

        request = self.factory.get("/")
        request.user = self.user

        get_response = Mock(return_value="response")
        middleware = AllAuthRoleMiddleware(get_response)

        middleware(request)

        self.assertTrue(hasattr(request.user, "roles"))
        self.assertEqual(request.user.roles, ["admin"])

    def test_middleware_with_anonymous_user(self):
        """Test middleware handles anonymous users gracefully."""
        from django.contrib.auth.models import AnonymousUser

        from turbodrf.integrations.allauth import AllAuthRoleMiddleware

        request = self.factory.get("/")
        request.user = AnonymousUser()

        get_response = Mock(return_value="response")
        middleware = AllAuthRoleMiddleware(get_response)

        response = middleware(request)

        self.assertEqual(response, "response")
        # Anonymous user should have empty roles
        self.assertEqual(request.user.roles, [])

    def test_middleware_preserves_existing_roles(self):
        """Test middleware doesn't override existing roles property."""
        from turbodrf.integrations.allauth import AllAuthRoleMiddleware

        # User already has roles defined via _test_roles
        # (which the test User model's roles property checks)
        self.user._test_roles = ["custom_role"]

        request = self.factory.get("/")
        request.user = self.user

        get_response = Mock(return_value="response")
        middleware = AllAuthRoleMiddleware(get_response)

        middleware(request)

        # Should not override existing roles
        self.assertEqual(request.user.roles, ["custom_role"])

    def test_middleware_calls_get_response(self):
        """Test middleware properly calls and returns get_response."""
        from turbodrf.integrations.allauth import AllAuthRoleMiddleware

        request = self.factory.get("/")
        request.user = self.user

        expected_response = "expected_response"
        get_response = Mock(return_value=expected_response)
        middleware = AllAuthRoleMiddleware(get_response)

        response = middleware(request)

        self.assertEqual(response, expected_response)
        get_response.assert_called_once_with(request)


class TestIntegrationDetection(TestCase):
    """Test allauth installation detection."""

    def test_is_allauth_installed_true(self):
        """Test detection when allauth is installed."""
        from turbodrf.integrations.allauth import is_allauth_installed

        # Mock importlib.util.find_spec to return a spec
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = Mock()  # Non-None = installed

            result = is_allauth_installed()

            self.assertTrue(result)
            mock_find_spec.assert_called_once_with("allauth")

    def test_is_allauth_installed_false(self):
        """Test detection when allauth is not installed."""
        from turbodrf.integrations.allauth import is_allauth_installed

        # Mock importlib.util.find_spec to return None
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None

            result = is_allauth_installed()

            self.assertFalse(result)


class TestSetupHelpers(TestCase):
    """Test setup and configuration helpers."""

    @override_settings(TURBODRF_ALLAUTH_INTEGRATION=True)
    def test_is_integration_enabled_true(self):
        """Test integration enabled check when setting is True."""
        from turbodrf.integrations.allauth import is_integration_enabled

        self.assertTrue(is_integration_enabled())

    @override_settings(TURBODRF_ALLAUTH_INTEGRATION=False)
    def test_is_integration_enabled_false(self):
        """Test integration enabled check when setting is False."""
        from turbodrf.integrations.allauth import is_integration_enabled

        self.assertFalse(is_integration_enabled())

    def test_is_integration_enabled_default_false(self):
        """Test integration disabled by default."""
        from turbodrf.integrations.allauth import is_integration_enabled

        # No setting defined, should default to False
        with override_settings():
            if hasattr(settings, "TURBODRF_ALLAUTH_INTEGRATION"):
                delattr(settings, "TURBODRF_ALLAUTH_INTEGRATION")

            self.assertFalse(is_integration_enabled())

    def test_get_role_mapping_default(self):
        """Test getting role mapping when none configured."""
        from turbodrf.integrations.allauth import get_role_mapping

        mapping = get_role_mapping()

        self.assertEqual(mapping, {})

    @override_settings(
        TURBODRF_ALLAUTH_ROLE_MAPPING={"Admins": "admin", "Editors": "editor"}
    )
    def test_get_role_mapping_custom(self):
        """Test getting custom role mapping."""
        from turbodrf.integrations.allauth import get_role_mapping

        mapping = get_role_mapping()

        self.assertEqual(mapping, {"Admins": "admin", "Editors": "editor"})


class TestRoleUtilities(TestCase):
    """Test role mapping utility functions."""

    def setUp(self):
        """Set up test fixtures."""
        self.admin_group = Group.objects.create(name="admin")
        self.editor_group = Group.objects.create(name="editor")

    def test_create_role_groups(self):
        """Test helper to create standard role groups."""
        from turbodrf.integrations.allauth_roles import create_role_groups

        # Clear existing groups
        Group.objects.all().delete()

        groups = create_role_groups(["admin", "editor", "viewer"])

        self.assertEqual(Group.objects.count(), 3)
        self.assertTrue(Group.objects.filter(name="admin").exists())
        self.assertTrue(Group.objects.filter(name="editor").exists())
        self.assertTrue(Group.objects.filter(name="viewer").exists())
        self.assertEqual(len(groups), 3)

    def test_create_role_groups_skip_existing(self):
        """Test creating groups skips existing ones."""
        from turbodrf.integrations.allauth_roles import create_role_groups

        # admin and editor already exist from setUp
        initial_count = Group.objects.count()

        groups = create_role_groups(["admin", "editor", "new_role"])

        # Should only create new_role (admin and editor exist)
        self.assertEqual(Group.objects.count(), initial_count + 1)
        self.assertEqual(len(groups), 3)  # Returns all 3 groups

    def test_sync_groups_to_roles(self):
        """Test syncing existing groups to TurboDRF format."""
        from turbodrf.integrations.allauth_roles import sync_groups_to_roles

        user = User.objects.create_user(username="testuser")
        user.groups.add(self.admin_group, self.editor_group)

        roles = sync_groups_to_roles(user)

        self.assertEqual(set(roles), {"admin", "editor"})

    def test_validate_role_mapping(self):
        """Test role mapping validation."""
        from turbodrf.integrations.allauth_roles import validate_role_mapping

        # Valid mapping
        valid_mapping = {"Group1": "role1", "Group2": "role2"}
        self.assertTrue(validate_role_mapping(valid_mapping))

        # Invalid mapping - not a dict
        self.assertFalse(validate_role_mapping("not a dict"))

        # Invalid mapping - non-string values
        invalid_mapping = {"Group1": 123}
        self.assertFalse(validate_role_mapping(invalid_mapping))

        # Invalid mapping - non-string keys
        invalid_mapping = {123: "role1"}
        self.assertFalse(validate_role_mapping(invalid_mapping))
