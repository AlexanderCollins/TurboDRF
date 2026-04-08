"""
Compiled read path for TurboDRF list views.

At startup, reads each model's turbodrf() config and pre-computes a query plan
that uses Django .values() + F() annotations instead of DRF serializers.
This bypasses model instantiation and serializer field-by-field processing.
"""

import logging
from collections import defaultdict

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.db.models import F

logger = logging.getLogger(__name__)

# Module-level registry: model class -> CompiledQueryPlan
_compiled_plans = {}


def register_compiled_plan(model, plan):
    _compiled_plans[model] = plan


def get_compiled_plan(model):
    return _compiled_plans.get(model)


def is_compiled(model):
    return model in _compiled_plans


class DictProxy:
    """Wraps a dict for attribute access so model @property functions work."""

    __slots__ = ("_d",)

    def __init__(self, d):
        object.__setattr__(self, "_d", d)

    def __getattr__(self, name):
        try:
            return self._d[name]
        except KeyError:
            raise AttributeError(name)


def _coerce_decimal(value):
    if value is None:
        return None
    return str(value)


def _build_type_coercers(model, field_names):
    """Build type coercion map for fields that need conversion (e.g. Decimal -> str)."""
    coercers = {}
    for field_name in field_names:
        try:
            field = model._meta.get_field(field_name)
            if isinstance(field, models.DecimalField):
                coercers[field_name] = _coerce_decimal
        except FieldDoesNotExist:
            pass
    return coercers


def _build_fk_type_coercers(model, fk_annotations):
    """Build type coercers for FK annotation target fields (e.g. related__price)."""
    coercers = {}
    for output_key, f_expr in fk_annotations.items():
        # Resolve the target field type by traversing the relationship
        field_path = f_expr.name  # e.g. 'related__name'
        parts = field_path.split("__")
        current_model = model
        for part in parts[:-1]:
            try:
                field = current_model._meta.get_field(part)
                if hasattr(field, "related_model") and field.related_model:
                    current_model = field.related_model
                else:
                    break
            except FieldDoesNotExist:
                break
        # Check the final field
        try:
            target_field = current_model._meta.get_field(parts[-1])
            if isinstance(target_field, models.DecimalField):
                coercers[output_key] = _coerce_decimal
        except FieldDoesNotExist:
            pass
    return coercers


def _compile_m2m_spec(model, m2m_field_name, sub_field_names):
    """Compile M2M field info for the two-query merge."""
    m2m_field = model._meta.get_field(m2m_field_name)
    through_model = m2m_field.remote_field.through
    related_model = m2m_field.related_model

    # Find FK field names on through model
    source_fk = None
    target_fk = None

    for f in through_model._meta.get_fields():
        if not hasattr(f, "related_model") or f.related_model is None:
            continue
        if f.related_model == model and source_fk is None:
            source_fk = f.name
        elif f.related_model == related_model and target_fk is None:
            target_fk = f.name

    if source_fk is None or target_fk is None:
        raise ImproperlyConfigured(
            f"Could not resolve M2M through table FKs for "
            f"{model.__name__}.{m2m_field_name}"
        )

    # Build F() annotations for sub-fields
    annotations = {}
    for sub_field in sub_field_names:
        annotations[sub_field] = F(f"{target_fk}__{sub_field}")

    # Build type coercers for M2M sub-fields
    m2m_coercers = {}
    for sub_field in sub_field_names:
        try:
            target_field = related_model._meta.get_field(sub_field)
            if isinstance(target_field, models.DecimalField):
                m2m_coercers[sub_field] = _coerce_decimal
        except FieldDoesNotExist:
            pass

    return {
        "through_model": through_model,
        "source_fk": source_fk,
        "target_fk": target_fk,
        "related_model": related_model,
        "sub_fields": sub_field_names,
        "annotations": annotations,
        "type_coercers": m2m_coercers,
    }


