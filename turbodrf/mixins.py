"""
Core mixins for TurboDRF.

This module provides the TurboDRFMixin that enables automatic API generation
for Django models.
"""

from django.core.exceptions import FieldDoesNotExist


class TurboDRFMixin:
    """
    Mixin to add TurboDRF capabilities to Django models.

    This mixin enables automatic REST API generation for any Django model.
    By inheriting from this mixin and defining a turbodrf() classmethod,
    models will be automatically discovered and exposed via REST endpoints.

    Example:
        >>> class Book(models.Model, TurboDRFMixin):
        ...     title = models.CharField(max_length=200)
        ...     author = models.CharField(max_length=100)
        ...     price = models.DecimalField(max_digits=10, decimal_places=2)
        ...
        ...     # Optional: Define searchable fields
        ...     searchable_fields = ['title', 'author']
        ...
        ...     @classmethod
        ...     def turbodrf(cls):
        ...         return {
        ...             'fields': ['title', 'author', 'price']
        ...         }

    The mixin provides several helper methods for field introspection and
    configuration that are used internally by TurboDRF.
    """

    @classmethod
    def turbodrf(cls):
        """
        Configure the API for this model.

        This method should be overridden in subclasses to provide
        model-specific configuration for the auto-generated API.

        Returns:
            dict: Configuration dictionary with the following keys:
                - 'enabled' (bool): Whether to enable API for this model.
                  Defaults to True.
                - 'endpoint' (str): Custom endpoint name. If not provided,
                  defaults to the pluralized model name.
                - 'fields' (list|dict|str): Fields to expose in the API.
                  Can be:
                  - A list of field names: ['title', 'author', 'price']
                  - A dict with different fields for list/detail views:
                    {'list': ['title', 'author'], 'detail': '__all__'}
                  - The string '__all__' to include all fields

        Example:
            >>> @classmethod
            ... def turbodrf(cls):
            ...     return {
            ...         'fields': {
            ...             'list': ['title', 'author__name', 'price'],
            ...             'detail': ['title', 'author__name',
            ...                       'author__email', 'price',
            ...                       'description', 'created_at']
            ...         },
            ...         'endpoint': 'books',
            ...         'enabled': True
            ...     }
        """
        return {
            "enabled": True,
            "endpoint": f"{cls._meta.model_name}s",
            "fields": "__all__",
        }

    @classmethod
    def get_api_fields(cls, view_type="list"):
        """
        Get fields for a specific view type.

        This method resolves the fields configuration based on the view type
        (list or detail). It handles the various field configuration formats
        supported by TurboDRF.

        Args:
            view_type (str): The type of view ('list' or 'detail').
                Defaults to 'list'.

        Returns:
            list: List of field names to include in the API response.

        Example:
            >>> Book.get_api_fields('list')
            ['title', 'author', 'price']
            >>> Book.get_api_fields('detail')
            ['title', 'author', 'price', 'description', 'created_at']
        """
        config = cls.turbodrf()
        fields = config.get("fields", "__all__")

        if fields == "__all__":
            # Get all model fields except reverse relations
            return [
                f.name
                for f in cls._meta.get_fields()
                if not f.many_to_many and not f.one_to_many
            ]

        if isinstance(fields, dict):
            # Different fields for list/detail views
            return fields.get(view_type, [])

        # If it's a list, use for both views
        return fields

    @classmethod
    def get_field_type(cls, field_path):
        """
        Resolve field type for nested fields.

        This method traverses relationships to determine the type of a field
        specified using Django's double-underscore notation
        (e.g., 'author__name').

        Args:
            field_path (str): Field path using double-underscore notation.
                Example: 'author__name' or 'category__parent__name'

        Returns:
            Field: The Django field instance, or None if the field
            doesn't exist.

        Example:
            >>> Book.get_field_type('author__name')
            <django.db.models.fields.CharField: name>
            >>> Book.get_field_type('nonexistent__field')
            None
        """
        parts = field_path.split("__")
        model = cls

        # Traverse relationships
        for part in parts[:-1]:
            try:
                field = model._meta.get_field(part)
                if hasattr(field, "related_model"):
                    model = field.related_model
                else:
                    return None
            except FieldDoesNotExist:
                return None

        # Get the final field
        try:
            return model._meta.get_field(parts[-1])
        except FieldDoesNotExist:
            return None
