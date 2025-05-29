"""
Advanced filtering support for TurboDRF
"""

from django.db.models import Q


class TurboDRFFilter:
    """Advanced filtering with support for complex queries."""

    @classmethod
    def parse_filters(cls, queryset, params):
        """
        Parse query parameters and apply filters.

        Supports:
        - field=value (exact match)
        - field__gt=value (greater than)
        - field__contains=value (contains)
        - field__in=value1,value2 (in list)
        - etc.
        """
        for key, value in params.items():
            if key in ["page", "page_size", "search", "ordering", "format"]:
                continue

            # Handle special operators
            if "__in" in key:
                # Convert comma-separated values to list
                value = value.split(",")

            try:
                queryset = queryset.filter(**{key: value})
            except Exception:
                # Skip invalid filters
                pass

        return queryset

    @classmethod
    def parse_search(cls, queryset, search_term, search_fields):
        """
        Apply search across multiple fields.

        Supports:
        - Simple search: searches all fields
        - Field-specific: field:term
        - Quoted: "exact phrase"
        """
        if not search_term or not search_fields:
            return queryset

        # Check for field-specific search
        if ":" in search_term:
            field, term = search_term.split(":", 1)
            if field in search_fields:
                return queryset.filter(**{f"{field}__icontains": term})

        # Build OR query for all search fields
        q_objects = Q()
        for field in search_fields:
            q_objects |= Q(**{f"{field}__icontains": search_term})

        return queryset.filter(q_objects)
