"""Differential fuzz of the 0.8.4 query rewrites against the shapes they
replaced (tests/integration/test_query_shapes.py has the hand-picked cases).

Property 1: ``Authorization(tenant_q, q).scope(qs)``, which turns each to-many
lookup of ``q`` into ``pk__in`` the rows it matches
(``views.without_to_many_joins``), matches the rows of the reference shape
``qs.filter(pk__in=qs.filter(q).values("pk"))``, each once.

Property 2: ``build_traversal_scope_q(model, path, request)``, in one
``filter()`` call with a lookup on ``path`` (as the filter and search backends
use it), matches the rows it matches when each related model's scope is
checked as ``<prefix>__in=scoped_target_queryset(...)`` instead of on the
joined row (``validation._scope_on_joined_row``).

Both run on random rows and random ``Q`` trees (AND / OR / XOR, negated,
nested) over the test app's Parity* models, ScopeBoard, User and Brokerage:
FK, nullable FK, ``to_field`` FK, one-to-one and reverse one-to-one, M2M and
reverse FK paths, multi-table inheritance; constants, ``F()`` (also inside an
``__in`` list or a ``__range``), ``Exists`` and ``Subquery``. Skipped: an input
the reference shape can't run, a mismatch while Django mis-correlated a
subquery (``_split_exclude_alias_clashes``), and the known bug's input
(``_scope_without_conditions``). TestFuzzCounterexamples holds the minimised
counterexamples.

CI runs ``TURBODRF_REWRITE_EXAMPLES`` (200) examples per property. To fuzz
harder, raise it:

    TURBODRF_REWRITE_EXAMPLES=5000 pytest -n 0 -p no:cacheprovider \\
        tests/conformance/test_rewrite_equivalence.py
"""

import os
from collections import Counter, namedtuple
from contextlib import contextmanager
from decimal import Decimal
from functools import lru_cache
from types import SimpleNamespace
from unittest import mock
from uuid import UUID

import pytest

pytest.importorskip("hypothesis", reason="hypothesis not installed (dev/test extra)")

from django.contrib.auth import get_user_model  # noqa: E402
from django.core.exceptions import FieldError, ValidationError  # noqa: E402
from django.db import DatabaseError, models, transaction  # noqa: E402
from django.db.models import Exists, F, OuterRef, Q, QuerySet, Subquery  # noqa: E402
from django.db.models.sql.query import Query  # noqa: E402
from django.test import TestCase as DjangoTestCase  # noqa: E402
from hypothesis import HealthCheck, assume, event, given, note, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.errors import InvalidArgument  # noqa: E402
from hypothesis.extra.django import TestCase  # noqa: E402

from tests.test_app.apps import _test_user_brokerages  # noqa: E402
from tests.test_app.models import (  # noqa: E402
    Brokerage,
    ParityAllTypes,
    ParityBase,
    ParityChild,
    ParityCodePk,
    ParityCurrency,
    ParityLeaf,
    ParityTag,
    ParityTarget,
    ParityUuidPk,
    ScopeBoard,
)
from turbodrf import predicates as registry  # noqa: E402
from turbodrf import validation  # noqa: E402
from turbodrf.predicates import (  # noqa: E402
    Conditional,
    Custom,
    Either,
    Members,
    Owner,
)
from turbodrf.validation import (  # noqa: E402
    build_traversal_scope_q,
    get_nested_field_model,
)
from turbodrf.views import (  # noqa: E402
    Authorization,
    _leaves,
    _q_spans_multivalued,
    without_to_many_joins,
)

User = get_user_model()

settings.register_profile(
    "rewrite-equivalence",
    max_examples=int(os.environ.get("TURBODRF_REWRITE_EXAMPLES", "200")),
    deadline=None,
    database=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.data_too_large,
        HealthCheck.large_base_example,
    ],
)
FUZZ = settings.get_profile("rewrite-equivalence")

# ---------------------------------------------------------------------------
# Schema: per model, the columns and the relations a path may use.
# ---------------------------------------------------------------------------

