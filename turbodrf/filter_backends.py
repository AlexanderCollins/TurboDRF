"""
Custom filter backends for TurboDRF.

This module provides advanced filtering capabilities beyond what
django-filter provides out of the box.
"""

from django.db.models import Q
from rest_framework.filters import BaseFilterBackend


class ORFilterBackend(BaseFilterBackend):
    """
    Filter backend that supports OR queries across multiple fields.

    This backend allows filtering with OR logic by using special
    query parameter syntax. Fields ending with '_or' will be combined
    using OR logic instead of AND.

    Usage:
        # Search for users where email OR private_email matches
        GET /api/users/?email_or=test@example.com&private_email_or=test@example.com

        # This will match users where:
        # email='test@example.com' OR private_email='test@example.com'

        # You can also use multiple OR groups:
        GET /api/users/?email_or=test@example.com&
            private_email_or=alt@example.com&name=John

        # This matches: (email='test@example.com' OR
        # private_email='alt@example.com') AND name='John'

    Note:
        - Parameters ending with '_or' are grouped together with OR logic
        - Different '_or' groups are combined with AND logic
        - Regular parameters (without '_or') are combined with AND logic
        - All lookups supported by Django are supported (e.g., __icontains, __gte, etc.)

    Example:
        # Find users with name containing 'John' OR 'Jane'
        GET /api/users/?name__icontains_or=John&name__icontains_or=Jane

        # Find products in specific categories OR price range
        GET /api/products/?category_or=electronics&category_or=books&price__lt=50
    """

    def filter_queryset(self, request, queryset, view):
        """
        Apply OR filtering to the queryset based on query parameters.

        Args:
            request: The HTTP request object containing query parameters.
            queryset: The base queryset to filter.
            view: The view instance (unused but required by interface).

        Returns:
            QuerySet: The filtered queryset with OR logic applied.
        """
        # Get valid filterable fields from the view
        valid_fields = self._get_valid_filter_fields(view, queryset.model)

        # Get all query parameters ending with '_or'
        or_params = {}
        regular_params = {}

        # Handle both DRF Request (query_params) and Django Request (GET)
        query_dict = getattr(request, "query_params", request.GET)

        for key in query_dict.keys():
            # Skip pagination and other special parameters
            if key in ["page", "page_size", "search", "ordering", "format"]:
                continue

            if key.endswith("_or"):
                # Remove the '_or' suffix to get the actual field name
                field_name = key[:-3]

                # Validate field name against valid fields AND permissions
                if not self._is_valid_filter_field(
                    field_name, valid_fields, queryset.model, request.user
                ):
                    continue  # Skip invalid fields

                # Get all values for this parameter (handles multiple values)
                values = query_dict.getlist(key)
                or_params[field_name] = values
            else:
                # Validate regular filter fields AND permissions
                if self._is_valid_filter_field(
                    key, valid_fields, queryset.model, request.user
                ):
                    regular_params[key] = query_dict.get(key)

        # Build OR queries
        if or_params:
            q_objects = Q()
            for field_name, values in or_params.items():
                # Create OR condition for all values of this field
                field_q = Q()
                for value in values:
                    field_q |= Q(**{field_name: value})
                # Combine with AND between different fields
                q_objects &= field_q

            queryset = queryset.filter(q_objects)

        # Apply regular filters (these use AND logic)
        for key, value in regular_params.items():
            try:
                # Handle __in lookups specially
                if "__in" in key:
                    value = value.split(",")

                queryset = queryset.filter(**{key: value})
            except (ValueError, TypeError, Exception):
                # Skip filters that cause type conversion errors
                # (e.g., "true" string for a BooleanField)
                continue

        return queryset

    def _get_valid_filter_fields(self, view, model):
        """
        Get the set of valid filterable fields for this model/view.

        Returns a set of valid field names including lookups (e.g., 'price__gte').
        """
        valid_fields = set()

        # Get filterset fields from view if available
        if hasattr(view, "filterset_fields"):
            filterset_fields = view.filterset_fields
            if callable(filterset_fields):
                filterset_fields = filterset_fields()

            # Add base field names and their lookups
            if isinstance(filterset_fields, dict):
                for field_name, lookups in filterset_fields.items():
                    valid_fields.add(field_name)
                    for lookup in lookups:
                        valid_fields.add(f"{field_name}__{lookup}")
            elif isinstance(filterset_fields, list):
                valid_fields.update(filterset_fields)

        # Also allow direct model field names
        for field in model._meta.fields:
            valid_fields.add(field.name)

        # Allow ManyToMany fields
        for field in model._meta.many_to_many:
            valid_fields.add(field.name)

        return valid_fields

    def _is_valid_filter_field(self, field_name, valid_fields, model, user):
        """
        Check if a field name is valid for filtering with permission checking.

        Validates:
        1. Exact field names and field lookups (e.g., 'price__gte')
        2. Nesting depth limits
        3. Nested field permissions (user must have read permission)
           - only if TurboDRF permissions enabled

        Args:
            field_name: Filter parameter (e.g., 'author__name__icontains')
            valid_fields: Set of valid filterable field names
            model: Django model class
            user: User object for permission checking

        Returns:
            bool: True if field is valid and user has permission
        """
        import logging

        from django.conf import settings

        logger = logging.getLogger(__name__)

        # Basic validation - check if field exists
        # Check exact match
        if field_name in valid_fields:
            return True

        # Check if it's a lookup (e.g., 'price__gte')
        if "__" in field_name:
            # Get the base field name
            base_field = field_name.split("__")[0]
            if base_field in valid_fields:
                # Base field is valid, now check nesting and permissions if enabled
                pass  # Continue to advanced validation below
            # Also check if the full lookup is in valid_fields
            elif field_name in valid_fields:
                return True
            else:
                return False
        else:
            # Simple field not in valid_fields
            return False

        # Advanced validation with nesting depth and permissions
        # Only run if new validation module is available and permissions are enabled
        try:
            from .validation import (
                check_nested_field_permissions,
                validate_filter_field,
            )

            # Parse the filter parameter to separate field path from lookup
            try:
                field_path, lookup = validate_filter_field(model, field_name)
            except Exception as e:
                # Nesting depth exceeded or invalid field - use basic validation
                logger.debug(f"Filter validation failed for '{field_name}': {str(e)}")
                return False

            # Check nested field permissions ONLY if TurboDRF permissions are enabled
            # AND the user has roles configured
            disable_perms = getattr(settings, "TURBODRF_DISABLE_PERMISSIONS", False)
            use_default_perms = getattr(
                settings, "TURBODRF_USE_DEFAULT_PERMISSIONS", False
            )

            if not disable_perms and not use_default_perms:
                # TurboDRF role-based permissions are active - check if user has roles
                from .backends import get_user_roles

                try:
                    user_roles = get_user_roles(user)
                    # Only check permissions if user has roles
                    if user_roles:
                        if not check_nested_field_permissions(model, field_path, user):
                            logger.debug(
                                f"Permission denied for filter '{field_name}' "
                                f"(user lacks read permission)"
                            )
                            return False
                except Exception as e:
                    # Permission check failed - log and allow (backward compatible)
                    logger.debug(f"Permission check error for '{field_name}': {str(e)}")
                    pass

        except ImportError:
            # Validation module not available - use basic validation only
            pass

        return True

    def get_schema_operation_parameters(self, view):
        """
        Return schema parameters for Swagger/OpenAPI documentation.

        This method provides documentation for the OR filter parameters
        so they appear correctly in API documentation.
        """
        return [
            {
                "name": "field_or",
                "required": False,
                "in": "query",
                "description": (
                    "OR filter: Use field_or parameter to filter with OR logic. "
                    "Multiple values are combined with OR. "
                    "Example: ?email_or=test1@example.com&email_or=test2@example.com"
                ),
                "schema": {"type": "string"},
            }
        ]
