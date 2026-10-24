"""
Database models for TurboDRF dynamic role-based permissions.

This module provides database-backed permission storage that allows
runtime permission changes without code deployment.
"""

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models, transaction


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
    """A named role; collection of `RolePermission` rows. Optionally links to
    a Django Group for integration with existing auth setups. The `version`
    counter is bumped on update for permission-snapshot cache invalidation.
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
        """Increment version on save for cache invalidation.

        An update bumps the stored version in the database (``F() + 1``, as
        ``RolePermission`` does) and never writes the in-memory one: a stale
        instance would move the version backwards, and permission-cache keys
        built from (name, version) would bring back a revoked permission's
        cached snapshot."""
        if self._state.adding and (
            self.pk is None
            or kwargs.get("force_insert")
            or not type(self)._base_manager.filter(pk=self.pk).exists()
        ):
            super().save(*args, **kwargs)
            return
        update_fields = kwargs.get("update_fields")
        if update_fields is None:
            update_fields = [
                f.name
                for f in self._meta.concrete_fields
                if not f.primary_key and f.name != "version"
            ]
        kwargs["update_fields"] = [f for f in update_fields if f != "version"]
        with transaction.atomic(using=kwargs.get("using")):
            super().save(*args, **kwargs)
            type(self)._base_manager.filter(pk=self.pk).update(
                version=models.F("version") + 1
            )
        self.refresh_from_db(fields=["version"])


class RolePermission(models.Model):
    """A single permission granted to a `TurboDRFRole`. Supports model-level
    actions (read/create/update/delete) and field-level rules (read/write).
    The CheckConstraint enforces that exactly one form is set per row.
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

    def clean(self):
        """Reject a rule that can't match anything.

        Rules are matched exactly against a model's ``app_label`` and
        ``model_name`` and a concrete or M2M field name, as in static mode,
        whose boot gate rejects the same strings. A field rule that matches
        nothing fails open: the field it was meant to restrict stays readable
        and writable. A model that isn't loaded is accepted only with
        ``TURBODRF_ALLOW_UNKNOWN_PERMISSIONS = True``, as in static mode.
        """
        import difflib

        from django.apps import apps
        from django.core.exceptions import ValidationError

        errors = {}
        loaded = [m for models_ in apps.all_models.values() for m in models_.values()]
        model = next(
            (
                m
                for m in loaded
                if (m._meta.app_label, m._meta.model_name)
                == (self.app_label, self.model_name)
            ),
            None,
        )
        if model is None:
            wanted = (str(self.app_label).lower(), str(self.model_name).lower())
            match = next(
                (
                    m
                    for m in loaded
                    if (m._meta.app_label.lower(), m._meta.model_name) == wanted
                ),
                None,
            )
            if match is not None:
                errors["model_name"] = (
                    f"Write the model as {match._meta.app_label}."
                    f"{match._meta.model_name}: permission rules are matched "
                    f"exactly."
                )
            elif not getattr(settings, "TURBODRF_ALLOW_UNKNOWN_PERMISSIONS", False):
                errors["model_name"] = (
                    f"No model {self.app_label}.{self.model_name} is installed "
                    f"(app label and lowercase model name)."
                )
        elif self.field_name is not None:
            # Field rules apply to concrete and M2M fields, by name (not an
            # FK's attname such as "related_id").
            names = {f.name for f in model._meta.fields} | {
                f.name for f in model._meta.many_to_many
            }
            if self.field_name not in names:
                close = difflib.get_close_matches(
                    self.field_name, sorted(names), n=3, cutoff=0.6
                )
                hint = f" Close matches: {close}." if close else ""
                errors["field_name"] = (
                    f"{model._meta.label} has no field {self.field_name!r}.{hint}"
                )
        if self.action is not None and self.action not in dict(self.ACTION_CHOICES):
            errors["action"] = (
                f"Action must be one of {sorted(dict(self.ACTION_CHOICES))}."
            )
        if self.permission_type is not None and self.permission_type not in dict(
            self.PERMISSION_TYPE_CHOICES
        ):
            errors["permission_type"] = (
                f"Permission type must be one of "
                f"{sorted(dict(self.PERMISSION_TYPE_CHOICES))}."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Validate the rule (see ``clean``), then update the role version on
        permission change for cache invalidation."""
        self.clean()
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
        verbose_name = "User Role Assignment"
        verbose_name_plural = "User Role Assignments"
        constraints = [
            models.UniqueConstraint(fields=("user", "role"), name="unique_user_role"),
        ]

    def __str__(self):
        return f"{self.user} -> {self.role.name}"
