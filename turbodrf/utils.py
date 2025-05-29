"""
Utility functions for TurboDRF
"""


def create_options_metadata(model, fields, user):
    """Generate metadata for OPTIONS requests."""
    from .settings import TURBODRF_ROLES

    user_permissions = set()
    for role in user.roles:
        user_permissions.update(TURBODRF_ROLES.get(role, []))

    metadata = {
        "name": model._meta.verbose_name,
        "description": model.__doc__ or "",
        "fields": {},
    }

    app_label = model._meta.app_label
    model_name = model._meta.model_name

    for field_name in fields:
        if "__" in field_name:
            # Handle nested fields
            base_field, nested = field_name.split("__", 1)
            if base_field not in metadata["fields"]:
                metadata["fields"][base_field] = {"type": "nested", "fields": {}}
            # Add nested field info
            continue

        try:
            field = model._meta.get_field(field_name)

            # Check permissions
            can_read = f"{app_label}.{model_name}.{field_name}.read" in user_permissions
            can_write = (
                f"{app_label}.{model_name}.{field_name}.write" in user_permissions
            )

            field_info = {
                "type": field.__class__.__name__,
                "required": not field.blank if hasattr(field, "blank") else True,
                "read_only": not can_write,
                "write_only": not can_read,
                "label": field.verbose_name,
                "help_text": field.help_text or "",
            }

            # Add choices if available
            if hasattr(field, "choices") and field.choices:
                field_info["choices"] = [
                    {"value": k, "display": v} for k, v in field.choices
                ]

            # Add max_length for char fields
            if hasattr(field, "max_length"):
                field_info["max_length"] = field.max_length

            metadata["fields"][field_name] = field_info

        except Exception as e:
            metadata["fields"][field_name] = {"type": "unknown", "error": str(e)}

    return metadata
