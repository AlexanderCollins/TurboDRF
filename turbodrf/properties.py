"""
Sandboxed evaluation of model ``@property`` fields.

A property listed in ``fields`` is ordinary Python on the model. Left alone it
reads whatever the instance can reach: another tenant's related row, a column
the caller has no read rule for, any query it cares to run. So every property
is computed on a sandbox copy of the row:

- The row is loaded with only the columns the caller may read. Every other
  column is deferred, and loading it is a query.
- The relations the model names in ``property_relations`` are loaded first,
  each hop within the caller's scope (the target's own ``tenant_field`` and
  predicates, as for a nested ``__`` field) and with only the target columns
  the caller may read. An out-of-scope FK target reads as ``None``; an
  out-of-scope row is missing from a reverse or many-to-many relation.
- While the property runs, every database query raises.

A property that needs anything else (an unreadable column, an undeclared
relation, a query of its own) fails, and is left out of the response and
logged, like any property that raises. ``unscoped_properties`` opts a property
out: it runs on the full instance with unrestricted database access.

Config::

    'property_relations': {'holding_label': ['holding', 'holding__portfolio']},
    'unscoped_properties': ['legacy_total'],
"""

import inspect
import logging
from contextlib import ExitStack, contextmanager

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.db.models import Prefetch, prefetch_related_objects
from django.db.models.fields.related import ForeignObjectRel

logger = logging.getLogger(__name__)

# Primary keys per sandbox query (SQLite's variable limit).
_BATCH_SIZE = 500

# (model, property) pairs already warned about; later failures log at DEBUG.
_warned = set()


class PropertyQueryBlocked(Exception):
    """A property tried to query the database inside the sandbox."""


def _block_query(execute, sql, params, many, context):
    raise PropertyQueryBlocked(
        "a @property can't query the database: declare the relations it "
        "reads in 'property_relations', or list it in 'unscoped_properties'"
    )


@contextmanager
def queries_blocked():
    """Every database query in the block raises ``PropertyQueryBlocked``."""
    from django.db import connections

    with ExitStack() as stack:
        for alias in connections:
            stack.enter_context(connections[alias].execute_wrapper(_block_query))
        yield


def _config(model):
    try:
        config = model.turbodrf()
    except Exception:
        return {}
    return config if isinstance(config, dict) else {}


def unscoped_properties(model):
    """Properties the model opts out of the sandbox."""
    return frozenset(_config(model).get("unscoped_properties") or ())


def attribute_getter(name):
    """Getter for property ``name``: a zero-argument method is called."""

    def getter(instance):
        value = getattr(instance, name)
        return value() if callable(value) else value

    return getter


# --------------------------------------------------------------------------
# Relation paths
# --------------------------------------------------------------------------


def _is_supported_relation(field):
    if isinstance(field, ForeignObjectRel):
        return type(field).__name__ in ("ManyToOneRel", "OneToOneRel", "ManyToManyRel")
    return isinstance(field, (models.ForeignKey, models.ManyToManyField))


def _resolve_hop(model, name):
    """The relation ``name`` on ``model`` (a forward field name, or a reverse
    relation's accessor or query name) as ``(field, accessor)``, or ``None``."""
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        field = None
    if field is None:
        for rel in model._meta.related_objects:
            if rel.get_accessor_name() == name:
                field = rel
                break
    if field is None or not field.is_relation:
        return None
    if isinstance(field, ForeignObjectRel):
        return field, field.get_accessor_name()
    return field, field.name


def _single_valued(field):
    return field.many_to_one or field.one_to_one


