import hashlib
import logging

from rest_framework import serializers

logger = logging.getLogger(__name__)


def _apply_predicate_writes(model, validated_data, instance, request):
    """Run write enforcement for create/update under the two-layer model.

    Layer 1 (mandatory tenant boundary):
      - Validate that any provided tenant_field value matches caller's tenant
      - Auto-fill tenant_field if missing
    Layer 2 (within-tenant predicates):
      - Run validate_write on each predicate
      - Run auto_fill on each predicate (Owner fills assigned_to, etc.)
    Layer 3 (FK injection / co-tenant checks):
      - Every FK provided must be visible under the related model's predicates
      - Every FK target with a tenant attribute must share the caller's tenant

    Raises serializers.ValidationError on violations.
    """
    from django.db import models as dj_models

    from .backends import get_user_roles
    from .predicates import get_predicates, get_tenant_field, get_user_tenant

    # Defensive: if a caller passes a non-dict body (list, string), pass it
    # through unchanged. The HTTP layer already rejects these upstream;
    # this guard makes the helper safe for direct programmatic use.
    if not isinstance(validated_data, dict):
        return validated_data

    tenant_field = get_tenant_field(model)
    predicates = get_predicates(model)
    if not tenant_field and not predicates:
        return validated_data

    errors = []

    # Layer 1: tenant validate_write (rejects setting tenant_field to a
    # different tenant) — mandatory check.
    if (
        tenant_field
        and "__" not in tenant_field
        and tenant_field in validated_data
    ):
        if not request or not request.user or not request.user.is_authenticated:
            errors.append(
                f"Cannot set {tenant_field}: no authenticated user."
            )
        else:
            provided = validated_data[tenant_field]
            expected = get_user_tenant(request.user)
            provided_pk = getattr(provided, "pk", provided)
            expected_pk = getattr(expected, "pk", expected)
            if provided_pk != expected_pk:
                errors.append(
                    f"Cannot set {tenant_field} to a different tenant."
                )

    # Layer 2: within-tenant predicate validate_write
    for pred in predicates:
        errors.extend(pred.validate_write(validated_data, instance, request))
    if errors:
        # Optional Sentry breadcrumb (no-op when Sentry not enabled)
        try:
            from .integrations.sentry import report_security_event

            report_security_event(
                "predicate_validate_write_rejected",
                f"Write rejected on {model.__name__}",
                model=model.__name__,
                errors=errors,
            )
        except Exception:
            pass
        raise serializers.ValidationError({"detail": errors})

    # Layer 1 auto-fill (always overwrite — never trust client)
    if (
        tenant_field
        and "__" not in tenant_field
        and request
        and request.user
        and request.user.is_authenticated
    ):
        tenant = get_user_tenant(request.user)
        if tenant is not None:
            validated_data = dict(validated_data)
            validated_data[tenant_field] = tenant

    # Layer 2 auto-fill
    for pred in predicates:
        validated_data = pred.auto_fill(validated_data, request)

    # Layer 3a: FK injection check — every FK target must be visible under
    #            the related model's predicate stack + tenant boundary.
    # Layer 3b: Co-tenant check — when the host has a tenant_field set, any
    #            FK target with a tenant attribute (via TURBODRF_TENANT_USER_FIELD)
    #            must share the caller's tenant. Catches User-FK assignment
    #            cross-tenant since User typically has no predicates.
    # Layer 3c: Unified error messages — same text whether the FK target
    #            doesn't exist or just isn't visible. Distinct messages
    #            would let an attacker enumerate other tenants' PKs.
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        from django.conf import settings as django_settings

        user_roles = set(get_user_roles(request.user))
        fk_errors = {}

        tenant_user_field = getattr(
            django_settings, "TURBODRF_TENANT_USER_FIELD", None
        )
        caller_tenant_pk = None
        if tenant_user_field and tenant_field:
            caller_tenant = get_user_tenant(request.user)
            caller_tenant_pk = getattr(caller_tenant, "pk", caller_tenant)

        for field in model._meta.fields:
            if not isinstance(field, dj_models.ForeignKey):
                continue
            if field.name not in validated_data:
                continue
            value = validated_data[field.name]
            if value is None:
                continue
            related_model = field.related_model
            value_pk = getattr(value, "pk", value)

            # Build the visibility filter for the related model: tenant +
            # within-tenant predicates.
            from django.db.models import Q

            q = Q()
            related_tenant_field = get_tenant_field(related_model)
            if related_tenant_field:
                related_tenant_q = Q()
                if request.user.is_authenticated:
                    rtenant = get_user_tenant(request.user)
                    if rtenant is not None:
                        related_tenant_q = Q(**{related_tenant_field: rtenant})
                    else:
                        related_tenant_q = Q(pk__in=[])  # fail closed
                q &= related_tenant_q

            for rp in get_predicates(related_model):
                q &= rp.q(request, user_roles)

            # If the related model is scoped at all (predicates or tenant),
            # check the FK target is visible.
            if get_predicates(related_model) or related_tenant_field:
                if not related_model.objects.filter(pk=value_pk).filter(q).exists():
                    fk_errors[field.name] = [
                        f"Invalid {field.name}: not found or not accessible."
                    ]
                    continue

            # Co-tenant check: the FK target's tenant attribute must match
            # the caller's, even when the related model has no predicates
            # (typical for User).
            if caller_tenant_pk is not None and tenant_user_field:
                target_tenant = getattr(value, tenant_user_field, None)
                if target_tenant is not None:
                    target_tenant_pk = getattr(target_tenant, "pk", target_tenant)
                    if target_tenant_pk != caller_tenant_pk:
                        fk_errors[field.name] = [
                            f"{field.name} belongs to a different tenant."
                        ]
        if fk_errors:
            try:
                from .integrations.sentry import report_security_event

                report_security_event(
                    "fk_injection_rejected",
                    f"FK injection blocked on {model.__name__}",
                    model=model.__name__,
                    fields=list(fk_errors.keys()),
                )
            except Exception:
                pass
            raise serializers.ValidationError(fk_errors)

    return validated_data


