"""
Core mixins for TurboDRF.

This module provides the TurboDRFMixin that enables automatic API generation
for Django models.
"""

from django.core.exceptions import FieldDoesNotExist


def get_searchable_fields(model):
    """Searchable fields for ``model`` — the fields enabled for ``?search=``.

    Read from the ``turbodrf()`` config dict (``'searchable_fields': [...]``)
    first, falling back to a ``searchable_fields`` class attribute. The
    config-dict form is preferred — it lives alongside ``fields`` /
    ``tenant_field`` and is honored by both the ``?search=`` handling and the
    boot-time searchable-fields safety check. (Previously only the class
    attribute was read, so declaring it in the config dict silently did
    nothing.)
    """
    config = model.turbodrf() if hasattr(model, "turbodrf") else None
    if isinstance(config, dict):
        from_config = config.get("searchable_fields")
        if from_config:
            return list(from_config)
    return list(getattr(model, "searchable_fields", None) or [])


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
                - 'lookup_field' (str): Field to use for object lookup in URLs.
                  Defaults to 'pk'. Use 'slug' or any other unique field
                  instead of integer IDs.
                - 'public_access' (bool): Whether to allow public (unauthenticated)
                  access to this model's endpoints. Defaults to False.
                  When True, unauthenticated users can read (GET) the model.
                  You can configure different fields for anonymous users using
                  the 'guest' role in your permission configuration.
                - 'tenant_field' (str): FK path on this model to the tenant
                  declared in TURBODRF_TENANT_MODEL. Supports '__' traversal
                  (e.g. 'bank_account__deal__brokerage'). Filters every
                  queryset to rows where this resolves to the calling user's
                  tenant. Auto-detected from the FK graph when TURBODRF_AUTODETECT_TENANT
                  is True. Mandatory wall — cannot be bypassed.
                - 'owner_field' (str | list[str]): FK path(s) on this model
                  to the owner user. List = OR (any column matching the user
                  grants visibility). Within-tenant scope only.
                - 'bypass_owner_roles' (list[str]): Roles that ignore the
                  owner check (still subject to tenant scope).
                - 'tenancy' (str): Use 'shared' to declare the model is not
                  tenant-scoped (reference data like currencies, categories).
                  Required when TURBODRF_REQUIRE_TENANCY is True and no
                  tenant_field / visibility is declared.
                - 'visibility' (list[Predicate]): Power form. A list of
                  Predicate instances (Tenant, Owner, Members, Group,
                  Conditional, Either, Custom) that compose with AND.
                  Use this when sugar form (tenant_field/owner_field) doesn't
                  fit the access pattern.
                - 'searchable_fields' (list[str]): Field names (or '__'-paths)
                  enabled for ``?search=``. Gated by field-level read
                  permissions and validated at startup. May also be declared as
                  a ``searchable_fields`` class attribute (legacy location).
                - 'read_only' (bool): When True, only list/retrieve are served;
                  writes (POST/PUT/PATCH/DELETE) return 405. Good for audit logs
                  and reference data.
                - 'http_methods' (list[str]): Explicit allow-list of HTTP
                  methods for the endpoint (e.g. ['get', 'post']); disallowed
                  methods return 405. ('read_only' is the common shorthand.)
                - 'actions' (list): Custom endpoints — a list of handlers each
                  decorated with ``@turbodrf_action(...)`` (import from
                  ``turbodrf``). Each is attached to the generated viewset and
                  inherits get_object()/get_queryset() tenant + predicate
                  scoping, so custom verbs don't re-implement access control.

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
