import hashlib
import logging

from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework.relations import ManyRelatedField, RelatedField

logger = logging.getLogger(__name__)


def _model_has_concrete_field(model, name):
    """True if ``name`` resolves to a field of ``model`` (concrete,
    many-to-many, or a reverse relation by its query name) rather than a
    @property or method."""
    try:
        model._meta.get_field(name)
        return True
    except Exception:
        return False


def _related_pk(value):
    """A model instance becomes its pk; any other value is returned as-is."""
    from django.db import models

    if isinstance(value, models.Model):
        return value.pk
    return value


def _report_write_security_event(event, message, **kwargs):
    """Best-effort Sentry breadcrumb (no-op when Sentry is not enabled)."""
    try:
        from .integrations.sentry import report_security_event

        report_security_event(event, message, **kwargs)
    except Exception:
        pass


def _caller_tenant_pk(request):
    """The pk of the caller's tenant, or ``None`` (anonymous, or no tenant)."""
    from .predicates import get_user_tenant

    user = getattr(request, "user", None) if request else None
    if user is None or not user.is_authenticated:
        return None
    tenant = get_user_tenant(user)
    return getattr(tenant, "pk", tenant)


def _tenant_model(model, tenant_field):
    """The model ``tenant_field`` (``brokerage``, ``deal__brokerage``) ends
    on, else ``TURBODRF_TENANT_MODEL``."""
    from django.apps import apps
    from django.conf import settings as django_settings

    current = model
    for part in tenant_field.split("__"):
        try:
            current = current._meta.get_field(part).related_model
        except Exception:
            current = None
        if current is None:
            break
    if current is not None:
        return current
    setting = getattr(django_settings, "TURBODRF_TENANT_MODEL", None)
    try:
        return apps.get_model(setting) if setting else None
    except (LookupError, ValueError):
        return None


def _in_caller_tenant(value, target, tenant_model, tenant_user_field, tenant_pk):
    """May a tenant-scoped host point at ``value`` (a ``target`` row)? A row
    of the tenant model must be the caller's tenant; a row carrying
    ``TURBODRF_TENANT_USER_FIELD`` (typically User) must carry the caller's
    tenant. Other rows have no tenant of their own to compare."""
    if target is tenant_model:
        tenant = value
    elif hasattr(target, tenant_user_field):
        tenant = getattr(value, tenant_user_field, None)
    else:
        return True
    value_pk = getattr(tenant, "pk", tenant)
    return value_pk is not None and value_pk == tenant_pk


def _co_tenant_queryset(host, queryset, request):
    """``queryset`` (a related field's choices) narrowed by the co-tenant rule
    ``_in_caller_tenant`` applies to written values, for a tenant-scoped
    ``host`` and a target without a ``tenant_field`` of its own. A tenant
    attribute that isn't a column (a property) can only be checked row by row,
    so it narrows only the choices an HTML page renders; a write is checked
    value by value in ``_check_fk_injection_writes``."""
    from django.conf import settings as django_settings
    from django.core.exceptions import FieldDoesNotExist

    from .predicates import get_tenant_field

    tenant_user_field = getattr(django_settings, "TURBODRF_TENANT_USER_FIELD", None)
    host_tenant_field = get_tenant_field(host)
    target = queryset.model
    if not (tenant_user_field and host_tenant_field) or get_tenant_field(target):
        return queryset
    tenant_pk = _caller_tenant_pk(request)
    tenant_model = _tenant_model(host, host_tenant_field)
    if target is not tenant_model and not hasattr(target, tenant_user_field):
        return queryset
    if tenant_pk is None:
        return queryset.none()
    if target is tenant_model:
        return queryset.filter(pk=tenant_pk)
    try:
        target._meta.get_field(tenant_user_field)
    except FieldDoesNotExist:
        renderer = getattr(request, "accepted_renderer", None)
        if getattr(renderer, "media_type", None) != "text/html":
            return queryset
        visible = [
            row.pk
            for row in queryset
            if _in_caller_tenant(row, target, None, tenant_user_field, tenant_pk)
        ]
        return queryset.filter(pk__in=visible)
    return queryset.filter(**{tenant_user_field: tenant_pk})


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

    # Layer 1, chained tenancy (`ledger__firm`): the first hop's target must
    # sit in the caller's tenant. The hop's own model may be a plain model
    # with no scoping of its own, so no other check covers it.
    if tenant_field and "__" in tenant_field:
        errors.extend(
            _validate_chained_tenant_write(
                model, validated_data, instance, request, tenant_field
            )
        )

    # Layer 2: within-tenant predicate validate_write
    for pred in predicates:
        errors.extend(pred.validate_write(validated_data, instance, request))
    return errors


