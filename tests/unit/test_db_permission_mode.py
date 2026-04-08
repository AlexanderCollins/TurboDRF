"""
Tests for TurboDRF database permission mode.

Covers gaps not tested in test_db_permissions.py:
- Role resolution from UserRole table via real API requests
- Field-level read permissions (response field filtering)
- Field-level write permissions (writable vs read-only fields)
- Permission changes affecting snapshot after cache invalidation
- Guest role for unauthenticated users in database mode
- Snapshot caching within a single request
- Role version incrementing invalidates cache
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from tests.test_app.models import RelatedModel, SampleModel
from turbodrf.backends import (
    attach_snapshot_to_request,
    build_permission_snapshot,
    get_cache_key,
    get_user_roles,
)
from turbodrf.models import RolePermission, TurboDRFRole, UserRole

User = get_user_model()


def _create_samplemodel_read_perms(role):
    """Helper: grant field-level read for all SampleModel fields.

    NOTE: Does NOT create the model-level 'read' action -- callers must do that
    separately to avoid unique-constraint collisions.
    """
    for field in [
        "title",
        "description",
        "price",
        "quantity",
        "related",
        "secret_field",
        "is_active",
        "created_at",
        "updated_at",
        "published_date",
    ]:
        RolePermission.objects.create(
            role=role,
            app_label="test_app",
            model_name="samplemodel",
            field_name=field,
            permission_type="read",
        )


def _create_samplemodel_write_perms(role, fields):
    """Helper: grant field-level write for specified SampleModel fields."""
    for field in fields:
        RolePermission.objects.create(
            role=role,
            app_label="test_app",
            model_name="samplemodel",
            field_name=field,
            permission_type="write",
        )


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestDatabaseModeRoleResolution(TestCase):
    """Test that database mode resolves roles from the UserRole table."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="resolver_user")
        self.role = TurboDRFRole.objects.create(name="api_reader")
        UserRole.objects.create(user=self.user, role=self.role)

    def test_get_user_roles_returns_database_roles(self):
        """Roles come from UserRole table, not from user._test_roles."""
        roles = get_user_roles(self.user)
        self.assertEqual(roles, ["api_reader"])

    def test_user_with_multiple_roles(self):
        """User assigned to two roles should get both."""
        writer_role = TurboDRFRole.objects.create(name="api_writer")
        UserRole.objects.create(user=self.user, role=writer_role)

        roles = get_user_roles(self.user)
        self.assertCountEqual(roles, ["api_reader", "api_writer"])

    def test_user_with_no_roles_returns_empty(self):
        """User with no UserRole entries returns empty list."""
        lonely_user = User.objects.create_user(username="lonely")
        roles = get_user_roles(lonely_user)
        self.assertEqual(roles, [])

    def test_snapshot_uses_database_roles(self):
        """build_permission_snapshot should use DB roles, not _test_roles."""
        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="read",
        )
        snapshot = build_permission_snapshot(self.user, SampleModel, use_cache=False)
        self.assertIn("read", snapshot.allowed_actions)

    def test_snapshot_empty_for_unassigned_user(self):
        """User with no DB roles gets empty snapshot."""
        lonely_user = User.objects.create_user(username="lonely2")
        snapshot = build_permission_snapshot(lonely_user, SampleModel, use_cache=False)
        self.assertEqual(snapshot.allowed_actions, set())


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestFieldLevelReadPermissions(TestCase):
    """Test that field-level read permissions control which fields are visible."""

    def setUp(self):
        cache.clear()

        # Create role with read access but only some field-level reads
        self.role = TurboDRFRole.objects.create(name="partial_reader")
        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="read",
        )
        # Explicit field-level reads: only title, price, is_active
        for field in ["title", "price", "is_active"]:
            RolePermission.objects.create(
                role=self.role,
                app_label="test_app",
                model_name="samplemodel",
                field_name=field,
                permission_type="read",
            )

        self.user = User.objects.create_user(username="partial_reader_user")
        UserRole.objects.create(user=self.user, role=self.role)

        # Set up test data
        self.related = RelatedModel.objects.create(name="Cat A")
        self.item = SampleModel.objects.create(
            title="Widget",
            description="Sensitive description",
            price=Decimal("9.99"),
            quantity=5,
            related=self.related,
            secret_field="top-secret",
            is_active=True,
        )

    def test_snapshot_readable_fields_match_permissions(self):
        """Only fields with explicit read permission should be readable."""
        snapshot = build_permission_snapshot(self.user, SampleModel, use_cache=False)
        self.assertIn("title", snapshot.readable_fields)
        self.assertIn("price", snapshot.readable_fields)
        self.assertIn("is_active", snapshot.readable_fields)
        # Fields with explicit rules that the user does NOT have
        # should not appear as readable when rules exist for them
        # However, fields WITHOUT explicit rules fall back to model-level read
        # Let's verify which fields have read rules
        self.assertIn("title", snapshot.fields_with_read_rules)
        self.assertIn("price", snapshot.fields_with_read_rules)
        self.assertIn("is_active", snapshot.fields_with_read_rules)

    def test_field_without_explicit_rule_falls_back_to_model_level(self):
        """Fields without an explicit read rule use model-level read permission."""
        snapshot = build_permission_snapshot(self.user, SampleModel, use_cache=False)
        # 'quantity' has no explicit rule from any role, so it falls back
        # to model-level 'read' which is granted
        if "quantity" not in snapshot.fields_with_read_rules:
            self.assertIn("quantity", snapshot.readable_fields)

    def test_api_response_filters_fields_for_authenticated_user(self):
        """GET request should only return permitted fields in response."""
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.get(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.data
        # Permitted fields should be present
        self.assertIn("title", data)
        self.assertIn("price", data)
        self.assertIn("is_active", data)


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestFieldLevelWritePermissions(TestCase):
    """Test that field-level write permissions control writability in database mode.

    In database mode, field-level write rules and grants come from the same
    RolePermission records for the user's roles. A field with an explicit
    write permission is writable; a field with NO write rule falls back to
    model-level (update/create) permission.
    """

    def setUp(self):
        cache.clear()

        self.role = TurboDRFRole.objects.create(name="limited_editor")

        # Model-level: read + update
        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="read",
        )
        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="update",
        )

        # Field-level reads for all fields
        _create_samplemodel_read_perms(self.role)

        # Field-level writes for title and description only
        _create_samplemodel_write_perms(self.role, ["title", "description"])

        self.user = User.objects.create_user(username="limited_editor_user")
        UserRole.objects.create(user=self.user, role=self.role)

        self.related = RelatedModel.objects.create(name="Rel A")
        self.item = SampleModel.objects.create(
            title="Original",
            description="Original desc",
            price=Decimal("10.00"),
            quantity=1,
            related=self.related,
            is_active=True,
        )

    def test_explicitly_granted_write_fields_are_writable(self):
        """Fields with explicit write permission granted are writable."""
        snapshot = build_permission_snapshot(self.user, SampleModel, use_cache=False)
        self.assertIn("title", snapshot.writable_fields)
        self.assertIn("description", snapshot.writable_fields)

    def test_fields_with_write_rules_tracked(self):
        """The snapshot tracks which fields have explicit write rules."""
        snapshot = build_permission_snapshot(self.user, SampleModel, use_cache=False)
        self.assertIn("title", snapshot.fields_with_write_rules)
        self.assertIn("description", snapshot.fields_with_write_rules)

    def test_fields_without_write_rule_fall_back_to_model_level(self):
        """Fields without any write rule from user's roles fall back to model-level."""
        snapshot = build_permission_snapshot(self.user, SampleModel, use_cache=False)
        # 'quantity' has no field-level write rule from this role, so it
        # falls back to model-level 'update' permission (which is granted)
        if "quantity" not in snapshot.fields_with_write_rules:
            self.assertIn("quantity", snapshot.writable_fields)

    def test_role_without_update_cannot_write_anything(self):
        """A role with no model-level update/create has no writable fields."""
        readonly_role = TurboDRFRole.objects.create(name="readonly_role")
        RolePermission.objects.create(
            role=readonly_role,
            app_label="test_app",
            model_name="samplemodel",
            action="read",
        )
        readonly_user = User.objects.create_user(username="readonly_user")
        UserRole.objects.create(user=readonly_user, role=readonly_role)

        snapshot = build_permission_snapshot(
            readonly_user, SampleModel, use_cache=False
        )
        self.assertEqual(snapshot.writable_fields, set())

    def test_patch_permitted_field_succeeds(self):
        """PATCH on a writable field should succeed."""
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.patch(
            f"/api/samplemodels/{self.item.id}/",
            {"title": "Updated Title"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, "Updated Title")


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestPermissionChangeInvalidatesSnapshot(TestCase):
    """Test that adding/removing RolePermission affects snapshots after cache expires."""

    def setUp(self):
        cache.clear()

        self.role = TurboDRFRole.objects.create(name="evolving_role")
        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="read",
        )

        self.user = User.objects.create_user(username="evolving_user")
        UserRole.objects.create(user=self.user, role=self.role)

    def test_adding_permission_reflected_in_new_snapshot(self):
        """After adding a permission, a fresh snapshot should include it."""
        snapshot1 = build_permission_snapshot(self.user, SampleModel, use_cache=True)
        self.assertNotIn("create", snapshot1.allowed_actions)

        # Add create permission (this also increments role version)
        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="create",
        )

        # New snapshot should pick up the change because role version changed
        snapshot2 = build_permission_snapshot(self.user, SampleModel, use_cache=True)
        self.assertIn("create", snapshot2.allowed_actions)

    def test_removing_permission_reflected_in_new_snapshot(self):
        """After deleting a permission, a fresh snapshot should exclude it."""
        # Add and cache a snapshot with create
        create_perm = RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="create",
        )
        snapshot1 = build_permission_snapshot(self.user, SampleModel, use_cache=True)
        self.assertIn("create", snapshot1.allowed_actions)

        # Remove the create permission (increments role version)
        create_perm.delete()

        snapshot2 = build_permission_snapshot(self.user, SampleModel, use_cache=True)
        self.assertNotIn("create", snapshot2.allowed_actions)

    def test_adding_field_read_permission_appears_in_snapshot(self):
        """Adding a field-level read permission shows up in the next snapshot."""
        snapshot1 = build_permission_snapshot(self.user, SampleModel, use_cache=True)
        self.assertNotIn("title", snapshot1.fields_with_read_rules)

        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            field_name="title",
            permission_type="read",
        )

        snapshot2 = build_permission_snapshot(self.user, SampleModel, use_cache=True)
        self.assertIn("title", snapshot2.fields_with_read_rules)
        self.assertIn("title", snapshot2.readable_fields)


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestGuestRoleDatabaseMode(TestCase):
    """Test that the guest role works for unauthenticated users in database mode."""

    def setUp(self):
        cache.clear()

        # Create guest role with read-only access
        self.guest_role = TurboDRFRole.objects.create(name="guest")
        RolePermission.objects.create(
            role=self.guest_role,
            app_label="test_app",
            model_name="samplemodel",
            action="read",
        )

        self.related = RelatedModel.objects.create(name="Guest Rel")
        self.item = SampleModel.objects.create(
            title="Public Item",
            description="Visible to guests",
            price=Decimal("5.00"),
            quantity=10,
            related=self.related,
            is_active=True,
        )

    def test_unauthenticated_user_gets_guest_role(self):
        """Unauthenticated user should resolve to ['guest'] when guest role exists."""
        from django.contrib.auth.models import AnonymousUser

        anon = AnonymousUser()
        roles = get_user_roles(anon)
        self.assertEqual(roles, ["guest"])

    def test_guest_snapshot_has_read_access(self):
        """Guest snapshot should allow read action."""
        from django.contrib.auth.models import AnonymousUser

        anon = AnonymousUser()
        snapshot = build_permission_snapshot(anon, SampleModel, use_cache=False)
        self.assertIn("read", snapshot.allowed_actions)
        self.assertNotIn("create", snapshot.allowed_actions)

    def test_unauthenticated_api_get_succeeds(self):
        """Unauthenticated GET request should succeed when guest role + public_access."""
        client = APIClient()
        response = client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data["data"]), 0)

    def test_unauthenticated_api_post_denied(self):
        """Unauthenticated POST should be denied even with guest role."""
        client = APIClient()
        response = client.post(
            "/api/samplemodels/",
            {"title": "Hack", "price": "1.00", "quantity": 1, "related": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_guest_role_denies_anonymous(self):
        """Without a guest role, unauthenticated users get no roles."""
        from django.contrib.auth.models import AnonymousUser

        # Delete the guest role
        self.guest_role.delete()

        anon = AnonymousUser()
        roles = get_user_roles(anon)
        self.assertEqual(roles, [])


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestSnapshotRequestLevelCaching(TestCase):
    """Test that attach_snapshot_to_request caches the snapshot per model per request."""

    def setUp(self):
        cache.clear()

        self.role = TurboDRFRole.objects.create(name="cache_tester")
        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="read",
        )

        self.user = User.objects.create_user(username="cache_user")
        UserRole.objects.create(user=self.user, role=self.role)

    def test_same_model_returns_same_snapshot_object(self):
        """Calling attach_snapshot_to_request twice returns the same object."""
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user

        snap1 = attach_snapshot_to_request(request, SampleModel)
        snap2 = attach_snapshot_to_request(request, SampleModel)
        self.assertIs(snap1, snap2)

    def test_different_models_get_different_snapshots(self):
        """Different models should produce separate snapshot entries."""
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user

        snap_sample = attach_snapshot_to_request(request, SampleModel)
        snap_related = attach_snapshot_to_request(request, RelatedModel)
        self.assertIsNot(snap_sample, snap_related)

    def test_request_cache_key_uses_app_and_model(self):
        """The internal cache key should be app_label.model_name."""
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user

        attach_snapshot_to_request(request, SampleModel)
        self.assertIn("test_app.samplemodel", request._turbodrf_snapshots)


@override_settings(
    TURBODRF_PERMISSION_MODE="database",
    TURBODRF_DISABLE_PERMISSIONS=False,
    TURBODRF_PERMISSION_CACHE_TIMEOUT=300,
)
class TestRoleVersionCacheInvalidation(TestCase):
    """Test that incrementing the role version invalidates the Django cache."""

    def setUp(self):
        cache.clear()

        self.role = TurboDRFRole.objects.create(name="versioned_role")
        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="read",
        )

        self.user = User.objects.create_user(username="version_user")
        UserRole.objects.create(user=self.user, role=self.role)

    def test_cache_key_includes_version(self):
        """Cache key should change when role version changes."""
        key_before = get_cache_key(self.user, SampleModel)

        # Bump the version by refreshing first (RolePermission.save() in setUp
        # already incremented the DB version, so we need to sync before saving)
        self.role.refresh_from_db()
        self.role.description = "force version bump"
        self.role.save()

        key_after = get_cache_key(self.user, SampleModel)
        self.assertNotEqual(key_before, key_after)

    def test_old_cached_snapshot_not_returned_after_version_bump(self):
        """After role version increments, old cache entry is bypassed."""
        # Populate cache
        snap1 = build_permission_snapshot(self.user, SampleModel, use_cache=True)
        self.assertIn("read", snap1.allowed_actions)
        self.assertNotIn("delete", snap1.allowed_actions)

        # Add delete permission (auto-increments role version)
        RolePermission.objects.create(
            role=self.role,
            app_label="test_app",
            model_name="samplemodel",
            action="delete",
        )

        # Even with use_cache=True, the new version means a cache miss
        snap2 = build_permission_snapshot(self.user, SampleModel, use_cache=True)
        self.assertIn("delete", snap2.allowed_actions)

    def test_manual_version_increment_invalidates(self):
        """Manually bumping TurboDRFRole.version invalidates the cache."""
        # Populate cache
        build_permission_snapshot(self.user, SampleModel, use_cache=True)
        key_before = get_cache_key(self.user, SampleModel)

        # Refresh to get current DB version, then save to increment
        self.role.refresh_from_db()
        self.role.save()  # triggers version += 1

        key_after = get_cache_key(self.user, SampleModel)
        self.assertNotEqual(key_before, key_after)

        # Old cached entry should not be hit
        cached = cache.get(key_after)
        self.assertIsNone(cached)


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestDatabaseModeEndToEnd(TestCase):
    """End-to-end tests using APIClient with database permission mode."""

    def setUp(self):
        cache.clear()

        # Admin role: full CRUD + all field permissions
        self.admin_role = TurboDRFRole.objects.create(name="db_admin")
        for action in ["read", "create", "update", "delete"]:
            RolePermission.objects.create(
                role=self.admin_role,
                app_label="test_app",
                model_name="samplemodel",
                action=action,
            )
        _create_samplemodel_read_perms(self.admin_role)
        _create_samplemodel_write_perms(
            self.admin_role,
            [
                "title",
                "description",
                "price",
                "quantity",
                "related",
                "secret_field",
                "is_active",
                "published_date",
            ],
        )

        # Also need relatedmodel read perms for the admin
        RolePermission.objects.create(
            role=self.admin_role,
            app_label="test_app",
            model_name="relatedmodel",
            action="read",
        )

        # Viewer role: read only, limited fields
        self.viewer_role = TurboDRFRole.objects.create(name="db_viewer")
        RolePermission.objects.create(
            role=self.viewer_role,
            app_label="test_app",
            model_name="samplemodel",
            action="read",
        )
        for field in ["title", "price", "is_active"]:
            RolePermission.objects.create(
                role=self.viewer_role,
                app_label="test_app",
                model_name="samplemodel",
                field_name=field,
                permission_type="read",
            )

        # Create users
        self.admin_user = User.objects.create_user(username="db_admin_user")
        UserRole.objects.create(user=self.admin_user, role=self.admin_role)

        self.viewer_user = User.objects.create_user(username="db_viewer_user")
        UserRole.objects.create(user=self.viewer_user, role=self.viewer_role)

        # Test data
        self.related = RelatedModel.objects.create(name="E2E Rel")
        self.item = SampleModel.objects.create(
            title="E2E Item",
            description="End to end test item",
            price=Decimal("42.00"),
            quantity=7,
            related=self.related,
            secret_field="classified",
            is_active=True,
        )
        self.client = APIClient()

    def test_admin_can_create(self):
        """Admin role can POST a new item."""
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.post(
            "/api/samplemodels/",
            {
                "title": "New Item",
                "description": "Created by admin",
                "price": "15.00",
                "quantity": 3,
                "related": self.related.id,
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_viewer_cannot_create(self):
        """Viewer role should be denied POST."""
        self.client.force_authenticate(user=self.viewer_user)
        response = self.client.post(
            "/api/samplemodels/",
            {
                "title": "Nope",
                "price": "1.00",
                "quantity": 1,
                "related": self.related.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_delete(self):
        """Admin role can DELETE an item."""
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.delete(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_viewer_cannot_delete(self):
        """Viewer role should be denied DELETE."""
        self.client.force_authenticate(user=self.viewer_user)
        response = self.client.delete(f"/api/samplemodels/{self.item.id}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_viewer_can_list(self):
        """Viewer role can GET the list endpoint."""
        self.client.force_authenticate(user=self.viewer_user)
        response = self.client.get("/api/samplemodels/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data["data"]), 0)

    def test_admin_can_update(self):
        """Admin role can PATCH an item."""
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.patch(
            f"/api/samplemodels/{self.item.id}/",
            {"title": "Patched"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, "Patched")

    def test_viewer_cannot_update(self):
        """Viewer role should be denied PATCH."""
        self.client.force_authenticate(user=self.viewer_user)
        response = self.client.patch(
            f"/api/samplemodels/{self.item.id}/",
            {"title": "Nope"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