SCHEMA = {
    ParityAllTypes: (
        [
            "id",
            "char",
            "char_null",
            "integer",
            "integer_null",
            "amount_null",
            "flag_null",
        ],
        ["target", "twin", "currency", "tags"],
    ),
    ParityTarget: (
        ["id", "name", "amount"],
        ["leaf", "partner", "currency", "rows", "twin_row", "paritycodepk"],
    ),
    ParityTag: (
        ["name", "weight", "is_deleted"],
        ["leaf", "currency", "rows", "code_rows", "uuid_rows"],
    ),
    ParityLeaf: (["name", "amount"], ["paritytarget", "partner_of", "paritytag"]),
    ParityCurrency: (
        ["code", "name"],
        ["paritytarget", "paritytag", "parityalltypes"],
    ),
    ParityCodePk: (["code", "name"], ["target", "tags"]),
    ParityUuidPk: (["id", "name", "rank"], ["tags"]),
    ParityBase: (["name"], ["paritychild"]),
    ParityChild: (["id", "name", "extra"], ["paritybase_ptr"]),
    ScopeBoard: (["title"], ["brokerage", "owner", "watchers"]),
    User: (["username"], ["scope_boards", "watched_scope_boards"]),
    Brokerage: (["name"], ["scope_boards"]),
}
MODELS = list(SCHEMA)

# Forward FK chains a model may use as its tenant_field (the user's tenant is
# an int pk, so any FK chain works as one).
TENANT_FIELDS = {
    ParityAllTypes: ["target", "twin", "target__leaf"],
    ParityTarget: ["leaf", "partner"],
    ParityTag: ["leaf"],
    ParityCodePk: ["target", "target__leaf"],
    ParityChild: ["paritybase_ptr"],
    ScopeBoard: ["brokerage", "owner"],
}

UUIDS = [UUID(int=1), UUID(int=2), UUID(int=9)]
CODES = ["AAA", "BBB", "CCC"]
CONSTANTS = {
    "str": ["a", "", "ab", "b", "A", "AAA", "c1", "u1"],
    "num": [1, 0, 2, -1, 3, 99, Decimal("2.5")],
    "bool": [True, False],
    "uuid": UUIDS,
}
LOOKUPS = {
    "str": ["exact", "in", "isnull", "gt", "lt", "icontains", "range"],
    "num": ["exact", "in", "isnull", "gt", "lt", "range"],
    "bool": ["exact", "in", "isnull"],
    "uuid": ["exact", "in", "isnull"],
}
RELATION_LOOKUPS = {"exact", "in", "isnull", "gt", "lt"}
MAX_HOPS = 3

Terminal = namedtuple("Terminal", "path kind relation to_many hops")


def _kind(field):
    """The kind of value ``field`` compares with: an FK compares as the column
    it references, a reverse or M2M relation as the related pk."""
    while field.is_relation:
        if field.many_to_many or not field.concrete:
            field = field.related_model._meta.pk
        else:
            field = field.target_field
    if isinstance(field, models.BooleanField):
        return "bool"
    if isinstance(field, models.UUIDField):
        return "uuid"
    if isinstance(field, (models.CharField, models.TextField)):
        return "str"
    return "num"


@lru_cache(maxsize=None)
def _terminals(model):
    """Every path from ``model`` over SCHEMA, at most MAX_HOPS relations long,
    ending on a column or on a relation."""
    found = []

    def walk(current, prefix, hops, to_many):
        columns, relations = SCHEMA[current]
        for name in columns:
            field = current._meta.get_field(name)
            found.append(Terminal(prefix + name, _kind(field), False, to_many, hops))
        for name in relations:
            field = current._meta.get_field(name)
            many = to_many or field.many_to_many or field.one_to_many
            if hops + 1 <= MAX_HOPS:
                found.append(
                    Terminal(prefix + name, _kind(field), True, many, hops + 1)
                )
            if hops + 1 <= MAX_HOPS and field.related_model in SCHEMA:
                walk(field.related_model, f"{prefix}{name}__", hops + 1, many)

    walk(model, "", 0, False)
    return tuple(found)


def _paths(model, min_hops=0, which="any"):
    """The terminals from ``model`` at least ``min_hops`` relations long
    (with ``__`` when ``min_hops``): ``which`` is "any", "single" (no to-many
    hop) or "to_many" (one or more)."""
    return [
        t
        for t in _terminals(model)
        if t.hops >= min_hops
        and not (min_hops and "__" not in t.path)
        and (which == "any" or t.to_many == (which == "to_many"))
    ]


@lru_cache(maxsize=None)
def _terminal(model, min_hops=0, which="any"):
    """A path from ``model`` (see ``_paths``), short ones first: Hypothesis
    shrinks to them."""
    by_hops = {}
    for t in _paths(model, min_hops, which):
        by_hops.setdefault(t.hops, []).append(t)
    hops = sorted(by_hops)
    # Bias towards short paths: every hop count once, the shortest twice.
    return st.sampled_from([hops[0], *hops]).flatmap(
        lambda h: st.sampled_from(by_hops[h])
    )