def _validate_chained_tenant_write(
    model, validated_data, instance, request, tenant_field
):
    """Errors for a write that would attach the row, through the first hop of
    a chained ``tenant_field``, to a target outside the caller's tenant."""
    from .predicates import get_user_tenant

    hop, rest = tenant_field.split("__", 1)
    if instance is not None and hop not in validated_data:
        return []  # unchanged — the row is already in the caller's tenant
    message = f"Invalid {hop}: not found or not accessible."
    user = getattr(request, "user", None) if request else None
    if user is None or not user.is_authenticated:
        return [message]
    tenant = get_user_tenant(user)
    value = validated_data.get(hop)
    if tenant is None or value is None:
        return [message]
    target_model = model._meta.get_field(hop).related_model
    in_tenant = target_model._base_manager.filter(
        pk=getattr(value, "pk", value), **{rest: tenant}
    ).exists()
    return [] if in_tenant else [message]


def _autofill_predicate_writes(
    model, validated_data, request, tenant_field, predicates, instance=None
):
    """Layer 1 (tenant) + Layer 2 (predicate) auto-fill. The tenant FK is always
    overwritten with the caller's tenant — never trust the client. Predicate
    auto-fill runs on create only: on update a missing owner field means
    "unchanged", not "assign to the caller"."""
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
            if hasattr(tenant, "pk"):
                validated_data[tenant_field] = tenant
            else:
                # An integer pk (TURBODRF_TENANT_USER_FIELD may return one):
                # the FK takes an instance, its column the pk.
                attname = model._meta.get_field(tenant_field).attname
                validated_data.pop(tenant_field, None)
                validated_data[attname] = tenant

    # Layer 2 auto-fill
    if instance is None:
        for pred in predicates:
            validated_data = pred.auto_fill(validated_data, request)
    return validated_data


def _require_unwritable_columns(model, validated_data):
    """After a failed insert: if the create left out a column the database
    requires, the caller couldn't set it (a role with ``.create`` but not that
    field's ``.write``, or a column missing from ``fields``), since DRF already
    requires the ones it accepts. Raise a 400 naming it instead of the
    IntegrityError."""
    if not isinstance(validated_data, dict):
        return
    missing = {
        field.name: ["This field is required, but you can't set it through this API."]
        for field in model._meta.concrete_fields
        if not field.null
        and not field.primary_key
        and not getattr(field, "auto_now", False)
        and not getattr(field, "auto_now_add", False)
        and field.get_default() is None
        and field.name not in validated_data
        and field.attname not in validated_data
    }
    if missing:
        raise serializers.ValidationError(missing)


