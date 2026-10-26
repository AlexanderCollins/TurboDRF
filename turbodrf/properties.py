"""
Sandboxed evaluation of model ``@property`` fields.

A property listed in ``fields`` is ordinary Python on the model. Left alone it
reads whatever the instance can reach: another tenant's related row, a column
the caller has no read rule for, any query it cares to run. So every property
is computed on a sandbox copy of the row:

- The row is loaded with only the columns the caller may read. Every other
  column is deferred, and loading it is a query. A column loaded anyway while
  the row is built (by a ``post_init`` handler, say) is dropped again.
- The relations the model names in ``property_relations`` are loaded first,
  each hop within the caller's scope (the target's own ``tenant_field`` and
  predicates, as for a nested ``__`` field) and with only the target columns
  the caller may read. An out-of-scope FK target reads as ``None``; an
  out-of-scope row is missing from a reverse or many-to-many relation. A hop
  isn't loaded when the caller can't read its target model or the column it
  joins on.
- While the property runs, every database query raises. Its result is turned
  into plain data (dicts, lists, scalars) before the sandbox closes, so a
  queryset or generator nested in it can't run later.

A property that needs anything else (an unreadable column, an undeclared
relation, a query of its own) or returns a model instance fails, and is left
out of the response and logged, like any property that raises.
``unscoped_properties`` opts a property out: it runs on the full instance with
unrestricted database access.

Config::

    'property_relations': {'holding_label': ['holding', 'holding__portfolio']},
    'unscoped_properties': ['legacy_total'],
"""

import datetime
import decimal
import ipaddress
import logging
import uuid
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager

from django.core.exceptions import (
    FieldDoesNotExist,
    ImproperlyConfigured,
    ObjectDoesNotExist,
)
from django.db import models
from django.db.models import Prefetch, prefetch_related_objects
from django.db.models.fields.related import ForeignObjectRel
from django.db.models.manager import BaseManager
from django.utils.functional import Promise

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
    """The current scope's config of ``model`` (mixins._model_config)."""
    from .mixins import _model_config

    try:
        return _model_config(model)
    except Exception:
        return {}


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


def _readable_columns(model, snapshot_for, bypassed):
    """Concrete field names of ``model`` the caller may read."""
    from .validation import is_field_path_sensitive

    names = [
        f.name
        for f in model._meta.concrete_fields
        if not is_field_path_sensitive(f.name)
    ]
    if bypassed:
        return set(names)
    snapshot = snapshot_for(model)
    return {name for name in names if snapshot.can_read_field(name)}


def _hop_readable(model, field, snapshot_for, bypassed):
    """May the caller follow ``field`` from ``model``? Same rule as a nested
    ``__`` field: an explicit read rule if the field has one, else
    model-level read; and the caller must be able to read the target model."""
    from .validation import is_field_path_sensitive

    names = {field.name}
    if isinstance(field, ForeignObjectRel):
        names.add(field.get_accessor_name())
    if any(is_field_path_sensitive(n) for n in names):
        return False
    if bypassed:
        return True
    snapshot = snapshot_for(model)
    if snapshot.has_read_rule(field.name):
        readable = snapshot.can_read_field(field.name)
    else:
        readable = snapshot.can_perform_action("read")
    return readable and snapshot_for(field.related_model).can_perform_action("read")


def _join_column(field):
    """The column of a hop's source row that its relation joins on."""
    reverse = isinstance(field, ForeignObjectRel)
    if not field.many_to_many:
        return field.field.target_field if reverse else field
    m2m = field.field if reverse else field
    source_fk = m2m.m2m_reverse_field_name() if reverse else m2m.m2m_field_name()
    return m2m.remote_field.through._meta.get_field(source_fk).target_field


def _target_columns(field, target, readable):
    """The columns of a hop's target rows to load: those the caller may read,
    plus the join column (its value is already known from the source side)."""
    columns = set(readable)
    if isinstance(field, ForeignObjectRel) and not field.many_to_many:
        columns.add(field.field.name)
    elif not field.many_to_many and not isinstance(field, ForeignObjectRel):
        columns.add(field.target_field.name)
    columns.discard(target._meta.pk.name)
    return columns


def _hop_queryset(field, target, request, columns):
    """Scoped queryset for one hop's target rows, loading only ``columns``."""
    from .validation import scoped_target_queryset

    # Django follows a single-valued relation without the default manager
    # and a many-valued one through it; the sandbox reads the same rows.
    single = _single_valued(field)
    manager = target._base_manager if single else target._default_manager
    scoped = scoped_target_queryset(target, request, base_manager=single)
    qs = manager.all() if scoped is None else scoped
    return qs.only(target._meta.pk.name, *columns)


def _strip(rows, model, columns):
    """Drop from ``rows`` every column but the pk and ``columns``: a
    ``post_init`` handler or ``__init__`` can load a deferred column while a
    row is built."""
    keep = {model._meta.pk.attname}
    keep.update(model._meta.get_field(name).attname for name in columns)
    drop = [f.attname for f in model._meta.concrete_fields if f.attname not in keep]
    for row in rows:
        for attname in drop:
            row.__dict__.pop(attname, None)


