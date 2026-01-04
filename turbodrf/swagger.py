"""
TurboDRF Swagger/OpenAPI Schema Generation

This module provides role-based OpenAPI schema generation for TurboDRF APIs.
It extends drf-yasg's schema generation to filter API documentation based on
user roles and permissions, ensuring users only see the parts of the API
they are authorized to access.

Key Features:
    - Dynamic schema filtering based on user roles
    - Field-level permission filtering in response schemas
    - Session-based role selection for documentation viewing
    - Automatic permission extraction from URL patterns
    - Custom action parameter handling
"""

from drf_yasg.generators import OpenAPISchemaGenerator
from drf_yasg.inspectors import SwaggerAutoSchema


class RoleBasedSchemaGenerator(OpenAPISchemaGenerator):
    """
    Custom OpenAPI schema generator that filters based on user roles.

    This generator extends drf-yasg's OpenAPISchemaGenerator to provide
    role-based filtering of API documentation. It ensures that users only
    see endpoints and fields they have permission to access based on their
    assigned roles in the TurboDRF permission system.

    The generator works by:
    1. Intercepting the schema generation process
    2. Checking the current user's role (from query param or session)
    3. Filtering paths and operations based on role permissions
    4. Filtering response schema fields based on field-level permissions

    Attributes:
        current_role (str): The role to use for filtering the schema.
                           Set from request query parameters or session.

    Example:
        # User with 'viewer' role sees only GET endpoints
        # User with 'admin' role sees all CRUD operations
        # Each role sees only the fields they have permission to read
    """

    def __init__(self, info, version="", url=None, patterns=None, urlconf=None):
        super().__init__(info, version, url, patterns, urlconf)
        self.current_role = None

    def get_schema(self, request=None, public=False):
        """
        Generate OpenAPI schema filtered by user role.

        This method overrides the default schema generation to apply
        role-based filtering. It processes the complete schema and
        removes paths, operations, and fields that the current role
        doesn't have permission to access.

        Args:
            request: The HTTP request object containing role information.
                    Role can be specified via 'role' query parameter or
                    stored in session as 'api_role'.
            public: Whether to generate a public schema (currently unused).

        Returns:
            dict: The filtered OpenAPI schema containing only the paths,
                 operations, and fields accessible to the current role.

        Role Selection:
            1. Query parameter: ?role=admin
            2. Session storage: request.session['api_role']
            3. No role: Shows unfiltered schema (for backwards compatibility)

        Example:
            GET /api/schema/?role=editor
            Returns schema showing only endpoints and fields accessible
            to users with the 'editor' role.
        """
        # Get role from query parameter or session
        if request:
            self.current_role = request.GET.get("role", request.session.get("api_role"))

        schema = super().get_schema(request, public)

        if self.current_role:
            # Filter paths based on role permissions
            filtered_paths = {}
            from .settings import TURBODRF_ROLES

            permissions = set(TURBODRF_ROLES.get(self.current_role, []))

            for path, methods in schema["paths"].items():
                filtered_methods = {}

                for method, operation in methods.items():
                    # Extract model info from path
                    model_info = self._extract_model_info(path)
                    if model_info and self._has_permission(
                        model_info, method, permissions
                    ):
                        # Filter response schema fields
                        if "responses" in operation:
                            for status_code, response in operation["responses"].items():
                                if "schema" in response:
                                    response["schema"] = self._filter_schema_fields(
                                        response["schema"], model_info, permissions
                                    )

                        filtered_methods[method] = operation

                if filtered_methods:
                    filtered_paths[path] = filtered_methods

            schema["paths"] = filtered_paths

        return schema

    def _extract_model_info(self, path):
        """
        Extract model information from API endpoint path.

        This method parses the API path to determine which Django model
        it corresponds to. This information is used to check permissions
        for the endpoint.

        Args:
            path (str): The API endpoint path (e.g., '/api/articles/').

        Returns:
            dict: Contains 'app_label' and 'model_name' if extraction
                 succeeds, None otherwise.

        Path Format:
            Expected: /api/{model_name_plural}/
            Example: /api/articles/ -> {app_label: 'myapp', model_name: 'article'}

        Note:
            This is a simplified implementation that assumes:
            - URLs follow the pattern /api/{model_name_plural}/
            - Model names are pluralized with simple 's' suffix
            - All models belong to 'myapp' (should be made configurable)

        TODO:
            - Extract app_label from URL or model registry
            - Handle complex pluralization rules
            - Support nested resources (e.g., /api/articles/1/comments/)
        """
        # This is a simplified version - adjust based on your URL patterns
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "api":
            # Get the model name from URL
            model_name = parts[1].rstrip("s")  # Remove plural 's'

            # Try to find the actual app label from registered models
            from django.apps import apps

            for model in apps.get_models():
                if model._meta.model_name == model_name:
                    return {
                        "app_label": model._meta.app_label,
                        "model_name": model_name,
                    }

            # Fallback to books app for this example
            return {
                "app_label": "books",
                "model_name": model_name,
            }
        return None

    def _has_permission(self, model_info, method, permissions):
        """
        Check if the current role has permission for an HTTP method.

        This method maps HTTP methods to CRUD operations and checks if
        the role has the corresponding permission for the model.

        Args:
            model_info (dict): Contains 'app_label' and 'model_name'.
            method (str): HTTP method (GET, POST, PUT, PATCH, DELETE).
            permissions (set): Set of permission strings for the current role.

        Returns:
            bool: True if the role has permission for this operation.

        Permission Format:
            '{app_label}.{model_name}.{operation}'
            Example: 'myapp.article.read', 'myapp.article.create'

        Method Mapping:
            - GET -> read
            - POST -> create
            - PUT/PATCH -> update
            - DELETE -> delete

        Example:
            # Check if role can read articles
            _has_permission(
                {'app_label': 'myapp', 'model_name': 'article'},
                'GET',
                {'myapp.article.read', 'myapp.article.create'}
            )  # Returns True
        """
        method_map = {
            "get": "read",
            "post": "create",
            "put": "update",
            "patch": "update",
            "delete": "delete",
        }

        perm_type = method_map.get(method.lower())
        if not perm_type:
            return False

        required_perm = (
            f"{model_info['app_label']}.{model_info['model_name']}.{perm_type}"
        )
        return required_perm in permissions

    def _filter_schema_fields(self, schema, model_info, permissions):
        """
        Filter response schema fields based on field-level permissions.

        This method processes the response schema for an endpoint and removes
        fields that the current role doesn't have permission to read. This
        ensures that API documentation accurately reflects what data users
        will actually receive.

        Args:
            schema (dict): The response schema containing field definitions.
            model_info (dict): Contains 'app_label' and 'model_name'.
            permissions (set): Set of permission strings for the current role.

        Returns:
            dict: The filtered schema with only permitted fields.

        Permission Format:
            '{app_label}.{model_name}.{field_name}.read'
            Example: 'myapp.article.title.read'

        Schema Structure:
            {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'title': {'type': 'string'},
                    'secret_field': {'type': 'string'}  # Removed if no permission
                }
            }

        Example:
            # Role has permissions:
            # ['myapp.article.id.read', 'myapp.article.title.read']
            # Input schema has fields: id, title, secret_field
            # Output schema will only include: id, title
        """
        if "properties" not in schema:
            return schema

        filtered_properties = {}
        for field_name, field_schema in schema["properties"].items():
            field_perm = (
                f"{model_info['app_label']}.{model_info['model_name']}."
                f"{field_name}.read"
            )
            if field_perm in permissions:
                filtered_properties[field_name] = field_schema

        schema["properties"] = filtered_properties
        return schema

    def get_endpoints(self, request=None):
        """
        Get API endpoints, filtering out duplicate no-slash variants.

        This override filters out the duplicate URL patterns created for
        trailing slash handling, preventing duplicate entries in the
        Swagger documentation.
        """
        endpoints = super().get_endpoints(request)

        # Filter out _no_slash variants
        filtered_endpoints = []
        for path, path_regex, method, callback in endpoints:
            # Skip endpoints with _no_slash suffix in the URL name
            if hasattr(callback, "cls") and hasattr(callback.cls, "_basename"):
                # Check if this is a duplicate no-slash endpoint
                # by looking at the URL pattern name
                if (
                    hasattr(callback, "actions")
                    and hasattr(callback, "name")
                    and callback.name
                    and callback.name.endswith("_no_slash")
                ):
                    continue

            filtered_endpoints.append((path, path_regex, method, callback))

        return filtered_endpoints


