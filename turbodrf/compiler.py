"""
Compiled read path for TurboDRF list views.

At startup, reads each model's turbodrf() config and pre-computes a query plan
that uses Django .values() + F() annotations instead of DRF serializers.
This bypasses model instantiation and serializer field-by-field processing.

A list row must read exactly as the serializer path renders the same row, so a
config the plan can't render faithfully (a nested @property, a path through a
reverse FK, ...) isn't compiled: the model is served by the serializer path.
"""

import base64
import logging
from collections import defaultdict

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.db.models import Case, F, Value, When

from .scope import is_platform

logger = logging.getLogger(__name__)

# Module-level registries: model class -> CompiledQueryPlan, one per scope
# (a model's platform block compiles its own list fields).
_compiled_plans = {}
_platform_compiled_plans = {}


def _plans():
    return _platform_compiled_plans if is_platform() else _compiled_plans


def register_compiled_plan(model, plan):
    _plans()[model] = plan


def unregister_compiled_plan(model):
    _plans().pop(model, None)


def get_compiled_plan(model):
    return _plans().get(model)


def is_compiled(model):
    return model in _plans()


class NotCompilable(Exception):
    """A field config the compiled path can't render the way the serializer
    path does; the model falls back to the serializer path."""


# Django model fields whose raw value (as .values() returns it) the JSON
# encoder renders exactly as their DRF serializer field does: the DRF field's
# to_representation is str() / int() / float() / bool() of it, or the value
# itself. Every other field renders through its DRF field: Decimal("0") with
# decimal_places=8 is "0.00000000", not "0E-8"; datetimes are converted to the
# current timezone and keep their microseconds; durations are "1 02:00:00", not
# "93600.0"; DATE_FORMAT / DATETIME_FORMAT / TIME_FORMAT apply; an ArrayField
# renders each item through its base field.
_RAW_FIELDS = (
    models.CharField,
    models.TextField,
    models.IntegerField,
    models.FloatField,
    models.BooleanField,
    models.UUIDField,
    models.GenericIPAddressField,
    models.JSONField,
)

_field_representations = {}


def _renders_raw(model_field):
    """True if ``model_field``'s raw value renders as its DRF field renders
    it. A relation renders its raw target value (PrimaryKeyRelatedField's
    ``.pk``, SlugRelatedField's ``to_field``). Choices go through DRF's
    ChoiceField, and a field class from outside Django through the DRF field
    ModelSerializer picks for it (its value may be any Python object)."""
    if model_field.is_relation:
        return True
    return (
        isinstance(model_field, _RAW_FIELDS)
        and not model_field.choices
        and type(model_field).__module__.startswith("django.")
    )


def _drf_field(model_field):
    """The DRF field ModelSerializer builds for ``model_field``."""
    from rest_framework.serializers import ModelSerializer

    field_class, field_kwargs = ModelSerializer().build_standard_field(
        model_field.name, model_field
    )
    return field_class(**field_kwargs)


def field_representation(model_field, request=None):
    """How the serializer path renders a value of ``model_field``, when that
    differs from the JSON encoder's rendering of the raw value; else ``None``.

    Uses the DRF field ModelSerializer builds for the model field, so a value
    rendered through it matches a top-level field on the serializer path,
    settings included (COERCE_DECIMAL_TO_STRING, DATETIME_FORMAT, ...). Used for
    compiled rows and for nested / M2M values on the serializer path. File
    fields render their URL (absolute when ``request`` is given), binary fields
    base64.
    """
    if model_field is None:
        return None
    if isinstance(model_field, models.FileField):
        return _file_representation(model_field, request)
    if isinstance(model_field, models.BinaryField):
        return _binary_representation
    if _renders_raw(model_field):
        return None
    try:
        return _field_representations[model_field]
    except KeyError:
        pass
    from rest_framework.fields import ModelField

    try:
        represent = _drf_field(model_field)
    except Exception:
        represent = None
    if isinstance(represent, ModelField):
        # DRF's fallback for a field it has no mapping for: it renders from
        # the model instance, not from a value.
        represent = None
    if represent is not None:
        represent = represent.to_representation
    # None: no DRF field renders the value, which then renders raw (the
    # compiled path refuses such a field, see _check_representable).
    _field_representations[model_field] = represent
    return represent