def _loaded_rows(instances, lookup):
    """The rows a prefetch loaded at ``lookup`` (``holding__portfolio``)
    below ``instances``."""
    rows = instances
    for accessor in lookup.split("__"):
        found = []
        for row in rows:
            try:
                value = getattr(row, accessor)
            except ObjectDoesNotExist:  # an empty reverse one-to-one
                continue
            if isinstance(value, BaseManager):
                found.extend(value.all())
            elif value is not None:
                found.append(value)
        rows = found
    return rows


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


# Values the JSON encoder renders without running any code of their own.
_SCALARS = (
    str,
    int,
    float,
    bytes,
    decimal.Decimal,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    uuid.UUID,
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
    ipaddress.IPv4Interface,
    ipaddress.IPv6Interface,
)


def _plain(value):
    """``value`` as plain data (dicts, lists, scalars), evaluated now: a
    queryset, manager, generator or other iterable anywhere in a property's
    result would otherwise run when the response is encoded, after the
    sandbox, unscoped. A model instance, or anything else the encoder can't
    render, fails the property."""
    if value is None or isinstance(value, _SCALARS):
        return value
    if isinstance(value, Promise):
        return str(value)
    if isinstance(value, models.Model):
        raise TypeError(
            f"a @property can't return a model instance "
            f"({type(value).__name__}); return the fields it needs"
        )
    if hasattr(value, "tolist"):  # numpy, as DRF's encoder renders it
        return _plain(value.tolist())
    if isinstance(value, BaseManager):
        value = value.all()
    if isinstance(value, Mapping):
        plain = {}
        for key, item in value.items():
            if key is not None and not isinstance(key, (str, int, float)):
                raise TypeError(f"a @property returned a {type(key).__name__} key")
            plain[key] = _plain(item)
        return plain
    if hasattr(value, "__iter__"):
        return [_plain(item) for item in value]
    raise TypeError(f"a @property returned a {type(value).__name__}")


def _log_failure(model, name, exc):
    key = (model, name)
    level = logging.DEBUG if key in _warned else logging.WARNING
    _warned.add(key)
    logger.log(level, "Property field %s on %s failed: %r", name, model.__name__, exc)


def evaluate(instance, name, getter):
    """``(True, value)``, or ``(False, None)`` if the property raised."""
    try:
        return True, _plain(getter(instance))
    except Exception as exc:
        _log_failure(type(instance), name, exc)
        return False, None


def sandboxed_values(model, pks, getters, request):
    """``{pk: {property: value}}`` for the rows ``pks``, each property computed
    in the sandbox. A property that fails is left out of its row's dict."""
    from .backends import build_permission_snapshot
    from .permissions import permissions_bypassed

    pks = list(dict.fromkeys(pk for pk in pks if pk is not None))
    results = {pk: {} for pk in pks}
    if not pks or not getters:
        return results

    user = getattr(request, "user", None) if request is not None else None
    bypassed = permissions_bypassed()
    snapshots = {}
    readable = {}

    def snapshot_for(target):
        if target not in snapshots:
            snapshots[target] = build_permission_snapshot(user, target)
        return snapshots[target]

    def columns_of(target):
        if target not in readable:
            readable[target] = _readable_columns(target, snapshot_for, bypassed)
        return readable[target]

    relations = property_relations(model)
    lookups = {}  # lookup -> (target model, columns it loads, queryset)
    for name in getters:
        for path in relations.get(name) or ():
            if not _within_depth(path):
                continue
            prefix = []
            for source, field, accessor, target in relation_hops(model, path):
                if not _hop_readable(source, field, snapshot_for, bypassed):
                    break
                # The prefetch reads the join column off each source row.
                join = _join_column(field).name
                if join != source._meta.pk.name and join not in columns_of(source):
                    break
                prefix.append(accessor)
                lookup = "__".join(prefix)
                if lookup not in lookups:
                    columns = _target_columns(field, target, columns_of(target))
                    queryset = _hop_queryset(field, target, request, columns)
                    lookups[lookup] = (target, columns, queryset)
    ordered = sorted(lookups, key=lambda key: (key.count("__"), key))
    prefetches = [Prefetch(lookup, queryset=lookups[lookup][2]) for lookup in ordered]

    pk_name = model._meta.pk.name
    columns = columns_of(model) - {pk_name}
    for start in range(0, len(pks), _BATCH_SIZE):
        batch = pks[start : start + _BATCH_SIZE]
        instances = list(
            model._base_manager.filter(pk__in=batch).only(pk_name, *columns)
        )
        # Building a row can load more than the sandbox allows (a post_init
        # handler reading a deferred column or a related object): drop it
        # before the scoped prefetch, which skips a relation already cached.
        _strip(instances, model, columns)
        for instance in instances:
            instance._state.fields_cache.clear()
            instance.__dict__.pop("_prefetched_objects_cache", None)
        if prefetches:
            prefetch_related_objects(instances, *prefetches)
            with queries_blocked():
                for lookup in ordered:
                    target, target_columns, _queryset = lookups[lookup]
                    _strip(_loaded_rows(instances, lookup), target, target_columns)
        with queries_blocked():
            for instance in instances:
                row = results.setdefault(instance.pk, {})
                for name, getter in getters.items():
                    ok, value = evaluate(instance, name, getter)
                    if ok:
                        row[name] = value
    return results