@lru_cache(maxsize=None)
def _f_paths(model, kind, single_valued=False):
    """Paths from ``model`` an ``F()`` comparing with a ``kind`` value may
    name, at most two hops long; ``single_valued``: none crossing a to-many
    relation."""
    paths = [
        t.path
        for t in _terminals(model)
        if t.kind == kind and t.hops <= 2 and not (single_valued and t.to_many)
    ]
    return sorted(paths, key=lambda p: (p.count("__"), p))


class _Exists(Exists):
    """``Exists`` with a readable repr (Hypothesis prints the inputs)."""

    label = "Exists(...)"

    def __repr__(self):
        return self.label


class _Subquery(Subquery):
    label = "Subquery(...)"

    def __repr__(self):
        return self.label


def _exists(model):
    """A correlated ``Exists`` over ``model``: an expression child of a Q."""

    def build(leaf):
        expr = _Exists(model._base_manager.filter(Q(leaf), pk=OuterRef("pk")))
        expr.label = (
            f"Exists({model.__name__}.filter({leaf[0]}={leaf[1]!r}, "
            f"pk=OuterRef('pk')))"
        )
        return expr

    return _leaf(model, mode=PLAIN).map(build)


def _one_value_subquery(model, path, correlated):
    rows = model._base_manager.all()
    if correlated:
        rows = rows.filter(pk=OuterRef("pk"))
    expr = _Subquery(rows.order_by(path).values(path)[:1])
    where = "pk=OuterRef('pk')" if correlated else ""
    expr.label = (
        f"Subquery({model.__name__}.filter({where}).order_by({path!r})"
        f".values({path!r})[:1])"
    )
    return expr


def _values_subquery(model, path):
    expr = _Subquery(model._base_manager.values(path))
    expr.label = f"Subquery({model.__name__}.values({path!r}))"
    return expr


# Value modes. ANY: constants, F() (single-valued or to-many, also inside an
# __in list or a __range), Subquery. PLAIN: constants. REWRITABLE: what the
# rewrites keep rather than refuse: constants, and F() on a single-valued path
# as the value of a single-valued lookup.
ANY, PLAIN, REWRITABLE = "any", "plain", "rewritable"


@st.composite
def _value(draw, model, t, lookup, mode):
    consts = CONSTANTS[t.kind]
    if lookup == "isnull":
        return draw(st.booleans())
    if mode == REWRITABLE:
        f_paths = [] if t.to_many else _f_paths(model, t.kind, single_valued=True)
        if lookup in ("exact", "gt", "lt", "icontains") and f_paths:
            if draw(st.integers(0, 3)) == 0:
                return F(draw(st.sampled_from(f_paths)))
        mode = PLAIN
    f_paths = _f_paths(model, t.kind)
    expression = mode == ANY and f_paths and draw(st.integers(0, 3)) == 0
    if lookup == "in":
        if not expression:
            return draw(st.lists(st.sampled_from([*consts, None]), max_size=3))
        if draw(st.integers(0, 2)):
            items = draw(st.lists(st.sampled_from(consts), max_size=2))
            items.insert(
                draw(st.integers(0, len(items))), F(draw(st.sampled_from(f_paths)))
            )
            return tuple(items) if draw(st.booleans()) else items
        return _values_subquery(model, draw(st.sampled_from(f_paths)))
    if lookup == "range":
        bounds = [draw(st.sampled_from(consts)), draw(st.sampled_from(consts))]
        if expression:
            bounds[draw(st.integers(0, 1))] = F(draw(st.sampled_from(f_paths)))
        return tuple(bounds)
    if expression:
        how = draw(st.sampled_from(["F", "F", "subquery", "correlated subquery"]))
        path = draw(st.sampled_from(f_paths))
        if how == "F":
            return F(path)
        return _one_value_subquery(model, path, how == "correlated subquery")
    if lookup == "exact":
        return draw(st.sampled_from([*consts, None]))
    return draw(st.sampled_from(consts))


@st.composite
def _leaf(draw, model, terminal=None, mode=ANY, which="any"):
    """A ``(lookup, value)`` pair on ``model``."""
    if terminal is None and mode == REWRITABLE and _paths(model, which="to_many"):
        # Mostly to-many lookups: the ones the rewrite turns into pk__in.
        which = draw(st.sampled_from(["any", "to_many", "to_many"]))
    t = terminal or draw(_terminal(model, which=which))
    lookups = [lk for lk in LOOKUPS[t.kind] if not t.relation or lk in RELATION_LOOKUPS]
    lookup = draw(st.sampled_from(lookups))
    value = draw(_value(model, t, lookup, mode))
    if lookup == "exact" and draw(st.booleans()):
        return (t.path, value)
    return (f"{t.path}__{lookup}", value)


