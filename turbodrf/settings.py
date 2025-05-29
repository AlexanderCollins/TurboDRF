"""
Define role-based permissions here.
Format: role_name -> list of permissions
"""

TURBODRF_ROLES = {
    "super_admin": [
        # Model-level permissions (all models)
        "app_name.model_name.read",
        "app_name.model_name.create",
        "app_name.model_name.update",
        "app_name.model_name.delete",
        # Field-level permissions
        "app_name.model_name.field_name.read",
        "app_name.model_name.field_name.write",
    ],
    "editor": [
        "app_name.model_name.read",
        "app_name.model_name.update",
        "app_name.model_name.field_name.read",
    ],
    "viewer": [
        "app_name.model_name.read",
        "app_name.model_name.field_name.read",
    ],
}