def _check_fk_injection_writes(model, validated_data, request, tenant_field):
    """Layer 3: FK / M2M injection + co-tenant checks. Returns a
    ``{field: [errors]}`` dict ({} when every provided target is allowed).

    Runs for every host model — tenant-scoped, predicate-scoped or shared: a
    shared model pointing at another tenant's row is still an injection.

    Layer 3a: every provided FK / M2M target must be visible under the related
      model's tenant boundary + predicate stack (as through its own endpoint;
      an anonymous caller can't reach a tenant-scoped target).
    Layer 3b: when the host is scoped (a tenant_field, or predicates alone), a
      target of the tenant model must be the caller's tenant, and a target with
      a tenant attribute (via TURBODRF_TENANT_USER_FIELD) must share the
      caller's tenant — catches cross-tenant User-FK and Members assignment
      (User typically has no predicates).
    Layer 3c: unified error text (same whether the target doesn't exist or just
      isn't visible) so an attacker can't enumerate other tenants' PKs.

    Without a request (programmatic use) there is no caller to check against.
    """
    from django.conf import settings as django_settings

    from .predicates import get_predicates
    from .validation import scoped_target_queryset

    fk_errors = {}
    if not request:
        return fk_errors

    tenant_user_field = getattr(django_settings, "TURBODRF_TENANT_USER_FIELD", None)
    co_tenant = bool(tenant_user_field and (tenant_field or get_predicates(model)))
    tenant_pk = _caller_tenant_pk(request) if co_tenant else None
    tenant_model = _tenant_model(model, tenant_field or "") if co_tenant else None

    relations = [f for f in model._meta.fields if f.many_to_one or f.one_to_one] + list(
        model._meta.many_to_many
    )
    for field in relations:
        if field.name not in validated_data:
            continue
        value = validated_data[field.name]
        values = list(value) if field.many_to_many else [value]
        values = [v for v in values if v is not None]
        if not values:
            continue
        message = [f"Invalid {field.name}: not found or not accessible."]

        scoped = scoped_target_queryset(field.related_model, request)
        if scoped is not None:
            pks = {getattr(v, "pk", v) for v in values}
            visible = set(scoped.filter(pk__in=pks).values_list("pk", flat=True))
            if visible != pks:
                fk_errors[field.name] = message
                continue

        # Co-tenant check, even when the related model has no predicates
        # (typical for User). Unified message (identical to the not-visible
        # case) so the error can't be used as a cross-tenant PK existence
        # oracle.
        if co_tenant and not all(
            _in_caller_tenant(
                v, field.related_model, tenant_model, tenant_user_field, tenant_pk
            )
            for v in values
        ):
            fk_errors[field.name] = message
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
    _check_fk_injection_writes. The platform scope has its own rules
    (_apply_platform_writes).
    """
    from .predicates import get_predicates, get_tenant_field
    from .scope import is_platform

    # Defensive: if a caller passes a non-dict body (list, string), pass it
    # through unchanged. The HTTP layer already rejects these upstream;
    # this guard makes the helper safe for direct programmatic use.
    if not isinstance(validated_data, dict):
        return validated_data
    if is_platform():
        return _apply_platform_writes(model, validated_data, instance, request)

    tenant_field = get_tenant_field(model)
    predicates = get_predicates(model)
    if tenant_field or predicates:
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
            model, validated_data, request, tenant_field, predicates, instance
        )

    # Layer 3: FK-injection + co-tenant checks.
    fk_errors = _check_fk_injection_writes(model, validated_data, request, tenant_field)
    if fk_errors:
        _report_write_security_event(
            "fk_injection_rejected",
            f"FK injection blocked on {model.__name__}",
            model=model.__name__,
            fields=list(fk_errors.keys()),
        )
        raise serializers.ValidationError(fk_errors)

    return validated_data


def _apply_platform_writes(model, validated_data, instance, request):
    """Write checks on the platform scope, where no tenant boundary or
    predicate applies and nothing is filled in from the caller:

    - every FK / M2M value must be a row of a model the platform scope can
      reach (scope.platform_reachable: a model with a platform block, the
      tenant model, or a plain model). Any other model's rows read as
      missing: the unified "not found or not accessible" 400;
    - a create on a tenant-scoped model must name its tenant (the first hop
      of its ``tenant_field``);
    - a row and the rows it points at stay in one tenant: a value whose own
      tenant differs from the row's is a 400. When an update moves the row to
      another tenant, the relations it keeps are checked too.
    """
    errors = _check_fk_injection_writes(model, validated_data, request, None)
    if not errors:
        errors = _platform_tenant_errors(model, validated_data, instance)
    if errors:
        _report_write_security_event(
            "platform_write_rejected",
            f"Platform write rejected on {model.__name__}",
            model=model.__name__,
            fields=list(errors.keys()),
        )
        raise serializers.ValidationError(errors)
    return validated_data


def _platform_tenant_model():
    from django.conf import settings as django_settings

    from .tenancy import _resolve_tenant_model

    try:
        return _resolve_tenant_model(
            getattr(django_settings, "TURBODRF_TENANT_MODEL", None)
        )
    except Exception:
        return None


def _same_model(a, b):
    return (
        a is not None
        and b is not None
        and a._meta.concrete_model is b._meta.concrete_model
    )


def _platform_tenant_errors(model, validated_data, instance):
    """``{field: [error]}`` for a platform write that would leave the row in
    no tenant, or point it at another tenant's rows (see
    _apply_platform_writes)."""
    from django.conf import settings as django_settings

    from .predicates import registered_tenant_field

    tenant_model = _platform_tenant_model()
    tenant_field = registered_tenant_field(model)
    is_tenant_model = _same_model(model, tenant_model)
    if not tenant_field and not is_tenant_model:
        return {}

    hop = tenant_field.split("__", 1)[0] if tenant_field else None
    if hop and instance is None and validated_data.get(hop) is None:
        return {
            hop: [
                f"{hop} is required: a create on the platform API must name the "
                f"row's tenant."
            ]
        }

    if is_tenant_model:
        row_tenant, moved = (instance.pk if instance is not None else None), False
    else:
        row_tenant, moved = _platform_row_tenant(
            model, tenant_field, validated_data, instance
        )
    if row_tenant is None:
        return {}

    tenant_user_field = getattr(django_settings, "TURBODRF_TENANT_USER_FIELD", None)
    relations = [f for f in model._meta.fields if f.many_to_one or f.one_to_one] + list(
        model._meta.many_to_many
    )
    errors = {}
    for field in relations:
        if field.name == hop:
            continue
        if field.name in validated_data:
            value = validated_data[field.name]
        elif moved and instance is not None:
            value = getattr(instance, field.name, None)
            if field.many_to_many:
                value = list(value.all()) if value is not None else []
        else:
            continue
        values = list(value) if field.many_to_many else [value]
        for item in values:
            if item is None:
                continue
            target_tenant = _platform_value_tenant(
                field.related_model, item, tenant_model, tenant_user_field
            )
            if target_tenant is not None and target_tenant != row_tenant:
                errors[field.name] = [
                    f"Invalid {field.name}: it belongs to a different tenant than "
                    f"this row."
                ]
                break
    return errors


def _platform_row_tenant(model, tenant_field, validated_data, instance):
    """``(tenant pk, moved)`` of the row after the write: from its
    ``tenant_field``'s first hop (written, or as stored), and whether the
    write changes it."""
    hop, _sep, rest = tenant_field.partition("__")
    current = getattr(instance, hop, None) if instance is not None else None
    if hop in validated_data:
        value = validated_data[hop]
        moved = instance is not None and _related_pk(value) != _related_pk(current)
    else:
        value, moved = current, False
    pk = _related_pk(value)
    if pk is None:
        return None, moved
    if not rest:
        return pk, moved
    target = model._meta.get_field(hop).related_model
    return (
        target._base_manager.filter(pk=pk)
        .values_list(f"{rest}__pk", flat=True)
        .first(),
        moved,
    )


def _platform_value_tenant(target, value, tenant_model, tenant_user_field):
    """The tenant pk of ``value`` (a ``target`` row), or None when it has no
    tenant of its own: a row of the tenant model is its own tenant; a row of
    a tenant-scoped model has its ``tenant_field``'s; a row carrying
    TURBODRF_TENANT_USER_FIELD (typically a user) has that attribute's."""
    from django.db import models

    from .predicates import registered_tenant_field

    pk = _related_pk(value)
    if pk is None:
        return None
    if _same_model(target, tenant_model):
        return pk
    tenant_field = registered_tenant_field(target)
    if tenant_field:
        # The tenant's pk, not the FK column (a to_field FK stores another
        # value).
        return (
            target._base_manager.filter(pk=pk)
            .values_list(f"{tenant_field}__pk", flat=True)
            .first()
        )
    if tenant_user_field and hasattr(target, tenant_user_field):
        from .predicates import user_tenant_value

        if not isinstance(value, models.Model):
            value = target._base_manager.filter(pk=pk).first()
        return _related_pk(user_tenant_value(value))
    return None


