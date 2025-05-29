from rest_framework import serializers
from django.db import models


class TurboDRFSerializer(serializers.ModelSerializer):
    """
    Base serializer for TurboDRF models with support for nested field notation.

    This serializer extends Django REST Framework's ModelSerializer to provide
    automatic handling of nested field relationships using double-underscore notation.

    Features:
        - Automatic traversal of related fields using '__' notation
        - Graceful handling of null relationships
        - Conversion of nested field names to underscore format in output

    Example:
        If your model configuration includes fields like 'author__name' or
        'category__parent__title', this serializer will automatically traverse
        these relationships and include them in the serialized output as
        'author_name' and 'category_parent_title' respectively.

    Note:
        The nested field functionality is activated when the Meta class
        contains a '_nested_fields' attribute, which is typically set by
        the TurboDRFSerializerFactory.
    """

    def to_representation(self, instance):
        """
        Convert a model instance to a dictionary representation.

        This method extends the default serialization to include nested fields
        that are defined using double-underscore notation. It traverses
        relationships and adds the resulting values to the output dictionary.

        Args:
            instance: The model instance to serialize.

        Returns:
            dict: The serialized representation including nested fields.

        Example:
            For a field definition 'author__name', this method will:
            1. Navigate from the instance to instance.author.name
            2. Add the value to the output as 'author_name'
            3. Handle None values gracefully without raising exceptions
        """
        data = super().to_representation(instance)

        # Handle nested fields if they're defined
        if hasattr(self.Meta, "_nested_fields"):
            for base_field, nested_fields in self.Meta._nested_fields.items():
                # Process each nested field
                for nested_field in nested_fields:
                    # Construct full field path
                    if "__" in nested_field:
                        # Already a full path
                        full_field_path = nested_field
                    else:
                        # Partial path, prepend base field
                        full_field_path = f"{base_field}__{nested_field}"

                    # Navigate through the relationship
                    value = instance
                    try:
                        for part in full_field_path.split("__"):
                            if value is None:
                                break
                            value = getattr(value, part, None)

                        # Add the nested field value with underscores
                        field_name = full_field_path.replace("__", "_")
                        data[field_name] = value
                    except Exception:
                        pass

        return data

    def update(self, instance, validated_data):
        """
        Update instance with write permission checking.

        This method filters out fields that the user doesn't have write
        permission for before updating the instance.
        """
        # Get the request user from context
        request = self.context.get("request")
        if request and hasattr(request.user, "roles"):
            # Check field write permissions
            from django.conf import settings
            TURBODRF_ROLES = getattr(settings, 'TURBODRF_ROLES', {})
            user_permissions = set()
            for role in request.user.roles:
                user_permissions.update(TURBODRF_ROLES.get(role, []))

            # Filter out fields without write permission
            app_label = instance._meta.app_label
            model_name = instance._meta.model_name

            filtered_data = {}
            for field_name, value in validated_data.items():
                # Check field write permission
                field_perm = f"{app_label}.{model_name}.{field_name}.write"
                model_perm = f"{app_label}.{model_name}.update"

                # Check if field has specific write permission
                # defined in ANY role
                has_field_write_perms = any(
                    perm.startswith(f"{app_label}.{model_name}.{field_name}.")
                    and perm.endswith(".write")
                    for role_perms in TURBODRF_ROLES.values()
                    for perm in role_perms
                )

                if has_field_write_perms:
                    # Field-level write permissions exist,
                    # check user has it
                    if field_perm in user_permissions:
                        filtered_data[field_name] = value
                elif model_perm in user_permissions:
                    # No field-level write permission defined, use model permission
                    filtered_data[field_name] = value

            validated_data = filtered_data

        return super().update(instance, validated_data)

    def create(self, validated_data):
        """
        Create instance with write permission checking.

        This method filters out fields that the user doesn't have write
        permission for before creating the instance.
        """
        # Get the request user from context
        request = self.context.get("request")
        if request and hasattr(request.user, "roles"):
            # Check field write permissions
            from django.conf import settings
            TURBODRF_ROLES = getattr(settings, 'TURBODRF_ROLES', {})
            user_permissions = set()
            for role in request.user.roles:
                user_permissions.update(TURBODRF_ROLES.get(role, []))

            # Filter out fields without write permission
            model = self.Meta.model
            app_label = model._meta.app_label
            model_name = model._meta.model_name

            filtered_data = {}
            for field_name, value in validated_data.items():
                # Check field write permission
                field_perm = f"{app_label}.{model_name}.{field_name}.write"
                model_perm = f"{app_label}.{model_name}.create"

                # Check if field has specific write permission
                # defined in ANY role
                has_field_write_perms = any(
                    perm.startswith(f"{app_label}.{model_name}.{field_name}.")
                    and perm.endswith(".write")
                    for role_perms in TURBODRF_ROLES.values()
                    for perm in role_perms
                )

                if has_field_write_perms:
                    # Field-level write permissions exist,
                    # check user has it
                    if field_perm in user_permissions:
                        filtered_data[field_name] = value
                elif model_perm in user_permissions:
                    # No field-level write permission defined, use model permission
                    filtered_data[field_name] = value

            validated_data = filtered_data

        return super().create(validated_data)