def _node(connector, negated, children):
    return Q(*children, _connector=connector, _negated=negated)


def _as_q(child):
    return child if isinstance(child, Q) else Q(child)


@lru_cache(maxsize=None)
def _tree(model, max_leaves, mode):
    leaf = _leaf(model, mode=mode)
    # An empty Q() child: no condition, but its parent is truthy.
    empty = st.just(Q())
    if mode == REWRITABLE:
        base = st.one_of(leaf, leaf, leaf, leaf, leaf, leaf, leaf, empty)
        connectors = st.sampled_from(["AND", "OR"])
    else:
        base = st.one_of(leaf, leaf, leaf, leaf, leaf, leaf, _exists(model), empty)
        connectors = st.sampled_from(["AND", "OR", "AND", "OR", "XOR"])
    return st.recursive(
        base,
        lambda children: st.builds(
            _node, connectors, st.booleans(), st.lists(children, min_size=1, max_size=3)
        ),
        max_leaves=max_leaves,
    ).map(_as_q)


def _q_tree(model, max_leaves=5):
    """A random ``Q`` over ``model``: (lookup, value) leaves, ``Exists`` and
    empty ``Q()`` children under nested AND / OR / XOR nodes, any of them
    negated. Half are built only of what the rewrites keep (``REWRITABLE``):
    without them nearly every tree holds something that sends the rewrite to
    its fallback, which is the reference shape itself."""
    return st.one_of(
        _tree(model, max_leaves, REWRITABLE), _tree(model, max_leaves, ANY)
    )


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

STRINGS = ["a", "", "ab", "b", "A"]
NUMBERS = [0, 1, 2]
DECIMALS = [Decimal("0"), Decimal("1"), Decimal("2.5")]


@st.composite
def datasets(draw):
    """Rows for every SCHEMA model: zero, one or several related rows per
    relation, NULL FKs and repeated values. Explicit pks, so a counterexample
    reads the same on every database."""

    def pick(pool):
        return draw(st.sampled_from(pool))

    def maybe(pool):
        return draw(st.sampled_from([None, *pool]))

    def subset(pool):
        if not pool:
            return []
        return draw(st.lists(st.sampled_from(pool), unique=True, max_size=3))

    def count(most):
        # Zero rows last: an empty table checks little, so shrink away from it.
        return range(1, draw(st.sampled_from([*range(1, most + 1), 0])) + 1)

    def unique(values):
        """A one-to-one column: a repeated target becomes NULL."""
        seen = set()
        out = []
        for v in values:
            out.append(None if v in seen else v)
            if v is not None:
                seen.add(v)
        return out

    users = [1, 2, 3]
    currencies = [
        dict(id=i + 1, code=c, name=pick(STRINGS)) for i, c in enumerate(CODES)
    ]
    leaves = [dict(id=i, name=pick(STRINGS), amount=maybe(DECIMALS)) for i in count(4)]
    leaf_ids = [r["id"] for r in leaves]
    targets = [
        dict(
            id=i,
            name=pick(STRINGS),
            amount=maybe(DECIMALS),
            leaf_id=maybe(leaf_ids),
            partner_id=maybe(leaf_ids),
            currency_id=maybe(CODES),
        )
        for i in count(4)
    ]
    for row, partner in zip(targets, unique([r["partner_id"] for r in targets])):
        row["partner_id"] = partner
    target_ids = [r["id"] for r in targets]
    tags = [
        dict(
            id=i,
            name=pick(STRINGS),
            weight=maybe(DECIMALS),
            leaf_id=maybe(leaf_ids),
            currency_id=maybe(CODES),
            is_deleted=pick([False, True]),
        )
        for i in count(4)
    ]
    tag_ids = [r["id"] for r in tags]
    rows = [
        dict(
            id=i,
            char=pick(STRINGS),
            char_null=maybe(STRINGS),
            integer=pick(NUMBERS),
            integer_null=maybe(NUMBERS),
            amount_null=maybe(DECIMALS),
            flag_null=maybe([True, False]),
            target_id=maybe(target_ids),
            twin_id=maybe(target_ids),
            currency_id=maybe(CODES),
            tags=subset(tag_ids),
        )
        for i in count(6)
    ]
    for row, twin in zip(rows, unique([r["twin_id"] for r in rows])):
        row["twin_id"] = twin
    code_rows = [
        dict(
            code=f"c{i}",
            name=pick(STRINGS),
            target_id=maybe(target_ids),
            tags=subset(tag_ids),
        )
        for i in count(3)
    ]
    uuid_rows = [
        dict(
            id=UUIDS[i - 1],
            name=pick(STRINGS),
            rank=pick(NUMBERS),
            tags=subset(tag_ids),
        )
        for i in count(2)
    ]
    bases = [
        dict(id=i, name=pick(STRINGS), child=draw(st.booleans()), extra=pick(STRINGS))
        for i in count(4)
    ]
    boards = [
        dict(
            id=i,
            title=pick(STRINGS),
            brokerage_id=pick([1, 2]),
            owner_id=maybe(users),
            watchers=subset(users),
        )
        for i in count(4)
    ]
    return dict(
        currencies=currencies,
        leaves=leaves,
        targets=targets,
        tags=tags,
        rows=rows,
        code_rows=code_rows,
        uuid_rows=uuid_rows,
        bases=bases,
        boards=boards,
    )


