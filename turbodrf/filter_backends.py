"""
Custom filter backends for TurboDRF.

This module provides advanced filtering capabilities beyond what
django-filter provides out of the box.
"""

import logging

from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import BaseFilterBackend, OrderingFilter, SearchFilter

logger = logging.getLogger(__name__)


class TurboDRFFilterBackend(DjangoFilterBackend):
    """DjangoFilterBackend whose FK / M2M filters validate their value against
    the target rows the caller can see. django-filter checks ``?folder=<pk>``
    against the target's whole table, so "exists in another tenant" (200) and
    "doesn't exist" (400) were distinguishable — a cross-tenant pk oracle."""

    def get_filterset(self, request, queryset, view):
        filterset = super().get_filterset(request, queryset, view)
        if filterset is None:
            return None
        from .validation import scoped_target_queryset

        for flt in filterset.filters.values():
            target_qs = getattr(flt, "queryset", None)
            if target_qs is None or callable(target_qs):
                continue
            scoped = scoped_target_queryset(target_qs.model, request)
            if scoped is not None:
                flt.queryset = target_qs.filter(pk__in=scoped.values("pk"))
        return filterset

    def filter_queryset(self, request, queryset, view):
        """An FK / M2M filter naming a row the caller can't see — or one that
        doesn't exist — matches nothing (200, empty), the same in both cases."""
        from django_filters import utils

        filterset = self.get_filterset(request, queryset, view)
        if filterset is None:
            return queryset
        if not filterset.is_valid():
            relation_filters = {
                name
                for name, flt in filterset.filters.items()
                if getattr(flt, "queryset", None) is not None
            }
            if set(filterset.errors) <= relation_filters:
                return queryset.none()
            raise utils.translate_validation(filterset.errors)
        return filterset.qs


class TurboDRFSearchFilter(SearchFilter):
    """SearchFilter that caps the length of each ``?search=`` term.

    The ORFilterBackend length guard intentionally skips the search/ordering
    params; without a cap here a huge ``?search=<100KB>`` runs an unbounded
    multi-field ILIKE (SQLite raises "LIKE pattern too complex" → 500; Postgres
    burns O(N*len) CPU). Over-length terms are dropped.
    """

    def get_search_terms(self, request):
        from django.conf import settings

        terms = super().get_search_terms(request)
        max_len = getattr(settings, "TURBODRF_MAX_FILTER_VALUE_LENGTH", 1000)
        return [t for t in terms if len(t) <= max_len]

    def filter_queryset(self, request, queryset, view):
        """DRF's search, with each ``__``-path term ANDed with the traversal
        scope of its JOIN targets INSIDE the same ``filter()`` call. On a
        reverse / M2M relation a scope applied in a separate ``filter()`` is a
        separate JOIN: one visible sibling satisfied it while the term matched
        a hidden row."""
        import operator
        from functools import reduce

        from django.db import models

        from .validation import build_traversal_scope_q

        search_fields = self.get_search_fields(view, request)
        search_terms = self.get_search_terms(request)
        if not search_fields or not search_terms:
            return queryset

        lookups = []
        for search_field in search_fields:
            path = str(search_field).lstrip("^=$@")
            scope = (
                build_traversal_scope_q(queryset.model, path, request)
                if "__" in path
                else models.Q()
            )
            lookups.append((self._construct(str(search_field), queryset), scope))

        base = queryset
        conditions = (
            reduce(
                operator.or_,
                (models.Q(**{lookup: term}) & scope for lookup, scope in lookups),
            )
            for term in search_terms
        )
        queryset = queryset.filter(reduce(operator.and_, conditions))

        # Remove duplicates from results, if necessary (as DRF does)
        if self.must_call_distinct(queryset, search_fields):
            queryset = queryset.filter(pk=models.OuterRef("pk"))
            queryset = base.filter(models.Exists(queryset))
        return queryset

    def _construct(self, search_field, queryset):
        try:
            return self.construct_search(search_field, queryset)
        except TypeError:  # DRF < 3.15: construct_search(field_name)
            return self.construct_search(search_field)