class TurboDRFSerializerFactory:
    """
    Factory class for creating dynamic serializers based on user permissions.

    This factory generates serializer classes at runtime that respect the
    TurboDRF permission system. It filters fields based on user roles and
    permissions, creates nested serializers for related fields, and sets
    appropriate read-only constraints.

    Key Features:
        - Dynamic field filtering based on user permissions
        - Automatic nested serializer creation for related fields
        - Read-only field detection based on write permissions
        - Support for both simple and nested field notation

    The factory integrates with Django's permission system and TurboDRF's
    role-based access control to ensure users only see and modify fields
    they have permission to access.

    Example:
        # Create a serializer for a specific user and model
        serializer_class = TurboDRFSerializerFactory.create_serializer(
            model=Article,
            fields=['title', 'content', 'author__name', 'category__title'],
            user=request.user,
            view_type='list'
        )

        # Use the generated serializer
        serializer = serializer_class(queryset, many=True)
    """

    @classmethod
    def create_serializer(cls, model, fields, user, view_type="list"):
        """
        Create a dynamic serializer class tailored to user permissions.

        This method generates a serializer class at runtime that includes only
        the fields the user has permission to read, and marks fields as
        read-only if the user lacks write permission.

        Args:
            model: The Django model class to serialize.
            fields: List of field names to include, supporting nested notation
                   (e.g., ['title', 'author__name', 'category__parent__title']).
            user: The user object with 'roles' attribute for permission checking.
            view_type: The type of view ('list' or 'detail') for context-specific
                      serialization. Defaults to 'list'.

        Returns:
            type: A dynamically created serializer class inheriting from
                 ModelSerializer with appropriate field configuration.

        Example:
            # For a user with 'editor' role having permissions:
            # - myapp.article.title.read
            # - myapp.article.title.write
            # - myapp.article.author.read (no write)

            serializer_class = cls.create_serializer(
                model=Article,
                fields=['title', 'author__name', 'content'],
                user=editor_user
            )
            # Result: serializer with 'title' (read-write), 'author' (read-only)
            # 'content' excluded due to lack of read permission
        """

        # Filter fields based on permissions
        permitted_fields = cls._get_permitted_fields(model, fields, user)

        # Debug: ensure we have required fields for the model
        # If 'related' field is required but filtered out due to
        # permissions,
        # we need to include it anyway for write operations
        if hasattr(model, "_meta"):
            for field in model._meta.fields:
                if (
                    not field.null
                    and field.name not in permitted_fields
                    and field.name in fields
                ):
                    # This is a required field that was filtered out - add it back
                    if isinstance(field, models.ForeignKey):
                        # For foreign keys, check if user has
                        # model-level write permission
                        model_write_perm = (
                            f"{model._meta.app_label}.{model._meta.model_name}.create"
                        )
                        user_permissions = cls._get_user_permissions_set(user)
                        if model_write_perm in user_permissions:
                            permitted_fields.append(field.name)

        # Handle nested fields
        nested_fields = {}
        simple_fields = []

        for field in permitted_fields:
            if "__" in field:
                base_field, nested_field = field.split("__", 1)
                if base_field not in nested_fields:
                    nested_fields[base_field] = []
                nested_fields[base_field].append(nested_field)
            else:
                simple_fields.append(field)

        # Create nested serializers
        nested_serializers = {}
        for base_field, nested_field_list in nested_fields.items():
            try:
                related_model = model._meta.get_field(base_field).related_model
                nested_serializer = cls._create_nested_serializer(
                    related_model, nested_field_list, user
                )
                nested_serializers[base_field] = nested_serializer
            except Exception:
                continue

        # Create variables for the closure
        model_class = model
        # Only include simple fields in the final field list, not the
        # nested serializer keys
        # This prevents issues with writable foreign keys being replaced
        # by read-only nested serializers
        all_fields = simple_fields
        read_only_fields_list = cls._get_read_only_fields(model, simple_fields, user)
        nested_fields_meta = nested_fields if nested_fields else {}

        # Create the main serializer class
        class DynamicSerializer(TurboDRFSerializer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

                # The base field should remain writable for
                # create/update operations
                # Nested serializers are used for display only
                pass

            class Meta:
                model = model_class
                fields = all_fields
                read_only_fields = read_only_fields_list
                # Include nested fields metadata for TurboDRFSerializer
                _nested_fields = nested_fields_meta

        return DynamicSerializer

    @classmethod
    def _get_permitted_fields(cls, model, fields, user):
        """
        Filter fields based on user's read permissions.

        This method checks each field against the user's permissions and returns
        only those fields the user is allowed to read. It supports both simple
        field names and nested field notation.

        Args:
            model: The Django model class.
            fields: List of field names to check, may include nested fields.
            user: User object with 'roles' attribute containing role names.

        Returns:
            list: Filtered list of field names the user can read.

        Permission Format:
            - Field-level: '{app_label}.{model_name}.{field_name}.read'
            - Model-level: '{app_label}.{model_name}.read' (grants all fields)

        Example:
            # User with permissions: ['myapp.article.title.read', 'myapp.article.read']
            # Input fields: ['title', 'content', 'author__name']
            # Output: ['title', 'content', 'author__name'] (model-level permission grants all)
        """
        from django.conf import settings
        TURBODRF_ROLES = getattr(settings, 'TURBODRF_ROLES', {})

        user_permissions = set()
        for role in user.roles:
            user_permissions.update(TURBODRF_ROLES.get(role, []))

        permitted = []
        app_label = model._meta.app_label
        model_name = model._meta.model_name

        # First check if we should handle fields as "__all__"
        if fields == "__all__":
            # Get all model fields
            fields = [f.name for f in model._meta.fields]

        # Get all defined field permissions for this model across
        # ALL roles
        # Only check read permissions - write permissions alone don't
        # restrict reading
        all_field_perms_read = set()
        for role_name, role_perms in TURBODRF_ROLES.items():
            for perm in role_perms:
                parts = perm.split(".")
                if (
                    len(parts) == 4
                    and parts[0] == app_label
                    and parts[1] == model_name
                    and parts[3] == "read"
                ):
                    # This is a field read permission for this model
                    all_field_perms_read.add(parts[2])

        for field in fields:
            base_field = field.split("__")[0]

            # Check if there are any field-level READ permissions
            # defined for this field
            if base_field in all_field_perms_read:
                # Field-level read permissions exist, so check for
                # read permission
                field_perm = f"{app_label}.{model_name}.{base_field}.read"
                if field_perm in user_permissions:
                    permitted.append(field)
            else:
                # No field-level read permissions defined, fall back to
                # model-level permission
                model_perm = f"{app_label}.{model_name}.read"
                if model_perm in user_permissions:
                    permitted.append(field)

        return permitted

    @classmethod
    def _get_user_permissions_set(cls, user):
        """Get all permissions for a user as a set."""
        from django.conf import settings
        TURBODRF_ROLES = getattr(settings, 'TURBODRF_ROLES', {})
        user_permissions = set()
        for role in user.roles:
            user_permissions.update(TURBODRF_ROLES.get(role, []))
        return user_permissions

    @classmethod
    def _get_read_only_fields(cls, model, fields, user):
        """
        Determine which fields should be read-only based on write permissions.

        This method identifies fields that the user can read but not write to,
        ensuring data integrity by preventing unauthorized modifications.

        Args:
            model: The Django model class.
            fields: List of field names to check for write permissions.
            user: User object with 'roles' attribute.

        Returns:
            list: Field names that should be marked as read-only.

        Permission Format:
            - Write permission: '{app_label}.{model_name}.{field_name}.write'

        Note:
            Fields without write permission are automatically made read-only,
            even if the user has read permission. This prevents validation
            errors when users attempt to modify restricted fields.

        Example:
            # User has 'myapp.article.title.read' but not 'myapp.article.title.write'
            # Result: 'title' will be in the read-only fields list
        """
        from django.conf import settings
        TURBODRF_ROLES = getattr(settings, 'TURBODRF_ROLES', {})

        user_permissions = set()
        for role in user.roles:
            user_permissions.update(TURBODRF_ROLES.get(role, []))

        read_only = []
        app_label = model._meta.app_label
        model_name = model._meta.model_name

        for field in fields:
            # Check field write permission
            field_perm = f"{app_label}.{model_name}.{field}.write"
            if field_perm not in user_permissions:
                read_only.append(field)

        return read_only

    @classmethod
    def _create_nested_serializer(cls, model, fields, user):
        """
        Create a nested serializer for related model fields.

        This method generates a simple serializer for related objects that
        includes only the specified fields. It's used internally to handle
        nested field relationships.

        Args:
            model: The related model class to serialize.
            fields: List of field names to include in the nested serializer.
            user: User object (currently unused but available for future
                 permission filtering at nested levels).

        Returns:
            type: A dynamically created ModelSerializer subclass.

        Note:
            Currently creates a simple serializer without permission checking
            at the nested level. Future versions may implement recursive
            permission filtering for nested serializers.

        Example:
            # For author__name field on Article model
            nested_serializer = cls._create_nested_serializer(
                model=User,
                fields=['name'],
                user=request.user
            )
            # Returns serializer that only includes 'name' field from User model
        """

        # Create variables for the closure
        model_class = model
        field_list = fields

        class NestedSerializer(serializers.ModelSerializer):
            class Meta:
                model = model_class
                fields = field_list

        return NestedSerializer
