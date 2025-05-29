"""
Role-based permissions for TurboDRF.

This module provides the permission classes that enforce role-based
access control for the auto-generated APIs.
"""

from rest_framework.permissions import BasePermission, DjangoModelPermissions


class TurboDRFPermission(BasePermission):
    """
    Permission class that checks against role-based permissions.

    This permission class implements a flexible role-based access control
    system for TurboDRF. It checks user roles against configured permissions
    to determine access to API endpoints.

    Permission Format:
        Permissions follow the pattern: app_label.model_name.action

        Model-level permissions:
        - 'books.book.read' - Can view books
        - 'books.book.create' - Can create books
        - 'books.book.update' - Can update books
        - 'books.book.delete' - Can delete books

        Field-level permissions (checked separately):
        - 'books.book.price.read' - Can view price field
        - 'books.book.price.write' - Can modify price field

    Configuration:
        Define roles and permissions in Django settings:

        TURBODRF_ROLES = {
            'admin': [
                'books.book.read',
                'books.book.create',
                'books.book.update',
                'books.book.delete',
                'books.book.price.read',
                'books.book.price.write',
            ],
            'editor': [
                'books.book.read',
                'books.book.update',
                'books.book.price.read',  # Read-only access to price
            ],
            'viewer': [
                'books.book.read',
                # No access to price field
            ]
        }

    Special Cases:
        - Unauthenticated users are allowed read-only access (GET requests)
        - Users must have a 'roles' property that returns a list of role names
    """

    def has_permission(self, request, view):
        """
        Check if the user has permission to perform the requested action.

        Args:
            request: The incoming HTTP request.
            view: The view being accessed.

        Returns:
            bool: True if permission is granted, False otherwise.
        """
        # Allow read access for unauthenticated users (viewer role)
        if not request.user or not request.user.is_authenticated:
            # Only allow GET and OPTIONS requests for unauthenticated users
            return request.method in ["GET", "OPTIONS"]

        if not hasattr(request.user, "roles"):
            return False

        user_permissions = self._get_user_permissions(request.user)
        model = view.model
        app_label = model._meta.app_label
        model_name = model._meta.model_name

        # Map HTTP methods to permission types
        permission_map = {
            "GET": "read",
            "POST": "create",
            "PUT": "update",
            "PATCH": "update",
            "DELETE": "delete",
            "OPTIONS": "read",  # OPTIONS should have same permission as read
        }

        permission_type = permission_map.get(request.method)
        if not permission_type:
            return False

        # Check model-level permission
        required_permission = f"{app_label}.{model_name}.{permission_type}"
        return required_permission in user_permissions

    def _get_user_permissions(self, user):
        """
        Get all permissions for a user based on their roles.

        This method aggregates permissions from all roles assigned to a user.

        Args:
            user: The user object, which must have a 'roles' property.

        Returns:
            set: Set of permission strings the user has.

        Example:
            If a user has roles ['admin', 'editor'], this method returns
            the union of all permissions defined for both roles.
        """
        from django.conf import settings

        from .settings import TURBODRF_ROLES as default_roles

        # Use Django settings if available, otherwise fall back to defaults
        TURBODRF_ROLES = getattr(settings, "TURBODRF_ROLES", default_roles)

        permissions = set()
        for role in user.roles:
            permissions.update(TURBODRF_ROLES.get(role, []))

        return permissions


class DefaultDjangoPermission(DjangoModelPermissions):
    """
    Default permission class using Django's built-in model permissions.

    This permission class provides a simpler alternative to TurboDRF's role-based
    permissions by using Django's standard permission system. When a user has
    write permission for a model, they have write access to all fields.

    Features:
        - Uses Django's built-in auth permissions (add, change, delete, view)
        - Integrates with Django admin permissions
        - No field-level permissions (all fields are accessible based on
          model permissions)
        - Works with Django's User and Group models out of the box

    Usage:
        To use Django's default permissions instead of TurboDRF's
        role-based system,
        set TURBODRF_USE_DEFAULT_PERMISSIONS = True in your settings.

    Permission Mapping:
        - GET: requires 'view' permission
        - POST: requires 'add' permission
        - PUT/PATCH: requires 'change' permission
        - DELETE: requires 'delete' permission
    """

    # Extend the default perms map to include OPTIONS
    perms_map = {
        "GET": ["%(app_label)s.view_%(model_name)s"],
        "OPTIONS": ["%(app_label)s.view_%(model_name)s"],
        "HEAD": ["%(app_label)s.view_%(model_name)s"],
        "POST": ["%(app_label)s.add_%(model_name)s"],
        "PUT": ["%(app_label)s.change_%(model_name)s"],
        "PATCH": ["%(app_label)s.change_%(model_name)s"],
        "DELETE": ["%(app_label)s.delete_%(model_name)s"],
    }

    def has_permission(self, request, view):
        """
        Check if the user has permission to perform the requested action.

        For unauthenticated users, allows read-only access by default.
        """
        # Allow read access for unauthenticated users
        if not request.user or not request.user.is_authenticated:
            return request.method in ["GET", "OPTIONS", "HEAD"]

        # For authenticated users, use Django's permission system
        return super().has_permission(request, view)
