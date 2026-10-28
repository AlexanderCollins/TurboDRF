"""
Custom filter backends for TurboDRF.

This module provides advanced filtering capabilities beyond what
django-filter provides out of the box.
"""

import datetime
import logging

from django.core.exceptions import FieldError, ValidationError
from django.db import models
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import exceptions
from rest_framework.filters import BaseFilterBackend, OrderingFilter, SearchFilter

logger = logging.getLogger(__name__)

# Most distinct ?search= terms a request may use: each term is matched (ILIKE)
# against every search field, ANDed with the others.
_MAX_SEARCH_TERMS = 20

# Query parameters that aren't filters.
_NON_FILTER_PARAMS = {"page", "page_size", "search", "ordering", "format", "fields"}

# Lookups that compare the value as text, whatever the field's type.
_TEXT_LOOKUPS = {
    "iexact",
    "contains",
    "icontains",
    "startswith",
    "istartswith",
    "endswith",
    "iendswith",
}

# Transforms, and the model field their output is parsed as.
_TRANSFORMS = {
    "date": models.DateField(),
    "time": models.TimeField(),
    **{
        name: models.IntegerField()
        for name in (
            "year",
            "month",
            "day",
            "week",
            "week_day",
            "quarter",
            "hour",
            "minute",
            "second",
        )
    },
}