def _check_representable(model_field, where):
    """Raise NotCompilable for a field the compiled path can't render through
    a DRF field the way the serializer path does."""
    if model_field is None or _renders_raw(model_field):
        return
    if isinstance(model_field, (models.FileField, models.BinaryField)):
        return
    field_representation(model_field)
    if _field_representations.get(model_field) is None:
        raise NotCompilable(
            f"'{where}' is a {type(model_field).__name__}, which has no DRF "
            f"field to render it through"
        )


def _file_representation(model_field, request):
    drf_field = _drf_field(model_field)
    # An unbound DRF field reads its context from itself.
    drf_field._context = {"request": request} if request is not None else {}

    def represent(value):
        if isinstance(value, str):  # a .values() row holds the stored name
            value = model_field.attr_class(None, model_field, value)
        return drf_field.to_representation(value)

    return represent


def _binary_representation(value):
    # As Django's BinaryField.value_to_string, which DRF's ModelField uses.
    return base64.b64encode(bytes(value)).decode("ascii")


def path_leaf_field(model, path):
    """The model field a ``__`` path ends on, or ``None`` if it doesn't resolve."""
    current = model
    field = None
    for part in path.split("__"):
        if current is None:
            return None
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            return None
        current = field.related_model if field.is_relation else None
    return field


def _forward_m2m(field):
    """A ManyToManyField declared on the model (not the reverse side). Not
    ``concrete``: Django 6.0 made ManyToManyField non-concrete."""
    return field.many_to_many and not field.auto_created


def _single_valued_path(model, path):
    """Check ``path`` crosses only single-valued relations (FK, one-to-one,
    reverse one-to-one) and ends on a concrete field. Raises NotCompilable."""
    current = model
    parts = path.split("__")
    for i, part in enumerate(parts):
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            if isinstance(getattr(current, part, None), property):
                raise NotCompilable(f"'{path}' ends on a @property")
            raise ImproperlyConfigured(
                f"TurboDRF compiled path: '{path}' on {model.__name__} does not "
                f"resolve ('{part}' is not a field of {current.__name__})."
            )
        if field.many_to_many or field.one_to_many:
            raise NotCompilable(f"'{path}' crosses a multi-valued relation")
        if i == len(parts) - 1:
            return field
        if not field.is_relation:
            raise ImproperlyConfigured(
                f"TurboDRF compiled path: '{path}' on {model.__name__} "
                f"traverses a non-relation field."
            )
        current = field.related_model


def _key_collides(model, key):
    """True if ``key`` names a field or attname of ``model``: a .values()
    annotation can't take that name."""
    for f in model._meta.get_fields():
        if key == getattr(f, "name", None) or key == getattr(f, "attname", None):
            return True
    return key == "pk"


def _build_type_coercers(model, field_names):
    """Map each field DRF renders differently from its raw value to the
    formatter that renders it the same way (see field_representation). File
    fields are left out: their URL depends on the request (see post_process)."""
    coercers = {}
    for field_name in field_names:
        try:
            field = model._meta.get_field(field_name)
        except FieldDoesNotExist:
            continue
        if isinstance(field, models.FileField):
            continue
        _check_representable(field, field_name)
        represent = field_representation(field)
        if represent is not None:
            coercers[field_name] = represent
    return coercers


def _build_fk_type_coercers(model, fk_annotations):
    """Build type coercers for FK annotation target fields (e.g. related__price)."""
    coercers = {}
    for output_key, f_expr in fk_annotations.items():
        leaf = path_leaf_field(model, f_expr.name)
        if isinstance(leaf, models.FileField):
            continue
        _check_representable(leaf, f_expr.name)
        represent = field_representation(leaf)
        if represent is not None:
            coercers[output_key] = represent
    return coercers


def _file_fields(model, keys_to_paths):
    """{output key: file model field} for the keys whose path ends on a file."""
    found = {}
    for key, path in keys_to_paths.items():
        leaf = path_leaf_field(model, path)
        if isinstance(leaf, models.FileField):
            found[key] = leaf
    return found