def _create(model, specs, m2m=None):
    rows = [{k: v for k, v in spec.items() if k != m2m} for spec in specs]
    model.objects.bulk_create([model(**row) for row in rows])
    if m2m:
        field = model._meta.get_field(m2m)
        through = field.remote_field.through
        source = field.m2m_field_name() + "_id"
        target = field.m2m_reverse_field_name() + "_id"
        pk = model._meta.pk.attname
        through.objects.bulk_create(
            [
                through(**{source: spec[pk], target: other})
                for spec in specs
                for other in spec[m2m]
            ]
        )


def _load(data):
    Brokerage.objects.bulk_create([Brokerage(id=i, name=f"b{i}") for i in (1, 2)])
    User.objects.bulk_create([User(id=i, username=f"u{i}") for i in (1, 2, 3)])
    _create(ParityCurrency, data["currencies"])
    _create(ParityLeaf, data["leaves"])
    _create(ParityTarget, data["targets"])
    ParityTag.all_objects.bulk_create([ParityTag(**row) for row in data["tags"]])
    _create(ParityAllTypes, data["rows"], m2m="tags")
    _create(ParityCodePk, data["code_rows"], m2m="tags")
    _create(ParityUuidPk, data["uuid_rows"], m2m="tags")
    for row in data["bases"]:
        if row["child"]:
            ParityChild.objects.create(
                id=row["id"], name=row["name"], extra=row["extra"]
            )
        else:
            ParityBase.objects.create(id=row["id"], name=row["name"])
    _create(ScopeBoard, data["boards"], m2m="watchers")


# ---------------------------------------------------------------------------
# Running both shapes
# ---------------------------------------------------------------------------

# What an input the database or Django can't run raises.
UNRUNNABLE = (FieldError, DatabaseError, ValueError, TypeError, ValidationError)


def _record(record, text):
    """Hypothesis ``note()`` / ``event()``, a no-op outside a Hypothesis test
    (the explicit regression tests below run the same checks)."""
    try:
        record(text)
    except InvalidArgument:
        pass


@contextmanager
def _split_exclude_alias_clashes():
    """The negated to-many lookups for which Django's ``split_exclude`` takes
    its ``alias in can_reuse`` branch while the block builds a query.

    KNOWN DJANGO BUG (TestFuzzCounterexamples): ``split_exclude`` builds the
    subquery of a negated to-many lookup on a fresh query and compares the
    alias it selects from with the outer ``filter()`` call's aliases BY NAME.
    When the outer call already joined the same table through another
    relation (both are named after the table), Django correlates the subquery
    to that other join instead of to the lookup's own relation. The rewrite
    never reaches ``split_exclude``; the reference shape can, and is then
    wrong."""
    clashes = []
    reusable = []
    split_exclude, trim_start = Query.split_exclude, Query.trim_start

    def spy_split_exclude(self, filter_expr, can_reuse, names_with_path):
        reusable.append(can_reuse or ())
        try:
            return split_exclude(self, filter_expr, can_reuse, names_with_path)
        finally:
            reusable.pop()

    def spy_trim_start(self, names_with_path):
        result = trim_start(self, names_with_path)
        if reusable and self.select and self.select[0].alias in reusable[-1]:
            clashes.append(self.select[0].alias)
        return result

    with mock.patch.object(Query, "split_exclude", spy_split_exclude):
        with mock.patch.object(Query, "trim_start", spy_trim_start):
            yield clashes


