"""
Tests for Keycloak/OpenID Connect integration.

Tests role extraction from ID tokens and integration with
TurboDRF's role-based permission system.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from turbodrf.integrations.keycloak import (
    extract_roles_from_token,
    get_role_claim_path,
    get_role_mapping,
    get_user_roles_from_social_auth,
    is_integration_enabled,
    is_social_auth_installed,
    map_keycloak_roles_to_turbodrf,
    setup_keycloak_integration,
)

User = get_user_model()


class TestKeycloakDetection(TestCase):
    """Test Keycloak integration detection."""

    def test_is_social_auth_installed(self):
        """Test social-auth-app-django detection."""
        result = is_social_auth_installed()

        # Should return boolean
        self.assertIsInstance(result, bool)

    @override_settings(TURBODRF_KEYCLOAK_INTEGRATION=True)
    def test_is_integration_enabled_when_true(self):
        """Test integration enabled detection."""
        enabled = is_integration_enabled()

        self.assertTrue(enabled)

    @override_settings(TURBODRF_KEYCLOAK_INTEGRATION=False)
    def test_is_integration_enabled_when_false(self):
        """Test integration disabled detection."""
        enabled = is_integration_enabled()

        self.assertFalse(enabled)

    def test_is_integration_enabled_default(self):
        """Test integration disabled by default."""
        enabled = is_integration_enabled()

        self.assertFalse(enabled)


class TestRoleClaimPath(TestCase):
    """Test role claim path configuration."""

    def test_get_role_claim_path_default(self):
        """Test default role claim path."""
        path = get_role_claim_path()

        self.assertEqual(path, "roles")

    @override_settings(TURBODRF_KEYCLOAK_ROLE_CLAIM="realm_access.roles")
    def test_get_role_claim_path_custom(self):
        """Test custom role claim path."""
        path = get_role_claim_path()

        self.assertEqual(path, "realm_access.roles")

    @override_settings(TURBODRF_KEYCLOAK_ROLE_CLAIM="resource_access.my-client.roles")
    def test_get_role_claim_path_client_roles(self):
        """Test client-specific role claim path."""
        path = get_role_claim_path()

        self.assertEqual(path, "resource_access.my-client.roles")


class TestRoleMapping(TestCase):
    """Test role mapping configuration."""

    def test_get_role_mapping_default(self):
        """Test empty role mapping by default."""
        mapping = get_role_mapping()

        self.assertEqual(mapping, {})

    @override_settings(
        TURBODRF_KEYCLOAK_ROLE_MAPPING={
            "realm-admin": "admin",
            "content-editor": "editor",
        }
    )
    def test_get_role_mapping_custom(self):
        """Test custom role mapping."""
        mapping = get_role_mapping()

        self.assertEqual(mapping["realm-admin"], "admin")
        self.assertEqual(mapping["content-editor"], "editor")


class TestRoleExtraction(TestCase):
    """Test role extraction from ID tokens."""

    def test_extract_roles_simple_claim(self):
        """Test extracting roles from simple 'roles' claim."""
        token = {"roles": ["admin", "editor", "viewer"]}

        roles = extract_roles_from_token(token)

        self.assertEqual(roles, ["admin", "editor", "viewer"])

    @override_settings(TURBODRF_KEYCLOAK_ROLE_CLAIM="realm_access.roles")
    def test_extract_roles_nested_claim(self):
        """Test extracting roles from nested realm_access claim."""
        token = {"realm_access": {"roles": ["admin", "editor"]}}

        roles = extract_roles_from_token(token)

        self.assertEqual(roles, ["admin", "editor"])

    @override_settings(TURBODRF_KEYCLOAK_ROLE_CLAIM="resource_access.my-client.roles")
    def test_extract_roles_client_claim(self):
        """Test extracting roles from client-specific claim."""
        token = {"resource_access": {"my-client": {"roles": ["app-admin", "app-user"]}}}

        roles = extract_roles_from_token(token)

        self.assertEqual(roles, ["app-admin", "app-user"])

    def test_extract_roles_missing_claim(self):
        """Test extracting roles when claim is missing."""
        token = {"sub": "user123", "email": "user@example.com"}

        roles = extract_roles_from_token(token)

        self.assertEqual(roles, [])

    @override_settings(TURBODRF_KEYCLOAK_ROLE_CLAIM="realm_access.roles")
    def test_extract_roles_partial_path(self):
        """Test extracting roles when partial path exists."""
        token = {"realm_access": {"foo": "bar"}}  # Missing 'roles' key

        roles = extract_roles_from_token(token)

        self.assertEqual(roles, [])

    def test_extract_roles_empty_list(self):
        """Test extracting roles when list is empty."""
        token = {"roles": []}

        roles = extract_roles_from_token(token)

        self.assertEqual(roles, [])

    def test_extract_roles_not_a_list(self):
        """Test extracting roles when value is not a list."""
        token = {"roles": "admin"}  # String instead of list

        roles = extract_roles_from_token(token)

        # Should return empty list since it's not a list
        self.assertEqual(roles, [])


class TestKeycloakRoleMapping(TestCase):
    """Test Keycloak to TurboDRF role mapping."""

    def test_map_roles_no_mapping(self):
        """Test role mapping with no custom mapping configured."""
        keycloak_roles = ["realm-admin", "content-editor"]

        mapped = map_keycloak_roles_to_turbodrf(keycloak_roles)

        # Should return roles as-is
        self.assertEqual(mapped, ["realm-admin", "content-editor"])

    @override_settings(
        TURBODRF_KEYCLOAK_ROLE_MAPPING={
            "realm-admin": "admin",
            "content-editor": "editor",
        }
    )
    def test_map_roles_with_mapping(self):
        """Test role mapping with custom mapping."""
        keycloak_roles = ["realm-admin", "content-editor"]

        mapped = map_keycloak_roles_to_turbodrf(keycloak_roles)

        self.assertEqual(mapped, ["admin", "editor"])

    @override_settings(
        TURBODRF_KEYCLOAK_ROLE_MAPPING={
            "realm-admin": "admin",
        }
    )
    def test_map_roles_partial_mapping(self):
        """Test role mapping with partial mapping (some unmapped)."""
        keycloak_roles = ["realm-admin", "unknown-role"]

        mapped = map_keycloak_roles_to_turbodrf(keycloak_roles)

        # Mapped role gets converted, unmapped role passes through
        self.assertEqual(mapped, ["admin", "unknown-role"])

    @override_settings(TURBODRF_KEYCLOAK_ROLE_MAPPING={})
    def test_map_roles_empty_mapping(self):
        """Test role mapping with empty mapping dict."""
        keycloak_roles = ["admin", "editor"]

        mapped = map_keycloak_roles_to_turbodrf(keycloak_roles)

        # Empty mapping should pass through
        self.assertEqual(mapped, ["admin", "editor"])


class TestKeycloakMiddleware(TestCase):
    """Test Keycloak role middleware."""

    def test_middleware_import(self):
        """Test that KeycloakRoleMiddleware can be imported."""
        from turbodrf.integrations.keycloak import KeycloakRoleMiddleware

        self.assertIsNotNone(KeycloakRoleMiddleware)

    def test_middleware_has_init(self):
        """Test that middleware has __init__ method."""
        from turbodrf.integrations.keycloak import KeycloakRoleMiddleware

        def get_response(request):
            return None

        middleware = KeycloakRoleMiddleware(get_response)

        self.assertIsNotNone(middleware)

    def test_middleware_has_call(self):
        """Test that middleware has __call__ method."""
        from turbodrf.integrations.keycloak import KeycloakRoleMiddleware

        def get_response(request):
            return None

        middleware = KeycloakRoleMiddleware(get_response)

        self.assertTrue(callable(middleware))


class TestKeycloakSetup(TestCase):
    """Test Keycloak setup helper."""

    def test_setup_keycloak_integration_returns_dict(self):
        """Test that setup helper returns status dict."""
        status = setup_keycloak_integration()

        self.assertIsInstance(status, dict)

    def test_setup_includes_social_auth_status(self):
        """Test that setup status includes social auth installed status."""
        status = setup_keycloak_integration()

        self.assertIn("social_auth_installed", status)
        self.assertIsInstance(status["social_auth_installed"], bool)

    def test_setup_includes_integration_status(self):
        """Test that setup status includes integration enabled status."""
        status = setup_keycloak_integration()

        self.assertIn("integration_enabled", status)
        self.assertIsInstance(status["integration_enabled"], bool)

    def test_setup_includes_role_claim_path(self):
        """Test that setup status includes role claim path."""
        status = setup_keycloak_integration()

        self.assertIn("role_claim_path", status)
        self.assertIsInstance(status["role_claim_path"], str)

    def test_setup_includes_role_mapping(self):
        """Test that setup status includes role mapping."""
        status = setup_keycloak_integration()

        self.assertIn("role_mapping", status)
        self.assertIsInstance(status["role_mapping"], dict)

    def test_setup_includes_has_custom_mapping(self):
        """Test that setup status includes custom mapping flag."""
        status = setup_keycloak_integration()

        self.assertIn("has_custom_mapping", status)
        self.assertIsInstance(status["has_custom_mapping"], bool)


class TestUserRolesFromSocialAuth(TestCase):
    """Test getting user roles from social auth."""

    def test_get_roles_user_without_social_auth(self):
        """Test getting roles from user without social_auth."""
        user = User.objects.create_user(username="testuser")

        roles = get_user_roles_from_social_auth(user)

        # Should return empty list
        self.assertEqual(roles, [])

    def test_get_roles_returns_list(self):
        """Test that function returns a list."""
        user = User.objects.create_user(username="testuser")

        roles = get_user_roles_from_social_auth(user)

        self.assertIsInstance(roles, list)


class TestKeycloakDocumentation(TestCase):
    """Test Keycloak integration documentation."""

    def test_module_has_docstring(self):
        """Test that keycloak module has documentation."""
        from turbodrf.integrations import keycloak

        docstring = keycloak.__doc__

        self.assertIsNotNone(docstring)
        self.assertIn("Keycloak", docstring)

    def test_extract_roles_has_docstring(self):
        """Test that extract_roles_from_token has documentation."""
        docstring = extract_roles_from_token.__doc__

        self.assertIsNotNone(docstring)
        self.assertIn("token", docstring.lower())

    def test_map_roles_has_docstring(self):
        """Test that map_keycloak_roles_to_turbodrf has documentation."""
        docstring = map_keycloak_roles_to_turbodrf.__doc__

        self.assertIsNotNone(docstring)

    def test_setup_has_docstring(self):
        """Test that setup_keycloak_integration has documentation."""
        docstring = setup_keycloak_integration.__doc__

        self.assertIsNotNone(docstring)


class TestRoleExtractionEdgeCases(TestCase):
    """Test edge cases in role extraction."""

    @override_settings(TURBODRF_KEYCLOAK_ROLE_CLAIM="a.b.c.d.roles")
    def test_extract_roles_deep_nesting(self):
        """Test extracting roles from deeply nested claim."""
        token = {"a": {"b": {"c": {"d": {"roles": ["admin"]}}}}}

        roles = extract_roles_from_token(token)

        self.assertEqual(roles, ["admin"])

    def test_extract_roles_none_value(self):
        """Test extracting roles when value is None."""
        token = {"roles": None}

        roles = extract_roles_from_token(token)

        self.assertEqual(roles, [])

    def test_extract_roles_with_duplicates(self):
        """Test extracting roles with duplicate values."""
        token = {"roles": ["admin", "editor", "admin"]}

        roles = extract_roles_from_token(token)

        # Should preserve duplicates (filtering is done elsewhere)
        self.assertEqual(roles, ["admin", "editor", "admin"])
