"""TurboDRF OPTIONS metadata.

Returns a self-describing JSON for `OPTIONS /api/<endpoint>/` covering:
- model identity (app, name)
- per-field metadata gated by read permission
- allowed CRUD actions
- search / ordering / filterable fields (already permission-filtered)
- tenancy info (tenant_field, predicate summary)
- pagination capability
"""

from rest_framework.metadata import SimpleMetadata


class TurboDRFMetadata(SimpleMetadata):
    """Custom metadata handler for TurboDRF OPTIONS requests."""

    def determine_metadata(self, request, view):
        metadata = super().determine_metadata(request, view)

        if not hasattr(view, "model"):
            return metadata

        model = view.model
        view_type = (
            "detail"
            if view.action in ["retrieve", "update", "partial_update"]
            else "list"
        )
        fields = model.get_api_fields(view_type)

        # Build snapshot once and reuse for read/write checks
        snapshot = self._get_snapshot(request, model)

        metadata["model"] = {
            "name": str(model._meta.verbose_name),
            "app_label": model._meta.app_label,
            "fields": self._get_field_metadata(
                model, fields, request.user, snapshot
            ),
        }
        metadata["actions"] = self._get_allowed_actions(model, request.user, snapshot)

        # New: capability metadata so frontends can introspect what the
        # endpoint supports without reading source.
        metadata["capabilities"] = self._get_capabilities(view, request)
        metadata["tenancy"] = self._get_tenancy_info(model)
        metadata["pagination"] = self._get_pagination_info(view)

        return metadata

    def _get_snapshot(self, request, model):
        """Get the user's permission snapshot for this model, or None."""
        from django.conf import settings

        if getattr(settings, "TURBODRF_DISABLE_PERMISSIONS", False):
            return None
        if getattr(settings, "TURBODRF_USE_DEFAULT_PERMISSIONS", False):
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
        - Anon-without-guest-role legacy: if no role system applies (the
          user has no resolved roles), don't gate metadata — public_access
          models without a guest role show all configured fields to anon.
        """
        from .backends import get_user_roles
        from .validation import is_field_visible_to_user

        field_metadata = {}
        gate_by_perms = bool(get_user_roles(user))

        for field_name in fields:
            if (
                gate_by_perms
                and not is_field_visible_to_user(model, field_name, user)
            ):
                continue

            if "__" in field_name:
                base_field = field_name.split("__")[0]
                if base_field not in field_metadata:
                    field_metadata[base_field] = {"type": "nested", "fields": []}
                field_metadata[base_field]["fields"].append(
                    field_name.split("__", 1)[1]
                )
            else:
                try:
                    field = model._meta.get_field(field_name)
                    if snapshot is not None:
                        can_read = snapshot.can_read_field(field_name)
                        can_write = snapshot.can_write_field(field_name)
                    else:
                        # No permission system in play (anon w/o guest, or
                        # perms disabled): assume both
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
                            {"value": k, "display": str(v)}
                            for k, v in field.choices
                        ]

                    field_metadata[field_name] = field_info
                except Exception:
                    field_metadata[field_name] = {"type": "unknown"}

        return field_metadata

    def _get_allowed_actions(self, model, user, snapshot):
        """Allowed CRUD actions for the calling user.

        Uses the snapshot's allowed_actions (which already applies the
        permission system uniformly).
        """
        if snapshot is None:
            # Permissions disabled or default Django perms — allow all
            return {
                "list": True,
                "retrieve": True,
                "create": True,
                "update": True,
                "partial_update": True,
                "destroy": True,
            }
        return {
            "list": snapshot.can_perform_action("read"),
            "retrieve": snapshot.can_perform_action("read"),
            "create": snapshot.can_perform_action("create"),
            "update": snapshot.can_perform_action("update"),
            "partial_update": snapshot.can_perform_action("update"),
            "destroy": snapshot.can_perform_action("delete"),
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

        # ?fields= client-driven field selection (compiled path only)
        try:
            from .compiler import is_compiled

            cap["client_fields_param"] = "fields" if is_compiled(view.model) else None
        except Exception:
            cap["client_fields_param"] = None

        return cap

    def _get_tenancy_info(self, model):
        """Tenancy and predicate summary for the model.

        Lets the frontend know whether the model is tenant-scoped, what
        predicates apply, and whether they need to send a tenant FK on
        write (or whether it's auto-filled). Does not leak the user's
        actual tenant value — just the structure."""
        from .predicates import get_predicates, get_tenant_field

        tenant_field = get_tenant_field(model)
        predicates = get_predicates(model)

        config = model.turbodrf() if hasattr(model, "turbodrf") else {}
        is_shared = config.get("tenancy") == "shared"

        info = {
            "tenant_field": tenant_field,
            "scoped": tenant_field is not None or bool(predicates),
            "shared": is_shared,
            "predicates": [type(p).__name__ for p in predicates],
        }
        return info

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