def _compile_m2m_spec(model, m2m_field_name, sub_field_names):
    """Compile M2M field info for the two-query merge. An empty
    ``sub_field_names`` (the M2M listed on its own) renders the list of target
    pks, as the serializer's related field does."""
    m2m_field = model._meta.get_field(m2m_field_name)
    through_model = m2m_field.remote_field.through
    related_model = m2m_field.related_model
    source_fk = m2m_field.m2m_field_name()
    target_fk = m2m_field.m2m_reverse_field_name()

    for sub_field in sub_field_names:
        _single_valued_path(related_model, sub_field)

    # F() annotations for sub-fields, under an internal alias where the key
    # would collide with a through-model field (e.g. 'id').
    annotations = {}
    aliases = {}
    for i, sub_field in enumerate(sub_field_names):
        alias = f"_tdrf_m2m_{i}" if _key_collides(through_model, sub_field) else None
        aliases[sub_field] = alias or sub_field
        annotations[sub_field] = F(f"{target_fk}__{sub_field}")

    # Build type coercers for M2M sub-fields
    m2m_coercers = {}
    for sub_field in sub_field_names:
        leaf = path_leaf_field(related_model, sub_field)
        if isinstance(leaf, models.FileField):
            continue
        _check_representable(leaf, f"{m2m_field_name}__{sub_field}")
        represent = field_representation(leaf)
        if represent is not None:
            m2m_coercers[sub_field] = represent

    return {
        "through_model": through_model,
        "source_fk": source_fk,
        "target_fk": target_fk,
        "related_model": related_model,
        "sub_fields": sub_field_names,
        "annotations": annotations,
        "aliases": aliases,
        "type_coercers": m2m_coercers,
        "file_fields": _file_fields(related_model, {s: s for s in sub_field_names}),
    }


def _target_ordering(related_model, target_fk):
    """The target's Meta.ordering, as through-model order_by() terms (string
    entries only; expressions are skipped)."""
    terms = []
    for entry in related_model._meta.ordering or ():
        if not isinstance(entry, str) or entry == "?":
            continue
        desc = entry.startswith("-")
        name = entry.lstrip("-")
        if name == "pk":
            name = related_model._meta.pk.name
        terms.append(f"{'-' if desc else ''}{target_fk}__{name}")
    return terms


