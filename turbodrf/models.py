"""
Database models for TurboDRF dynamic role-based permissions.

This module provides database-backed permission storage that allows
runtime permission changes without code deployment.
"""

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models


def _get_permission_check_constraint():
    """
    Get CheckConstraint with correct parameter name for Django version.

    Django 5.0 changed the parameter from 'check' to 'condition'.
    Uses try/except for maximum compatibility across Django versions.
    """
    constraint_q = models.Q(
        action__isnull=False,
        field_name__isnull=True,
        permission_type__isnull=True,
    ) | models.Q(
        action__isnull=True,
        field_name__isnull=False,
        permission_type__isnull=False,
    )

    # Try Django 5.0+ syntax first (condition)
    try:
        return models.CheckConstraint(
            condition=constraint_q,
            name="permission_type_check",
        )
    except TypeError:
        # Fall back to Django 3.2-4.2 syntax (check)
        return models.CheckConstraint(
            check=constraint_q,
            name="permission_type_check",
        )


class TurboDRFRole(models.Model):
    """
    A role represents a collection of permissions that can be assigned to users.

    Roles can be linked to Django Groups for seamless integration with
    existing Django authentication systems.

    Features:
        - Unique role names
        - Optional Django Group integration
        - Automatic timestamp tracking for cache invalidation
        - Description for documentation

    Example:
        # Create a role
        admin_role = TurboDRFRole.objects.create(
            name='admin',
            description='Full access to all resources'
        )

        # Link to Django Group
        admin_group = Group.objects.get(name='Administrators')
        admin_role.django_group = admin_group
        admin_role.save()
    """

    name = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Unique role identifier (e.g., 'admin', 'editor', 'viewer')",
    )

    description = models.TextField(
        blank=True, help_text="Human-readable description of this role's purpose"
    )

    django_group = models.OneToOneField(
        Group,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="turbodrf_role",
        help_text="Optional link to Django Group for integration",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(
        auto_now=True, db_index=True, help_text="Used for cache invalidation"
    )

    # Version counter for cache invalidation
    version = models.IntegerField(
        default=1, help_text="Incremented on each update for cache invalidation"
    )

    class Meta:
        db_table = "turbodrf_role"
        ordering = ["name"]
        verbose_name = "TurboDRF Role"
        verbose_name_plural = "TurboDRF Roles"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """Increment version on save for cache invalidation."""
        if self.pk:  # Only increment for updates, not creates
            self.version += 1
        super().save(*args, **kwargs)


class RolePermission(models.Model):
    """
    A permission grants specific access rights to a role.

    Supports both model-level permissions (read, create, update, delete)
    and optional field-level permissions (field read/write).

    Permission Format:
        Model-level: app_label.model_name.action
        - action: 'read', 'create', 'update', 'delete'

        Field-level: app_label.model_name.field_name.permission_type
        - permission_type: 'read', 'write'

    Examples:
        # Model-level permissions
        RolePermission.objects.create(
            role=admin_role,
            app_label='books',
            model_name='book',
            action='read'
        )

        # Field-level permissions
        RolePermission.objects.create(
            role=editor_role,
            app_label='books',
            model_name='book',
            field_name='price',
            permission_type='read'
        )
    """

    # Permission types
    ACTION_READ = "read"
    ACTION_CREATE = "create"
    ACTION_UPDATE = "update"
    ACTION_DELETE = "delete"

    ACTION_CHOICES = [
        (ACTION_READ, "Read"),
        (ACTION_CREATE, "Create"),
        (ACTION_UPDATE, "Update"),
        (ACTION_DELETE, "Delete"),
    ]

    PERMISSION_TYPE_READ = "read"
    PERMISSION_TYPE_WRITE = "write"

    PERMISSION_TYPE_CHOICES = [
        (PERMISSION_TYPE_READ, "Read"),
        (PERMISSION_TYPE_WRITE, "Write"),
    ]

    role = models.ForeignKey(
        TurboDRFRole,
        on_delete=models.CASCADE,
        related_name="permissions",
        help_text="The role this permission belongs to",
    )

    app_label = models.CharField(
        max_length=100, db_index=True, help_text="Django app label (e.g., 'books')"
    )

    model_name = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Model name in lowercase (e.g., 'book')",
    )

    # Model-level permission
    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES,
        null=True,
        blank=True,
        help_text="Model-level action (leave blank for field-level permissions)",
    )

    # Field-level permission
    field_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Field name (only for field-level permissions)",
    )

    permission_type = models.CharField(
        max_length=20,
        choices=PERMISSION_TYPE_CHOICES,
        null=True,
        blank=True,
        help_text="Permission type for field-level permissions",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(
        auto_now=True, help_text="Used for cache invalidation"
    )

    class Meta:
        db_table = "turbodrf_permission"
        ordering = ["role", "app_label", "model_name", "field_name"]
        verbose_name = "TurboDRF Permission"
        verbose_name_plural = "TurboDRF Permissions"

        # Ensure unique permissions per role
        constraints = [
            # Model-level permission uniqueness
            models.UniqueConstraint(
                fields=["role", "app_label", "model_name", "action"],
                condition=models.Q(field_name__isnull=True),
                name="unique_model_permission",
            ),
            # Field-level permission uniqueness
            models.UniqueConstraint(
                fields=[
                    "role",
                    "app_label",
                    "model_name",
                    "field_name",
                    "permission_type",
                ],
                condition=models.Q(field_name__isnull=False),
                name="unique_field_permission",
            ),
            # Check constraint: Either action OR (field_name + permission_type)
            # Django 5.0 changed parameter from 'check' to 'condition'
            _get_permission_check_constraint(),
        ]

    def __str__(self):
        if self.field_name:
            return (
                f"{self.role.name}: {self.app_label}.{self.model_name}."
                f"{self.field_name}.{self.permission_type}"
            )
        else:
            return f"{self.role.name}: {self.app_label}.{self.model_name}.{self.action}"

    def to_permission_string(self):
        """Convert to TurboDRF permission string format."""
        if self.field_name:
            return (
                f"{self.app_label}.{self.model_name}."
                f"{self.field_name}.{self.permission_type}"
            )
        else:
            return f"{self.app_label}.{self.model_name}.{self.action}"

    def save(self, *args, **kwargs):
        """Update role version on permission change for cache invalidation."""
        super().save(*args, **kwargs)
        # Increment role version to invalidate caches
        if self.role:
            TurboDRFRole.objects.filter(pk=self.role.pk).update(
                version=models.F("version") + 1, updated_at=models.functions.Now()
            )

    def delete(self, *args, **kwargs):
        """Update role version on permission deletion for cache invalidation."""
        role_pk = self.role.pk if self.role else None
        result = super().delete(*args, **kwargs)
        # Increment role version to invalidate caches
        if role_pk:
            TurboDRFRole.objects.filter(pk=role_pk).update(
                version=models.F("version") + 1, updated_at=models.functions.Now()
            )
        return result


class UserRole(models.Model):
    """
    Links users to roles for database-backed permission assignment.

    This provides an alternative to adding a 'roles' property to the User model,
    allowing roles to be assigned via the database.

    Example:
        # Assign roles to a user
        UserRole.objects.create(user=user, role=admin_role)
        UserRole.objects.create(user=user, role=editor_role)

        # Get user roles
        user_roles = UserRole.objects.filter(user=user).values_list(
            "role__name", flat=True
        )
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="turbodrf_roles",
    )

    role = models.ForeignKey(
        TurboDRFRole, on_delete=models.CASCADE, related_name="user_assignments"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "turbodrf_user_role"
        unique_together = [["user", "role"]]
        verbose_name = "User Role Assignment"
        verbose_name_plural = "User Role Assignments"

    def __str__(self):
        return f"{self.user} -> {self.role.name}"
