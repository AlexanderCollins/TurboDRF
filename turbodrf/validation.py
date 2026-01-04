"""
Validation utilities for TurboDRF nested fields and filters.

This module provides utilities for validating nesting depth and traversing
Django ORM relationships to check permissions at each level.
"""

import logging

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ValidationError

logger = logging.getLogger(__name__)


def get_max_nesting_depth():
    """
    Get the maximum nesting depth from settings.

    Returns:
        int or None: Maximum nesting depth, or None for unlimited
    """
    from .settings import TURBODRF_MAX_NESTING_DEPTH as default_depth

    return getattr(settings, "TURBODRF_MAX_NESTING_DEPTH", default_depth)


def validate_nesting_depth(field_name, max_depth=None):
    """
    Validate that a field name doesn't exceed the maximum nesting depth.

    Args:
        field_name: Field name potentially with __ notation
            (e.g., 'author__publisher__name')
        max_depth: Maximum allowed depth, or None to use setting

    Returns:
        bool: True if valid

    Raises:
        ValidationError: If nesting depth exceeds maximum

    Examples:
        >>> validate_nesting_depth('title')  # depth 0
        True
        >>> validate_nesting_depth('author__name')  # depth 1
        True
        >>> validate_nesting_depth('author__publisher__name')  # depth 2
        True
        >>> validate_nesting_depth('a__b__c__d')  # depth 3
        True
        >>> validate_nesting_depth('a__b__c__d__e')  # depth 4 - EXCEEDS DEFAULT
        ValidationError
    """
    if max_depth is None:
        max_depth = get_max_nesting_depth()

    # If max_depth is None, unlimited nesting is allowed
    if max_depth is None:
        return True

    # Count the number of __ separators to determine nesting depth
    depth = field_name.count("__")

    if depth > max_depth:
        raise ValidationError(
            f"Field '{field_name}' exceeds maximum nesting depth of {max_depth}. "
            f"Current depth: {depth}. "
            f"WARNING: Increasing TURBODRF_MAX_NESTING_DEPTH beyond 3 is "
            f"UNSUPPORTED and may cause performance issues, security risks, "
            f"and unexpected behavior."
        )

    return True


def get_nested_field_model(model, field_path):
    """
    Traverse a nested field path and return the final model and field info.

    Args:
        model: Starting Django model class
        field_path: Field path with __ notation (e.g., 'author__publisher__name')

    Returns:
        tuple: (final_model, field_chain)
            - final_model: The model class of the final field
            - field_chain: List of (model, field, field_name) tuples for each step

    Raises:
        FieldDoesNotExist: If any field in the path doesn't exist

    Example:
        >>> model, chain = get_nested_field_model(Book, 'author__publisher__name')
        >>> # Returns: (Publisher, [
        >>> #   (Book, ForeignKey, 'author'),
        >>> #   (Author, ForeignKey, 'publisher'),
        >>> #   (Publisher, CharField, 'name')
        >>> # ])
    """
    parts = field_path.split("__")
    field_chain = []
    current_model = model

    for part in parts:
        try:
            field = current_model._meta.get_field(part)
            field_chain.append((current_model, field, part))

            # If this is a relational field, get the related model
            if hasattr(field, "related_model") and field.related_model:
                current_model = field.related_model
            # For the final field, keep the current model
        except FieldDoesNotExist:
            raise FieldDoesNotExist(
                f"Field '{part}' does not exist on model {current_model.__name__}"
            )

    return current_model, field_chain


def check_nested_field_permissions(model, field_path, user, use_cache=True):
    """
    Check permissions for a nested field path using permission snapshots.

    This function traverses the relationship chain and checks read permissions
    at each level, building snapshots for related models as needed.

    Args:
        model: Starting Django model class
        field_path: Field path with __ notation
        user: Django user object for permission checking
        use_cache: Whether to use permission snapshot caching (default: True)

    Returns:
        bool: True if user has permission to read the entire path

    Example:
        For 'author__salary__amount':
        1. Check Book.author permission (build Book snapshot)
        2. Build Author model snapshot, check Author.salary permission
        3. Build Salary model snapshot, check Salary.amount permission
        Returns True only if ALL checks pass.
    """
    from .backends import build_permission_snapshot

    # Simple field (no nesting) - check on base model
    if "__" not in field_path:
        snapshot = build_permission_snapshot(user, model, use_cache=use_cache)
        base_field = field_path
        if snapshot.has_read_rule(base_field):
            return snapshot.can_read_field(base_field)
        else:
            return snapshot.can_perform_action("read")

    # Nested field - traverse and check permissions at each level
    parts = field_path.split("__")
    current_model = model

    for i, part in enumerate(parts):
        # Build snapshot for current model
        current_snapshot = build_permission_snapshot(
            user, current_model, use_cache=use_cache
        )

        # Check permission for this field
        if current_snapshot.has_read_rule(part):
            if not current_snapshot.can_read_field(part):
                logger.debug(
                    f"Permission denied: {current_model.__name__}.{part} "
                    f"(explicit read rule failed)"
                )
                return False
        else:
            if not current_snapshot.can_perform_action("read"):
                logger.debug(
                    f"Permission denied: {current_model.__name__}.{part} "
                    f"(model-level read permission failed)"
                )
                return False

        # If not the last part, traverse to the related model
        if i < len(parts) - 1:
            try:
                field = current_model._meta.get_field(part)
                if hasattr(field, "related_model") and field.related_model:
                    # Get the related model for the next iteration
                    current_model = field.related_model
                else:
                    # Not a relational field, can't traverse further
                    remaining_path = ".".join(parts[i + 1 :])
                    logger.warning(
                        f"Field '{part}' on {current_model.__name__} is not a "
                        f"relational field, cannot traverse to '{remaining_path}'"
                    )
                    return False
            except FieldDoesNotExist:
                logger.warning(
                    f"Field '{part}' does not exist on model {current_model.__name__}"
                )
                return False

    return True


def validate_filter_field(model, filter_param):
    """
    Validate a filter parameter including nesting depth and field existence.

    Args:
        model: Django model class
        filter_param: Filter parameter (e.g., 'author__name__icontains')

    Returns:
        tuple: (field_path, lookup) or raises ValidationError

    Example:
        >>> validate_filter_field(Book, 'author__name__icontains')
        ('author__name', 'icontains')
        >>> validate_filter_field(Book, 'price__gte')
        ('price', 'gte')
    """
    # Strip _or suffix if present
    if filter_param.endswith("_or"):
        filter_param = filter_param[:-3]

    # Split into field path and lookup
    parts = filter_param.split("__")

    # Common Django lookups
    lookups = {
        "exact",
        "iexact",
        "contains",
        "icontains",
        "in",
        "gt",
        "gte",
        "lt",
        "lte",
        "startswith",
        "istartswith",
        "endswith",
        "iendswith",
        "range",
        "date",
        "year",
        "month",
        "day",
        "week",
        "week_day",
        "quarter",
        "time",
        "hour",
        "minute",
        "second",
        "isnull",
        "regex",
        "iregex",
    }

    # Check if last part is a lookup
    if parts[-1] in lookups:
        field_path = "__".join(parts[:-1])
        lookup = parts[-1]
    else:
        field_path = filter_param
        lookup = "exact"

    # Validate nesting depth
    validate_nesting_depth(field_path)

    return field_path, lookup
