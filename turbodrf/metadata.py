from rest_framework.metadata import SimpleMetadata


class TurboDRFMetadata(SimpleMetadata):
    """Custom metadata handler for TurboDRF OPTIONS requests."""

    def determine_metadata(self, request, view):
        """Return metadata for OPTIONS requests."""
        metadata = super().determine_metadata(request, view)

        if hasattr(view, "model"):
            model = view.model
            view_type = (
                "detail"
                if view.action in ["retrieve", "update", "partial_update"]
                else "list"
            )
            fields = model.get_api_fields(view_type)

            # Add model info
            metadata["model"] = {
                "name": model._meta.verbose_name,
                "app_label": model._meta.app_label,
                "fields": self._get_field_metadata(
                    model, fields, request.user
                ),
            }

            # Add allowed actions based on permissions
            metadata["actions"] = self._get_allowed_actions(
                model, request.user
            )

        return metadata

    def _get_field_metadata(self, model, fields, user):
        """Get metadata for each field."""
        from .settings import TURBODRF_ROLES

        user_permissions = set()
        if hasattr(user, "roles"):
            for role in user.roles:
                user_permissions.update(TURBODRF_ROLES.get(role, []))

        field_metadata = {}
        app_label = model._meta.app_label
        model_name = model._meta.model_name

        for field_name in fields:
            if "__" in field_name:
                # Handle nested fields
                base_field = field_name.split("__")[0]
                if base_field not in field_metadata:
                    field_metadata[base_field] = {
                        "type": "nested", "fields": []
                    }
                field_metadata[base_field]["fields"].append(
                    field_name.split("__", 1)[1]
                )
            else:
                try:
                    field = model._meta.get_field(field_name)
                    can_read = (
                        f"{app_label}.{model_name}.{field_name}.read"
                        in user_permissions
                    )
                    can_write = (
                        f"{app_label}.{model_name}.{field_name}.write"
                        in user_permissions
                    )

                    field_info = {
                        "type": field.__class__.__name__,
                        "required": (
                            not field.blank
                            if hasattr(field, "blank")
                            else True
                        ),
                        "read_only": not can_write,
                        "write_only": not can_read and can_write,
                        "label": field.verbose_name,
                        "help_text": field.help_text or "",
                    }

                    # Add field-specific metadata
                    if hasattr(field, "max_length"):
                        field_info["max_length"] = field.max_length
                    if hasattr(field, "choices") and field.choices:
                        field_info["choices"] = [
                            {"value": k, "display": v}
                            for k, v in field.choices
                        ]

                    field_metadata[field_name] = field_info
                except Exception:
                    field_metadata[field_name] = {"type": "unknown"}

        return field_metadata

    def _get_allowed_actions(self, model, user):
        """Get allowed actions based on user permissions."""
        from .settings import TURBODRF_ROLES

        user_permissions = set()
        if hasattr(user, "roles"):
            for role in user.roles:
                user_permissions.update(TURBODRF_ROLES.get(role, []))

        app_label = model._meta.app_label
        model_name = model._meta.model_name

        return {
            "list": f"{app_label}.{model_name}.read" in user_permissions,
            "retrieve": f"{app_label}.{model_name}.read" in user_permissions,
            "create": f"{app_label}.{model_name}.create" in user_permissions,
            "update": f"{app_label}.{model_name}.update" in user_permissions,
            "partial_update": (
                f"{app_label}.{model_name}.update" in user_permissions
            ),
            "destroy": f"{app_label}.{model_name}.delete" in user_permissions,
        }
