"""
Test suite for database-backed dynamic permissions in TurboDRF.

Tests cover:
- Model-level read permissions
- Field-level read/write restrictions
- Permission snapshot reuse within requests
- Cache invalidation
- Integration with Django Groups
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient

from turbodrf.backends import (
    attach_snapshot_to_request,
    build_permission_snapshot,
    get_snapshot_from_request,
)
from turbodrf.models import RolePermission, TurboDRFRole, UserRole

User = get_user_model()


@override_settings(TURBODRF_PERMISSION_MODE="database")
class TestDatabasePermissionModels(TestCase):
    """Test the database models for roles and permissions."""

    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(username="testuser")

    def test_create_role(self):
        """Test creating a TurboDRF role."""
        role = TurboDRFRole.objects.create(
            name="editor", description="Content editor role"
        )
        self.assertEqual(role.name, "editor")
        self.assertEqual(role.version, 1)
        self.assertIsNotNone(role.created_at)

    def test_role_version_increment(self):
        """Test that role version increments on update."""
        role = TurboDRFRole.objects.create(name="admin")
        initial_version = role.version

        role.description = "Updated description"
        role.save()

        self.assertEqual(role.version, initial_version + 1)

    def test_create_model_level_permission(self):
        """Test creating a model-level permission."""
        role = TurboDRFRole.objects.create(name="viewer")
        perm = RolePermission.objects.create(
            role=role, app_label="books", model_name="book", action="read"
        )
        self.assertEqual(perm.to_permission_string(), "books.book.read")

    def test_create_field_level_permission(self):
        """Test creating a field-level permission."""
        role = TurboDRFRole.objects.create(name="editor")
        perm = RolePermission.objects.create(
            role=role,
            app_label="books",
            model_name="book",
            field_name="price",
            permission_type="read",
        )
        self.assertEqual(perm.to_permission_string(), "books.book.price.read")

    def test_assign_role_to_user(self):
        """Test assigning a role to a user."""
        role = TurboDRFRole.objects.create(name="admin")
        UserRole.objects.create(user=self.user, role=role)

        user_roles = UserRole.objects.filter(user=self.user)
        self.assertEqual(user_roles.count(), 1)
        self.assertEqual(user_roles.first().role.name, "admin")

    def test_link_role_to_django_group(self):
        """Test linking a TurboDRF role to a Django Group."""
        group = Group.objects.create(name="Editors")
        role = TurboDRFRole.objects.create(name="editor", django_group=group)
        self.assertEqual(role.django_group, group)
        self.assertEqual(group.turbodrf_role, role)

    def test_permission_updates_role_version(self):
        """Test that creating a permission updates the role version."""
        role = TurboDRFRole.objects.create(name="admin")
        initial_version = role.version

        RolePermission.objects.create(
            role=role, app_label="books", model_name="testbook", action="read"
        )

        role.refresh_from_db()
        self.assertGreater(role.version, initial_version)


@override_settings(TURBODRF_PERMISSION_MODE="database")
class TestPermissionSnapshot(TestCase):
    """Test permission snapshot functionality."""

    def setUp(self):
        """Set up test roles and permissions."""
        from django.db import models

        # Create a test model dynamically
        class TestBook(models.Model):
            title = models.CharField(max_length=100)
            price = models.DecimalField(max_digits=10, decimal_places=2)
            author = models.CharField(max_length=100)

            class Meta:
                app_label = "books"
                db_table = "test_book"

            @classmethod
            def turbodrf(cls):
                return {"fields": ["title", "price", "author"]}

        self.TestBook = TestBook

        # Create viewer role with limited permissions
        self.viewer_role = TurboDRFRole.objects.create(name="viewer")
        RolePermission.objects.create(
            role=self.viewer_role,
            app_label="books",
            model_name="testbook",
            action="read",
        )
        RolePermission.objects.create(
            role=self.viewer_role,
            app_label="books",
            model_name="testbook",
            field_name="price",
            permission_type="read",
        )

        # Create editor role with full permissions
        self.editor_role = TurboDRFRole.objects.create(name="editor")
        RolePermission.objects.create(
            role=self.editor_role,
            app_label="books",
            model_name="testbook",
            action="read",
        )
        RolePermission.objects.create(
            role=self.editor_role,
            app_label="books",
            model_name="testbook",
            action="update",
        )
        RolePermission.objects.create(
            role=self.editor_role,
            app_label="books",
            model_name="testbook",
            field_name="price",
            permission_type="read",
        )
        RolePermission.objects.create(
            role=self.editor_role,
            app_label="books",
            model_name="testbook",
            field_name="price",
            permission_type="write",
        )

        # Create users
        self.viewer_user = User.objects.create_user(username="viewer")
        UserRole.objects.create(user=self.viewer_user, role=self.viewer_role)

        self.editor_user = User.objects.create_user(username="editor")
        UserRole.objects.create(user=self.editor_user, role=self.editor_role)

    def test_viewer_can_read(self):
        """Test that viewer can perform read action."""
        snapshot = build_permission_snapshot(self.viewer_user, self.TestBook)
        self.assertTrue(snapshot.can_perform_action("read"))
        self.assertFalse(snapshot.can_perform_action("update"))

    def test_editor_can_update(self):
        """Test that editor can perform update action."""
        snapshot = build_permission_snapshot(self.editor_user, self.TestBook)
        self.assertTrue(snapshot.can_perform_action("read"))
        self.assertTrue(snapshot.can_perform_action("update"))

    def test_field_level_read_restrictions(self):
        """Test that field-level read permissions work."""
        snapshot = build_permission_snapshot(self.viewer_user, self.TestBook)

        # Price has explicit read permission
        self.assertTrue(snapshot.has_read_rule("price"))
        self.assertTrue(snapshot.can_read_field("price"))

        # Title and author fall back to model-level read permission
        self.assertFalse(snapshot.has_read_rule("title"))
        self.assertTrue(snapshot.can_read_field("title"))

    def test_field_level_write_restrictions(self):
        """Test that field-level write permissions work."""
        viewer_snapshot = build_permission_snapshot(self.viewer_user, self.TestBook)
        editor_snapshot = build_permission_snapshot(self.editor_user, self.TestBook)

        # Viewer has read but not write permission on price
        self.assertTrue(viewer_snapshot.can_read_field("price"))
        self.assertFalse(viewer_snapshot.can_write_field("price"))

        # Editor has both read and write permission on price
        self.assertTrue(editor_snapshot.can_read_field("price"))
        self.assertTrue(editor_snapshot.can_write_field("price"))

    def test_snapshot_reuse_within_request(self):
        """Test that snapshots are reused within the same request."""
        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.viewer_user

        # First call creates snapshot
        snapshot1 = attach_snapshot_to_request(request, self.TestBook)

        # Second call should return the same snapshot
        snapshot2 = attach_snapshot_to_request(request, self.TestBook)

        # Verify it's the same object
        self.assertIs(snapshot1, snapshot2)

        # Verify snapshot is stored on request
        cached_snapshot = get_snapshot_from_request(request, self.TestBook)
        self.assertIsNotNone(cached_snapshot)
        self.assertIs(cached_snapshot, snapshot1)

    @override_settings(TURBODRF_PERMISSION_CACHE_TIMEOUT=60)
    def test_snapshot_caching(self):
        """Test that snapshots are cached correctly."""
        from django.core.cache import cache

        # Clear cache
        cache.clear()

        # Build snapshot (should cache it)
        snapshot1 = build_permission_snapshot(
            self.viewer_user, self.TestBook, use_cache=True
        )

        # Build again (should use cache)
        snapshot2 = build_permission_snapshot(
            self.viewer_user, self.TestBook, use_cache=True
        )

        # Verify both have same permissions (cached)
        self.assertEqual(snapshot1.allowed_actions, snapshot2.allowed_actions)
        self.assertEqual(snapshot1.readable_fields, snapshot2.readable_fields)

    def test_cache_invalidation_on_permission_change(self):
        """Test that cache is invalidated when permissions change."""
        from django.core.cache import cache

        # Clear cache
        cache.clear()

        # Build initial snapshot
        snapshot1 = build_permission_snapshot(
            self.viewer_user, self.TestBook, use_cache=True
        )
        self.assertFalse(snapshot1.can_perform_action("update"))

        # Add update permission to viewer role
        # This should increment the role version
        RolePermission.objects.create(
            role=self.viewer_role,
            app_label="books",
            model_name="testbook",  # Match the actual model_name
            action="update",
        )

        # Build new snapshot (should not use cached version due to version change)
        snapshot2 = build_permission_snapshot(
            self.viewer_user, self.TestBook, use_cache=True
        )
        self.assertTrue(snapshot2.can_perform_action("update"))


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestDatabasePermissionsIntegration(TestCase):
    """Integration tests for database permissions with viewsets."""

    def setUp(self):
        """Set up test environment."""
        # Create test role and permissions
        self.viewer_role = TurboDRFRole.objects.create(name="viewer")
        RolePermission.objects.create(
            role=self.viewer_role,
            app_label="testapp",
            model_name="testmodel",
            action="read",
        )

        # Create user with role
        self.user = User.objects.create_user(username="testviewer")
        UserRole.objects.create(user=self.user, role=self.viewer_role)

        self.client = APIClient()

    def test_model_level_permission_check(self):
        """Test that model-level permissions are enforced."""
        from turbodrf.backends import build_permission_snapshot

        # Create a mock model
        class MockModel:
            class _meta:
                app_label = "testapp"
                model_name = "testmodel"

        snapshot = build_permission_snapshot(self.user, MockModel)
        self.assertTrue(snapshot.can_perform_action("read"))
        self.assertFalse(snapshot.can_perform_action("create"))
        self.assertFalse(snapshot.can_perform_action("update"))
        self.assertFalse(snapshot.can_perform_action("delete"))


@override_settings(TURBODRF_PERMISSION_MODE="database")
class TestStaticPermissionBackwardCompatibility(TestCase):
    """Test that static permissions still work when not in database mode."""

    @override_settings(
        TURBODRF_PERMISSION_MODE="static",
        TURBODRF_ROLES={
            "admin": ["books.book.read", "books.book.create", "books.book.price.write"],
            "viewer": ["books.book.read"],
        },
    )
    def test_static_mode_still_works(self):
        """Test that static permission mode still works correctly."""
        from turbodrf.backends import build_permission_snapshot

        # Create a mock user with roles
        class MockUser:
            roles = ["admin"]
            is_authenticated = True

        class MockModel:
            class _meta:
                app_label = "books"
                model_name = "book"
                fields = []

        user = MockUser()
        snapshot = build_permission_snapshot(user, MockModel)

        self.assertTrue(snapshot.can_perform_action("read"))
        self.assertTrue(snapshot.can_perform_action("create"))
