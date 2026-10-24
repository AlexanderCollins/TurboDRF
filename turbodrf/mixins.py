"""
Core mixins for TurboDRF.

This module provides the TurboDRFMixin that enables automatic API generation
for Django models, plus the helpers that read a model's ``turbodrf()``
configuration the same way everywhere (the API, the OpenAPI schema, OPTIONS
and the management commands).
"""

from .scope import is_platform, platform_config


def _model_config(model):
    """``model``'s ``turbodrf()`` config as the current scope serves it: the
    whole dict on the tenant scope, its ``"platform"`` block on the platform
    scope (see turbodrf.scope)."""
    config = model.turbodrf() if hasattr(model, "turbodrf") else None
    config = config if isinstance(config, dict) else {}
    if is_platform():
        return platform_config(config)
    return config


def get_searchable_fields(model):
    """Searchable fields for ``model`` — the fields enabled for ``?search=``.

    Read from the ``turbodrf()`` config dict (``'searchable_fields': [...]``)
    first, falling back to a ``searchable_fields`` class attribute. The
    config-dict form is preferred — it lives alongside ``fields`` /
    ``tenant_field`` and is honored by both the ``?search=`` handling and the
    boot-time searchable-fields safety check.

    A key present in the config wins even when it's empty:
    ``'searchable_fields': []`` turns search off for the model, whatever the
    class attribute says. On the platform scope only the platform block's own
    ``searchable_fields`` count: the class attribute is the tenant API's.
    """
    from_config = _model_config(model).get("searchable_fields")
    if from_config is not None:
        return list(from_config)
    if is_platform():
        return []
    return list(getattr(model, "searchable_fields", None) or [])


def _resolve_configured_fields(model, view_type="list"):
    """The fields ``model``'s ``turbodrf()`` config exposes for ``view_type``.

    The one resolution of the ``fields`` config, shared by the API, the
    OpenAPI schema, OPTIONS and the management commands:

    - ``view_type="list"`` uses the list fields; anything else
      (``"detail"``, ``"retrieve"``, writes, custom actions) uses the detail
      fields.
    - A ``{'list': ..., 'detail': ...}`` dict that leaves one key out uses
      the other key for it, never ``'__all__'``.
    - ``'__all__'`` (the whole value or one of the dict's values) means every
      concrete field plus every forward many-to-many field; no reverse
      relations.

    Returns a new list of the configured names (``__`` paths and property
    names included). Sensitive-field and permission filtering happen later,
    per caller.
    """
    fields = _model_config(model).get("fields", "__all__")
    if isinstance(fields, dict):
        list_fields = fields.get("list", fields.get("detail", []))
        detail_fields = fields.get("detail", list_fields)
        fields = list_fields if view_type == "list" else detail_fields
    if fields == "__all__":
        return [f.name for f in model._meta.fields] + [
            f.name for f in model._meta.many_to_many
        ]
    if isinstance(fields, str):
        return [fields]
    return list(fields or [])


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

    ``get_api_fields()`` returns the configured fields for a view type.
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
                  defaults to the lowercase model name with an "s" appended
                  (no real pluralization: ``Category`` -> ``categorys``).
                - 'fields' (list|dict|str): Fields to expose in the API.
                  Can be:
                  - A list of field names: ['title', 'author', 'price']
                  - A dict with different fields for list/detail views:
                    {'list': ['title', 'author'], 'detail': '__all__'}.
                    A dict with only one key uses it for both views.
                  - The string '__all__': every concrete field plus forward
                    many-to-many fields (no reverse relations)
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
                  a ``searchable_fields`` class attribute (legacy location);
                  the config key wins when present, even if it's empty.
                - 'read_only' (bool): When True, only list/retrieve are served;
                  CRUD writes (POST/PUT/PATCH/DELETE) return 405. Declared
                  'actions' keep their own methods. Good for audit logs and
                  reference data.
                - 'http_methods' (list[str]): Explicit allow-list of HTTP
                  methods for the endpoint (e.g. ['get', 'post']); disallowed
                  methods return 405. ('read_only' is the common shorthand.)
                - 'actions' (list): Custom endpoints — a list of handlers each
                  decorated with ``@turbodrf_action(...)`` (import from
                  ``turbodrf``). Each is attached to the generated viewset and
                  inherits get_object()/get_queryset() tenant + predicate
                  scoping, so custom verbs don't re-implement access control.
                - 'platform' (dict): The model's config on a platform router
                  (``TurboDRFRouter(scope="platform", gate=[...])``, staff
                  across tenants): the keys above that shape an API, none of
                  the tenancy keys. Without it the model isn't served there.
                  See docs/platform.md.

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
        Get the configured fields for a view type.

        Resolved as the viewset resolves them: a dict that leaves a view
        type out uses the other one, and ``'__all__'`` means every concrete
        field plus forward many-to-many fields.

        Args:
            view_type (str): ``'list'``, or anything else for the detail
                fields (``'detail'``). Defaults to ``'list'``.

        Returns:
            list: The configured field names, before any per-caller
            permission or sensitive-field filtering.

        Example:
            >>> Book.get_api_fields('list')
            ['title', 'author', 'price']
            >>> Book.get_api_fields('detail')
            ['title', 'author', 'price', 'description', 'created_at']
        """
        return _resolve_configured_fields(cls, view_type)