def _role_filters_writes(request):
    """True when the caller's role snapshot decides which provided fields are
    written — not when permissions are bypassed (disabled / Django default
    permissions), where a role-less caller would have every field dropped."""
    from .permissions import permissions_bypassed

    user = getattr(request, "user", None) if request else None
    return bool(user and user.is_authenticated and not permissions_bypassed())


def _verify_written_row(model, instance, request):
    """After a write, re-check predicates that can only be judged on the saved
    row (``Conditional``): the row must still be visible to the caller, or the
    write is rolled back. Called inside the write's transaction."""
    from django.db.models import Q

    from .backends import get_user_roles
    from .predicates import get_predicates

    predicates = get_predicates(model)
    if not request or not any(_needs_post_write_check(p) for p in predicates):
        return
    roles = set(get_user_roles(getattr(request, "user", None)))
    visible = Q()
    for pred in predicates:
        visible &= pred.q(request, roles)
    if not model._base_manager.filter(visible, pk=instance.pk).exists():
        raise serializers.ValidationError(
            {"detail": ["This write would create a row you are not allowed to see."]}
        )


def _needs_post_write_check(predicate):
    from .predicates import Either

    if getattr(predicate, "verify_after_write", False):
        return True
    if isinstance(predicate, Either):
        return any(_needs_post_write_check(p) for p in predicate.predicates)
    return False


