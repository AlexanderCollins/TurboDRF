"""
Unit tests for TurboDRF permissions.

Tests the role-based permission system.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from tests.test_app.models import SampleModel
from turbodrf.permissions import TurboDRFPermission

User = get_user_model()


class MockView:
    """Mock view for permission testing."""

    model = SampleModel


class TestTurboDRFPermission(TestCase):
    """Test cases for TurboDRF permission class."""

    def setUp(self):
        """Set up test fixtures."""
        from django.core.cache import cache

        cache.clear()  # Clear cache to avoid test pollution

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

    def test_role_to_actions_admin(self):
        """Admin role grants all CRUD actions on samplemodel via the snapshot."""
        from tests.test_app.models import SampleModel
        from turbodrf.backends import build_permission_snapshot

        snap = build_permission_snapshot(self.admin_user, SampleModel, use_cache=False)
        for action in ("read", "create", "update", "delete"):
            self.assertTrue(
                snap.can_perform_action(action),
                f"admin should have {action}",
            )

    def test_role_to_actions_editor(self):
        from tests.test_app.models import SampleModel
        from turbodrf.backends import build_permission_snapshot

        snap = build_permission_snapshot(self.editor_user, SampleModel, use_cache=False)
        self.assertTrue(snap.can_perform_action("read"))
        self.assertTrue(snap.can_perform_action("update"))
        self.assertFalse(snap.can_perform_action("delete"))

    def test_role_to_actions_viewer(self):
        from tests.test_app.models import SampleModel
        from turbodrf.backends import build_permission_snapshot

        snap = build_permission_snapshot(self.viewer_user, SampleModel, use_cache=False)
        self.assertTrue(snap.can_perform_action("read"))
        self.assertFalse(snap.can_perform_action("create"))
        self.assertFalse(snap.can_perform_action("update"))
        self.assertFalse(snap.can_perform_action("delete"))

    def test_custom_roles_combined(self):
        """A user with multiple roles gets the UNION of their permissions."""
        from tests.test_app.models import SampleModel
        from turbodrf.backends import build_permission_snapshot

        custom_user = User.objects.create_user(username="custom", password="custom123")
        custom_user._test_roles = ["admin", "editor"]
        snap = build_permission_snapshot(custom_user, SampleModel, use_cache=False)
        self.assertTrue(snap.can_perform_action("delete"))  # from admin
        self.assertTrue(snap.can_perform_action("update"))  # from both

    def test_invalid_http_method(self):
        """Test handling of invalid HTTP methods."""
        request = self.factory.generic("INVALID", "/api/samplemodels/")
        request.user = self.admin_user
        self.assertFalse(self.permission.has_permission(request, self.view))

    def test_field_level_permissions(self):
        """Snapshot exposes per-field readable/writable sets that match the
        configured TURBODRF_ROLES rules."""
        from tests.test_app.models import SampleModel
        from turbodrf.backends import build_permission_snapshot

        admin = build_permission_snapshot(self.admin_user, SampleModel, use_cache=False)
        self.assertIn("secret_field", admin.readable_fields)
        self.assertIn("secret_field", admin.writable_fields)
        self.assertIn("price", admin.readable_fields)
        self.assertIn("price", admin.writable_fields)

        editor = build_permission_snapshot(
            self.editor_user, SampleModel, use_cache=False
        )
        self.assertIn("price", editor.readable_fields)
        self.assertNotIn("price", editor.writable_fields)

        viewer = build_permission_snapshot(
            self.viewer_user, SampleModel, use_cache=False
        )
        self.assertNotIn("price", viewer.readable_fields)
        self.assertNotIn("secret_field", viewer.readable_fields)