class CompiledQueryPlan:
    """Pre-computed query plan for a model's list view."""

    def __init__(
        self,
        model,
        simple_fields,
        fk_annotations,
        m2m_specs,
        property_fields,
        type_coercers,
        pk_field,
        original_fields,
    ):
        self.model = model
        self.simple_fields = simple_fields
        self.fk_annotations = fk_annotations
        self.m2m_specs = m2m_specs
        self.property_fields = property_fields
        self.type_coercers = type_coercers
        self.pk_field = pk_field
        self._original_fields = set(original_fields)

    def _fk_base_field(self, output_key):
        """Get the base FK field name from an annotation output key.
        e.g. 'related_name' -> 'related' (by looking up the F() expression)."""
        f_expr = self.fk_annotations.get(output_key)
        if f_expr:
            return f_expr.name.split("__")[0]
        return None

    def apply_to_queryset(self, queryset, readable_fields=None):
        """Apply .values() + F() annotations to a queryset.

        Returns (compiled_queryset, active_plan_tuple).
        """
        active_simple = list(self.simple_fields)
        active_fk = dict(self.fk_annotations)
        active_m2m = dict(self.m2m_specs)
        active_props = dict(self.property_fields)

        if readable_fields is not None:
            active_simple = [f for f in self.simple_fields if f in readable_fields]
            active_fk = {
                k: v
                for k, v in self.fk_annotations.items()
                if self._fk_base_field(k) in readable_fields
            }
            active_m2m = {
                k: v for k, v in self.m2m_specs.items() if k in readable_fields
            }
            active_props = {
                k: v for k, v in self.property_fields.items() if k in readable_fields
            }

        # Always keep PK if we have M2M to merge
        if active_m2m and self.pk_field not in active_simple:
            active_simple = [self.pk_field] + active_simple

        compiled_qs = queryset.values(*active_simple, **active_fk)
        return compiled_qs, (active_simple, active_fk, active_m2m, active_props)

    def post_process(self, rows, active_plan):
        """Apply type coercion, property fields, and M2M merge to result rows."""
        active_simple, active_fk, active_m2m, active_props = active_plan

        # 1. Type coercion (Decimal -> str, etc.)
        if self.type_coercers:
            for row in rows:
                for field_name, coercer in self.type_coercers.items():
                    if field_name in row and row[field_name] is not None:
                        row[field_name] = coercer(row[field_name])

        # 2. Property fields via DictProxy
        if active_props:
            for row in rows:
                proxy = DictProxy(row)
                for prop_name, fget in active_props.items():
                    row[prop_name] = fget(proxy)

        # 3. M2M merge (two-query approach)
        if active_m2m:
            pk_values = [row[self.pk_field] for row in rows]

            for m2m_name, spec in active_m2m.items():
                # Second query on through table
                m2m_rows = list(
                    spec["through_model"]
                    .objects.filter(**{f"{spec['source_fk']}__in": pk_values})
                    .values(spec["source_fk"], **spec["annotations"])
                )

                # Apply M2M type coercion
                if spec.get("type_coercers"):
                    for m2m_row in m2m_rows:
                        for fname, coercer in spec["type_coercers"].items():
                            if fname in m2m_row and m2m_row[fname] is not None:
                                m2m_row[fname] = coercer(m2m_row[fname])

                # Group by parent PK
                source_fk_name = spec["source_fk"]
                grouped = defaultdict(list)
                for m2m_row in m2m_rows:
                    pid = m2m_row.pop(source_fk_name)
                    grouped[pid].append(m2m_row)

                # Attach to parent rows
                for row in rows:
                    row[m2m_name] = grouped.get(row[self.pk_field], [])

            # Remove PK from output if it wasn't in original config
            if self.pk_field not in self._original_fields:
                for row in rows:
                    row.pop(self.pk_field, None)

        return rows