class TurboDRFOrderingFilter(OrderingFilter):
    """OrderingFilter with a pk tie-breaker: ``?ordering=`` on a non-unique
    column otherwise leaves ties in plan-dependent order, so a row can repeat
    or vanish across pages (and the compiled and serializer paths disagree)."""

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if not ordering:
            return ordering
        # Order an FK by its raw column: ordering by the field itself sorts by
        # the TARGET's Meta.ordering through an unscoped JOIN, leaking the
        # order of rows the caller can't see.
        ordering = [_fk_column_term(queryset.model, term) for term in ordering]
        pk_names = {"pk", "-pk", queryset.model._meta.pk.name}
        pk_names.add(f"-{queryset.model._meta.pk.name}")
        if not any(isinstance(o, str) and o in pk_names for o in ordering):
            ordering = [*ordering, "pk"]
        return ordering


def _fk_column_term(model, term):
    """``folder`` / ``-folder`` -> ``folder_id`` / ``-folder_id`` for a concrete
    FK or one-to-one field; any other term unchanged."""
    from django.core.exceptions import FieldDoesNotExist

    if not isinstance(term, str):
        return term
    desc = term.startswith("-")
    name = term.lstrip("-")
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        return term
    if field.concrete and (field.many_to_one or field.one_to_one):
        return f"{'-' if desc else ''}{field.attname}"
    return term


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

        # Bound filter value length. Beyond this, SQLite raises "LIKE
        # pattern too complex" and Postgres burns CPU O(N×len) — both DoS
        # vectors. 1000 covers normal use (UUIDs, long titles, multi-word
        # search) and the framework would not run an unbounded
        # icontains scan in practice.
        from django.conf import settings as _s

        max_filter_value_len = getattr(_s, "TURBODRF_MAX_FILTER_VALUE_LENGTH", 1000)

        for key in query_dict.keys():
            # Skip pagination and other special parameters
            if key in ["page", "page_size", "search", "ordering", "format"]:
                continue
            # Reject excessively long filter values (DoS guard).
            for v in query_dict.getlist(key):
                if v is not None and len(str(v)) > max_filter_value_len:
                    return queryset.none()

            if key.endswith("_or"):
                # Remove the '_or' suffix to get the actual field name
                field_name = key[:-3]

                # Validate field name against valid fields AND permissions
                if not self._is_valid_filter_field(
                    field_name, valid_fields, queryset.model, request.user
                ):
                    # Silent drop (intentional — noisy 400s leak the field
                    # universe to scanners), but log so operators can spot
                    # scan attempts in their logs.
                    logger.info(
                        "turbodrf.filter.dropped: %s (invalid or "
                        "unauthorized) on %s",
                        key,
                        queryset.model.__name__,
                    )
                    continue

                # Get all values for this parameter (handles multiple values)
                values = query_dict.getlist(key)
                or_params[field_name] = values
            else:
                # Validate regular filter fields AND permissions
                if self._is_valid_filter_field(
                    key, valid_fields, queryset.model, request.user
                ):
                    regular_params[key] = query_dict.get(key)
                else:
                    logger.info(
                        "turbodrf.filter.dropped: %s (invalid or "
                        "unauthorized) on %s",
                        key,
                        queryset.model.__name__,
                    )

        # Traversal scoping: for `__`-paths to a target with registered
        # predicates / tenant_field, the JOIN must be scoped to rows the
        # caller can see via the target's own endpoint. Otherwise
        # `?fk__field=secret` joins the target without applying its own
        # rules and leaks row existence (same bug class as the compiled
        # M2M / search-field bypasses; URL-driven so it's gated here at
        # request time).
        from .validation import build_traversal_scope_q, validate_filter_field

        allow_unsafe_traversal = getattr(
            _s, "TURBODRF_ALLOW_UNSAFE_FILTER_TRAVERSAL", False
        )

        def _scope_path(key):
            if allow_unsafe_traversal or "__" not in key:
                return Q()
            try:
                field_path, _lookup = validate_filter_field(queryset.model, key)
            except Exception:
                return Q()
            if "__" not in field_path:
                return Q()
            return build_traversal_scope_q(queryset.model, field_path, request)

        # Build OR queries
        if or_params:
            q_objects = Q()
            for field_name, values in or_params.items():
                # Create OR condition for all values of this field
                field_q = Q()
                for value in values:
                    field_q |= Q(**{field_name: value})
                # AND in target scoping for this OR-group's path
                field_q &= _scope_path(field_name)
                # Combine with AND between different fields
                q_objects &= field_q

            queryset = queryset.filter(q_objects)

        # Apply regular filters (these use AND logic)
        for key, value in regular_params.items():
            try:
                # Handle __in lookups specially
                if key.endswith("__in"):
                    value = value.split(",")
                # Coerce isnull values to bool — Django's ORM rejects
                # strings here, so a malformed `?x__isnull=garbage` raises
                # at SQL-build time and surfaces as a 500.
                elif key.endswith("__isnull"):
                    coerced = self._parse_bool(value)
                    if coerced is None:
                        continue  # silently drop malformed isnull values
                    value = coerced

                queryset = queryset.filter(Q(**{key: value}) & _scope_path(key))
            except (ValueError, TypeError, Exception):
                # Skip filters that cause type conversion errors
                # (e.g., "true" string for a BooleanField)
                continue

        return queryset

    @staticmethod
    def _parse_bool(value):
        """Coerce a string to a bool for __isnull lookups, or None if invalid."""
        if isinstance(value, bool):
            return value
        if not isinstance(value, str):
            return None
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y"):
            return True
        if v in ("false", "0", "no", "n"):
            return False
        return None

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

        # Also allow direct model field names — only those the model's
        # turbodrf() config exposes (a filter on an unexposed column is an
        # oracle on its value).
        configured = getattr(view, "_configured_base_fields", None)
        configured = configured() if callable(configured) else None
        for field in list(model._meta.fields) + list(model._meta.many_to_many):
            if configured is None or field.name in configured:
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

        logger = logging.getLogger(__name__)

        # Basic validation - check if field exists in the model
        if "__" in field_name:
            base_field = field_name.split("__")[0]
            if base_field not in valid_fields and field_name not in valid_fields:
                return False
        else:
            if field_name not in valid_fields:
                return False

        # Permission check — runs for ALL filter fields, not just nested ones
        try:
            from .validation import (
                check_nested_field_permissions,
                is_field_path_sensitive,
                validate_filter_field,
            )

            # Parse the filter parameter to separate field path from lookup
            try:
                field_path, lookup = validate_filter_field(model, field_name)
            except Exception as e:
                logger.debug(f"Filter validation failed for '{field_name}': {str(e)}")
                return False

            # Sensitive fields are NEVER filterable, regardless of permissions.
            # A filter on a field that's stripped from responses would be a
            # value-confirmation / substring oracle for the hidden value
            # (e.g. ?api_key__istartswith=sk-a). This mirrors the deny-list
            # already enforced on the response and search pathways.
            if is_field_path_sensitive(field_path):
                logger.debug(f"Filter denied for sensitive field '{field_name}'")
                return False

            # Check field permissions if TurboDRF role-based permissions are active
            from .permissions import permissions_bypassed

            if not permissions_bypassed():
                try:
                    # Every caller is gated, anonymous included: on a
                    # public_access model the anonymous snapshot has
                    # model-level read, so field rules still apply.
                    if not check_nested_field_permissions(model, field_path, user):
                        logger.debug(
                            f"Permission denied for filter '{field_name}' "
                            f"(user lacks read permission)"
                        )
                        return False
                except Exception as e:
                    # Fail closed — deny access on permission check error
                    logger.warning(
                        f"Permission check error for filter '{field_name}': "
                        f"{str(e)} — denying access"
                    )
                    return False

        except ImportError:
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