def _max_filter_value_length():
    from django.conf import settings

    return getattr(settings, "TURBODRF_MAX_FILTER_VALUE_LENGTH", 1000)


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

        scoped_targets = {}
        for flt in filterset.filters.values():
            target_qs = getattr(flt, "queryset", None)
            if target_qs is None or callable(target_qs):
                continue
            target = target_qs.model
            if target not in scoped_targets:
                scoped_targets[target] = scoped_target_queryset(target, request)
            scoped = scoped_targets[target]
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
    """SearchFilter that bounds ``?search=``: at most 20 distinct terms, each
    at most ``TURBODRF_MAX_FILTER_VALUE_LENGTH`` long (1000 by default). Every
    term is matched against every search field, so an unbounded search is a
    DoS: SQLite raises ("LIKE pattern too complex", "Expression tree is too
    large") → 500, Postgres burns terms × fields × length of ILIKE. A search
    over either bound matches nothing (as the filter length guard does):
    dropping the offending terms would widen it to every row.
    """

    def get_search_terms(self, request):
        # Distinct terms, in order: a repeated term adds nothing but cost.
        return list(dict.fromkeys(super().get_search_terms(request)))

    def filter_queryset(self, request, queryset, view):
        """DRF's search, with each ``__``-path term ANDed with the traversal
        scope of its JOIN targets INSIDE the same ``filter()`` call. On a
        reverse / M2M relation a scope applied in a separate ``filter()`` is a
        separate JOIN: one visible sibling satisfied it while the term matched
        a hidden row."""
        import operator
        from functools import reduce

        from .validation import build_traversal_scope_q

        search_fields = self.get_search_fields(view, request)
        search_terms = self.get_search_terms(request)
        if not search_fields or not search_terms:
            return queryset
        max_len = _max_filter_value_length()
        if len(search_terms) > _MAX_SEARCH_TERMS or any(
            len(term) > max_len for term in search_terms
        ):
            return queryset.none()

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
    or vanish across pages."""

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


def _path_fields(model, field_path):
    """The model fields ``field_path`` walks, in order (fewer when it runs
    past a non-relation, e.g. into a JSON field's keys)."""
    from django.core.exceptions import FieldDoesNotExist

    fields, current = [], model
    for part in field_path.split("__"):
        if current is None:
            break
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            break
        fields.append(field)
        current = field.related_model if field.is_relation else None
    return fields


def _filter_value(field, lookup, raw):
    """``raw`` (a query-string value) as a value for ``<field>__<lookup>``.

    Parsed with the model field (or the transform's output type) the lookup
    compares against, as django-filter does for the filters it builds: a raw
    string reaching the ORM fails at SQL-build time instead (a 500). ``in``
    takes comma-separated values, ``range`` exactly two, ``isnull`` true or
    false. Raises django's ValidationError for a value the field can't hold.
    """
    parts = lookup.split("__")
    final = parts[-1] if parts[-1] not in _TRANSFORMS else "exact"
    transforms = [p for p in parts if p in _TRANSFORMS]
    if transforms:
        field = _TRANSFORMS[transforms[-1]]

    if final == "isnull":
        value = ORFilterBackend._parse_bool(raw)
        if value is None:
            raise ValidationError("Enter true or false.")
        return value
    if final in ("in", "range"):
        items = raw.split(",")
        if final == "range" and len(items) != 2:
            raise ValidationError("Enter two comma-separated values.")
        return [_parse_value(field, final, item) for item in items]
    return _parse_value(field, final, raw)


def _parse_value(field, final, raw):
    if field is None or final in _TEXT_LOOKUPS:
        return raw  # matched as text (or a JSON key's value)
    if isinstance(field, models.BooleanField):
        # As django-filter's BooleanWidget: true / false, 1 / 0, yes / no.
        value = ORFilterBackend._parse_bool(raw)
        if value is None:
            raise ValidationError("Enter true or false.")
        return value
    value = field.to_python(raw)
    if isinstance(value, datetime.datetime):
        from django.conf import settings
        from django.utils import timezone

        if settings.USE_TZ and timezone.is_naive(value):
            value = timezone.make_aware(value)
    return value


class ORFilterBackend(BaseFilterBackend):
    """
    Filter backend that supports OR queries over the values of one field,
    and the filters django-filter doesn't build (nested paths, other lookups).

    A parameter ending in '_or' may be repeated: its values are combined with
    OR. Every parameter — each '_or' group and each regular filter — is
    combined with the others with AND.

    Usage:
        # Products whose title is Django OR Python
        GET /api/products/?title_or=Django&title_or=Python

        # (title='Django' OR title='Python') AND price < 50
        GET /api/products/?title_or=Django&title_or=Python&price__lt=50

        # Lookups apply to '_or' parameters too
        GET /api/users/?name__icontains_or=John&name__icontains_or=Jane

    Note:
        - Only fields the model's ``fields`` config exposes are filterable: a
          nested path must be configured itself (``author__name``), not just
          its base. Other parameters are ignored (and logged).
        - A filter across an M2M or reverse relation matches each row once.
          One ending on such a relation (``?tags_or=3``, ``?tags__isnull=``)
          isn't accepted: ``?tags=3`` filters by an M2M (see
          TurboDRFFilterBackend).
        - A value the field can't hold is a 400.
        - Parameters django-filter already applied (``?price__gte=``) are
          left to it.
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
        model = queryset.model
        valid_fields = self._get_valid_filter_fields(view, model)
        applied = self._filterset_applied(view)
        user = getattr(request, "user", None)

        # Handle both DRF Request (query_params) and Django Request (GET)
        query_dict = getattr(request, "query_params", request.GET)

        # Bound filter value length. Beyond this, SQLite raises "LIKE
        # pattern too complex" and Postgres burns CPU O(N×len) — both DoS
        # vectors. 1000 covers normal use (UUIDs, long titles, multi-word
        # search) and the framework would not run an unbounded
        # icontains scan in practice.
        max_filter_value_len = _max_filter_value_length()

        filters = []  # (param, field path, lookup, [values], is_or)
        accepted = {}  # field path / lookup -> accepted?, once per request
        for key in query_dict.keys():
            if key in _NON_FILTER_PARAMS:
                continue
            is_or = key.endswith("_or")
            name = key[:-3] if is_or else key
            values = query_dict.getlist(key) if is_or else [query_dict.get(key)]
            if not is_or and key in applied:
                parsed = None  # django-filter applies it
            else:
                if name not in accepted:
                    accepted[name] = self._accepted_filter(
                        name, valid_fields, model, user
                    )
                parsed = accepted[name]
                if parsed is None:
                    # Silent drop (intentional — noisy 400s leak the field
                    # universe to scanners), but log so operators can spot
                    # scan attempts in their logs.
                    logger.info(
                        "turbodrf.filter.dropped: %s (invalid or "
                        "unauthorized) on %s",
                        key,
                        model.__name__,
                    )
                    continue
            # Reject excessively long filter values (DoS guard).
            if any(
                v is not None and len(str(v)) > max_filter_value_len for v in values
            ):
                return queryset.none()
            if parsed is not None:
                filters.append((key, *parsed, values, is_or))

        # Traversal scoping: for `__`-paths to a target with registered
        # predicates / tenant_field, the JOIN must be scoped to rows the
        # caller can see via the target's own endpoint. Otherwise
        # `?fk__field=secret` joins the target without applying its own
        # rules and leaks row existence.
        from .validation import build_traversal_scope_q

        scopes = {}
        for key, field_path, lookup, values, is_or in filters:
            field = self._value_field(model, field_path)
            condition = Q()
            try:
                for raw in values:
                    if raw is None or raw == "":
                        continue  # an empty value is no filter, as in django-filter
                    value = _filter_value(field, lookup, raw)
                    condition |= Q(**{f"{field_path}__{lookup}": value})
            except (ValidationError, ValueError, TypeError) as exc:
                raise exceptions.ValidationError({key: _messages(exc)})
            if not condition:
                continue
            if "__" in field_path:
                if field_path not in scopes:
                    scopes[field_path] = build_traversal_scope_q(
                        model, field_path, request
                    )
                # In the same filter() call, so it scopes the same JOIN.
                condition &= scopes[field_path]
            try:
                if _crosses_multivalued(model, field_path):
                    # A JOIN across an M2M / reverse relation returns a row
                    # once per matching related row: match by pk instead.
                    queryset = queryset.filter(
                        pk__in=model._base_manager.filter(condition).values("pk")
                    )
                else:
                    queryset = queryset.filter(condition)
            except (FieldError, ValidationError, ValueError, TypeError) as exc:
                raise exceptions.ValidationError({key: _messages(exc)})

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

    @staticmethod
    def _filterset_applied(view):
        """Parameter names the view's django-filter backend applies itself
        (``price``, ``price__gte``, ...): applying them again would parse
        their raw values a second time, and repeat an M2M JOIN."""
        backends = getattr(view, "filter_backends", None) or ()
        if not any(
            isinstance(b, type) and issubclass(b, DjangoFilterBackend) for b in backends
        ):
            return set()
        filterset_fields = getattr(view, "filterset_fields", None)
        if callable(filterset_fields):
            filterset_fields = filterset_fields()
        if isinstance(filterset_fields, dict):
            return {
                name if lookup == "exact" else f"{name}__{lookup}"
                for name, lookups in filterset_fields.items()
                for lookup in lookups
            }
        if isinstance(filterset_fields, (list, tuple)):
            return set(filterset_fields)
        return set()

    @staticmethod
    def _value_field(model, field_path):
        """The model field a value for ``field_path`` is parsed with: its last
        field (an FK parses as its target field); None past a JSON field's
        keys (the value is used as given)."""
        fields = _path_fields(model, field_path)
        if len(fields) != len(field_path.split("__")):
            return None
        return fields[-1]

    def _get_valid_filter_fields(self, view, model):
        """
        Get the set of valid filterable field names and paths for this view.

        The filterset's names with their lookups (e.g. ``price__gte``), and
        the field paths the model's turbodrf() config exposes: every list and
        detail entry (``author__name``), its base and the pk. A filter on a
        column the API doesn't expose is an oracle on its value, so a nested
        path is valid only if configured itself. A view without a TurboDRF
        config allows the model's own fields.
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

        configured = getattr(view, "_configured_view_fields", None)
        if callable(configured) and getattr(view, "model", None) is model:
            valid_fields.add(model._meta.pk.name)
            for view_type in ("list", "detail"):
                fields = configured(view_type)
                if not isinstance(fields, list):
                    continue
                for path in fields:
                    if isinstance(path, str):
                        valid_fields.add(path)
                        valid_fields.add(path.split("__")[0])
        else:
            for field in list(model._meta.fields) + list(model._meta.many_to_many):
                valid_fields.add(field.name)

        return valid_fields

    def _accepted_filter(self, field_name, valid_fields, model, user):
        """``(field path, lookup)`` for a valid filter parameter, else None.

        Valid: the field path (lookups split off) is configured, or a JSON
        key path under a configured JSON field, and doesn't end on an M2M or
        reverse relation; it's within the nesting depth; no hop is sensitive;
        and (with TurboDRF permissions on) the caller can read every hop."""
        from .permissions import permissions_bypassed
        from .validation import (
            check_nested_field_permissions,
            is_field_path_sensitive,
            validate_filter_field,
        )

        # Parse the filter parameter to separate field path from lookup
        try:
            field_path, lookup = validate_filter_field(model, field_name)
        except Exception as e:
            logger.debug("Filter validation failed for '%s': %s", field_name, e)
            return None

        fields = _path_fields(model, field_path)
        if not fields:
            return None
        if len(fields) < len(field_path.split("__")):
            # JSON key transforms: valid under a configured JSON field.
            json_path = "__".join(field_path.split("__")[: len(fields)])
            if json_path not in valid_fields or not isinstance(
                fields[-1], models.JSONField
            ):
                return None
        elif field_path not in valid_fields:
            return None
        last = fields[-1]
        if last.is_relation and (last.many_to_many or last.one_to_many):
            # A multi-valued relation's own lookups (`tags__in`, `tags_or`,
            # `tags__isnull`) would match targets the caller can't see.
            return None

        # Sensitive fields are NEVER filterable, regardless of permissions.
        # A filter on a field that's stripped from responses would be a
        # value-confirmation / substring oracle for the hidden value
        # (e.g. ?api_key__istartswith=sk-a). This mirrors the deny-list
        # already enforced on the response and search pathways.
        if is_field_path_sensitive(field_path):
            logger.debug("Filter denied for sensitive field '%s'", field_name)
            return None

        # Check field permissions if TurboDRF role-based permissions are active
        if not permissions_bypassed():
            try:
                # Every caller is gated, anonymous included: on a
                # public_access model the anonymous snapshot has
                # model-level read, so field rules still apply.
                if not check_nested_field_permissions(model, field_path, user):
                    logger.debug(
                        "Permission denied for filter '%s' (user lacks read "
                        "permission)",
                        field_name,
                    )
                    return None
            except Exception as e:
                # Fail closed — deny access on permission check error
                logger.warning(
                    "Permission check error for filter '%s': %s — denying access",
                    field_name,
                    e,
                )
                return None

        return field_path, lookup


def _crosses_multivalued(model, field_path):
    """True if ``field_path`` crosses an M2M or reverse relation."""
    return any(
        field.many_to_many or field.one_to_many
        for field in _path_fields(model, field_path)
    )


def _messages(exc):
    """The client-facing messages of a value / lookup error."""
    if isinstance(exc, ValidationError):
        return exc.messages
    if isinstance(exc, FieldError):
        return ["Unsupported lookup for this field."]
    return ["Enter a valid value."]
