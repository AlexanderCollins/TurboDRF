from django.conf import settings
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.pagination import PageNumberPagination

from .permissions import DefaultDjangoPermission, TurboDRFPermission
from .serializers import TurboDRFSerializer


class TurboDRFPagination(PageNumberPagination):
    """
    Custom pagination class for TurboDRF API responses.

    Extends Django REST Framework's PageNumberPagination to provide
    a more structured response format with comprehensive pagination metadata.

    Configuration:
        - Default page size: 20 items
        - Maximum page size: 100 items
        - Page size can be customized via 'page_size' query parameter

    Response Format:
        {
            "pagination": {
                "next": "http://api.example.com/items/?page=3",
                "previous": "http://api.example.com/items/?page=1",
                "current_page": 2,
                "total_pages": 10,
                "total_items": 200
            },
            "data": [...]
        }

    Example Usage:
        GET /api/articles/?page=2&page_size=50
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        """
        Create a paginated response with metadata.

        Overrides the default pagination response to include additional
        metadata that's useful for frontend pagination components.

        Args:
            data: The serialized page data.

        Returns:
            Response: A Response object containing pagination metadata
                     and the serialized data.
        """
        from rest_framework.response import Response

        return Response(
            {
                "pagination": {
                    "next": self.get_next_link(),
                    "previous": self.get_previous_link(),
                    "current_page": self.page.number,
                    "total_pages": self.page.paginator.num_pages,
                    "total_items": self.page.paginator.count,
                },
                "data": data,
            }
        )


class TurboDRFViewSet(viewsets.ModelViewSet):
    """
    Base ViewSet for TurboDRF-enabled models with automatic configuration.

    This ViewSet provides automatic API endpoint generation with:
    - Dynamic serializer creation based on model configuration
    - Role-based field filtering and permissions
    - Automatic query optimization with select_related/prefetch_related
    - Built-in filtering, searching, and ordering
    - Pagination with detailed metadata

    The ViewSet reads configuration from the model's turbodrf() method
    and automatically configures serializers, permissions, and query
    optimizations based on that configuration.

    Features:
        - Dynamic field selection for list vs detail views
        - Automatic handling of nested field relationships
        - Permission-based field filtering per user role
        - Query optimization based on requested fields
        - Full CRUD operations with permission checking

    Model Configuration Example:
        class Article(models.Model):
            title = models.CharField(max_length=200)
            content = models.TextField()
            author = models.ForeignKey(User, on_delete=models.CASCADE)

            @classmethod
            def turbodrf(cls):
                return {
                    'fields': {
                        'list': ['id', 'title', 'author__name'],
                        'detail': [
                            'id', 'title', 'content',
                            'author__name', 'author__email'
                        ]
                    }
                }

            searchable_fields = ['title', 'content']

    Attributes:
        model: The Django model class (set automatically by TurboDRFRouter)
        permission_classes: Uses TurboDRFPermission for role-based access
        pagination_class: Uses TurboDRFPagination for structured responses
        filter_backends: Enables filtering, searching, and ordering
    """

    # Use default Django permissions if configured,
    # otherwise use TurboDRF's role-based permissions
    permission_classes = [
        (
            DefaultDjangoPermission
            if getattr(settings, "TURBODRF_USE_DEFAULT_PERMISSIONS", False)
            else TurboDRFPermission
        )
    ] if not getattr(settings, "TURBODRF_DISABLE_PERMISSIONS", False) else []
    pagination_class = TurboDRFPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]

    model = None  # Will be set by the router

    def get_serializer_class(self):
        """
        Dynamically create a serializer class based on model configuration.

        This method generates a serializer at runtime that respects:
        - The model's field configuration from turbodrf()
        - Different field sets for list vs detail views
        - Nested field relationships using '__' notation
        - User permissions for field visibility

        The method handles both simple field lists and complex configurations
        with separate list/detail field sets. It automatically processes
        nested fields and ensures base fields are included when nested
        fields are requested.

        Returns:
            type: A dynamically created serializer class configured for
                 the current action (list/detail) and model.

        Field Configuration Examples:
            # Simple configuration (same fields for all views)
            'fields': ['id', 'title', 'author__name']

            # Complex configuration (different fields per view)
            'fields': {
                'list': ['id', 'title', 'author__name'],
                'detail': [
                    'id', 'title', 'content',
                    'author__name', 'author__bio'
                ]
            }

        Nested Field Handling:
            - 'author__name' requires 'author' to be included
            - Nested fields are collected and passed to the serializer
            - The serializer handles traversal and flattening
        """
        config = self.model.turbodrf()
        fields = config.get("fields", "__all__")

        # Handle different field configurations
        if isinstance(fields, dict):
            # Different fields for list and detail views
            if self.action == "list":
                fields_to_use = fields.get("list", "__all__")
            elif self.action in ["create", "update", "partial_update"]:
                # For write operations, use detail fields which
                # typically include all fields
                fields_to_use = fields.get("detail", "__all__")
            else:
                fields_to_use = fields.get("detail", "__all__")
        else:
            fields_to_use = fields

        # Store original fields before processing
        original_fields = (
            fields_to_use if isinstance(fields_to_use, list) else fields_to_use
        )

        # Process fields to separate simple and nested fields
        if isinstance(fields_to_use, list):
            simple_fields = []
            nested_fields = {}

            for field in fields_to_use:
                if "__" in field:
                    # This is a nested field
                    base_field = field.split("__")[0]
                    if base_field not in nested_fields:
                        nested_fields[base_field] = []
                    nested_fields[base_field].append(field)
                else:
                    simple_fields.append(field)

            # Add base fields for nested fields if not already present
            for base_field in nested_fields:
                if base_field not in simple_fields:
                    simple_fields.append(base_field)

            fields_to_use = simple_fields

        # Check if we should use the factory for permission-based filtering
        request = getattr(self, "request", None)
        user = getattr(request, "user", None) if request else None

        # Only use permission-based field filtering for read operations
        # For write operations, include all configured fields and let
        # validation handle permissions
        use_default_perms = getattr(settings, "TURBODRF_USE_DEFAULT_PERMISSIONS", False)

        if (
            not use_default_perms
            and user
            and hasattr(user, "roles")
            and self.action in ["list", "retrieve"]
        ):
            # Use the factory for permission-based field filtering
            # (TurboDRF permissions mode)
            from .serializers import TurboDRFSerializerFactory

            return TurboDRFSerializerFactory.create_serializer(
                self.model, original_fields, user
            )

        # Create serializer class dynamically with unique name per action
        action = self.action or "default"
        serializer_name = f"{self.model.__name__}{action.capitalize()}Serializer"

        # Create unique ref_name for swagger
        if hasattr(self.model, "_meta"):
            ref_name = (
                f"{self.model._meta.app_label}_"
                f"{self.model._meta.model_name}_{action}"
            )
        else:
            # Fallback for non-Django models (e.g., in tests)
            ref_name = f"{self.model.__name__}_{action}"

        serializer_class = type(
            serializer_name,
            (TurboDRFSerializer,),
            {
                "Meta": type(
                    "Meta",
                    (),
                    {
                        "model": self.model,
                        "fields": fields_to_use,
                        "_nested_fields": (
                            nested_fields if isinstance(fields_to_use, list) else {}
                        ),
                        "ref_name": ref_name,  # Unique reference name
                    },
                ),
                "__module__": (
                    f"turbodrf.generated.{self.model._meta.app_label}"
                    if hasattr(self.model, "_meta")
                    else "turbodrf.generated"
                ),
            },
        )

        return serializer_class

    def get_queryset(self):
        """
        Get the queryset with automatic query optimizations.

        This method enhances the base queryset with select_related
        optimizations based on the fields configured in the model's
        turbodrf() method. It automatically detects foreign key
        relationships and adds appropriate select_related calls
        to minimize database queries.

        The optimization is particularly important when using nested
        field notation (e.g., 'author__name') as it prevents N+1
        query problems by fetching related objects in a single query.

        Returns:
            QuerySet: An optimized queryset with select_related applied
                     for all foreign key fields referenced in the
                     field configuration.

        Example:
            If fields include ['title', 'author__name', 'category__title'],
            this method will automatically add:
            queryset.select_related('author', 'category')

        Note:
            Future enhancements could include:
            - prefetch_related for many-to-many relationships
            - Automatic detection of optimal fetch strategies
            - Configuration options for custom optimizations
        """
        queryset = super().get_queryset()

        # Add default ordering by primary key to avoid pagination warnings
        if not queryset.ordered:
            queryset = queryset.order_by("pk")

        # Add select_related and prefetch_related optimizations
        # This is a simple implementation - could be enhanced
        config = self.model.turbodrf()
        fields = config.get("fields", [])

        if isinstance(fields, dict):
            fields = fields.get("list", []) + fields.get("detail", [])

        # Extract foreign key fields for select_related
        select_related_fields = []
        for field in fields:
            if "__" in field:
                # This is a related field
                base_field = field.split("__")[0]
                if base_field not in select_related_fields:
                    select_related_fields.append(base_field)

        if select_related_fields:
            queryset = queryset.select_related(*select_related_fields)

        return queryset

    @property
    def search_fields(self):
        """
        Get the fields to use for text search functionality.

        Returns the search fields defined on the model class via
        the 'searchable_fields' attribute. This integrates with
        Django REST Framework's SearchFilter to enable text search
        across specified fields.

        Returns:
            list: Field names that can be searched, or empty list
                 if no searchable fields are defined.

        Model Example:
            class Article(models.Model):
                title = models.CharField(max_length=200)
                content = models.TextField()

                searchable_fields = ['title', 'content']

        API Usage:
            GET /api/articles/?search=django
            # Searches in both title and content fields
        """
        if hasattr(self.model, "searchable_fields"):
            return self.model.searchable_fields
        return []

    @property
    def ordering_fields(self):
        """
        Define fields available for result ordering.

        Currently returns '__all__' to allow ordering by any model field.
        This integrates with Django REST Framework's OrderingFilter.

        Returns:
            str: '__all__' to enable ordering by any field.

        API Usage:
            GET /api/articles/?ordering=created_at
            GET /api/articles/?ordering=-updated_at  # Descending order

        Note:
            Future versions might restrict ordering fields based on
            model configuration or user permissions.
        """
        return "__all__"

    def get_filterset_fields(self):
        """
        Define fields available for filtering with lookup expressions.

        This method dynamically generates filter configurations for all
        model fields with common lookup expressions like gte, lte, exact,
        icontains, etc.

        Returns:
            dict: Field configurations with lookup expressions.

        API Usage:
            GET /api/articles/?author=1
            GET /api/articles/?created_at__gte=2024-01-01
            GET /api/articles/?title__icontains=django
            GET /api/articles/?price__gte=10&price__lte=100

        Note:
            JSONField and BinaryField are excluded from automatic filtering
            as they require special handling that django-filter doesn't
            support out of the box.
        """
        from django.db import models

        filterset_fields = {}

        # Get all fields from the model
        for field in self.model._meta.fields:
            field_name = field.name

            # Check field class name for special handling
            field_class_name = field.__class__.__name__

            # Skip fields that django-filter doesn't support or that
            # don't make sense to filter
            unsupported_fields = ["JSONField", "BinaryField", "FilePathField"]
            if field_class_name in unsupported_fields:
                continue

            # Also check by importing JSONField classes directly for extra safety
            try:
                from django.db.models import JSONField as ModelsJSONField

                if isinstance(field, ModelsJSONField):
                    continue
            except ImportError:
                pass

            # Check for PostgreSQL JSONField (older Django versions)
            try:
                from django.contrib.postgres.fields import JSONField as PGJSONField

                if isinstance(field, PGJSONField):
                    continue
            except ImportError:
                pass

            # Skip any field that has 'json' in its class name (case insensitive)
            # This catches custom JSONField implementations
            if "json" in field_class_name.lower():
                continue

            # Define lookups based on field type
            if isinstance(
                field, (models.IntegerField, models.DecimalField, models.FloatField)
            ):
                # Numeric fields get comparison lookups
                filterset_fields[field_name] = ["exact", "gte", "lte", "gt", "lt"]
            elif isinstance(field, (models.DateField, models.DateTimeField)):
                # Date fields get date lookups
                filterset_fields[field_name] = [
                    "exact",
                    "gte",
                    "lte",
                    "gt",
                    "lt",
                    "year",
                    "month",
                    "day",
                ]
            elif isinstance(field, models.BooleanField):
                # Boolean fields only need exact
                filterset_fields[field_name] = ["exact"]
            elif isinstance(field, (models.CharField, models.TextField)):
                # Text fields get string lookups
                filterset_fields[field_name] = [
                    "exact",
                    "icontains",
                    "istartswith",
                    "iendswith",
                ]
            elif isinstance(field, models.ForeignKey):
                # Foreign keys get exact lookup
                filterset_fields[field_name] = ["exact"]
            elif isinstance(field, (models.FileField, models.ImageField)):
                # File fields can be filtered by exact match or if they're null
                filterset_fields[field_name] = ["exact", "isnull"]
            elif isinstance(field, models.UUIDField):
                # UUID fields only support exact matching
                filterset_fields[field_name] = ["exact", "isnull"]
            elif isinstance(field, models.GenericIPAddressField):
                # IP address fields support exact and startswith
                filterset_fields[field_name] = ["exact", "istartswith"]
            else:
                # Default to exact lookup
                filterset_fields[field_name] = ["exact"]

        return filterset_fields

    @property
    def filterset_fields(self):
        """Property wrapper for filterset_fields to work with
        DjangoFilterBackend."""
        return self.get_filterset_fields()
