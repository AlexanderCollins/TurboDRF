"""TurboDRF OPTIONS metadata.

Returns a self-describing JSON for `OPTIONS /api/<endpoint>/` covering:
- model identity (app, name)
- per-field metadata gated by read permission
- allowed CRUD actions
- search / ordering / filterable fields (already permission-filtered)
- pagination capability
"""

from django.conf import settings
from rest_framework.metadata import SimpleMetadata

# CRUD action -> (HTTP method that routes it, model-level permission).
_ACTIONS = (
    ("list", "get", "read"),
    ("retrieve", "get", "read"),
    ("create", "post", "create"),
    ("update", "put", "update"),
    ("partial_update", "patch", "update"),
    ("destroy", "delete", "delete"),
)

# Model-level permission -> Django's model permission codename prefix.
_DJANGO_PERMS = {
    "read": "view",
    "create": "add",
    "update": "change",
    "delete": "delete",
}


class TurboDRFMetadata(SimpleMetadata):
    """Custom metadata handler for TurboDRF OPTIONS requests."""

    def determine_metadata(self, request, view):
        metadata = super().determine_metadata(request, view)

        if not hasattr(view, "model"):
            return metadata

        model = view.model
        # DRF sets action="metadata" for OPTIONS: the route (a lookup in the
        # URL) says whether this describes the detail view.
        lookup = getattr(view, "lookup_url_kwarg", None) or getattr(
            view, "lookup_field", "pk"
        )
        detail = lookup in (getattr(view, "kwargs", None) or {}) or view.action in (
            "retrieve",
            "update",
            "partial_update",
            "destroy",
        )
        view_type = "detail" if detail else "list"
        # The serializer's own field resolution (an `"__all__"` config includes
        # M2M fields).
        configured = getattr(view, "_configured_view_fields", None)
        if callable(configured):
            fields = configured(view_type)
        else:
            fields = model.get_api_fields(view_type)
        if not isinstance(fields, list):
            fields = []

        # Build snapshot once and reuse for read/write checks
        snapshot = self._get_snapshot(request, model)
        user = getattr(request, "user", None)

        metadata["model"] = {
            "name": str(model._meta.verbose_name),
            "app_label": model._meta.app_label,
            "fields": self._get_field_metadata(model, fields, user, snapshot),
        }
        metadata["actions"] = self._get_allowed_actions(model, user, snapshot, view)

        # New: capability metadata so frontends can introspect what the
        # endpoint supports without reading source.
        metadata["capabilities"] = self._get_capabilities(view, request)
        metadata["pagination"] = self._get_pagination_info(view)

        return metadata

    def determine_actions(self, request, view):
        """Nothing: TurboDRF describes the actions itself (determine_metadata).
        DRF's version runs the permission checks, ``get_object()`` and the
        serializer for every write method, only to be replaced."""
        return {}

    def _get_snapshot(self, request, model):
        """Get the user's permission snapshot for this model, or None."""
        from .permissions import permissions_bypassed

        if permissions_bypassed():
            return None
        from .backends import attach_snapshot_to_request

        return attach_snapshot_to_request(request, model)

    def _get_field_metadata(self, model, fields, user, snapshot):
        """Get metadata for each field, gated by read permissions.

        - Field is hidden from metadata if user can't read it.
        - read_only / write_only flags use the SNAPSHOT's permission logic
          (which falls back to model-level perms when no field rule exists),
          matching the actual serializer behavior. Previously this used a
          direct permission lookup and incorrectly showed read_only=True
          for users with model-level write permission but no explicit
          field-level write rule.
        - Anonymous callers are gated like everyone else (on a public_access
          model their snapshot has model-level read). Only bypassed
          permissions (snapshot ``None``) skip the gate.
        """
        from .validation import is_field_path_sensitive, is_field_visible_to_user

        field_metadata = {}
        gate_by_perms = snapshot is not None

        for field_name in fields:
            if is_field_path_sensitive(field_name):
                continue
            if gate_by_perms and not is_field_visible_to_user(model, field_name, user):
                continue

            if "__" in field_name:
                # The base may also be listed on its own (an FK and its nested
                # fields): one entry then describes both, in either order.
                base_field = field_name.split("__")[0]
                entry = field_metadata.setdefault(base_field, {"type": "nested"})
                entry.setdefault("fields", []).append(field_name.split("__", 1)[1])
            else:
                try:
                    field = model._meta.get_field(field_name)
                    if snapshot is not None:
                        can_read = snapshot.can_read_field(field_name)
                        can_write = snapshot.can_write_field(field_name)
                    else:
                        # Permissions bypassed: assume both
                        can_read = True
                        can_write = True

                    field_info = {
                        "type": field.__class__.__name__,
                        "required": (
                            not field.blank if hasattr(field, "blank") else True
                        ),
                        "read_only": not can_write,
                        "write_only": not can_read and can_write,
                        "label": str(field.verbose_name),
                        "help_text": str(field.help_text or ""),
                    }

                    if hasattr(field, "max_length"):
                        field_info["max_length"] = field.max_length
                    if hasattr(field, "choices") and field.choices:
                        field_info["choices"] = [
                            {"value": k, "display": str(v)} for k, v in field.choices
                        ]
                except Exception:
                    field_info = {"type": "unknown"}
                # Keep nested sub-fields recorded before the base field itself.
                nested = field_metadata.get(field_name, {}).get("fields")
                if nested is not None:
                    field_info["fields"] = nested
                field_metadata[field_name] = field_info

        return field_metadata

    def _get_allowed_actions(self, model, user, snapshot, view=None):
        """Allowed CRUD actions for the calling user: those the viewset routes
        (a ``read_only`` resource or an ``http_methods`` allow-list has no
        route for the others) that the caller's permissions allow — the
        snapshot's model-level actions; Django's model permissions with
        ``TURBODRF_USE_DEFAULT_PERMISSIONS``; every action with
        ``TURBODRF_DISABLE_PERMISSIONS``.
        """
        if view is not None:
            methods = {m.lower() for m in getattr(view, "http_method_names", ())}
            read_only = getattr(view, "_read_only", False)
        else:
            methods = {method for _name, method, _perm in _ACTIONS}
            read_only = False

        if snapshot is not None:
            allowed = snapshot.can_perform_action
        elif getattr(settings, "TURBODRF_DISABLE_PERMISSIONS", False) or not getattr(
            settings, "TURBODRF_USE_DEFAULT_PERMISSIONS", False
        ):

            def allowed(perm):
                return True

        else:

            def allowed(perm):
                # As DefaultDjangoPermission: anonymous callers read a
                # public_access model, and nothing else.
                if user is None or not user.is_authenticated:
                    config = model.turbodrf()
                    return perm == "read" and bool(config.get("public_access"))
                return user.has_perm(
                    f"{model._meta.app_label}."
                    f"{_DJANGO_PERMS[perm]}_{model._meta.model_name}"
                )

        return {
            name: method in methods
            and (perm == "read" or not read_only)
            and allowed(perm)
            for name, method, perm in _ACTIONS
        }

    def _get_capabilities(self, view, request):
        """Search / ordering / filtering capabilities — already
        permission-filtered, so frontends can advertise only what works."""
        cap = {}

        # search_fields is a property that already does perm filtering
        try:
            search = view.search_fields
            cap["search_fields"] = list(search) if search else []
        except Exception:
            cap["search_fields"] = []

        # ordering_fields — list or "__all__"
        try:
            ordering = view.ordering_fields
            if isinstance(ordering, str):
                cap["ordering_fields"] = ordering
            else:
                cap["ordering_fields"] = list(ordering)
        except Exception:
            cap["ordering_fields"] = []

        # filterable fields (with their lookups)
        try:
            cap["filterset_fields"] = view.get_filterset_fields()
        except Exception:
            cap["filterset_fields"] = {}

        # ?fields= client-driven field selection (list and detail, every path)
        cap["client_fields_param"] = "fields"

        return cap

    def _get_pagination_info(self, view):
        """Pagination knobs the client can use."""
        pagination_class = getattr(view, "pagination_class", None)
        if pagination_class is None:
            return None
        return {
            "default_page_size": getattr(pagination_class, "page_size", None),
            "page_size_query_param": getattr(
                pagination_class, "page_size_query_param", "page_size"
            ),
            "max_page_size": getattr(pagination_class, "max_page_size", None),
            "page_query_param": getattr(pagination_class, "page_query_param", "page"),
        }