class TurboDRFSerializer(serializers.ModelSerializer):
    """
    Base serializer for TurboDRF models with support for nested field notation.

    This serializer extends Django REST Framework's ModelSerializer to provide
    automatic handling of nested field relationships using double-underscore notation.
    """

    def to_internal_value(self, data):
        """Catch DRF's per-field 'Invalid pk - does not exist.' messages on
        foreign-key fields and replace them with the unified
        'Invalid <field>: not found or not accessible.' message used by the
        predicate-write FK injection check.

        Distinct error texts for "doesn't exist" vs "exists but invisible"
        let an attacker enumerate other tenants' PKs.
        """
        from django.db import models as dj_models
        from rest_framework.exceptions import ValidationError as DRFValidationError

        try:
            return super().to_internal_value(data)
        except DRFValidationError as exc:
            detail = exc.detail
            if not isinstance(detail, dict):
                raise
            fk_names = {
                f.name
                for f in self.Meta.model._meta.fields
                if isinstance(f, dj_models.ForeignKey)
            }
            replaced = False
            new_detail = {}
            for field_name, errors in detail.items():
                if field_name in fk_names:
                    new_detail[field_name] = [
                        f"Invalid {field_name}: not found or not accessible."
                    ]
                    replaced = True
                else:
                    new_detail[field_name] = errors
            if replaced:
                raise DRFValidationError(new_detail) from exc
            raise

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
                        except Exception as exc:
                            # Surface the failure in logs instead of silently
                            # returning broken data. The path stays out of the
                            # response (caller falls back to whatever DRF
                            # already serialized) but operators can see why.
                            logger.warning(
                                "Nested field traversal failed for %s.%s: %r",
                                instance.__class__.__name__,
                                full_field_path,
                                exc,
                            )

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

            # I-3 fix: apply the target model's tenant_field + within-tenant
            # predicates to the M2M render. Without this, a nested array in
            # a parent response could leak target rows the user can't see
            # via the target's own endpoint.
            related_objects = m2m_manager.all()
            related_model = getattr(m2m_manager, "model", None)
            if related_model is not None:
                from django.db.models import Q

                from .backends import get_user_roles
                from .predicates import (
                    get_predicates,
                    get_tenant_field,
                    get_user_tenant,
                )

                request = self.context.get("request")
                user = getattr(request, "user", None) if request else None
                if user is not None and getattr(user, "is_authenticated", False):
                    user_roles = set(get_user_roles(user))
                    q = Q()
                    target_tenant_field = get_tenant_field(related_model)
                    if target_tenant_field:
                        tenant = get_user_tenant(user)
                        if tenant is None:
                            return []  # fail closed
                        q &= Q(**{target_tenant_field: tenant})
                    for pred in get_predicates(related_model):
                        q &= pred.q(request, user_roles)
                    if q != Q():
                        related_objects = related_objects.filter(q)

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

        # Apply predicate-based write enforcement
        # (validate_write → auto_fill → FK injection check)
        validated_data = _apply_predicate_writes(
            instance.__class__, validated_data, instance, request
        )

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

        # Apply predicate-based write enforcement
        # (validate_write → auto_fill → FK injection check)
        model = self.Meta.model
        validated_data = _apply_predicate_writes(
            model, validated_data, None, request
        )

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

                if snapshot_to_use:
                    self._permission_snapshot = snapshot_to_use

                # DRF's PrimaryKeyRelatedField.queryset is unscoped by
                # default, so the browsable HTML API populates `<select>`
                # dropdowns from `Model.objects.all()` — leaking
                # cross-tenant rows. Replacing the queryset with a
                # tenant-filtered version closes that leak and aligns
                # JSON-write rejection with the FK injection check.
                self._scope_fk_querysets()

            def _scope_fk_querysets(self):
                """Scope each FK field's queryset to the caller's tenant.

                Three sources of scope:
                  1. The related model has its own tenant_field setting →
                     filter by caller's tenant via that field.
                  2. The related model IS the tenant model itself →
                     restrict to the caller's tenant pk.
                  3. The related model has the TURBODRF_TENANT_USER_FIELD
                     attribute pointing at the same tenant entity (typical
                     for User.brokerage) → filter by it.
                """
                from django.conf import settings as _s
                from rest_framework.relations import (
                    ManyRelatedField,
                    PrimaryKeyRelatedField,
                )

                from .predicates import (
                    get_predicates,
                    get_tenant_field,
                    get_user_tenant,
                )

                request = self.context.get("request") if self.context else None
                user = (
                    getattr(request, "user", None) if request else None
                )
                if user is None or not getattr(user, "is_authenticated", False):
                    return

                # Only act when the host model is tenant-scoped — there's
                # nothing to scope against on a non-tenant model.
                host_tenant_field = get_tenant_field(model_class)
                if not host_tenant_field:
                    return

                caller_tenant = get_user_tenant(user)
                caller_tenant_pk = getattr(caller_tenant, "pk", caller_tenant)
                tenant_user_field = getattr(_s, "TURBODRF_TENANT_USER_FIELD", None)

                # Resolve tenant model: prefer the host model's own tenant FK
                # target (works without TURBODRF_TENANT_MODEL being declared).
                tenant_model = None
                try:
                    host_tf = model_class._meta.get_field(host_tenant_field)
                    if host_tf.is_relation:
                        tenant_model = host_tf.related_model
                except Exception:
                    pass
                if tenant_model is None:
                    tenant_model_setting = getattr(_s, "TURBODRF_TENANT_MODEL", None)
                    if tenant_model_setting:
                        try:
                            from django.apps import apps as _apps

                            tenant_model = _apps.get_model(tenant_model_setting)
                        except Exception:
                            tenant_model = None

                from .backends import get_user_roles

                user_roles = set(get_user_roles(user))

                for field in self.fields.values():
                    target_field = field
                    if isinstance(field, ManyRelatedField):
                        target_field = field.child_relation
                    if not isinstance(target_field, PrimaryKeyRelatedField):
                        continue
                    qs = getattr(target_field, "queryset", None)
                    if qs is None:
                        continue
                    # Manager → QuerySet so we can iterate without
                    # tripping `'Manager' object is not iterable` later.
                    from django.db.models import QuerySet as _QuerySet

                    if not isinstance(qs, _QuerySet):
                        qs = qs.all()
                    related_model = qs.model

                    # Case 1: related model has its own tenant_field
                    rt = get_tenant_field(related_model)
                    if rt:
                        if caller_tenant is not None:
                            qs = qs.filter(**{rt: caller_tenant})
                        else:
                            qs = qs.none()

                    # Case 2: related model IS the tenant entity
                    elif tenant_model is not None and related_model is tenant_model:
                        if caller_tenant is not None:
                            tenant_pk = getattr(caller_tenant, "pk", caller_tenant)
                            qs = qs.filter(pk=tenant_pk)
                        else:
                            qs = qs.none()

                    # Case 3: related model has TURBODRF_TENANT_USER_FIELD
                    # attribute as a field/FK (typical: User.brokerage)
                    elif tenant_user_field:
                        is_field = False
                        try:
                            related_model._meta.get_field(tenant_user_field)
                            is_field = True
                        except Exception:
                            is_field = False
                        if is_field:
                            if caller_tenant is not None:
                                qs = qs.filter(
                                    **{tenant_user_field: caller_tenant}
                                )
                            else:
                                qs = qs.none()
                        elif hasattr(related_model, tenant_user_field):
                            # Property/descriptor — filter row-by-row.
                            # This only fires for HTML browsable-API form
                            # population (small N) and JSON-write validation
                            # (single-row lookup).
                            if caller_tenant_pk is None:
                                qs = qs.none()
                            else:
                                visible = []
                                for obj in qs:
                                    val = getattr(obj, tenant_user_field, None)
                                    val_pk = getattr(val, "pk", val)
                                    if val_pk == caller_tenant_pk:
                                        visible.append(obj.pk)
                                qs = qs.filter(pk__in=visible)
                        # else: no scoping signal — fall through.

                    # Apply within-tenant predicates from related model
                    for pred in get_predicates(related_model):
                        qs = qs.filter(pred.q(request, user_roles))
                    target_field.queryset = qs

            class Meta:
                model = model_class
                fields = all_fields
                read_only_fields = read_only_fields_list
                _nested_fields = nested_fields_meta
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

        # Get sensitive fields deny-list
        from django.conf import settings as django_settings

        from .settings import TURBODRF_SENSITIVE_FIELDS as default_sensitive

        sensitive_fields = set(
            getattr(django_settings, "TURBODRF_SENSITIVE_FIELDS", default_sensitive)
        )

        # First check if we should handle fields as "__all__"
        if fields == "__all__":
            # Get all model fields
            fields = [f.name for f in model._meta.fields]

        from .validation import is_field_path_sensitive

        for field in fields:
            # Strip sensitive fields at every segment of the path (I-2 fix).
            if is_field_path_sensitive(field):
                logger.debug(f"Stripping sensitive field '{field}'")
                continue

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
