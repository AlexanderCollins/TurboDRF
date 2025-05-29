"""
Unit tests for TurboDRF permissions.

Tests the role-based permission system.
"""

from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory
from turbodrf.permissions import TurboDRFPermission
from tests.test_app.models import SampleModel


User = get_user_model()


class MockView:
    """Mock view for permission testing."""

    model = SampleModel


class TestTurboDRFPermission(TestCase):
    """Test cases for TurboDRF permission class."""

    def setUp(self):
        """Set up test fixtures."""
        self.factory = APIRequestFactory()
        self.permission = TurboDRFPermission()
        self.view = MockView()

        # Create test users
        self.admin_user = User.objects.create_user(
            username="admin", password="admin123", is_superuser=True
        )
        self.admin_user._test_roles = ["admin"]
        
        self.editor_user = User.objects.create_user(
            username="editor", password="editor123", is_staff=True
        )
        self.editor_user._test_roles = ["editor"]
        
        self.viewer_user = User.objects.create_user(
            username="viewer", password="viewer123"
        )
        self.viewer_user._test_roles = ["viewer"]

    def test_unauthenticated_user_read_permission(self):
        """Test that unauthenticated users can only read."""
        # GET request should be allowed
        request = self.factory.get("/api/samplemodels/")
        request.user = None
        self.assertTrue(self.permission.has_permission(request, self.view))

        # POST request should be denied
        request = self.factory.post("/api/samplemodels/")
        request.user = None
        self.assertFalse(self.permission.has_permission(request, self.view))

    def test_admin_has_all_permissions(self):
        """Test that admin users have all permissions."""
        methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]

        for method in methods:
            request = getattr(self.factory, method.lower())("/api/samplemodels/")
            request.user = self.admin_user
            self.assertTrue(
                self.permission.has_permission(request, self.view),
                f"Admin should have {method} permission",
            )

    def test_editor_permissions(self):
        """Test editor permissions (read and update, no delete)."""
        # Editor should have read permission
        request = self.factory.get("/api/samplemodels/")
        request.user = self.editor_user
        self.assertTrue(self.permission.has_permission(request, self.view))

        # Editor should have update permission
        request = self.factory.put("/api/samplemodels/1/")
        request.user = self.editor_user
        self.assertTrue(self.permission.has_permission(request, self.view))

        # Editor should have patch permission
        request = self.factory.patch("/api/samplemodels/1/")
        request.user = self.editor_user
        self.assertTrue(self.permission.has_permission(request, self.view))

        # Editor should NOT have delete permission
        request = self.factory.delete("/api/samplemodels/1/")
        request.user = self.editor_user
        self.assertFalse(self.permission.has_permission(request, self.view))

        # Editor should NOT have create permission (based on our test config)
        request = self.factory.post("/api/samplemodels/")
        request.user = self.editor_user
        self.assertFalse(self.permission.has_permission(request, self.view))

    def test_viewer_permissions(self):
        """Test viewer permissions (read only)."""
        # Viewer should have read permission
        request = self.factory.get("/api/samplemodels/")
        request.user = self.viewer_user
        self.assertTrue(self.permission.has_permission(request, self.view))

        # Viewer should NOT have any write permissions
        write_methods = ["POST", "PUT", "PATCH", "DELETE"]
        for method in write_methods:
            request = getattr(self.factory, method.lower())("/api/samplemodels/")
            request.user = self.viewer_user
            self.assertFalse(
                self.permission.has_permission(request, self.view),
                f"Viewer should not have {method} permission",
            )

    def test_get_user_permissions(self):
        """Test _get_user_permissions method."""
        # Admin permissions
        admin_perms = self.permission._get_user_permissions(self.admin_user)
        self.assertIn("test_app.samplemodel.read", admin_perms)
        self.assertIn("test_app.samplemodel.create", admin_perms)
        self.assertIn("test_app.samplemodel.update", admin_perms)
        self.assertIn("test_app.samplemodel.delete", admin_perms)

        # Editor permissions
        editor_perms = self.permission._get_user_permissions(self.editor_user)
        self.assertIn("test_app.samplemodel.read", editor_perms)
        self.assertIn("test_app.samplemodel.update", editor_perms)
        self.assertNotIn("test_app.samplemodel.delete", editor_perms)

        # Viewer permissions
        viewer_perms = self.permission._get_user_permissions(self.viewer_user)
        self.assertIn("test_app.samplemodel.read", viewer_perms)
        self.assertNotIn("test_app.samplemodel.create", viewer_perms)
        self.assertNotIn("test_app.samplemodel.update", viewer_perms)
        self.assertNotIn("test_app.samplemodel.delete", viewer_perms)

    def test_custom_roles(self):
        """Test custom role assignment."""
        # Create a user with custom roles
        custom_user = User.objects.create_user(username="custom", password="custom123")
        # Assign custom roles
        custom_user._test_roles = ["admin", "editor"]

        # Should have combined permissions
        perms = self.permission._get_user_permissions(custom_user)
        self.assertIn("test_app.samplemodel.delete", perms)  # From admin
        self.assertIn("test_app.samplemodel.update", perms)  # From both

    def test_invalid_http_method(self):
        """Test handling of invalid HTTP methods."""
        request = self.factory.generic("INVALID", "/api/samplemodels/")
        request.user = self.admin_user
        self.assertFalse(self.permission.has_permission(request, self.view))

    def test_field_level_permissions(self):
        """Test field-level permissions in user permissions."""
        # Admin should have field permissions
        admin_perms = self.permission._get_user_permissions(self.admin_user)
        self.assertIn("test_app.samplemodel.secret_field.read", admin_perms)
        self.assertIn("test_app.samplemodel.secret_field.write", admin_perms)
        self.assertIn("test_app.samplemodel.price.read", admin_perms)
        self.assertIn("test_app.samplemodel.price.write", admin_perms)

        # Editor should have read-only price permission
        editor_perms = self.permission._get_user_permissions(self.editor_user)
        self.assertIn("test_app.samplemodel.price.read", editor_perms)
        self.assertNotIn("test_app.samplemodel.price.write", editor_perms)

        # Viewer should not have price or secret field permissions
        viewer_perms = self.permission._get_user_permissions(self.viewer_user)
        self.assertNotIn("test_app.samplemodel.price.read", viewer_perms)
        self.assertNotIn("test_app.samplemodel.secret_field.read", viewer_perms)
