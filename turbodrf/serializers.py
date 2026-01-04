import hashlib
import logging

from rest_framework import serializers

logger = logging.getLogger(__name__)


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
        that are defined using double-underscore notation. It handles both:
        - ForeignKey relationships: Flat fields
          (e.g., author__name → author_name)
        - ManyToMany relationships: Arrays of objects
          (e.g., categories__name → categories: [{name: ...}])

        Args:
            instance: The model instance to serialize.

        Returns:
            dict: The serialized representation including nested fields.

        Example:
            For a FK field 'author__name': adds 'author_name' as flat field
            For an M2M field 'categories__name': adds 'categories' as array of objects
        """
        data = super().to_representation(instance)

        # Handle nested fields if they're defined
        if hasattr(self.Meta, "_nested_fields"):
            for base_field, nested_fields in self.Meta._nested_fields.items():
                # Check if this is a ManyToMany field
                is_m2m = self._is_many_to_many_field(instance, base_field)

                if is_m2m:
                    # Handle ManyToMany: serialize as array of objects
                    data[base_field] = self._serialize_m2m_field(
                        instance, base_field, nested_fields
                    )
                else:
                    # Handle ForeignKey/OneToOne: serialize as flat fields
                    for nested_field in nested_fields:
                        # Handle both formats:
                        # 1. Full path: "author__name" (from factory/views)
                        # 2. Short form: "name" (manual serializer creation)
                        if nested_field.startswith(f"{base_field}__"):
                            full_field_path = nested_field
                        else:
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

    def _is_many_to_many_field(self, instance, field_name):
        """
        Check if a field is a ManyToManyField.

        Args:
            instance: Model instance
            field_name: Name of the field to check

        Returns:
            bool: True if the field is a ManyToManyField
        """
        try:
            field = instance._meta.get_field(field_name)
            return field.many_to_many
        except Exception:
            return False

    def _serialize_m2m_field(self, instance, base_field, nested_fields):
        """
        Serialize a ManyToMany field as an array of objects.

        Args:
            instance: Model instance
            base_field: Name of the M2M field (e.g., 'categories')
            nested_fields: List of nested field paths
                (e.g., ['categories__name', 'categories__id'])

        Returns:
            list: Array of dictionaries containing the nested field values

        Example:
            Input: categories__name, categories__id
            Output: [{"id": 66, "name": "Sales"}, {"id": 72, "name": "Marketing"}]
        """
        try:
            # Get the ManyToMany manager
            m2m_manager = getattr(instance, base_field, None)
            if m2m_manager is None:
                return []

            # Get all related objects (should be prefetched for performance)
            related_objects = m2m_manager.all()

            # Extract the field names to include (strip the base_field__ prefix)
            fields_to_extract = set()
            for nested_field in nested_fields:
                if nested_field.startswith(f"{base_field}__"):
                    # Extract the actual field name after the base field
                    field_parts = nested_field[len(base_field) + 2 :].split("__")
                    fields_to_extract.add(field_parts[0])  # Get first level field
                else:
                    fields_to_extract.add(nested_field)

            # Serialize each related object
            result = []
            for related_obj in related_objects:
                obj_data = {}
                for field_name in fields_to_extract:
                    try:
                        obj_data[field_name] = getattr(related_obj, field_name, None)
                    except Exception:
                        obj_data[field_name] = None
                result.append(obj_data)

            return result

        except Exception:
            return []

    def update(self, instance, validated_data):
        """
        Update instance with write permission checking.

        This method filters out fields that the user doesn't have write
        permission for before updating the instance.

        Uses permission snapshots for O(1) field permission checking.
        """
        # Get the request user from context
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            # Use snapshot if attached, otherwise build one
            if hasattr(self, "_permission_snapshot"):
                snapshot = self._permission_snapshot
            else:
                from .backends import (
                    build_permission_snapshot,
                    get_snapshot_from_request,
                )

                snapshot = get_snapshot_from_request(request, instance.__class__)
                if snapshot is None:
                    snapshot = build_permission_snapshot(
                        request.user, instance.__class__
                    )

            # Filter out fields without write permission using snapshot
            filtered_data = {}
            for field_name, value in validated_data.items():
                # O(1) check using snapshot
                if snapshot.has_write_rule(field_name):
                    # Field has explicit write permission rule
                    if snapshot.can_write_field(field_name):
                        filtered_data[field_name] = value
                elif snapshot.can_perform_action("update"):
                    # No explicit field rule, use model-level permission
                    filtered_data[field_name] = value

            validated_data = filtered_data

        return super().update(instance, validated_data)

    def create(self, validated_data):
        """
        Create instance with write permission checking.

        This method filters out fields that the user doesn't have write
        permission for before creating the instance.

        Uses permission snapshots for O(1) field permission checking.
        """
        # Get the request user from context
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            # Use snapshot if attached, otherwise build one
            if hasattr(self, "_permission_snapshot"):
                snapshot = self._permission_snapshot
            else:
                from .backends import (
                    build_permission_snapshot,
                    get_snapshot_from_request,
                )

                model = self.Meta.model
                snapshot = get_snapshot_from_request(request, model)
                if snapshot is None:
                    snapshot = build_permission_snapshot(request.user, model)

            # Filter out fields without write permission using snapshot
            filtered_data = {}
            for field_name, value in validated_data.items():
                # O(1) check using snapshot
                if snapshot.has_write_rule(field_name):
                    # Field has explicit write permission rule
                    if snapshot.can_write_field(field_name):
                        filtered_data[field_name] = value
                elif snapshot.can_perform_action("create"):
                    # No explicit field rule, use model-level permission
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
    def create_serializer(cls, model, fields, user, view_type="list", snapshot=None):
        """
        Create a dynamic serializer class tailored to user permissions.

        This method generates a serializer class at runtime that includes only
        the fields the user has permission to read, and marks fields as
        read-only if the user lacks write permission.

        Uses permission snapshots for O(1) field permission checking.

        Args:
            model: The Django model class to serialize.
            fields: List of field names to include, supporting nested notation
                   (e.g., ['title', 'author__name', 'category__parent__title']).
            user: The user object with 'roles' attribute for permission checking.
            view_type: The type of view ('list' or 'detail') for context-specific
                      serialization. Defaults to 'list'.
            snapshot: Optional PermissionSnapshot to use (for performance)

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

        # Build snapshot if not provided
        if snapshot is None:
            from .backends import build_permission_snapshot

            snapshot = build_permission_snapshot(user, model)

        # Filter fields based on permissions using snapshot
        # AND nested permission checking
        permitted_fields = cls._get_permitted_fields_with_snapshot(model, fields, user)

        # Handle nested fields
        nested_fields = {}
        simple_fields = []

        for field in permitted_fields:
            if "__" in field:
                base_field = field.split("__")[0]
                if base_field not in nested_fields:
                    nested_fields[base_field] = []
                # Store full path (not remainder) for consistency with non-factory path
                # This fixes multi-level nesting: author__parent__title
                nested_fields[base_field].append(field)
            else:
                simple_fields.append(field)

        # Add base fields for nested fields if not already present
        # This ensures FK id fields are included (e.g., 'author' for 'author__name')
        for base_field in nested_fields:
            if base_field not in simple_fields:
                simple_fields.append(base_field)

        # Create variables for the closure
        model_class = model
        # Only include simple fields in the final field list, not the
        # nested serializer keys
        # This prevents issues with writable foreign keys being replaced
        # by read-only nested serializers
        all_fields = simple_fields
        read_only_fields_list = cls._get_read_only_fields_with_snapshot(
            model, simple_fields, snapshot
        )
        nested_fields_meta = nested_fields if nested_fields else {}

        # Generate unique ref_name for swagger schema generation
        fields_hash = hashlib.md5(",".join(sorted(all_fields)).encode()).hexdigest()[:8]
        app_label = model_class._meta.app_label
        model_name = model_class._meta.model_name
        ref_name_value = f"{app_label}_{model_name}_{view_type}_{fields_hash}"

        # Store snapshot for use in create/update methods
        snapshot_to_use = snapshot

        # Create the main serializer class
        class DynamicSerializer(TurboDRFSerializer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

                # The base field should remain writable for
                # create/update operations
                # Nested serializers are used for display only

                # Attach snapshot to serializer for create/update
                if snapshot_to_use:
                    self._permission_snapshot = snapshot_to_use

            class Meta:
                model = model_class
                fields = all_fields
                read_only_fields = read_only_fields_list
                # Include nested fields metadata for TurboDRFSerializer
                _nested_fields = nested_fields_meta
                # Unique ref_name for swagger schema generation
                ref_name = ref_name_value

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
            # Output: ['title', 'content', 'author__name']
            # (model-level permission grants all)
        """
        from django.conf import settings

        TURBODRF_ROLES = getattr(settings, "TURBODRF_ROLES", {})

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
    def _get_permitted_fields_with_snapshot(cls, model, fields, user):
        """
        Filter fields based on user's read permissions with nested permission checking.

        This version validates nesting depth and checks permissions at each level
        of nested field paths.

        Args:
            model: The Django model class.
            fields: List of field names to check, may include nested fields.
            user: Django user object for permission checking

        Returns:
            list: Filtered list of field names the user can read.
        """
        from .validation import check_nested_field_permissions, validate_nesting_depth

        permitted = []

        # First check if we should handle fields as "__all__"
        if fields == "__all__":
            # Get all model fields
            fields = [f.name for f in model._meta.fields]

        for field in fields:
            # Validate nesting depth
            try:
                validate_nesting_depth(field)
            except Exception as e:
                # Log and skip fields that exceed nesting depth
                logger.warning(f"Skipping field '{field}': {str(e)}")
                continue

            # Check nested permissions (traverses relationships)
            if check_nested_field_permissions(model, field, user):
                permitted.append(field)

        return permitted

    @classmethod
    def _get_user_permissions_set(cls, user):
        """Get all permissions for a user as a set."""
        from django.conf import settings

        TURBODRF_ROLES = getattr(settings, "TURBODRF_ROLES", {})
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

        TURBODRF_ROLES = getattr(settings, "TURBODRF_ROLES", {})

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
    def _get_read_only_fields_with_snapshot(cls, model, fields, snapshot):
        """
        Determine which fields should be read-only based on write permissions.

        This is the optimized version using permission snapshots.

        Args:
            model: The Django model class.
            fields: List of field names to check for write permissions.
            snapshot: PermissionSnapshot with pre-computed permissions

        Returns:
            list: Field names that should be marked as read-only.
        """
        read_only = []

        for field in fields:
            # Check field write permission using snapshot
            if not snapshot.can_write_field(field):
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

        # Generate unique ref_name for swagger schema generation
        fields_hash = hashlib.md5(",".join(sorted(field_list)).encode()).hexdigest()[:8]
        app_label = model_class._meta.app_label
        model_name = model_class._meta.model_name
        nested_ref_name = f"{app_label}_{model_name}_nested_{fields_hash}"

        class NestedSerializer(serializers.ModelSerializer):
            class Meta:
                model = model_class
                fields = field_list
                # Unique ref_name for swagger schema generation
                ref_name = nested_ref_name

        return NestedSerializer