def _relation_flags(fk_annotations):
    """{flag alias: [output keys]}: the nested values of each relation path,
    under the alias its visibility flag is selected as (see
    apply_to_queryset)."""
    by_relation = defaultdict(list)
    for key, expr in fk_annotations.items():
        by_relation[expr.name.rsplit("__", 1)[0]].append(key)
    return {
        f"_tdrf_visible_{i}": by_relation[relation]
        for i, relation in enumerate(sorted(by_relation))
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
        # The configured list fields the plan renders (after the sensitive
        # deny-list and the nesting-depth limit), as ``?fields=`` selects them.
        self.configured_fields = list(original_fields)
        self._original_fields = set(original_fields)

        # FK annotation keys that would collide with a model field or attname
        # (`target__id` -> `target_id`) are selected under an internal alias
        # and renamed in post_process.
        self._fk_aliases = {
            key: f"_tdrf_fk_{i}"
            for i, key in enumerate(fk_annotations)
            if _key_collides(model, key)
        }
        # FK bases added only to carry nested fields (not configured alone).
        self._implicit_fk_bases = {
            e.name.split("__")[0] for e in fk_annotations.values()
        } - self._original_fields
        # Keys whose value is a file: rendered as a URL per request.
        self._file_fields = _file_fields(
            model,
            {
                **{f: f for f in simple_fields},
                **{k: e.name for k, e in fk_annotations.items()},
            },
        )
        # Concrete FK / one-to-one fields, select_related when properties load
        # instances.
        self._fk_names = []
        for name in simple_fields:
            try:
                field = model._meta.get_field(name)
            except FieldDoesNotExist:
                continue
            if field.concrete and (field.many_to_one or field.one_to_one):
                self._fk_names.append(name)

    def _fk_base_field(self, output_key):
        """Get the base FK field name from an annotation output key.
        e.g. 'related_name' -> 'related' (by looking up the F() expression)."""
        f_expr = self.fk_annotations.get(output_key)
        if f_expr:
            return f_expr.name.split("__")[0]
        return None

    def apply_to_queryset(
        self,
        queryset,
        readable_fields=None,
        allowed_fk_keys=None,
        allowed_m2m_subfields=None,
        fk_visibility=None,
    ):
        """Apply .values() + F() annotations to a queryset.

        Args:
            readable_fields: Set of readable BASE field names (snapshot
                level). Filters simple/property fields and the BASE of FK
                and M2M annotations.
            allowed_fk_keys: Optional set of FK annotation output keys (e.g.
                {'author_name', 'author_email'}) allowed at the NESTED
                level. Filtering by BASE field alone would leak nested
                fields the user shouldn't see.
            allowed_m2m_subfields: Optional dict {m2m_base: {sub_field, ...}}
                for per-nested-M2M-field permission filtering. If a user
                has `categories.read` but not `category.description.read`,
                pass {'categories': {'name'}} to drop the description
                sub-field. None = no per-sub-field filter (legacy behavior).
            fk_visibility: Optional dict {output_key: Q}. A nested value
                renders only where its Q holds (the JOIN target is in the
                caller's scope) and is NULL elsewhere — the row itself stays.
                The Q is selected once per relation path, as a flag
                post_process reads, however many nested values share it.

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

        # Per-nested-field FK perm gate.
        if allowed_fk_keys is not None:
            active_fk = {k: v for k, v in active_fk.items() if k in allowed_fk_keys}

        # An FK the config lists only through nested fields (`author__name`)
        # renders its raw id only alongside a nested value, as on the
        # serializer path.
        active_bases = {self._fk_base_field(k) for k in active_fk}
        active_simple = [
            f
            for f in active_simple
            if f in self._original_fields
            or f == self.pk_field
            or f in active_bases
            or f not in self._implicit_fk_bases
        ]

        # Per-nested-M2M-field perm gate. Trim each spec's annotations /
        # sub_fields / type_coercers to only the allowed sub-fields. If
        # all sub-fields are stripped, drop the whole spec. A bare M2M (no
        # sub-fields: renders target pks) is gated by its base field only.
        if allowed_m2m_subfields is not None:
            trimmed_m2m = {}
            for base_name, spec in active_m2m.items():
                if not spec["sub_fields"]:
                    trimmed_m2m[base_name] = spec
                    continue
                allowed = allowed_m2m_subfields.get(base_name)
                if allowed is None:
                    # No allow-list for this M2M → drop entirely (defaults
                    # to deny when the caller is being explicit)
                    continue
                kept_subs = [s for s in spec["sub_fields"] if s in allowed]
                if not kept_subs:
                    if spec.get("bare"):
                        # The M2M is also listed on its own: render its pks,
                        # as the serializer does when the sub-fields are hidden.
                        trimmed_m2m[base_name] = {**spec, "sub_fields": []}
                    continue
                trimmed_m2m[base_name] = {
                    **spec,
                    "sub_fields": kept_subs,
                    "annotations": {
                        k: v for k, v in spec["annotations"].items() if k in allowed
                    },
                    "type_coercers": {
                        k: v
                        for k, v in spec.get("type_coercers", {}).items()
                        if k in allowed
                    },
                }
            active_m2m = trimmed_m2m

        # Always FETCH the pk, even when it isn't rendered (post_process strips
        # it unless it's configured and selected): the M2M merge and property
        # fields key on it, and `.values()` with no arguments would return
        # EVERY column — including ones the caller can't read — whenever
        # permission filtering or `?fields=` leaves nothing else selected.
        values_fields = list(active_simple)
        if self.pk_field not in values_fields:
            values_fields.insert(0, self.pk_field)

        annotations = {}
        for key, expr in active_fk.items():
            annotations[self._fk_aliases.get(key, key)] = expr
        for alias, keys in _relation_flags(active_fk).items():
            visible = next(
                (fk_visibility[k] for k in keys if (fk_visibility or {}).get(k)), None
            )
            if visible:
                annotations[alias] = Case(
                    When(visible, then=Value(True)),
                    default=Value(False),
                    output_field=models.BooleanField(),
                )

        compiled_qs = queryset.values(*values_fields, **annotations)
        return compiled_qs, (active_simple, active_fk, active_m2m, active_props)

    def post_process(
        self,
        rows,
        active_plan,
        m2m_target_filters=None,
        request=None,
        m2m_visibility=None,
    ):
        """Apply type coercion, property fields, and M2M merge to result rows.

        ``m2m_target_filters`` optionally maps an M2M output name to a queryset
        of target PKs the caller may see (finding F4). When present, the M2M
        merge's second query is scoped to those targets so a compiled M2M cannot
        render rows from a predicate/tenant-protected target. ``None`` = no
        scoping (public targets, or non-compiled callers).

        ``m2m_visibility`` optionally maps an M2M output name to
        ``{sub_field: Q on the through model}``: a sub-field that crosses
        another hop renders NULL where that hop's target is out of scope.

        ``request`` builds absolute file URLs, as the serializer does.
        """
        active_simple, active_fk, active_m2m, active_props = active_plan

        # 0. Internal aliases back to their output keys, and nested values
        #    whose relation the caller can't see to NULL.
        for key, alias in self._fk_aliases.items():
            if key in active_fk:
                for row in rows:
                    row[key] = row.pop(alias, None)
        for alias, keys in _relation_flags(active_fk).items():
            for row in rows:
                if not row.pop(alias, True):
                    for key in keys:
                        row[key] = None

        # 1. Property fields, computed on model instances (as the serializer
        #    path does).
        if active_props:
            self._compute_properties(rows, active_props, request)

        # 2. Type coercion: render as the serializer does (field_representation)
        coercers = dict(self.type_coercers)
        for key, field in self._file_fields.items():
            coercers[key] = field_representation(field, request)
        if coercers:
            for row in rows:
                for field_name, coercer in coercers.items():
                    if field_name in row and row[field_name] is not None:
                        row[field_name] = coercer(row[field_name])

        # 3. M2M merge (two-query approach)
        if active_m2m:
            pk_values = [row[self.pk_field] for row in rows]
            for m2m_name, spec in active_m2m.items():
                grouped = self._merge_m2m(
                    m2m_name,
                    spec,
                    pk_values,
                    (m2m_target_filters or {}).get(m2m_name),
                    (m2m_visibility or {}).get(m2m_name) or {},
                    request,
                )
                for row in rows:
                    row[m2m_name] = grouped.get(row[self.pk_field], [])

        # The pk is always fetched (see apply_to_queryset); render it only if
        # it's configured and selected.
        if (
            self.pk_field not in active_simple
            or self.pk_field not in self._original_fields
        ):
            for row in rows:
                row.pop(self.pk_field, None)

        return rows

    def _merge_m2m(self, m2m_name, spec, pk_values, target_filter, visibility, request):
        """{parent pk: [rendered targets]} for one M2M, in the target's
        Meta.ordering, without targets the target's default manager hides."""
        source_fk, target_fk = spec["source_fk"], spec["target_fk"]
        related_model = spec["related_model"]
        through_qs = spec["through_model"]._base_manager.filter(
            **{f"{source_fk}__in": pk_values}
        )
        # The serializer reads the M2M through the target's default manager:
        # a target it hides (soft delete) isn't rendered. F4: scope to target
        # rows visible via the target's endpoint (that queryset is built on
        # the default manager already).
        if target_filter is None:
            default_qs = related_model._default_manager.all()
            if default_qs.query.has_filters():
                target_filter = default_qs.values("pk")
        if target_filter is not None:
            through_qs = through_qs.filter(**{f"{target_fk}__in": target_filter})
        ordering = _target_ordering(related_model, target_fk)
        if ordering:
            through_qs = through_qs.order_by(*ordering)

        grouped = defaultdict(list)
        if not spec["sub_fields"]:
            # Bare M2M: the target pks.
            for source, target in through_qs.values_list(source_fk, target_fk):
                grouped[source].append(target)
            return grouped

        annotations = {}
        for sub_field, expr in spec["annotations"].items():
            visible = visibility.get(sub_field)
            if visible:
                expr = Case(When(visible, then=expr), default=Value(None))
            annotations[spec["aliases"][sub_field]] = expr
        coercers = dict(spec.get("type_coercers") or {})
        for sub_field, field in spec.get("file_fields", {}).items():
            if sub_field in spec["annotations"]:
                coercers[sub_field] = field_representation(field, request)

        for m2m_row in through_qs.values(source_fk, **annotations):
            pid = m2m_row.pop(source_fk)
            item = {}
            for sub_field in spec["annotations"]:
                value = m2m_row.get(spec["aliases"][sub_field])
                if value is not None and sub_field in coercers:
                    value = coercers[sub_field](value)
                item[sub_field] = value
            grouped[pid].append(item)
        return grouped

    def _compute_properties(self, rows, active_props, request=None):
        """Compute @property fields for ``rows`` exactly as the serializer path
        does: in the caller's sandbox (see ``turbodrf.properties``), or, for
        ``unscoped_properties``, on the full instances loaded in ONE query. A
        property that raises is omitted and logged."""
        from .properties import evaluate, sandboxed_values, unscoped_properties

        pks = [row.get(self.pk_field) for row in rows]
        unscoped = unscoped_properties(self.model)
        sandboxed = {k: v for k, v in active_props.items() if k not in unscoped}
        raw = {k: v for k, v in active_props.items() if k in unscoped}

        values = (
            sandboxed_values(self.model, pks, sandboxed, request) if sandboxed else {}
        )
        if raw:
            qs = self.model._base_manager.filter(
                pk__in={pk for pk in pks if pk is not None}
            )
            if self._fk_names:
                qs = qs.select_related(*self._fk_names)
            for instance in qs:
                computed = values.setdefault(instance.pk, {})
                for prop_name, fget in raw.items():
                    ok, value = evaluate(instance, prop_name, fget)
                    if ok:
                        computed[prop_name] = value

        for row in rows:
            row.update(values.get(row.get(self.pk_field), {}))


def compile_model(model):
    """Compile a query plan for a model's list view.

    Returns a CompiledQueryPlan if the model opts in (compiled=True), or None
    if it doesn't — or if its fields can't be rendered on the compiled path the
    way the serializer path renders them (see ``compile_blocker``); such a
    model is served by the serializer path.
    Raises ImproperlyConfigured for a field that doesn't exist.
    """
    try:
        return _compile_model(model)
    except NotCompilable as exc:
        logger.info(
            "TurboDRF: %s is served by the serializer path, not compiled: %s",
            model.__name__,
            exc,
        )
        return None


def compile_blocker(model):
    """Why ``model`` isn't compiled despite opting in, or ``None``."""
    try:
        _compile_model(model)
    except NotCompilable as exc:
        return str(exc)
    return None


def _compile_model(model):
    from .mixins import _model_config

    # The current scope's config (a platform block on the platform scope).
    config = _model_config(model)

    if not config.get("compiled", True):
        return None

    # Get list fields
    fields_config = config.get("fields", "__all__")
    if isinstance(fields_config, dict):
        list_fields = fields_config.get("list", fields_config.get("detail", []))
    else:
        list_fields = fields_config

    # Resolve __all__ as the serializer path does: concrete + M2M fields.
    if list_fields == "__all__":
        list_fields = [f.name for f in model._meta.fields] + [
            f.name for f in model._meta.many_to_many
        ]

    # Strip sensitive fields

    from .validation import is_field_path_sensitive

    list_fields = [f for f in list_fields if not is_field_path_sensitive(f)]

    # Drop paths deeper than TURBODRF_MAX_NESTING_DEPTH, as the serializer
    # path does.
    from django.core.exceptions import ValidationError

    from .validation import validate_nesting_depth

    within_depth = []
    for f in list_fields:
        try:
            validate_nesting_depth(f)
        except ValidationError as exc:
            logger.warning("Skipping field '%s' on %s: %s", f, model.__name__, exc)
            continue
        within_depth.append(f)
    list_fields = within_depth

    original_fields = list(list_fields)

    simple_fields = []
    fk_annotations = {}
    m2m_groups = {}  # base_field -> [sub_field_names] ([] = the M2M on its own)
    bare_m2m = set()  # M2Ms listed on their own (render pks)
    property_fields = {}

    for field_name in list_fields:
        if "__" not in field_name:
            # Simple field — check if it's a DB column or property
            try:
                field = model._meta.get_field(field_name)
            except FieldDoesNotExist:
                # Check if it's a model property
                model_attr = getattr(model, field_name, None)
                if isinstance(model_attr, property):
                    property_fields[field_name] = model_attr.fget
                    continue
                raise ImproperlyConfigured(
                    f"TurboDRF compiled path: '{field_name}' on "
                    f"{model.__name__} is not a database field or property."
                )
            if _forward_m2m(field):
                m2m_groups.setdefault(field_name, [])
                bare_m2m.add(field_name)
            elif field.many_to_many or field.one_to_many:
                raise NotCompilable(f"'{field_name}' is a reverse relation")
            elif field_name not in simple_fields:
                simple_fields.append(field_name)
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

            if _forward_m2m(base_field):
                # M2M — group sub-fields by base name
                sub_field = "__".join(parts[1:])
                m2m_groups.setdefault(base, []).append(sub_field)
            elif base_field.is_relation:
                # FK / one-to-one (either side) — create F() annotation
                _single_valued_path(model, field_name)
                output_key = field_name.replace("__", "_")
                fk_annotations[output_key] = F(field_name)

                # Ensure base FK field is in simple_fields (for the raw ID)
                if base not in simple_fields:
                    simple_fields.append(base)
            else:
                raise ImproperlyConfigured(
                    f"TurboDRF compiled path: '{field_name}' on "
                    f"{model.__name__} traverses a non-relation field."
                )

    # Compile M2M specs (a bare M2M plus its sub-fields renders the sub-fields,
    # as the serializer path does)
    m2m_specs = {}
    for base_name, sub_fields in m2m_groups.items():
        m2m_specs[base_name] = _compile_m2m_spec(model, base_name, sub_fields)
        m2m_specs[base_name]["bare"] = base_name in bare_m2m

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


def _walk_fk_annotation_chain(model, path):
    """Walk an FK annotation path (e.g. 'author__publisher__name') and
    yield every intermediate model along the JOIN chain (excluding the
    starting model). Stops at the first non-relational hop — the leaf
    column itself is not a model.

    Returns a list of (step_model, field_name) tuples for each JOIN
    target, or an empty list if the path is unresolvable.
    """
    parts = path.split("__")
    chain = []
    current_model = model
    for part in parts:
        try:
            field = current_model._meta.get_field(part)
        except FieldDoesNotExist:
            return []
        if hasattr(field, "related_model") and field.related_model:
            current_model = field.related_model
            chain.append((current_model, part))
        else:
            # Reached a column — no further JOINs.
            break
    return chain


def validate_compiled_path_safety(model):
    """Refuse to boot if the compiled read path would JOIN into related rows
    with their own row-level rules.

    At request time the compiled list already scopes both cases below to the
    targets the caller can see (``views._compiled_list``: the M2M merge's
    second query is filtered, a nested FK value is NULL where its target is
    out of scope). This gate catches the configurations at deploy time
    instead, as defence in depth. Two related cases are checked:

    1. **Compiled M2M target bypass.** The compiled M2M merge issues a
       separate through-table query (``CompiledQueryPlan._merge_m2m``)
       joining the target model. A model that nests an M2M whose target
       carries its own visibility rules is refused.

    2. **Compiled FK annotation bypass.** ``F('author__name')`` produces
       a SQL JOIN to ``author``. Same shape as the M2M case; same rule,
       checked at every model along the JOIN chain.

    Detection is fully static: walk each compiled plan's ``m2m_specs``
    and ``fk_annotations``, look up every model along the JOIN chain in
    the predicate registry, and refuse to start if any link has
    predicates or a tenant_field the parent doesn't.

    Escape hatches (raised in the error message):
        * Drop ``<rel>__*`` from the model's ``turbodrf()`` fields.
        * Set ``'compiled': False`` on the model to use the DRF path,
          which applies target predicates correctly.
        * Set ``TURBODRF_ALLOW_UNSAFE_COMPILED_M2M = True`` /
          ``TURBODRF_ALLOW_UNSAFE_COMPILED_FK = True`` to bypass the
          relevant gate (logs a loud warning; for migrations only).

    See ``docs/security.md`` for the full bug class.
    """
    from django.conf import settings

    from .predicates import get_predicates, get_tenant_field

    plan = get_compiled_plan(model)
    if plan is None:
        return

    allow_unsafe_m2m = getattr(settings, "TURBODRF_ALLOW_UNSAFE_COMPILED_M2M", False)
    allow_unsafe_fk = getattr(settings, "TURBODRF_ALLOW_UNSAFE_COMPILED_FK", False)
    parent_tenant = get_tenant_field(model)

    def _classify_step(step_model):
        target_predicates = get_predicates(step_model)
        target_tenant = get_tenant_field(step_model)
        reasons = []
        if target_predicates:
            reasons.append(
                f"{step_model.__name__} has "
                f"{len(target_predicates)} registered predicate(s) "
                f"({', '.join(type(p).__name__ for p in target_predicates)}) "
                f"that the compiled read path does not apply"
            )
        if target_tenant is not None and parent_tenant is None:
            reasons.append(
                f"{step_model.__name__} declares tenant_field="
                f"{target_tenant!r} but {model.__name__} is shared "
                f"(no tenant_field) — a shared parent rendering "
                f"tenanted rows leaks across tenants"
            )
        return reasons

    # ----- Case 1: M2M target bypass -----
    for m2m_base, spec in plan.m2m_specs.items():
        target = spec["related_model"]
        reasons = _classify_step(target)
        # ... and every further hop a sub-field crosses (`tags__folder__name`).
        for sub_field in spec["sub_fields"]:
            for step_model, _name in _walk_fk_annotation_chain(target, sub_field):
                reasons.extend(_classify_step(step_model))
        if not reasons:
            continue

        message = (
            f"{model.__name__}.turbodrf() exposes M2M field "
            f"'{m2m_base}' (target: {target.__name__}), but: "
            f"{'; '.join(reasons)}.\n\n"
            f"This is a real data-leak class — the compiled read path "
            f"renders {target.__name__} rows the caller cannot see via "
            f"the {target.__name__} endpoint. See "
            f"docs/security.md#compiled-m2m-target-bypass.\n\n"
            f"Fix one of:\n"
            f"  • Drop '{m2m_base}__*' from {model.__name__}'s "
            f"turbodrf() 'fields'.\n"
            f"  • Set 'compiled': False on {model.__name__}.turbodrf() "
            f"to use the serializer path, which scopes each target at "
            f"request time instead.\n"
            f"  • Remove predicates / tenant_field from "
            f"{target.__name__} only if it is genuinely public "
            f"reference data with no row-level rules.\n"
            f"  • Set TURBODRF_ALLOW_UNSAFE_COMPILED_M2M=True to "
            f"bypass this gate (NOT recommended; logs a warning)."
        )

        if allow_unsafe_m2m:
            logger.warning(
                "TURBODRF_ALLOW_UNSAFE_COMPILED_M2M=True — bypassing "
                "compiled M2M safety gate. %s",
                message,
            )
            continue

        raise ImproperlyConfigured(message)

    # ----- Case 2: FK annotation JOIN bypass -----
    for output_key, f_expr in plan.fk_annotations.items():
        path = f_expr.name
        chain = _walk_fk_annotation_chain(model, path)
        if not chain:
            continue

        offending = []
        for step_model, _name in chain:
            reasons = _classify_step(step_model)
            if reasons:
                offending.append((step_model, reasons))

        if not offending:
            continue

        joined = "; ".join(f"[{m.__name__}] {' / '.join(r)}" for m, r in offending)
        terminal = offending[-1][0]
        message = (
            f"{model.__name__}.turbodrf() exposes FK annotation "
            f"'{path}' (output key: '{output_key}'), but: {joined}.\n\n"
            f"This is the same data-leak class as the compiled M2M "
            f"target bypass — the F() annotation joins to "
            f"{terminal.__name__} without applying its own visibility "
            f"rules. Field-level perms gate which output keys render, "
            f"but the JOIN still executes and leaks row existence and "
            f"timing side channels. See "
            f"docs/security.md#compiled-fk-annotation-bypass.\n\n"
            f"Fix one of:\n"
            f"  • Drop '{path}' from {model.__name__}'s turbodrf() "
            f"'fields'.\n"
            f"  • Set 'compiled': False on {model.__name__}.turbodrf() "
            f"to use the DRF serializer path.\n"
            f"  • Remove predicates / tenant_field from "
            f"{terminal.__name__} only if it is genuinely public "
            f"reference data with no row-level rules.\n"
            f"  • Set TURBODRF_ALLOW_UNSAFE_COMPILED_FK=True to bypass "
            f"this gate (NOT recommended; logs a warning)."
        )

        if allow_unsafe_fk:
            logger.warning(
                "TURBODRF_ALLOW_UNSAFE_COMPILED_FK=True — bypassing "
                "compiled FK annotation safety gate. %s",
                message,
            )
            continue

        raise ImproperlyConfigured(message)