def _skip_if_django_clashed(reference_clashes, optimised_clashes):
    """A mismatch while Django mis-correlated a shape is Django's bug, not
    the rewrite's (and the reference can't tell which rows are right)."""
    for shape, clashes in (
        ("reference", reference_clashes),
        ("optimised", optimised_clashes),
    ):
        if clashes:
            _record(event, f"skipped: Django split_exclude alias clash ({shape})")
    if reference_clashes or optimised_clashes:
        assume(False)


def _pks(queryset):
    """The pks ``queryset`` returns, in a savepoint (a Postgres error would
    abort the test's transaction)."""
    with transaction.atomic():
        return list(queryset.values_list("pk", flat=True))


def _reference(build):
    """``_pks(build())``, or None when Django or the database can't run it."""
    try:
        return _pks(build())
    except UNRUNNABLE as exc:
        _record(note, f"reference shape can't run: {exc!r}")
        return None


def _coverage(ref, queryset):
    """How much of ``queryset`` the reference matched (for ``event()``)."""
    total = queryset.count()
    if not ref:
        return "no rows matched" if total else "no rows"
    return "every row matched" if len(set(ref)) == total else "some rows matched"


def _q_text(q):
    """``repr(q)`` without running the querysets inside it (a QuerySet's repr
    runs it; a subquery that only runs inside its outer query would raise)."""
    with mock.patch.object(
        QuerySet, "__repr__", lambda qs: f"<QuerySet of {qs.model.__name__}>"
    ):
        return repr(q)


def _sql(queryset):
    try:
        return str(queryset.query)
    except Exception as exc:  # EmptyResultSet: the query matches nothing
        return f"<{type(exc).__name__}>"


