import hashlib
import logging

from rest_framework import serializers

logger = logging.getLogger(__name__)


def _model_has_concrete_field(model, name):
    """True if ``name`` is a concrete model field (vs a @property / method)."""
    try:
        model._meta.get_field(name)
        return True
    except Exception:
        return False


def _report_write_security_event(event, message, **kwargs):
    """Best-effort Sentry breadcrumb (no-op when Sentry is not enabled)."""
    try:
        from .integrations.sentry import report_security_event

        report_security_event(event, message, **kwargs)
    except Exception:
        pass


def _validate_predicate_writes(
    model, validated_data, instance, request, tenant_field, predicates
):
    """Layer 1 (mandatory tenant boundary) + Layer 2 (within-tenant predicate)
    write validation. Returns a list of error strings ([] when allowed)."""
    from .predicates import get_user_tenant

    errors = []

    # Layer 1: tenant validate_write (rejects setting tenant_field to a
    # different tenant) — mandatory check.
    if tenant_field and "__" not in tenant_field and tenant_field in validated_data:
        if not request or not request.user or not request.user.is_authenticated:
            errors.append(f"Cannot set {tenant_field}: no authenticated user.")
        else:
            provided = validated_data[tenant_field]
            expected = get_user_tenant(request.user)
            provided_pk = getattr(provided, "pk", provided)
            expected_pk = getattr(expected, "pk", expected)
            if provided_pk != expected_pk:
                errors.append(f"Cannot set {tenant_field} to a different tenant.")

    # Layer 2: within-tenant predicate validate_write
    for pred in predicates:
        errors.extend(pred.validate_write(validated_data, instance, request))
    return errors


def _autofill_predicate_writes(validated_data, request, tenant_field, predicates):
    """Layer 1 (tenant) + Layer 2 (predicate) auto-fill. The tenant FK is always
    overwritten with the caller's tenant — never trust the client."""
    from .predicates import get_user_tenant

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
    return validated_data