class TurboDRFSerializer(serializers.ModelSerializer):
    """
    Base serializer for TurboDRF models with support for nested field notation.

    This serializer extends Django REST Framework's ModelSerializer to provide
    automatic handling of nested field relationships using double-underscore notation.
    """

    def get_fields(self):
        """The primary key is read-only on update: writing it saved a copy of
        the row under the new key (a create through an update-only grant).

        Every related field's choices (what a write may name, and the
        browsable API's dropdowns) are the rows the caller can reference: those
        visible through the target's own endpoint and, for a tenant-scoped
        host, in the caller's tenant. Without a request (programmatic use)
        there is no caller to scope to."""
        fields = super().get_fields()
        if self.instance is not None:
            pk_name = self.Meta.model._meta.pk.name
            if pk_name in fields:
                fields[pk_name].read_only = True
        request = self.context.get("request")
        if request is not None:
            for field in fields.values():
                relation = getattr(field, "child_relation", field)
                if isinstance(relation, RelatedField) and relation.queryset is not None:
                    relation.queryset = self._referenceable(relation.queryset, request)
        return fields

    def _referenceable(self, queryset, request):
        """``queryset`` narrowed to the rows the caller may reference: the
        same rows the write check (_check_fk_injection_writes) accepts."""
        from .validation import scoped_target_queryset

        queryset = queryset.all()
        scoped = scoped_target_queryset(queryset.model, request)
        if scoped is not None:
            queryset = queryset.filter(pk__in=scoped.values("pk"))
        return _co_tenant_queryset(self.Meta.model, queryset, request)

    def build_relational_field(self, field_name, relation_info):
        """A reverse relation is read-only: it renders from the scoped query
        to-many fields read (``_prefetch_to_many``). Written, DRF would
        ``.set()`` it, moving rows between parents."""
        field_class, field_kwargs = super().build_relational_field(
            field_name, relation_info
        )
        if relation_info.reverse:
            field_kwargs.pop("queryset", None)
            field_kwargs["read_only"] = True
        return field_class, field_kwargs

    def to_internal_value(self, data):
        """A related field's "does not exist" error becomes the unified
        'Invalid <field>: not found or not accessible.' message used by the
        predicate-write FK injection check. Its choices are scoped
        (``get_fields``), so a row the caller can't see fails here like one
        that doesn't exist: distinct texts would let an attacker enumerate
        other tenants' PKs. Other errors (required, wrong type) are kept.
        """
        from rest_framework.exceptions import ValidationError as DRFValidationError

        try:
            return super().to_internal_value(data)
        except DRFValidationError as exc:
            detail = exc.detail
            if not isinstance(detail, dict):
                raise
            hidden = [
                name
                for name, errors in detail.items()
                if isinstance(self.fields.get(name), (RelatedField, ManyRelatedField))
                and isinstance(errors, list)
                and any(getattr(e, "code", None) == "does_not_exist" for e in errors)
            ]
            if not hidden:
                raise
            new_detail = dict(detail)
            for name in hidden:
                new_detail[name] = [f"Invalid {name}: not found or not accessible."]
            raise DRFValidationError(new_detail) from exc

    def to_representation(self, instance):
        """
        Convert a model instance to a dictionary representation.

        This method extends the default serialization to include nested fields
        that are defined using double-underscore notation. It handles both:
        - ForeignKey relationships: Flat fields
          (e.g., author__name → author_name)
        - To-many relationships (M2M, reverse FK): Arrays of objects
          (e.g., categories__name → categories: [{name: ...}])

        To-many relations render only the target rows the caller can see,
        loaded for the whole page at once (``_prefetch_to_many``).

        Args:
            instance: The model instance to serialize.

        Returns:
            dict: The serialized representation including nested fields.

        Example:
            For a FK field 'author__name': adds 'author_name' as flat field
            For an M2M field 'categories__name': adds 'categories' as array of objects
        """
        self._prefetch_to_many(instance)
        data = super().to_representation(instance)

        # Handle nested fields if they're defined
        if hasattr(self.Meta, "_nested_fields"):
            from .compiler import field_representation, path_leaf_field

            request = self.context.get("request")
            for base_field, nested_fields in self.Meta._nested_fields.items():
                if self._is_many_to_many_field(instance, base_field):
                    # Handle to-many relations: serialize as array of objects
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
                        field_name = full_field_path.replace("__", "_")
                        leaf = path_leaf_field(type(instance), full_field_path)

                        try:
                            if not self._nested_target_visible(
                                instance, full_field_path
                            ):
                                # The JOIN target is outside the caller's scope.
                                data[field_name] = None
                                continue
                            value = self._nested_value(instance, full_field_path, leaf)
                            # Format as a top-level field of that type renders
                            # (and as the compiled list does).
                            represent = field_representation(leaf, request)
                            if represent is not None and value is not None:
                                value = represent(value)
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

        # Inject model @property / method fields (read-only), computed in the
        # caller's sandbox (turbodrf.properties) as on the compiled list.
        property_names = getattr(self.Meta, "_property_fields", [])
        if property_names:
            data.update(self._property_values(instance, property_names))

        return data

    def _page_rows(self, instance):
        """The rows rendered with ``instance``: its list serializer's page, or
        just ``instance``."""
        model = type(instance)
        parent = getattr(self, "parent", None)
        page = getattr(parent, "instance", None)
        if isinstance(parent, serializers.ListSerializer) and page is not None:
            rows = [obj for obj in page if isinstance(obj, model)]
            if any(obj is instance for obj in rows):
                return rows
        return [instance]

    def _property_values(self, instance, names):
        """The ``names`` properties of ``instance``; one that fails is left
        out. Sandboxed values are computed for the whole page at once when
        ``instance`` is rendered by a list serializer."""
        from .properties import (
            attribute_getter,
            evaluate,
            sandboxed_values,
            unscoped_properties,
        )

        model = type(instance)
        unscoped = unscoped_properties(model)
        values = {}
        for name in names:
            if name in unscoped:
                ok, value = evaluate(instance, name, attribute_getter(name))
                if ok:
                    values[name] = value

        sandboxed = [name for name in names if name not in unscoped]
        if not sandboxed or instance.pk is None:
            return values

        cache = self.__dict__.setdefault("_turbodrf_property_cache", {})
        if instance.pk not in cache:
            cache.update(
                sandboxed_values(
                    model,
                    [obj.pk for obj in self._page_rows(instance)],
                    {name: attribute_getter(name) for name in sandboxed},
                    self.context.get("request"),
                )
            )
        values.update(cache.get(instance.pk, {}))
        return values

    def _is_many_to_many_field(self, instance, field_name):
        """
        Check if a field is a to-many relation: an M2M (either side) or a
        reverse FK, rendered as an array of objects.

        Args:
            instance: Model instance
            field_name: Name of the field to check

        Returns:
            bool: True if the field is a to-many relation
        """
        try:
            field = instance._meta.get_field(field_name)
            return field.many_to_many or field.one_to_many
        except Exception:
            return False

    @staticmethod
    def _nested_value(obj, path, leaf=None):
        """The value at ``path`` from ``obj``. A path ending on an FK /
        one-to-one renders the column the parent stores (the related pk, or
        the ``to_field`` value), as the compiled path's F() does — without
        loading the related object."""
        parts = path.split("__")
        if leaf is not None and leaf.concrete and (leaf.many_to_one or leaf.one_to_one):
            parent_parts, last = parts[:-1], leaf.attname
        else:
            parent_parts, last = parts[:-1], parts[-1]
        value = obj
        for part in parent_parts:
            if value is None:
                return None
            value = getattr(value, part, None)
        if value is None:
            return None
        return _related_pk(getattr(value, last, None))

    def _nested_target_visible(self, instance, path):
        """True if every JOIN target along ``path`` is visible to the caller
        through the target's own endpoint (tenant + predicates).

        Read from the annotation the viewset's queryset carries; for an
        instance without it (a create / update response) each hop is checked
        directly. Without a request (programmatic use) nothing is scoped."""
        request = self.context.get("request")
        if request is None:
            return True
        relation_path = path.rsplit("__", 1)[0]
        alias = (self.context.get("turbodrf_nested_visibility") or {}).get(
            relation_path
        )
        if alias is not None and hasattr(instance, alias):
            return bool(getattr(instance, alias))
        return self._hops_visible(instance, path.split("__")[:-1], request)

    def _hops_visible(self, obj, hops, request):
        from .validation import scoped_target_queryset

        cache = self.__dict__.setdefault("_visible_targets", {})
        for hop in hops:
            obj = getattr(obj, hop, None)
            if obj is None:
                return True  # an empty relation joins nothing
            model = type(obj)
            key = (model, obj.pk)
            if key not in cache:
                scoped = scoped_target_queryset(model, request, base_manager=True)
                cache[key] = scoped is None or scoped.filter(pk=obj.pk).exists()
            if not cache[key]:
                return False
        return True

    @staticmethod
    def _sub_paths(base_field, nested_fields):
        """Paths relative to a to-many target (``name``, ``leaf__name``) of
        nested fields named in full (``tags__name``) or short (``name``)."""
        sub_paths = []
        for nested_field in nested_fields:
            if nested_field.startswith(f"{base_field}__"):
                sub_path = nested_field[len(base_field) + 2 :]
            else:
                sub_path = nested_field
            if sub_path not in sub_paths:
                sub_paths.append(sub_path)
        return sub_paths

    def _to_many_relations(self, model):
        """``{name: relation}`` for each to-many relation this serializer
        renders: a bare M2M or reverse field, or the base of nested fields
        (``tags__name``). See ``_to_many_relation``."""
        relations = self.__dict__.get("_turbodrf_to_many")
        if relations is not None:
            return relations
        from .properties import _resolve_hop

        nested = getattr(self.Meta, "_nested_fields", None) or {}
        bare = [
            f.source for f in self.fields.values() if isinstance(f, ManyRelatedField)
        ]
        accessors = {}  # name -> accessor
        targets = {}  # accessor -> (target model, sub-paths)
        for name in [*bare, *nested]:
            resolved = _resolve_hop(model, name)
            if resolved is None or name in accessors:
                continue
            field, accessor = resolved
            if not (field.many_to_many or field.one_to_many):
                continue
            accessors[name] = accessor
            target, sub_paths = targets.setdefault(accessor, (field.related_model, []))
            for sub_path in self._sub_paths(name, nested.get(name, ())):
                if sub_path not in sub_paths:
                    sub_paths.append(sub_path)
        built = {
            accessor: self._to_many_relation(target, accessor, sub_paths)
            for accessor, (target, sub_paths) in targets.items()
        }
        relations = {name: built[accessor] for name, accessor in accessors.items()}
        self.__dict__["_turbodrf_to_many"] = relations
        return relations

    def _to_many_relation(self, target, accessor, sub_paths):
        """How one to-many relation loads: the target rows the caller can see
        through the target's own endpoint (in the target's default manager
        and ordering), with the single-valued hops of ``sub_paths`` joined in
        and, per hop, whether its row is in the caller's scope — one query
        for the page, not one per row and hop."""
        from django.core.exceptions import ImproperlyConfigured
        from django.db.models import BooleanField, Case, Prefetch, Value, When

        from .compiler import field_representation, path_leaf_field
        from .properties import _single_valued, relation_hops
        from .validation import build_traversal_scope_q, scoped_target_queryset

        request = self.context.get("request")
        queryset = target._default_manager.all()
        scoped = scoped_target_queryset(target, request)
        if scoped is not None:
            queryset = queryset.filter(pk__in=scoped.values("pk"))

        hops = {}  # a sub-path's relation prefix -> its visibility alias, or None
        annotations = {}
        for sub_path in sub_paths:
            prefix = sub_path.rpartition("__")[0]
            if not prefix or prefix in hops:
                continue
            try:
                hop_fields = [hop[1] for hop in relation_hops(target, prefix)]
            except ImproperlyConfigured:
                continue
            if not all(_single_valued(f) for f in hop_fields):
                continue  # through a to-many hop: renders null
            hops[prefix] = None
            if request is None:
                continue  # programmatic use: no caller to scope the hop to
            q = build_traversal_scope_q(target, sub_path, request)
            if q:
                alias = f"_tdrf_visible_{len(annotations)}"
                hops[prefix] = alias
                annotations[alias] = Case(
                    When(q, then=Value(True)),
                    default=Value(False),
                    output_field=BooleanField(),
                )
        if hops:
            queryset = queryset.select_related(*hops)
        if annotations:
            queryset = queryset.annotate(**annotations)

        leaves = {sub_path: path_leaf_field(target, sub_path) for sub_path in sub_paths}
        return {
            "accessor": accessor,
            "prefetch": Prefetch(accessor, queryset=queryset),
            "hops": hops,
            "leaves": leaves,
            "represents": {
                sub_path: field_representation(leaf, request)
                for sub_path, leaf in leaves.items()
            },
        }

    def _prefetch_to_many(self, instance):
        """Load the to-many relations this serializer renders for the whole
        page at once, one scoped query each (``_to_many_relation``). DRF's
        related field and ``_serialize_m2m_field`` read the prefetched rows,
        never the unscoped relation."""
        from django.db.models import prefetch_related_objects

        if getattr(instance, "pk", None) is None:
            return
        relations = self._to_many_relations(type(instance))
        needed = {relation["accessor"] for relation in relations.values()}
        if not needed or needed <= instance.__dict__.get("_tdrf_to_many", set()):
            return
        rows = [
            row
            for row in self._page_rows(instance)
            if not needed <= row.__dict__.get("_tdrf_to_many", set())
        ]
        prefetches = {r["accessor"]: r["prefetch"] for r in relations.values()}
        for row in rows:
            # Replace what the queryset may have prefetched itself, unscoped.
            row.__dict__.pop("_prefetched_objects_cache", None)
        prefetch_related_objects(rows, *prefetches.values())
        for row in rows:
            row.__dict__["_tdrf_to_many"] = needed

    def _serialize_m2m_field(self, instance, base_field, nested_fields):
        """
        Serialize a to-many relation (M2M or reverse FK) as an array of objects.

        Args:
            instance: Model instance
            base_field: Name of the relation (e.g., 'categories')
            nested_fields: List of nested field paths
                (e.g., ['categories__name', 'categories__id'])

        Returns:
            list: Array of dictionaries containing the nested field values

        Example:
            Input: categories__name, categories__id
            Output: [{"id": 66, "name": "Sales"}, {"id": 72, "name": "Marketing"}]

        Only target rows the caller can see through the target's own endpoint
        are rendered (for an anonymous caller on a tenant-scoped target, none),
        and a sub-field across a further hop is null where that hop's row is
        out of scope. Each sub-field renders under its path below the
        relation, as on the compiled path.
        """
        try:
            relation = self._to_many_relations(type(instance)).get(base_field)
            if relation is None:
                return []
            self._prefetch_to_many(instance)
            result = []
            for row in getattr(instance, relation["accessor"]).all():
                item = {}
                for sub_path in self._sub_paths(base_field, nested_fields):
                    item[sub_path] = self._to_many_value(row, sub_path, relation)
                result.append(item)
            return result
        except Exception as exc:
            logger.warning(
                "To-many field render failed for %s.%s: %r",
                type(instance).__name__,
                base_field,
                exc,
            )
            return []

    def _to_many_value(self, row, sub_path, relation):
        """The value at ``sub_path`` of a to-many target ``row``; null where a
        hop on the way is out of the caller's scope."""
        prefix = sub_path.rpartition("__")[0]
        try:
            if prefix:
                if prefix not in relation["hops"]:
                    return None
                alias = relation["hops"][prefix]
                if alias is not None and not getattr(row, alias, False):
                    return None
            value = self._nested_value(row, sub_path, relation["leaves"].get(sub_path))
            represent = relation["represents"].get(sub_path)
            if value is not None and represent is not None:
                value = represent(value)
            return value
        except Exception:
            return None

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
        from .mixins import _model_config

        config = _model_config(model)
        if not (isinstance(config, dict) and config.get("full_clean")):
            return attrs

        import copy

        from django.core.exceptions import ValidationError as DjangoValidationError

        m2m_names = {f.name for f in model._meta.many_to_many}
        # Validate a COPY so a failed validation can't leave self.instance
        # half-updated.
        instance = copy.copy(self.instance) if self.instance is not None else model()
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

        # Keep what clean() normalised for the provided keys — the save uses
        # attrs, not the validated instance.
        for key in list(attrs):
            if key not in m2m_names:
                attrs[key] = getattr(instance, key)
        return attrs

    def _writable(self, validated_data, model, action):
        """``validated_data`` without the fields the caller's role may not
        write: an explicit write rule if the field has one, else model-level
        ``action`` permission. Uses permission snapshots for O(1) checks."""
        request = self.context.get("request")
        if not _role_filters_writes(request):
            return validated_data
        snapshot = getattr(self, "_permission_snapshot", None)
        if snapshot is None:
            from .backends import build_permission_snapshot, get_snapshot_from_request

            snapshot = get_snapshot_from_request(request, model)
            if snapshot is None:
                snapshot = build_permission_snapshot(request.user, model)
        return {
            name: value
            for name, value in validated_data.items()
            if (
                snapshot.can_write_field(name)
                if snapshot.has_write_rule(name)
                else snapshot.can_perform_action(action)
            )
        }

    def _keep_hidden_links(self, instance, validated_data, request):
        """An M2M write replaces the links the caller can see. Links to rows
        the caller can't reference (another tenant's, a predicate's) are
        kept: the caller can't see them, so can't mean to remove them."""
        if request is None or not isinstance(validated_data, dict):
            return validated_data
        for field in instance._meta.many_to_many:
            if field.name not in validated_data:
                continue
            visible = self._referenceable(field.related_model._default_manager, request)
            hidden = list(
                getattr(instance, field.name).exclude(pk__in=visible.values("pk"))
            )
            if hidden:
                provided = list(validated_data[field.name])
                validated_data = {**validated_data, field.name: provided + hidden}
        return validated_data

    def _forget_read_state(self, instance):
        """After a write, drop what the read path cached on ``instance`` for
        its old values: the viewset's per-hop visibility flags (computed by
        ``get_object()``) and the to-many rows loaded for an earlier render.
        The response then re-checks each hop."""
        for alias in (self.context.get("turbodrf_nested_visibility") or {}).values():
            instance.__dict__.pop(alias, None)
        instance.__dict__.pop("_tdrf_to_many", None)
        instance.__dict__.pop("_prefetched_objects_cache", None)

    def update(self, instance, validated_data):
        """
        Update instance with write permission checking.

        This method filters out fields that the user doesn't have write
        permission for before updating the instance, then applies the
        predicate-based write enforcement (validate_write → auto_fill → FK
        injection check).
        """
        request = self.context.get("request")
        model = instance.__class__
        validated_data = self._writable(validated_data, model, "update")
        validated_data = _apply_predicate_writes(
            model, validated_data, instance, request
        )
        validated_data = self._keep_hidden_links(instance, validated_data, request)

        with transaction.atomic():
            instance = super().update(instance, validated_data)
            _verify_written_row(model, instance, request)
        self._forget_read_state(instance)
        return instance

    def create(self, validated_data):
        """
        Create instance with write permission checking.

        This method filters out fields that the user doesn't have write
        permission for before creating the instance, then applies the
        predicate-based write enforcement (validate_write → auto_fill → FK
        injection check).
        """
        request = self.context.get("request")
        model = self.Meta.model
        validated_data = self._writable(validated_data, model, "create")
        validated_data = _apply_predicate_writes(model, validated_data, None, request)

        with transaction.atomic():
            try:
                instance = super().create(validated_data)
            except IntegrityError:
                _require_unwritable_columns(model, validated_data)
                raise
            _verify_written_row(model, instance, request)
        return instance


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
        permitted_fields = cls._get_permitted_fields_with_snapshot(
            model, fields, user, snapshot
        )

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
        # A property is gated by MODEL-level read (it can't carry a field-level
        # read rule) and computed in the caller's sandbox: only readable
        # columns, declared relations loaded within scope, no queries (see
        # turbodrf.properties).
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
        from .scope import TENANT, current_scope

        fields_hash = hashlib.md5(",".join(sorted(all_fields)).encode()).hexdigest()[:8]
        app_label = model_class._meta.app_label
        model_name = model_class._meta.model_name
        ref_name_value = f"{app_label}_{model_name}_{view_type}_{fields_hash}"
        if current_scope() != TENANT:
            ref_name_value = f"{current_scope()}_{ref_name_value}"

        # Store snapshot for use in create/update methods
        snapshot_to_use = snapshot

        # Create the main serializer class. Related-field scoping (the
        # browsable API's dropdowns, write lookups) is TurboDRFSerializer's.
        class DynamicSerializer(TurboDRFSerializer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

                if snapshot_to_use:
                    self._permission_snapshot = snapshot_to_use

            class Meta:
                model = model_class
                fields = all_fields
                read_only_fields = read_only_fields_list
                _nested_fields = nested_fields_meta
                _property_fields = property_fields_meta
                ref_name = ref_name_value

        return DynamicSerializer

    @classmethod
    def _get_permitted_fields_with_snapshot(cls, model, fields, user, snapshot=None):
        """
        Filter fields based on user's read permissions with nested permission checking.

        This version validates nesting depth and checks permissions at each level
        of nested field paths, building each model's snapshot once (``snapshot``
        is ``model``'s, when the caller has it).

        Args:
            model: The Django model class.
            fields: List of field names to check, may include nested fields.
            user: Django user object for permission checking
            snapshot: Optional PermissionSnapshot of ``user`` on ``model``

        Returns:
            list: Filtered list of field names the user can read.
        """
        from .backends import build_permission_snapshot
        from .validation import (
            _normalised_path,
            _path_readable,
            is_field_path_sensitive,
            validate_nesting_depth,
        )

        snapshots = {model: snapshot} if snapshot is not None else {}

        def snapshot_for(current):
            if current not in snapshots:
                snapshots[current] = build_permission_snapshot(user, current)
            return snapshots[current]

        permitted = []

        # "__all__" is every concrete and many-to-many field, as on the views
        # and the compiled path.
        if fields == "__all__":
            fields = [f.name for f in model._meta.fields] + [
                f.name for f in model._meta.many_to_many
            ]

        for field in fields:
            # Strip sensitive fields at every segment of the path (I-2 fix),
            # an FK named by its attname (`token_id`) included.
            if is_field_path_sensitive(_normalised_path(model, field)):
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
            if _path_readable(model, field, snapshot_for):
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
