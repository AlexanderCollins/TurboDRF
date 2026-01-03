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
        GET /api/users/?email_or=test@example.com&private_email_or=alt@example.com&name=John

        # This matches: (email='test@example.com' OR private_email='alt@example.com') AND name='John'

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
        # Get all query parameters ending with '_or'
        or_params = {}
        regular_params = {}

        for key in request.query_params.keys():
            # Skip pagination and other special parameters
            if key in ["page", "page_size", "search", "ordering", "format"]:
                continue

            if key.endswith("_or"):
                # Remove the '_or' suffix to get the actual field name
                field_name = key[:-3]
                # Get all values for this parameter (handles multiple values)
                values = request.query_params.getlist(key)
                or_params[field_name] = values
            else:
                regular_params[key] = request.query_params.get(key)

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
            except Exception:
                # Skip invalid filters
                pass

        return queryset

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