def relation_hops(model, path):
    """The hops of a ``property_relations`` path as
    ``[(source_model, field, accessor, target_model), ...]``. A trailing plain
    field (``holding__name``) is allowed and ignored. Raises
    ``ImproperlyConfigured`` for anything else."""
    if not isinstance(path, str) or not path:
        raise ImproperlyConfigured(
            f"{model.__name__} property_relations: {path!r} is not a field path"
        )

    parts = path.split("__")
    hops = []
    current = model
    for i, part in enumerate(parts):
        resolved = _resolve_hop(current, part)
        if resolved is None:
            try:
                plain = current._meta.get_field(part)
            except FieldDoesNotExist:
                plain = None
            if i == len(parts) - 1 and i > 0 and plain is not None:
                break
            raise ImproperlyConfigured(
                f"{model.__name__} property_relations: '{part}' in {path!r} "
                f"is not a relation on {current.__name__}"
            )
        field, accessor = resolved
        if not _is_supported_relation(field):
            raise ImproperlyConfigured(
                f"{model.__name__} property_relations: '{part}' in {path!r} "
                f"is a {type(field).__name__}; only ForeignKey, OneToOneField, "
                f"ManyToManyField and their reverse relations can be loaded"
            )
        target = field.related_model
        hops.append((current, field, accessor, target))
        current = target
    return hops


def _within_depth(path):
    from django.core.exceptions import ValidationError

    from .validation import validate_nesting_depth

    try:
        validate_nesting_depth(path)
    except ValidationError:
        return False
    return True


def property_relations(model):
    """``{property: [relation path, ...]}`` from the model's config."""
    relations = _config(model).get("property_relations") or {}
    return relations if isinstance(relations, dict) else {}


def validate_property_config(model):
    """Startup check of ``property_relations`` / ``unscoped_properties``."""
    config = _config(model)
    relations = config.get("property_relations")
    unscoped = config.get("unscoped_properties")

    def _check_name(key, name):
        if not isinstance(name, str) or not name:
            raise ImproperlyConfigured(
                f"{model.__name__} {key}: {name!r} is not a property name"
            )
        try:
            model._meta.get_field(name)
        except FieldDoesNotExist:
            pass
        else:
            raise ImproperlyConfigured(
                f"{model.__name__} {key}: '{name}' is a model field, not a "
                f"@property"
            )
        if not hasattr(model, name):
            raise ImproperlyConfigured(
                f"{model.__name__} {key}: '{name}' is not an attribute of "
                f"{model.__name__}"
            )

    if relations is not None:
        if not isinstance(relations, dict):
            raise ImproperlyConfigured(
                f"{model.__name__} property_relations must be a dict of "
                f"property name -> list of relation paths"
            )
        for name, paths in relations.items():
            _check_name("property_relations", name)
            if isinstance(paths, str) or not isinstance(paths, (list, tuple)):
                raise ImproperlyConfigured(
                    f"{model.__name__} property_relations['{name}'] must be a "
                    f"list of relation paths"
                )
            for path in paths:
                relation_hops(model, path)
                if not _within_depth(path):
                    # Like a too-deep field: not loaded, so a property that
                    # reads it fails closed.
                    logger.warning(
                        "%s property_relations path '%s' exceeds "
                        "TURBODRF_MAX_NESTING_DEPTH and is not loaded",
                        model.__name__,
                        path,
                    )

    if unscoped is not None:
        if isinstance(unscoped, str) or not isinstance(unscoped, (list, tuple)):
            raise ImproperlyConfigured(
                f"{model.__name__} unscoped_properties must be a list of "
                f"property names"
            )
        for name in unscoped:
            _check_name("unscoped_properties", name)
            if relations and name in relations:
                raise ImproperlyConfigured(
                    f"{model.__name__}: '{name}' is in both property_relations "
                    f"and unscoped_properties"
                )


# --------------------------------------------------------------------------
# What the caller may read
# --------------------------------------------------------------------------


def _readable_columns(model, user, bypassed):
    """Concrete field names of ``model`` the caller may read."""
    from .backends import build_permission_snapshot
    from .validation import is_field_path_sensitive

    names = [
        f.name
        for f in model._meta.concrete_fields
        if not is_field_path_sensitive(f.name)
    ]
    if bypassed:
        return set(names)
    snapshot = build_permission_snapshot(user, model)
    return {name for name in names if snapshot.can_read_field(name)}