def compile_model(model):
    """Compile a query plan for a model's list view.

    Returns a CompiledQueryPlan if the model opts in (compiled=True),
    or None if the model doesn't opt in.
    Raises ImproperlyConfigured if the model opts in but has unsupported fields.
    """
    config = model.turbodrf()

    if not config.get("compiled", True):
        return None

    # Get list fields
    fields_config = config.get("fields", "__all__")
    if isinstance(fields_config, dict):
        list_fields = fields_config.get("list", "__all__")
    else:
        list_fields = fields_config

    # Resolve __all__
    if list_fields == "__all__":
        list_fields = [f.name for f in model._meta.get_fields() if hasattr(f, "column")]

    # Strip sensitive fields
    from django.conf import settings as django_settings

    from .settings import TURBODRF_SENSITIVE_FIELDS as default_sensitive

    sensitive_fields = set(
        getattr(django_settings, "TURBODRF_SENSITIVE_FIELDS", default_sensitive)
    )
    list_fields = [
        f
        for f in list_fields
        if (f.split("__")[0] if "__" in f else f) not in sensitive_fields
    ]

    original_fields = list(list_fields)

    simple_fields = []
    fk_annotations = {}
    m2m_groups = defaultdict(list)  # base_field -> [sub_field_names]
    property_fields = {}

    # Track which base FK fields we've added
    fk_base_fields_added = set()

    for field_name in list_fields:
        if "__" not in field_name:
            # Simple field — check if it's a DB column or property
            try:
                model._meta.get_field(field_name)
                if field_name not in simple_fields:
                    simple_fields.append(field_name)
            except FieldDoesNotExist:
                # Check if it's a model property
                model_attr = getattr(model, field_name, None)
                if isinstance(model_attr, property):
                    property_fields[field_name] = model_attr.fget
                else:
                    raise ImproperlyConfigured(
                        f"TurboDRF compiled path: '{field_name}' on "
                        f"{model.__name__} is not a database field or property."
                    )
        else:
            # Nested field — FK or M2M
            parts = field_name.split("__")
            base = parts[0]

            try:
                base_field = model._meta.get_field(base)
            except FieldDoesNotExist:
                raise ImproperlyConfigured(
                    f"TurboDRF compiled path: base field '{base}' on "
                    f"{model.__name__} does not exist."
                )

            if base_field.many_to_many:
                # M2M — group sub-fields by base name
                sub_field = "__".join(parts[1:])
                m2m_groups[base].append(sub_field)
            elif hasattr(base_field, "related_model") and base_field.related_model:
                # FK/OneToOne — create F() annotation
                output_key = field_name.replace("__", "_")
                fk_annotations[output_key] = F(field_name)

                # Ensure base FK field is in simple_fields (for the raw ID)
                if base not in fk_base_fields_added:
                    fk_base_fields_added.add(base)
                    if base not in simple_fields:
                        simple_fields.append(base)
            else:
                raise ImproperlyConfigured(
                    f"TurboDRF compiled path: '{field_name}' on "
                    f"{model.__name__} traverses a non-relation field."
                )

    # Compile M2M specs
    m2m_specs = {}
    for base_name, sub_fields in m2m_groups.items():
        m2m_specs[base_name] = _compile_m2m_spec(model, base_name, sub_fields)

    # Ensure PK is in simple_fields (needed for M2M merge and ordering)
    pk_field = model._meta.pk.name
    if pk_field not in simple_fields:
        simple_fields.insert(0, pk_field)

    # Build type coercers for simple fields
    type_coercers = _build_type_coercers(model, simple_fields)

    # Build type coercers for FK annotation target fields
    fk_coercers = _build_fk_type_coercers(model, fk_annotations)
    type_coercers.update(fk_coercers)

    plan = CompiledQueryPlan(
        model=model,
        simple_fields=simple_fields,
        fk_annotations=fk_annotations,
        m2m_specs=m2m_specs,
        property_fields=property_fields,
        type_coercers=type_coercers,
        pk_field=pk_field,
        original_fields=original_fields,
    )

    logger.info(
        f"Compiled read path for {model.__name__}: "
        f"{len(simple_fields)} simple, {len(fk_annotations)} FK, "
        f"{len(m2m_specs)} M2M, {len(property_fields)} property fields"
    )

    return plan