def _check_fk_injection_writes(model, validated_data, request, tenant_field):
    """Layer 3: FK-injection + co-tenant checks. Returns a ``{field: [errors]}``
    dict ({} when every provided FK is allowed).

    Layer 3a: every provided FK target must be visible under the related model's
      predicate stack + tenant boundary.
    Layer 3b: when the host is tenant-scoped, any FK target with a tenant
      attribute (via TURBODRF_TENANT_USER_FIELD) must share the caller's tenant
      — catches cross-tenant User-FK assignment (User typically has no
      predicates).
    Layer 3c: unified error text (same whether the target doesn't exist or just
      isn't visible) so an attacker can't enumerate other tenants' PKs.
    """
    from django.conf import settings as django_settings
    from django.db import models as dj_models
    from django.db.models import Q

    from .backends import get_user_roles
    from .predicates import get_predicates, get_tenant_field, get_user_tenant

    fk_errors = {}
    if not (
        request and getattr(request, "user", None) and request.user.is_authenticated
    ):
        return fk_errors

    user_roles = set(get_user_roles(request.user))

    tenant_user_field = getattr(django_settings, "TURBODRF_TENANT_USER_FIELD", None)
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

        # If the related model is scoped at all (predicates or tenant), check
        # the FK target is visible.
        if get_predicates(related_model) or related_tenant_field:
            if not related_model.objects.filter(pk=value_pk).filter(q).exists():
                fk_errors[field.name] = [
                    f"Invalid {field.name}: not found or not accessible."
                ]
                continue

        # Co-tenant check: the FK target's tenant attribute must match the
        # caller's, even when the related model has no predicates (typical for
        # User).
        if caller_tenant_pk is not None and tenant_user_field:
            target_tenant = getattr(value, tenant_user_field, None)
            if target_tenant is not None:
                target_tenant_pk = getattr(target_tenant, "pk", target_tenant)
                if target_tenant_pk != caller_tenant_pk:
                    # Unified message (identical to the not-visible case) so the
                    # error can't be used as a cross-tenant PK existence oracle.
                    fk_errors[field.name] = [
                        f"Invalid {field.name}: not found or not accessible."
                    ]
    return fk_errors


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

    Raises serializers.ValidationError on violations. The per-layer logic lives
    in _validate_predicate_writes / _autofill_predicate_writes /
    _check_fk_injection_writes.
    """
    from .predicates import get_predicates, get_tenant_field

    # Defensive: if a caller passes a non-dict body (list, string), pass it
    # through unchanged. The HTTP layer already rejects these upstream;
    # this guard makes the helper safe for direct programmatic use.
    if not isinstance(validated_data, dict):
        return validated_data

    tenant_field = get_tenant_field(model)
    predicates = get_predicates(model)
    if not tenant_field and not predicates:
        return validated_data

    # Layers 1 + 2: validate the write.
    errors = _validate_predicate_writes(
        model, validated_data, instance, request, tenant_field, predicates
    )
    if errors:
        _report_write_security_event(
            "predicate_validate_write_rejected",
            f"Write rejected on {model.__name__}",
            model=model.__name__,
            errors=errors,
        )
        raise serializers.ValidationError({"detail": errors})

    # Layers 1 + 2: auto-fill (tenant FK + predicate auto-fills).
    validated_data = _autofill_predicate_writes(
        validated_data, request, tenant_field, predicates
    )

    # Layer 3: FK-injection + co-tenant checks.
    fk_errors = _check_fk_injection_writes(
        model, validated_data, request, tenant_field
    )
    if fk_errors:
        _report_write_security_event(
            "fk_injection_rejected",
            f"FK injection blocked on {model.__name__}",
            model=model.__name__,
            fields=list(fk_errors.keys()),
        )
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

        # Inject model @property / method fields (read-only), computed on the
        # real instance. NOTE: gated only by model-level read and run UNSCOPED
        # (see the mixin docs); the compiled read path drops properties for
        # role-scoped users, so list (compiled) and detail (DRF) can differ.
        for prop_name in getattr(self.Meta, "_property_fields", []):
            try:
                value = getattr(instance, prop_name, None)
                data[prop_name] = value() if callable(value) else value
            except Exception as exc:
                logger.warning(
                    "Property field %s on %s failed: %r",
                    prop_name,
                    instance.__class__.__name__,
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

            # Scope the M2M render to target rows the caller can see via the
            # target's own endpoint. scoped_target_queryset() returns None for a
            # public target (no scoping needed) and a scoped queryset otherwise
            # — which is .none() for an anonymous caller on a tenant-scoped
            # target, so the render fails closed for anon. (Previously the
            # scoping was skipped entirely for unauthenticated requests, leaking
            # a tenant-scoped M2M target to anonymous callers.)
            related_objects = m2m_manager.all()
            related_model = getattr(m2m_manager, "model", None)
            if related_model is not None:
                from .validation import scoped_target_queryset

                request = self.context.get("request")
                scoped = scoped_target_queryset(related_model, request)
                if scoped is not None:
                    related_objects = related_objects.filter(
                        pk__in=scoped.values("pk")
                    )

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

    def validate(self, attrs):
        """Optionally run Django model validation (``full_clean``) so model
        ``clean()`` / constraints surface as clean 400s instead of 500s.

        Opt in per model via ``turbodrf()`` config ``{"full_clean": True}``;
        off by default (DRF's field-level validation is unchanged).
        """
        attrs = super().validate(attrs)
        model = getattr(getattr(self, "Meta", None), "model", None)
        if model is None or not hasattr(model, "turbodrf"):
            return attrs
        config = model.turbodrf()
        if not (isinstance(config, dict) and config.get("full_clean")):
            return attrs

        from django.core.exceptions import ValidationError as DjangoValidationError

        m2m_names = {f.name for f in model._meta.many_to_many}
        instance = self.instance or model()
        for key, value in attrs.items():
            if key not in m2m_names:
                setattr(instance, key, value)

        exclude = set(m2m_names)
        if self.instance is not None and getattr(self, "partial", False):
            provided = set(attrs.keys())
            for f in model._meta.get_fields():
                fname = getattr(f, "name", None)
                if fname and fname not in provided:
                    exclude.add(fname)

        try:
            instance.full_clean(exclude=exclude or None, validate_unique=False)
        except DjangoValidationError as exc:
            detail = (
                exc.message_dict
                if hasattr(exc, "message_dict")
                else {"non_field_errors": list(exc.messages)}
            )
            raise serializers.ValidationError(detail)
        return attrs

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
        validated_data = _apply_predicate_writes(model, validated_data, None, request)

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

        # Split out model @property / method fields — they can't live in
        # Meta.fields (not model fields), so they're injected read-only in
        # to_representation. SENSITIVE-named properties are dropped entirely
        # (the same deny-list the response/filter/search pathways enforce).
        # SECURITY NOTE: property fields are gated only by MODEL-level read (a
        # property can't carry a field-level read rule) and run UNSCOPED — a
        # property that derives from a restricted column or queries other rows
        # will leak. See the mixin docs before exposing one.
        from .validation import is_field_path_sensitive

        _prop_candidates = [
            f
            for f in simple_fields
            if not _model_has_concrete_field(model, f) and hasattr(model, f)
        ]
        if _prop_candidates:
            simple_fields = [f for f in simple_fields if f not in _prop_candidates]
        property_fields_meta = [
            f for f in _prop_candidates if not is_field_path_sensitive(f)
        ]

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
                user = getattr(request, "user", None) if request else None
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
                                qs = qs.filter(**{tenant_user_field: caller_tenant})
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
                _property_fields = property_fields_meta
                ref_name = ref_name_value

        return DynamicSerializer

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