def _hop_readable(model, field, user, bypassed):
    """May the caller follow ``field`` from ``model``? Same rule as a nested
    ``__`` field: an explicit read rule if the field has one, else
    model-level read."""
    from .backends import build_permission_snapshot
    from .validation import is_field_path_sensitive

    names = {field.name}
    if isinstance(field, ForeignObjectRel):
        names.add(field.get_accessor_name())
    if any(is_field_path_sensitive(n) for n in names):
        return False
    if bypassed:
        return True
    snapshot = build_permission_snapshot(user, model)
    if snapshot.has_read_rule(field.name):
        return snapshot.can_read_field(field.name)
    return snapshot.can_perform_action("read")


def _hop_queryset(field, target, request, user, bypassed):
    """Scoped, column-restricted queryset for one hop's target rows."""
    from .validation import scoped_target_queryset

    # Django follows a single-valued relation without the default manager
    # and a many-valued one through it; the sandbox reads the same rows.
    single = _single_valued(field)
    manager = target._base_manager if single else target._default_manager
    scoped = scoped_target_queryset(target, request, base_manager=single)
    qs = manager.all() if scoped is None else manager.filter(pk__in=scoped.values("pk"))

    columns = _readable_columns(target, user, bypassed)
    # The join column: its value is already known from the source side.
    if isinstance(field, ForeignObjectRel) and not field.many_to_many:
        columns.add(field.field.name)
    elif not field.many_to_many and not isinstance(field, ForeignObjectRel):
        columns.add(field.target_field.name)
    columns.discard(target._meta.pk.name)
    return qs.only(target._meta.pk.name, *columns)


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def _materialize(value):
    """Evaluate lazy results inside the sandbox: a queryset or generator
    returned by a property would otherwise query after it, unscoped."""
    from django.db.models.manager import BaseManager

    if isinstance(value, BaseManager):
        value = value.all()
    if isinstance(value, models.QuerySet) or inspect.isgenerator(value):
        return list(value)
    return value


def _log_failure(model, name, exc):
    key = (model, name)
    level = logging.DEBUG if key in _warned else logging.WARNING
    _warned.add(key)
    logger.log(level, "Property field %s on %s failed: %r", name, model.__name__, exc)


def evaluate(instance, name, getter):
    """``(True, value)``, or ``(False, None)`` if the property raised."""
    try:
        return True, _materialize(getter(instance))
    except Exception as exc:
        _log_failure(type(instance), name, exc)
        return False, None


def sandboxed_values(model, pks, getters, request):
    """``{pk: {property: value}}`` for the rows ``pks``, each property computed
    in the sandbox. A property that fails is left out of its row's dict."""
    from .permissions import permissions_bypassed

    pks = list(dict.fromkeys(pk for pk in pks if pk is not None))
    results = {pk: {} for pk in pks}
    if not pks or not getters:
        return results

    user = getattr(request, "user", None) if request is not None else None
    bypassed = permissions_bypassed()

    relations = property_relations(model)
    lookups = {}
    for name in getters:
        for path in relations.get(name) or ():
            if not _within_depth(path):
                continue
            prefix = []
            for source, field, accessor, target in relation_hops(model, path):
                if not _hop_readable(source, field, user, bypassed):
                    break
                prefix.append(accessor)
                lookup = "__".join(prefix)
                if lookup not in lookups:
                    lookups[lookup] = _hop_queryset(
                        field, target, request, user, bypassed
                    )
    prefetches = [
        Prefetch(lookup, queryset=lookups[lookup])
        for lookup in sorted(lookups, key=lambda key: (key.count("__"), key))
    ]

    pk_name = model._meta.pk.name
    columns = _readable_columns(model, user, bypassed) - {pk_name}
    for start in range(0, len(pks), _BATCH_SIZE):
        batch = pks[start : start + _BATCH_SIZE]
        instances = list(
            model._base_manager.filter(pk__in=batch).only(pk_name, *columns)
        )
        if prefetches:
            prefetch_related_objects(instances, *prefetches)
        with queries_blocked():
            for instance in instances:
                row = results.setdefault(instance.pk, {})
                for name, getter in getters.items():
                    ok, value = evaluate(instance, name, getter)
                    if ok:
                        row[name] = value
    return results