def _verdict(ref, got):
    extra = sorted(set(got) - set(ref), key=str)
    missing = sorted(set(ref) - set(got), key=str)
    lines = [
        f"reference rows: {sorted(ref, key=str)}",
        f"optimised rows: {sorted(got, key=str)}",
    ]
    if extra:
        lines.append(f"WIDER: the optimised shape also matches {extra}")
    if missing:
        lines.append(f"NARROWER: the optimised shape misses {missing}")
    dupes = sorted((pk for pk, n in Counter(got).items() if n > 1), key=str)
    if dupes:
        lines.append(f"DUPLICATES in the optimised shape: {dupes}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Property 1: Authorization.scope
# ---------------------------------------------------------------------------


@st.composite
def scope_cases(draw):
    model = draw(st.sampled_from(MODELS))
    q = draw(_q_tree(model))
    # A tenant boundary: a plain single-valued lookup, applied first.
    tenant = draw(st.none() | _leaf(model, mode=PLAIN, which="single").map(Q))
    return model, tenant, q


def check_scope(model, tenant_q, q):
    """``Authorization(tenant_q, q).scope()`` returns the rows of the reference
    shape, each once."""
    qs = model._default_manager.all()
    base = qs if tenant_q is None else qs.filter(tenant_q)
    with _split_exclude_alias_clashes() as clashes:
        ref = _reference(lambda: base.filter(pk__in=base.filter(q).values("pk")))
    if ref is None:
        assume(False)
    if not _q_spans_multivalued(model, q):
        _record(event, "scope: plain filter")
    elif without_to_many_joins(model, q) is None:
        _record(event, "scope: pk__in wrap")
    else:
        _record(event, "scope: to-many lookups rewritten")
    _record(event, f"scope: {_coverage(ref, qs)}")
    with _split_exclude_alias_clashes() as clashes_too, transaction.atomic():
        scoped = Authorization(tenant_q, q).scope(qs)
        got = _pks(scoped)
    if Counter(got) != Counter(ref):
        _skip_if_django_clashed(clashes, clashes_too)
        raise AssertionError(
            f"Authorization.scope differs from the pk__in wrap on {model.__name__}\n"
            f"q = {q!r}\ntenant_q = {tenant_q!r}\n{_verdict(ref, got)}\n"
            f"optimised SQL: {_sql(scoped)}"
        )


class TestScopeRewrite(TestCase):
    @FUZZ
    @given(data=datasets(), case=scope_cases())
    def test_scope_matches_the_pk_in_wrap(self, data, case):
        _load(data)
        check_scope(*case)


# ---------------------------------------------------------------------------
# Property 2: build_traversal_scope_q
# ---------------------------------------------------------------------------


def _predicate_specs(model):
    """A predicate as plain data (readable when Hypothesis prints it)."""
    simple = [
        st.tuples(st.just("Custom"), _q_tree(model, max_leaves=4)),
        st.tuples(st.just("Conditional"), _q_tree(model, max_leaves=3)),
    ]
    if model is ScopeBoard:
        simple += [st.just(("Owner", "owner")), st.just(("Members", "watchers"))]
    one = st.one_of(*simple)
    either = st.tuples(st.just("Either"), st.lists(one, min_size=2, max_size=2))
    return st.one_of(one, one, either)


def _predicate(spec):
    kind, arg = spec
    if kind == "Custom":
        return Custom(lambda request, roles: arg, write_validator=lambda d, i, r: [])
    if kind == "Conditional":
        return Conditional(when=arg, require_roles=["boss"])
    if kind == "Owner":
        return Owner(arg, bypass=["boss"])
    if kind == "Members":
        return Members(arg)
    return Either(*[_predicate(child) for child in arg])


@st.composite
def traversal_cases(draw):
    parent = draw(st.sampled_from(MODELS))
    terms = []
    # Half the paths single-valued: only those hops are scoped on the joined
    # row (after a to-many hop both shapes are the same __in).
    paths = _terminal(parent, min_hops=1)
    if _paths(parent, min_hops=1, which="single"):
        paths = st.one_of(paths, _terminal(parent, min_hops=1, which="single"))
    for _ in range(draw(st.integers(1, 2))):
        t = draw(paths)
        terms.append((t.path, draw(_leaf(parent, terminal=t))))
    targets = []
    for path, _leaf_ in terms:
        for model, _field, _name in get_nested_field_model(parent, path)[1][1:]:
            if model not in targets:
                targets.append(model)
    rules = {}
    for model in targets:
        if draw(st.integers(0, 4)):
            tenant_field = draw(st.sampled_from([None, *TENANT_FIELDS.get(model, [])]))
            specs = draw(st.lists(_predicate_specs(model), max_size=2))
            rules[model] = (tenant_field, specs)
    extra = draw(st.none() | _q_tree(parent, max_leaves=3))
    caller = dict(
        user=draw(st.sampled_from([1, 2, 3])),
        roles=draw(st.sampled_from([[], ["boss"]])),
        tenant=draw(st.sampled_from([1, 2, None])),
    )
    return parent, terms, rules, extra, caller


def _scope_without_conditions(model, request):
    """KNOWN BUG (see TestFuzzCounterexamples): ``model``'s scope is a truthy
    ``Q`` with no condition in it (``Q(Q())``, ``~Q(Q())``...), which matches
    every row of the target's own endpoint. Moved onto a nullable relation it
    lands in ``Q(<prefix>__isnull=True) | scope``; Django drops the empty child
    from the OR, which leaves ``<prefix> IS NULL`` alone."""
    scope = registry.tenant_q(
        registry.get_tenant_field(model), request
    ) & registry.predicates_q(registry.get_predicates(model), request)
    return bool(scope) and _leaves(scope) == []


def check_traversal(parent, terms, rules, extra, caller, skip_known_bugs=True):
    """Each term's lookup ANDed with its path's traversal scope, ORed (as the
    search backend builds it; one term is the filter backend's shape), in one
    ``filter()`` call: the same rows with the scope on the joined row as with
    ``<prefix>__in=<scoped queryset>``."""
    user = User.objects.get(pk=caller["user"])
    user._test_roles = caller["roles"]
    request = SimpleNamespace(user=user)
    saved = (
        dict(registry._model_predicates),
        dict(registry._model_tenant_fields),
        dict(_test_user_brokerages),
    )
    try:
        for model in SCHEMA:
            registry._model_predicates.pop(model, None)
            registry._model_tenant_fields.pop(model, None)
        for model, (tenant_field, specs) in rules.items():
            registry.register_predicates(model, [_predicate(s) for s in specs])
            registry.register_tenant_field(model, tenant_field)
        _test_user_brokerages[user.pk] = caller["tenant"]
        if skip_known_bugs and any(
            _scope_without_conditions(m, request) for m in rules
        ):
            assume(False)

        def condition():
            q = Q()
            for path, leaf in terms:
                q |= Q(leaf) & build_traversal_scope_q(parent, path, request)
            return q if extra is None else q & extra

        with (
            mock.patch("turbodrf.validation._scope_on_joined_row", return_value=None),
            _split_exclude_alias_clashes() as clashes,
        ):
            # scoped_target_queryset filters as it builds: Django may refuse
            # the input here already.
            ref = _reference(lambda: parent._base_manager.filter(condition()))
            reference_q = condition() if ref is not None else None
        if ref is None:
            assume(False)
        moved = []
        joined_row_scope = validation._scope_on_joined_row

        def recording(target, request, prefix):
            step_q = joined_row_scope(target, request, prefix)
            moved.append(bool(step_q))
            return step_q

        with (
            mock.patch("turbodrf.validation._scope_on_joined_row", recording),
            _split_exclude_alias_clashes() as clashes_too,
        ):
            optimised_q = condition()
            filtered = parent._base_manager.filter(optimised_q)
            got = _pks(filtered)
        _record(event, f"traversal: scope moved onto the joined row: {any(moved)}")
        _record(event, f"traversal: {_coverage(ref, parent._base_manager.all())}")
    finally:
        for live, copy in zip(
            (
                registry._model_predicates,
                registry._model_tenant_fields,
                _test_user_brokerages,
            ),
            saved,
        ):
            live.clear()
            live.update(copy)
    if Counter(got) != Counter(ref):
        _skip_if_django_clashed(clashes, clashes_too)
        raise AssertionError(
            f"build_traversal_scope_q's joined-row scope differs from the __in "
            f"subquery on {parent.__name__}\n"
            f"optimised q = {_q_text(optimised_q)}\n"
            f"reference q = {_q_text(reference_q)}\n"
            f"{_verdict(ref, got)}\noptimised SQL: {_sql(filtered)}"
        )


class TestTraversalScopeRewrite(TestCase):
    @FUZZ
    @given(data=datasets(), case=traversal_cases())
    def test_joined_row_scope_matches_the_in_subquery(self, data, case):
        _load(data)
        check_traversal(*case)


class TestFuzzCounterexamples(DjangoTestCase):
    """Counterexamples the fuzz found on 0.8.4, minimised. A bug's input is
    skipped by the properties above until it's fixed: then drop its xfail and
    its skip."""

    EMPTY = dict.fromkeys(
        ["currencies", "leaves", "targets", "tags", "rows"]
        + ["code_rows", "uuid_rows", "bases", "boards"],
        [],
    )

    def test_the_rewrite_avoids_djangos_split_exclude_alias_clash(self):
        """Leaf 1's partner target has no rows (so is_deleted IS NULL through
        the LEFT JOINs) and no target points at leaf 1 (so the negated AND
        holds): leaf 1 matches. Django's own filter(q), the reference shape,
        correlates the paritytarget subquery to the partner_of join and
        returns nothing (Django 5.2 and 6.0; 4.2 builds this one right); the
        rewrite returns leaf 1."""
        _load(
            dict(
                self.EMPTY,
                leaves=[dict(id=1, name="a")],
                targets=[dict(id=4, name="a", partner_id=1)],
            )
        )
        q = Q(partner_of__rows__tags__is_deleted=None) & ~Q(
            name__isnull=False, paritytarget__isnull=False
        )
        scoped = Authorization(None, q).scope(ParityLeaf.objects.all())
        self.assertEqual(_pks(scoped), [1])

    def test_negated_lookups_sharing_a_relation_avoid_the_alias_clash(self):
        """The rule hides a leaf with a target named "hidden" (amount 5).
        Left to Django, the two negated lookups through paritytarget went to
        split_exclude, which correlated them to partner_of's earlier JOIN of
        ParityTarget, and leaf 1 was returned (0.8.4). Each negated lookup is
        now its own subquery, as Django means them."""
        _load(
            dict(
                self.EMPTY,
                leaves=[dict(id=1, name="x")],
                targets=[
                    dict(id=1, name="a", partner_id=1),
                    dict(id=2, name="hidden", amount=5, leaf_id=1),
                ],
            )
        )
        q = Q(partner_of__name="a") & ~Q(
            paritytarget__name="hidden", paritytarget__amount=5
        )
        scoped = Authorization(None, q).scope(ParityLeaf.objects.all())
        self.assertEqual(_pks(scoped), [])

    def test_a_scope_without_conditions_on_a_nullable_fk(self):
        """A Custom rule returning Q(Q()) shows every ParityTarget on its own
        endpoint; ?target__name=a on ParityAllTypes found no row (0.8.4: the
        empty scope, moved into `target__isnull | scope`, was dropped)."""
        _load(
            dict(
                self.EMPTY,
                targets=[dict(id=1, name="a")],
                rows=[dict(id=1, char="a", target_id=1, tags=[])],
            )
        )
        check_traversal(
            ParityAllTypes,
            [("target__name", ("target__name", "a"))],
            {ParityTarget: (None, [("Custom", Q(Q()))])},
            None,
            dict(user=1, roles=[], tenant=None),
            skip_known_bugs=False,
        )