class TurboDRFSwaggerAutoSchema(SwaggerAutoSchema):
    """
    Custom SwaggerAutoSchema for TurboDRF ViewSets.

    This schema inspector prevents custom actions from incorrectly showing
    all model fields as request parameters. Custom actions decorated with
    @action should only show their actual parameters, not the entire model
    serializer.
    """

    def get_request_body_parameters(self, consumes):
        """
        Get request body parameters for the current operation.

        For custom actions (methods decorated with @action), this method
        returns an empty list to prevent drf-yasg from including all model
        fields in the request schema.

        For standard CRUD operations (create, update, etc.), it delegates
        to the parent implementation which correctly includes model fields.
        """
        # Check if this is a custom action
        if hasattr(self.view, "action"):
            action = self.view.action

            # Standard actions that should include model fields
            standard_actions = [
                "create",
                "update",
                "partial_update",
                "list",
                "retrieve",
            ]

            # If it's a custom action (not in standard actions), don't include
            # model fields unless explicitly defined
            if action not in standard_actions:
                # For custom actions, only include explicitly defined parameters
                # Don't auto-generate from the serializer
                return []

        # For standard actions, use the default behavior
        return super().get_request_body_parameters(consumes)

    def get_request_serializer(self):
        """
        Get the request serializer for the current operation.

        For write operations (create, update, partial_update), returns a serializer
        that includes ALL writable fields based on user permissions, ensuring Swagger
        documentation shows complete field set for write operations.

        For custom actions, returns None to prevent automatic serializer
        field inclusion in the request schema.
        """
        # Check if this is a custom action
        if hasattr(self.view, "action"):
            action = self.view.action

            # Write operations that should show all writable fields
            write_actions = ["create", "update", "partial_update"]

            # If it's a write operation, generate schema with all writable fields
            if action in write_actions:
                return self._get_write_operation_serializer()

            # Standard read actions (use default)
            standard_actions = ["list", "retrieve"]
            if action in standard_actions:
                return super().get_request_serializer()

            # Custom actions - check for explicit serializer
            action_method = getattr(self.view, action, None)
            if action_method and hasattr(action_method, "kwargs"):
                serializer_class = action_method.kwargs.get("serializer_class")
                if serializer_class:
                    return serializer_class()

            # No serializer for request body
            return None

        # For standard actions, use the default behavior
        return super().get_request_serializer()

    def _get_write_operation_serializer(self):
        """
        Generate a serializer for write operations with all writable fields.

        This ensures Swagger documentation shows ALL fields that can be written,
        not just the fields configured for list/detail views.

        By default, fields are filtered based on user permissions to maintain
        security in documentation. However, this can be disabled for development
        by setting TURBODRF_SWAGGER_SHOW_ALL_FIELDS=True, which shows all fields
        in Swagger regardless of permissions (API still enforces permissions).

        Returns:
            Serializer instance with all writable fields for the user's role.
        """
        from django.conf import settings

        # Get the model from the view
        if not hasattr(self.view, "model"):
            return super().get_request_serializer()

        model_class = self.view.model

        # Get all fields defined in turbodrf() configuration
        if hasattr(model_class, "turbodrf"):
            config = model_class.turbodrf()
            all_fields = config.get("fields", "__all__")

            # If fields is a dict with list/detail, get the detail fields
            # (which typically has more fields than list)
            if isinstance(all_fields, dict):
                # Prefer detail fields, fallback to list
                all_fields = all_fields.get("detail", all_fields.get("list", "__all__"))

            # For write operations in Swagger, we want to show all configured fields
            # The actual API will still enforce permissions via the serializer
            # This is just for documentation purposes
            from .serializers import TurboDRFSerializer

            # Capture variables for the closure
            fields_to_use = all_fields
            ref_name_value = f"{model_class._meta.model_name}_write"

            # Check if we should show all fields regardless of permissions
            # (useful for development/documentation purposes)
            show_all_fields = getattr(
                settings, "TURBODRF_SWAGGER_SHOW_ALL_FIELDS", False
            )

            # Create a serializer with all configured fields
            class WriteOperationSerializer(TurboDRFSerializer):
                class Meta:
                    model = model_class
                    fields = fields_to_use
                    ref_name = ref_name_value

                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    # If showing all fields for Swagger, don't filter by permissions
                    # This is ONLY for documentation - API still enforces permissions
                    if show_all_fields:
                        # Override to show all fields in Swagger
                        # The actual API requests will still be permission-filtered
                        pass

            return WriteOperationSerializer()

        # Fallback to default behavior
        return super().get_request_serializer()
