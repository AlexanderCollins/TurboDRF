"""Property-based fuzz of the WRITE path: a write can't reach what its caller
can't see.

Caller C is in tenant A (or anonymous, or has no tenant, or is platform
staff). Hypothesis builds a random write W (POST, PUT, PATCH, DELETE, or a
custom action with a write method) against any endpoint of the tenant router
(tests/urls.py) or the platform router (tests/platform_urls.py), sends it as
C and checks:

1. Hidden data is untouched. Every table of the test app and every M2M link
   is snapshotted before and after W. A change is allowed only where W may
   make it: the row it creates (with its MTI parent and its links), the
   columns of its URL row the caller can write (worked out here from
   ``TURBODRF_ROLES`` and the configured fields), the links of a writable M2M
   to rows the caller can see, and, for a DELETE of a row the caller can see,
   exactly what Django's own ``Collector`` cascades from it (documented). A
   non-2xx response changes nothing. A created row's columns the caller can't
   write hold their default (or the caller's tenant / the caller, where
   TurboDRF fills them in).
2. References stay in bounds. Each FK / M2M value W sets on a row points at a
   row of the caller's tenant (a raw fact: tenants are walked through the FK
   columns of the snapshot, like ``monitor.resolve_tenant_pk``) and, if the
   target has row rules, one the caller sees through its own endpoint. On the
   platform router: the value's tenant is the row's (all values, when the
   write moves the row) and its model is reachable there. On both: W creates
   no new cross-tenant FK / M2M pair anywhere in the database.
3. A write lands where C can see it: after a 2xx create or update, a fresh
   GET of the row as C is a 200 (not for a create on a model scoped by
   Members / Group / Custom, which docs/security.md says isn't checked).
4. Hidden equals missing: W is sent again, from the same starting state,
   with every hidden pk (URL and body) swapped for a nonexistent one. Status
   and body must match once each replacement is mapped back. SQL query
   counts that differ are recorded separately (``TURBODRF_NONINTERFERENCE_TIMING``)
   as possible timing oracles. Users are left out: with a property as
   ``TURBODRF_TENANT_USER_FIELD`` (as in the test app), docs/security.md
   says an error can tell another tenant's user from no user.
5. No 500s, for any body: JSON (any top-level value, NaN, huge numbers),
   form, multipart, text/plain and broken JSON; wrong types, nulls, lists,
   nested dicts, NUL characters, unknown and dangerous keys.

Also: no text value of a row C can't see, or of a column C can't read, shows
up in W's response (every text value in the base data carries a ``cq<n>x``
token).

"Can see" is an oracle taken once on the base data: each scoped row's detail
GET as the caller (models with no row rules: every row; users and the tenant
model: the caller's tenant; a plain model on a tenant path: its tenant).

The base data is generated from the models and their configs: rows in
tenants A and B for every test-app model, one row per user of the tenant for
each Owner-scoped model (so other users' rows are predicate-hidden), and M2M
links to rows of the other tenant (links the caller can't see, which an M2M
write must keep), and one row per tenant whose nullable FK points at the
other tenant's row. For the duration of the test, models without predicates
get the ones the test app has no model for (``_extra_rules``: a Conditional,
an Owner over a path, an Either of one with a Custom). Roles are generated
per model: nf_read, nf_create, nf_update, nf_full, nf_blind (the full role's
write grants without one read grant) and the bypass roles manager / admin,
with two generated field rules per model (a ``secret`` column only the full
roles read and write, a ``locked`` one only they write).

CI runs ``TURBODRF_NONINTERFERENCE_EXAMPLES`` (30) examples. To fuzz harder:

    TURBODRF_NONINTERFERENCE_EXAMPLES=2000 pytest -n 0 -p no:cacheprovider \\
        tests/conformance/test_noninterference_writes.py

``TURBODRF_NONINTERFERENCE_COLLECT=<file>`` appends each violating example to
<file> (JSON lines) instead of failing, so a campaign finds every bug, not
the first; ``TURBODRF_NONINTERFERENCE_TIMING=<file>`` records the query-count
differences and the documented user-FK differences.
"""

import json
import os
import re
import traceback
import uuid
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from functools import lru_cache
from unittest import mock
from urllib.parse import urlencode

import pytest

pytest.importorskip("hypothesis", reason="hypothesis not installed (dev/test extra)")

from django.apps import apps  # noqa: E402
from django.conf import settings as django_settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.core.exceptions import FieldDoesNotExist  # noqa: E402
from django.db import connection, models, transaction  # noqa: E402
from django.db.models import Q, QuerySet  # noqa: E402
from django.db.models.deletion import Collector  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart  # noqa
from django.test.utils import CaptureQueriesContext  # noqa: E402
from hypothesis import HealthCheck, event, given, note, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.errors import InvalidArgument  # noqa: E402
from hypothesis.extra.django import TestCase  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

import tests.platform_urls as platform_urls  # noqa: E402  (builds its routers now)
import tests.urls as tenant_urls  # noqa: E402
from tests.test_app.apps import _test_user_brokerages  # noqa: E402
from tests.test_app.models import BankAccount, Brokerage, Portfolio, Transaction  # noqa
from turbodrf import predicates as registry  # noqa: E402
from turbodrf.mixins import is_turbodrf_model, turbodrf_config  # noqa: E402
from turbodrf.predicates import (  # noqa: E402
    Conditional,
    Custom,
    Either,
    Group,
    Members,
    Owner,
    validate_permission_strings,
)

User = get_user_model()

settings.register_profile(
    "noninterference-writes",
    max_examples=int(os.environ.get("TURBODRF_NONINTERFERENCE_EXAMPLES", "30")),
    deadline=None,
    database=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.data_too_large,
        HealthCheck.large_base_example,
    ],
)
FUZZ = settings.get_profile("noninterference-writes")
COLLECT = os.environ.get("TURBODRF_NONINTERFERENCE_COLLECT")
TIMING = os.environ.get("TURBODRF_NONINTERFERENCE_TIMING")

TENANT, PLATFORM = "tenant", "platform"
ROUTERS = {
    TENANT: ("/api/", tenant_urls.router),
    PLATFORM: ("/console-api/", platform_urls.platform_router),
}
FIXED_NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt_timezone.utc)
# Bounded, so a random CSRF token on an HTML page doesn't match.
TOKEN_RE = re.compile(r"(?<![a-z0-9])cq\d+x(?![a-z0-9])")
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def _record(record, text):
    """Hypothesis ``note()`` / ``event()``; a no-op outside a Hypothesis test
    (the plain regression tests run the same checks)."""
    try:
        record(text)
    except InvalidArgument:
        pass


# ---------------------------------------------------------------------------
# The schema, from the app registry and the routers
# ---------------------------------------------------------------------------


def _app_models():
    # Only the app's own (migrated) models: a test module can declare a
    # model under test_app with no table (tests/unit/test_lookup_field.py).
    return [
        m
        for m in apps.get_app_config("test_app").get_models()
        if not m._meta.proxy
        and not m._meta.auto_created
        and m.__module__ == "tests.test_app.models"
    ]


def _throughs():
    """``{through: (host, m2m field, host column, target column)}`` for every
    auto-created M2M table of the test app."""
    out = {}
    for model in _app_models():
        for field in model._meta.local_many_to_many:
            through = field.remote_field.through
            if through._meta.auto_created:
                meta = through._meta
                out[through] = (
                    model,
                    field,
                    meta.get_field(field.m2m_field_name()).attname,
                    meta.get_field(field.m2m_reverse_field_name()).attname,
                )
    return out


THROUGHS = _throughs()
TABLES = [User, *_app_models()]  # snapshotted, with THROUGHS


def _fk_fields(model):
    """Forward FK / one-to-one columns, MTI parent links left out."""
    return [
        f
        for f in model._meta.concrete_fields
        if f.is_relation and not getattr(f.remote_field, "parent_link", False)
    ]


FK_FIELDS = {model: _fk_fields(model) for model in TABLES}

# Every model's registered tenant_field (plain hops of a chained path
# included), as the router registered it from the configs.
TENANT_PATHS = dict(registry._model_tenant_fields)


def _walk(predicates):
    for p in predicates:
        yield p
        if isinstance(p, Either):
            yield from _walk(p.predicates)


def _predicates(model):
    return list(registry._model_predicates.get(model, []))


def _owner_columns(model):
    """User columns an Owner predicate (anywhere in the stack) names."""
    cols = []
    for p in _walk(_predicates(model)):
        if isinstance(p, Owner):
            cols += [f for f in p.fields if "__" not in f and f not in cols]
    return cols


def _single_owner_columns(model):
    return {
        p.fields[0]
        for p in _walk(_predicates(model))
        if isinstance(p, Owner) and len(p.fields) == 1 and "__" not in p.fields[0]
    }


def _has_rules(model):
    return model in TENANT_PATHS or bool(_predicates(model))


def _create_unchecked(model):
    """docs/security.md: Members and Group don't check creates, and a Custom's
    write rule is its own, so a create may leave a row its writer can't see.
    Not inside an Either with an Owner, which a create must satisfy."""
    return any(
        isinstance(p, (Members, Group, Custom))
        or (
            isinstance(p, Either)
            and not any(isinstance(c, Owner) for c in p.predicates)
        )
        for p in _predicates(model)
    )


def _platform_reachable(model):
    if model is Brokerage or not is_turbodrf_model(model):
        return True
    return isinstance((turbodrf_config(model) or {}).get("platform"), dict)


def _config(model, router):
    config = turbodrf_config(model) or {}
    if router == PLATFORM:
        config = config.get("platform") or {}
    return config


def _configured_fields(model, router):
    fields = _config(model, router).get("fields", "__all__")
    if isinstance(fields, dict):
        listed = fields.get("list", fields.get("detail", []))
        fields = fields.get("detail", listed)
    if fields == "__all__":
        fields = [f.name for f in model._meta.fields] + [
            f.name for f in model._meta.many_to_many
        ]
    return list(fields)


def _forward_field(model, name):
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        return None
    if field.concrete or isinstance(field, models.ManyToManyField):
        return field
    return None


def _configured_bases(model, router):
    """The model's own fields its configured fields are or start from (a
    nested ``a__b`` adds ``a``, as the serializer does)."""
    bases = []
    for name in _configured_fields(model, router):
        base = name.split("__")[0]
        if _forward_field(model, base) is not None and base not in bases:
            bases.append(base)
    return bases


def _endpoints(router):
    return [
        Endpoint(router, prefix, viewset.model, viewset)
        for prefix, viewset, _basename in ROUTERS[router][1].registry
    ]


@dataclass(frozen=True)
class Endpoint:
    router: str
    prefix: str
    model: type
    viewset: type

    def __repr__(self):
        return f"<{self.router} /{self.prefix}/ ({self.model.__name__})>"


def _write_ops(endpoint):
    """``(method, kind, detail, url_path)``: the CRUD writes, then every
    custom action with a write method."""
    ops = [
        ("POST", "create", False, ""),
        ("PATCH", "update", True, ""),
        ("PUT", "update", True, ""),
        ("DELETE", "delete", True, ""),
    ]
    for action in endpoint.viewset.get_extra_actions():
        for method in action.mapping:
            if method in ("post", "put", "patch", "delete"):
                ops.append(
                    (method.upper(), action.__name__, action.detail, action.url_path)
                )
    return ops


# ---------------------------------------------------------------------------
# Roles, generated per model
# ---------------------------------------------------------------------------

TENANT_MODELS = [e.model for e in _endpoints(TENANT)]
BASE_ROLES = {
    name: list(perms) for name, perms in django_settings.TURBODRF_ROLES.items()
}
PLATFORM_ROLES = django_settings.TURBODRF_PLATFORM_ROLES
FULL = ("read", "create", "update", "delete")
LEVELS = {
    "nf_read": ("read",),
    "nf_create": ("read", "create"),
    "nf_update": ("read", "update"),
    "nf_full": FULL,
    "manager": FULL,  # the bypass roles (Owner bypass, the Conditional below)
    "admin": FULL,
    "nf_blind": FULL,  # the full role's write grants, without the secret's read
}


def _label(model):
    return f"{model._meta.app_label}.{model._meta.model_name}"


def _field_rules(roles_map, model):
    """``(read rules, write rules)``: the fields of ``model`` any role has a
    field rule for (plus ``sensitive_fields``, read only by grant)."""
    label = _label(model)
    read, write = set(), set()
    for perms in roles_map.values():
        for perm in perms:
            parts = perm.split(".")
            if len(parts) == 4 and f"{parts[0]}.{parts[1]}" == label:
                {"read": read, "write": write}.get(parts[3], set()).add(parts[2])
    declared = getattr(model, "sensitive_fields", None) or ()
    read |= {declared} if isinstance(declared, str) else set(declared)
    return read, write


def _restricted_columns(model):
    """``(secret, locked)``: optional configured columns of ``model`` to put
    field rules on. ``secret``: only the full roles read and write it;
    ``locked``: only they write it."""
    tenant_hop = (TENANT_PATHS.get(model) or "").split("__")[0]
    owners = set(_owner_columns(model))
    plain, relations = [], []
    for name in sorted(_configured_bases(model, TENANT)):
        f = model._meta.get_field(name)
        if f.primary_key or not f.editable or name in owners or name == tenant_hop:
            continue
        if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False):
            continue
        if not (f.many_to_many or f.null or f.blank or f.has_default()):
            continue
        (relations if f.is_relation else plain).append(name)
    secret = plain[0] if plain else None
    locked = relations[0] if relations else (plain[1] if len(plain) > 1 else None)
    return secret, locked


RESTRICTED = {model: _restricted_columns(model) for model in TENANT_MODELS}


def _build_roles():
    roles = dict(BASE_ROLES)
    rules = {model: _field_rules(BASE_ROLES, model) for model in TENANT_MODELS}
    for level, actions in LEVELS.items():
        perms = []
        full = actions == FULL
        for model in TENANT_MODELS:
            label = _label(model)
            perms += [f"{label}.{a}" for a in actions]
            read, write = rules[model]
            secret, locked = RESTRICTED[model]
            read = set(read) - set(getattr(model, "sensitive_fields", None) or ())
            for name in sorted(read | ({secret} - {None})):
                if (full and level != "nf_blind") or name != secret:
                    perms.append(f"{label}.{name}.read")
            if {"create", "update"} & set(actions):
                for name in sorted(write | ({secret, locked} - {None})):
                    if full or name not in (secret, locked):
                        perms.append(f"{label}.{name}.write")
        roles[level] = perms
    return roles


ROLES = _build_roles()
SETTINGS = dict(
    ROOT_URLCONF="tests.platform_urls",
    TURBODRF_TENANT_MODEL="test_app.Brokerage",
    TURBODRF_ROLES=ROLES,
)


def _roles_map(router):
    return PLATFORM_ROLES if router == PLATFORM else ROLES


@lru_cache(maxsize=None)
def _perms(router, roles):
    roles_map = _roles_map(router)
    return frozenset(p for r in roles for p in roles_map.get(r, []))


@lru_cache(maxsize=None)
def _rules(router, model):
    return _field_rules(_roles_map(router), model)


@lru_cache(maxsize=None)
def _writable(router, roles, model, action):
    """The fields a caller with ``roles`` may write on ``model`` through
    ``action`` (create / update), worked out from the role map and the
    configured fields: the action's permission, model read, and each
    field's read and write grant where a rule exists."""
    perms = _perms(router, roles)
    label = _label(model)
    if f"{label}.{action}" not in perms:
        return frozenset()
    read_rules, write_rules = _rules(router, model)
    out = set()
    for name in _configured_bases(model, router):
        f = model._meta.get_field(name)
        if not f.editable or getattr(f, "auto_now", False):
            continue
        if getattr(f, "auto_now_add", False) or (f.primary_key and action != "create"):
            continue
        if name in read_rules:
            if f"{label}.{name}.read" not in perms:
                continue
        elif f"{label}.read" not in perms:
            continue
        if name in write_rules and f"{label}.{name}.write" not in perms:
            continue
        out.add(name)
    return frozenset(out)


def _unreadable(router, roles, model, name):
    """A column with a read rule the caller isn't granted."""
    read_rules, _write = (
        _rules(router, model) if is_turbodrf_model(model) else (set(), set())
    )
    return name in read_rules and f"{_label(model)}.{name}.read" not in _perms(
        router, roles
    )


# ---------------------------------------------------------------------------
# Snapshots and raw tenant facts
# ---------------------------------------------------------------------------


def _norm(value):
    return bytes(value) if isinstance(value, memoryview) else value


def snapshot():
    """Every test-app row (``{model: {pk: {attname: value}}}``) and M2M link
    (``{through: {(host pk, target pk)}}``), read through base managers."""
    snap = {}
    for model in TABLES:
        names = [f.attname for f in model._meta.concrete_fields]
        at = names.index(model._meta.pk.attname)
        snap[model] = {
            row[at]: dict(zip(names, map(_norm, row)))
            for row in model._base_manager.values_list(*names)
        }
    for through, (_host, _field, src, tgt) in THROUGHS.items():
        snap[through] = set(through._base_manager.values_list(src, tgt))
    return snap


def _target_pk(snap, field, value):
    """The pk of the row an FK column value names (a ``to_field`` FK stores
    another column)."""
    target = field.related_model
    attname = field.target_field.attname
    if attname == target._meta.pk.attname or value is None:
        return value
    for pk, row in snap.get(target, {}).items():
        if row.get(attname) == value:
            return pk
    return None


def tenant_of(snap, model, pk):
    """The tenant pk of a row, walking its tenant path through the snapshot's
    FK columns (a raw fact, like monitor.resolve_tenant_pk): itself for a
    tenant, the user's tenant attribute for a user, None without a path."""
    if pk is None:
        return None
    if model is Brokerage:
        return pk
    if model is User:
        return WORLD.user_tenant.get(pk)
    path = TENANT_PATHS.get(model)
    if not path:
        return None
    current, current_pk = model, pk
    for part in path.split("__"):
        field = current._meta.get_field(part)
        row = snap.get(current, {}).get(current_pk)
        if row is None or row[field.attname] is None:
            return None
        current, current_pk = (
            field.related_model,
            _target_pk(snap, field, row[field.attname]),
        )
    return current_pk


def cross_pairs(snap):
    """Every FK value and M2M link between two rows with different tenants."""
    out = set()
    memo = {}

    def tenant(model, pk):
        key = (model, pk)
        if key not in memo:
            memo[key] = tenant_of(snap, model, pk)
        return memo[key]

    for model in TABLES:
        for field in FK_FIELDS[model]:
            for pk, row in snap[model].items():
                value = row[field.attname]
                if value is None:
                    continue
                target = _target_pk(snap, field, value)
                a, b = tenant(model, pk), tenant(field.related_model, target)
                if a is not None and b is not None and a != b:
                    out.add((model.__name__, pk, field.name, target))
    for through, (host, field, _src, _tgt) in THROUGHS.items():
        for link in snap[through]:
            a, b = tenant(host, link[0]), tenant(field.related_model, link[1])
            if a is not None and b is not None and a != b:
                out.add((host.__name__, link[0], field.name, link[1]))
    return out


def diff(before, after):
    """The changes from ``before`` to ``after``: ``("insert"|"delete",
    model, pk)``, ``("update", model, pk, attname, old, new)``,
    ``("link"|"unlink", through, (host pk, target pk))``."""
    changes = []
    for model in TABLES:
        b, a = before[model], after[model]
        changes += [("delete", model, pk) for pk in b.keys() - a.keys()]
        changes += [("insert", model, pk) for pk in a.keys() - b.keys()]
        for pk in b.keys() & a.keys():
            for att, old in b[pk].items():
                if a[pk][att] != old:
                    changes.append(("update", model, pk, att, old, a[pk][att]))
    for through in THROUGHS:
        changes += [
            ("unlink", through, link) for link in before[through] - after[through]
        ]
        changes += [
            ("link", through, link) for link in after[through] - before[through]
        ]
    return changes


def _fmt(change):
    kind, model = change[0], change[1]
    if model in THROUGHS:
        host, field, *_ = THROUGHS[model]
        return f"{kind} {host.__name__}.{field.name} link {change[2]}"
    if kind == "update":
        return f"update {model.__name__} {change[2]}: {change[3]} {change[4]!r} -> {change[5]!r}"
    return f"{kind} {model.__name__} {change[2]}"


# ---------------------------------------------------------------------------
# The base data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    key: str
    tenant: object  # "A", "B" or None
    roles: tuple
    platform_roles: tuple = ()
    staff: bool = False


USER_SPECS = [
    Spec("full", "A", ("nf_full",)),
    Spec("create", "A", ("nf_create",)),
    Spec("update", "A", ("nf_update",)),
    Spec("bypass", "A", ("manager", "admin")),
    Spec("read", "A", ("nf_read",)),
    Spec("blind", "A", ("nf_blind",)),
    Spec("multi", "A", ("nf_create", "nf_update")),
    Spec("otherA", "A", ("nf_full",)),
    Spec("userB", "B", ("nf_full",)),
    Spec("none", None, ("nf_full",)),
    Spec("staff_admin", "A", (), ("admin",), True),
    Spec("staff_support", None, (), ("support",), True),
]
TENANT_USERS = {
    "A": ["full", "create", "update", "bypass", "read", "blind", "multi", "otherA"],
    "B": ["userB"],
}
TENANT_CALLERS = [
    "full",
    "create",
    "update",
    "bypass",
    "read",
    "blind",
    "multi",
    "anon",
    "none",
]
PLATFORM_CALLERS = ["staff_admin", "staff_support", "full"]
CATEGORIES = ("visible", "hiddenA", "hiddenB", "unreachable", "missing")
SWAPPED = ("hiddenA", "hiddenB", "unreachable")


def _topological(models_):
    """``models_`` with every FK target (and MTI parent) first; a cycle is
    broken at a nullable FK."""
    placed = {User, Brokerage}
    order, pending = [], list(models_)

    def deps(model, required_only):
        out = set(model._meta.get_parent_list())
        for f in FK_FIELDS.get(model, _fk_fields(model)):
            if f.related_model is not model and not (required_only and f.null):
                out.add(f.related_model._meta.concrete_model)
        return out

    while pending:
        ready = [m for m in pending if deps(m, False) <= placed] or [
            m for m in pending if deps(m, True) <= placed
        ][:1]
        if not ready:
            raise RuntimeError(f"can't order {pending}")
        for model in ready:
            order.append(model)
            placed.add(model)
            pending.remove(model)
    return order


@dataclass
class View:
    """What one caller sees on one router (the oracle), and its pk pools."""

    router: str
    caller: str
    user: object
    tenant: object
    roles: tuple
    visible: dict
    pools: dict
    hidden_tokens: frozenset

    def sees(self, model, pk):
        return pk in self.visible.get(model, ())


class World:
    def __init__(self):
        self.counter = 0
        self.tenants = {}  # "A" -> Brokerage
        self.users = {}  # spec key -> User (with its test roles)
        self.user_tenant = {}  # user pk -> tenant pk or None
        self.rows = defaultdict(list)
        self.row_tenant = {}
        self.hint = {}  # (model, pk) -> "A" / "B"
        self.used = defaultdict(set)  # one-to-one targets taken
        self.tokens = {}  # token -> (model, pk, field name)
        self.views = {}
        self.disagreements = []  # read path vs declared policy

    # -- building ----------------------------------------------------------

    def build(self):
        for key in "AB":
            self.tenants[key] = self._save(Brokerage(name=self._text(None, 0)), key)
        for spec in USER_SPECS:
            user = User.objects.create_user(
                username=f"nf-{spec.key}", password="x", is_staff=spec.staff
            )
            user._test_roles = list(spec.roles)
            user._test_platform_roles = list(spec.platform_roles)
            self.users[spec.key] = user
            tenant = self.tenants.get(spec.tenant)
            self.user_tenant[user.pk] = tenant.pk if tenant else None
        self.bind()
        for model in _topological([m for m in _app_models() if m is not Brokerage]):
            for key in "AB":
                for idx, variant in enumerate(self._variants(model, key)):
                    self._make(model, key, variant, idx)
        self._link()
        self.base = snapshot()
        self.base_pairs = cross_pairs(self.base)
        self.endpoints = {r: _endpoints(r) for r in ROUTERS}
        for router, callers in ((TENANT, TENANT_CALLERS), (PLATFORM, PLATFORM_CALLERS)):
            for caller in callers:
                self.views[(router, caller)] = self._view(router, caller)

    def bind(self):
        """TURBODRF_TENANT_USER_FIELD reads this registry (tests/test_app/apps.py)."""
        for spec in USER_SPECS:
            user = self.users.get(spec.key)
            if user is not None and spec.tenant:
                _test_user_brokerages[user.pk] = self.tenants[spec.tenant]

    def _next(self):
        self.counter += 1
        return self.counter

    def _text(self, max_length, idx):
        text = f"{'vip' if idx % 2 else 'pub'}-cq{self._next()}x"
        if max_length and len(text) > max_length:
            text = str(self._next()).zfill(max_length)[-max_length:]
        return text

    def _variants(self, model, key):
        """One row per user of the tenant for each Owner-scoped model (others'
        rows are hidden by the predicate), plus an unowned one; two rows of
        any other model."""
        owners = _owner_columns(model)
        if not owners:
            return [{}, {}]
        users = [self.users[k] for k in TENANT_USERS[key]]
        out = [{owners[0]: u} for u in users]
        if len(owners) > 1:
            out.append({owners[1]: users[-1]})
            if key == "A":
                out.append(
                    {owners[0]: self.users["otherA"], owners[1]: self.users["full"]}
                )
        if all(model._meta.get_field(c).null for c in owners):
            out.append({})
        return out

    def _rows_in(self, model, key):
        if model is User:
            return [self.users[k].pk for k in TENANT_USERS[key]]
        if model is Brokerage or model in TENANT_PATHS:
            want = self.tenants[key].pk
            return [
                pk for pk in self.rows[model] if self.row_tenant[(model, pk)] == want
            ]
        return list(self.rows[model])

    def _target(self, model, field, key, idx):
        target = field.related_model
        if target is Brokerage:
            return self.tenants[key]
        if target is User:
            users = [self.users[k] for k in TENANT_USERS[key]]
            return users[idx % len(users)]
        candidates = self._rows_in(target, key)
        if field.null and idx == 1 and target in TENANT_PATHS:
            # A legacy row pointing at the other tenant's: a write must keep
            # what it doesn't change, and never render the target.
            candidates = self._rows_in(target, "B" if key == "A" else "A") or candidates
        if field.unique:
            candidates = [pk for pk in candidates if pk not in self.used[field]]
        if not candidates or (field.null and idx % 3 == 2):
            if not field.null:
                raise RuntimeError(f"no {target.__name__} row for {field}")
            return None
        pk = candidates[idx % len(candidates)]
        if field.unique:
            self.used[field].add(pk)
        return target._base_manager.get(pk=pk)

    def _value(self, f, idx):
        if f.choices:
            return f.choices[0][0]
        if isinstance(f, models.BooleanField):
            return None if f.null else False
        if isinstance(f, models.GenericIPAddressField):
            return "10.0.0.1"
        if isinstance(f, models.FileField):
            return ""
        if isinstance(f, models.BinaryField):
            return b"x"
        if isinstance(f, models.JSONField):
            return {"note": self._text(None, idx)}
        if isinstance(f, models.UUIDField):
            return uuid.UUID(int=0xA0000000 + self._next())
        if isinstance(f, models.DurationField):
            return timedelta(days=1)
        if isinstance(f, models.DateTimeField):
            return FIXED_NOW
        if isinstance(f, models.DateField):
            return date(2024, 1, 1)
        if isinstance(f, models.TimeField):
            return time(12, 0)
        if isinstance(f, models.DecimalField):
            return Decimal("1.00")
        if isinstance(f, models.FloatField):
            return 1.0
        if isinstance(f, models.IntegerField):
            return idx + 1
        if isinstance(f, models.EmailField):
            return f"cq{self._next()}x@x.io"
        if isinstance(f, models.URLField):
            return f"http://cq{self._next()}x.io"
        if isinstance(f, models.SlugField):
            return f"cq{self._next()}x"
        if isinstance(f, (models.CharField, models.TextField)):
            return self._text(f.max_length, idx)
        return None

    def _make(self, model, key, variant, idx):
        owners = set(_owner_columns(model))
        kwargs = {}
        for f in model._meta.concrete_fields:
            if f.primary_key:
                if isinstance(f, models.UUIDField):
                    kwargs[f.attname] = uuid.UUID(int=0xB0000000 + self._next())
                elif isinstance(f, models.CharField):
                    kwargs[f.attname] = f"code{self._next()}"
                continue
            if f.name in variant:
                kwargs[f.name] = variant[f.name]
            elif getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False):
                continue
            elif f.is_relation:
                if getattr(f.remote_field, "parent_link", False):
                    continue
                kwargs[f.name] = (
                    None if f.name in owners else self._target(model, f, key, idx)
                )
            else:
                kwargs[f.attname] = self._value(f, idx)
        self._save(model(**kwargs), key)

    def _save(self, obj, key):
        obj.save()
        model = type(obj)
        self.rows[model].append(obj.pk)
        self.hint[(model, obj.pk)] = key
        if model is Brokerage:
            self.row_tenant[(model, obj.pk)] = obj.pk
        elif model in TENANT_PATHS:
            current = obj
            for part in TENANT_PATHS[model].split("__"):
                current = getattr(current, part) if current is not None else None
            self.row_tenant[(model, obj.pk)] = getattr(current, "pk", None)
        for f in model._meta.concrete_fields:
            if f.is_relation:
                continue
            value = getattr(obj, f.attname)
            text = json.dumps(value) if isinstance(value, dict) else value
            if isinstance(text, str):
                for token in TOKEN_RE.findall(text):
                    self.tokens[token] = (model, obj.pk, f.name)
        return obj

    def _link(self):
        """Each host row links to rows of its own tenant and, every other
        row, one of the other tenant's (links its tenant can't see)."""
        for through, (host, field, src, tgt) in THROUGHS.items():
            target = field.related_model
            scoped = target is User or target in TENANT_PATHS
            links = []
            for i, pk in enumerate(self.rows[host]):
                key = self.hint[(host, pk)]
                same = self._rows_in(target, key)
                other = (
                    self._rows_in(target, "B" if key == "A" else "A") if scoped else []
                )
                picks = set()
                if same:
                    picks.add(same[i % len(same)])
                    if len(same) > 1 and i % 2 == 0:
                        picks.add(same[(i + 1) % len(same)])
                if other and i % 2 == 1:
                    picks.add(other[0])
                links += [through(**{src: pk, tgt: t}) for t in picks]
            through._base_manager.bulk_create(links)

    # -- the oracle ----------------------------------------------------------

    def _view(self, router, caller):
        user = self.users.get(caller)
        tenant = self.user_tenant.get(user.pk) if user is not None else None
        if user is None:
            roles = ()
        elif router == PLATFORM:
            roles = tuple(user._test_platform_roles)
        else:
            roles = tuple(user._test_roles)
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        served = {e.model: e.prefix for e in self.endpoints[router]}
        cache.clear()
        visible, pools = {}, {}
        for model in TABLES:
            pks = list(self.base[model])
            seen = self._visible(
                router, client, served, model, pks, tenant, user, roles
            )
            visible[model] = frozenset(seen)
            pool = defaultdict(list)
            for pk in pks:
                if pk in seen:
                    pool["visible"].append(pk)
                elif router == PLATFORM:
                    pool["unreachable"].append(pk)
                else:
                    row_tenant = tenant_of(self.base, model, pk)
                    hidden_b = row_tenant is not None and row_tenant != tenant
                    pool["hiddenB" if hidden_b or model is User else "hiddenA"].append(
                        pk
                    )
            pool["missing"] = [_missing(model)]
            pools[model] = dict(pool)
        hidden = frozenset(
            token
            for token, (model, pk, name) in self.tokens.items()
            if pk not in visible[model] or _unreadable(router, roles, model, name)
        )
        return View(router, caller, user, tenant, roles, visible, pools, hidden)

    def _visible(self, router, client, served, model, pks, tenant, user, roles):
        """The rows the declared policy shows the caller (``_policy``). Where
        the caller can read the model, each row's detail GET must agree;
        a disagreement is a read-path bug, reported by
        test_the_read_path_agrees_with_the_policy."""
        policy = self._policy(router, model, pks, tenant, user, roles)
        if model not in served or user is None:
            return policy
        if f"{_label(model)}.read" not in _perms(router, roles):
            return policy
        base = ROUTERS[router][0] + served[model]
        seen = set()
        for pk in pks:
            status = client.get(f"{base}/{pk}/").status_code
            if status == 200:
                seen.add(pk)
            elif status not in (401, 403, 404):
                self.disagreements.append(f"GET {base}/{pk}/ as {user}: {status}")
        if seen != policy:
            self.disagreements.append(
                f"{router} {model.__name__} as {user.username}: the policy shows "
                f"{sorted(policy, key=str)}, GET shows {sorted(seen, key=str)}"
            )
        return policy

    def _policy(self, router, model, pks, tenant, user, roles):
        """The declared policy, from raw facts and plain Django filters (not
        TurboDRF's scoping code): the tenant path walked through the
        snapshot, each predicate's documented meaning as a filter on the
        base manager."""
        if router == PLATFORM:
            return set(pks) if _platform_reachable(model) else set()
        if model is User:
            return {
                pk
                for pk in pks
                if tenant is not None and self.user_tenant[pk] == tenant
            }
        if model is Brokerage:
            return {pk for pk in pks if pk == tenant}
        if not _has_rules(model):
            return set(pks)
        keep = set(pks)
        if model in TENANT_PATHS:
            keep = {
                pk
                for pk in pks
                if tenant is not None and tenant_of(self.base, model, pk) == tenant
            }
        for predicate in _predicates(model):
            q = _policy_q(predicate, user, set(roles))
            if q is not None:
                keep &= set(model._base_manager.filter(q).values_list("pk", flat=True))
        return keep


WORLD = World()


def _policy_q(predicate, user, roles):
    """What ``predicate`` lets the caller see, as docs/tenancy.md defines it,
    or None for no restriction."""
    nothing = Q(pk__in=[])
    if isinstance(predicate, Either):
        parts = [_policy_q(p, user, roles) for p in predicate.predicates]
        if any(p is None for p in parts):
            return None
        q = nothing
        for part in parts:
            q |= part
        return q
    if isinstance(predicate, Owner):
        if roles & predicate.bypass:
            return None
        q = nothing
        for name in predicate.fields if user is not None else ():
            q |= Q(**{name: user.pk})
        return q
    if isinstance(predicate, Members):
        return Q(**{predicate.m2m_field: user.pk}) if user is not None else nothing
    if isinstance(predicate, Group):
        lookup = f"{predicate.field}__{predicate.user_via}"
        return Q(**{lookup: user.pk}) if user is not None else nothing
    if isinstance(predicate, Conditional):
        return None if roles & predicate.require_roles else ~predicate.when
    if isinstance(predicate, Custom):
        from types import SimpleNamespace

        from django.contrib.auth.models import AnonymousUser

        request = SimpleNamespace(user=user if user is not None else AnonymousUser())
        return predicate.q_func(request, roles)
    raise NotImplementedError(f"no policy for {predicate!r}")


def _missing(model):
    pk = model._meta.pk
    while pk.is_relation:  # an MTI child's pk is its parent link
        pk = pk.target_field
    if isinstance(pk, models.UUIDField):
        return uuid.UUID(int=0xFFFF0001)
    if isinstance(pk, models.CharField):
        return "zz-missing"
    return 999991


def _replacement(value, i):
    if isinstance(value, uuid.UUID):
        return uuid.UUID(int=0xBEEF0000 + i)
    if isinstance(value, str):
        return f"zz-repl-{i}"
    return 800001 + 17 * i


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PK:
    """A pk drawn from one of a caller's pools: ``cat`` says whether the
    caller sees the row (``visible``), can't (``hiddenA``: same tenant,
    hidden by a predicate; ``hiddenB``: another tenant or none;
    ``unreachable``: a model the platform scope can't reach), or it doesn't
    exist (``missing``)."""

    model: str
    cat: str
    value: object

    def raw(self):
        return str(self.value) if isinstance(self.value, uuid.UUID) else self.value


class _NoBody:
    def __repr__(self):
        return "NOBODY"


NOBODY = _NoBody()


@dataclass
class Case:
    router: str
    caller: str
    endpoint: Endpoint
    method: str
    kind: str  # create / update / delete / an action's name
    detail: bool
    url_path: str
    url_pk: object  # PK, a junk string, or None
    encoding: str
    body: object
    variant: str = ""  # a query string, or "no-slash"

    @property
    def model(self):
        return self.endpoint.model

    @property
    def view(self):
        return WORLD.views[(self.router, self.caller)]

    def url_value(self):
        return self.url_pk.value if isinstance(self.url_pk, PK) else None


JUNK = [
    None, True, False, 0, -1, 1, 2**31, 2**63, 10**30, 1.5, float("nan"),
    float("inf"), "", " ", "\x00", "a\x00b", "nf-junk", "x" * 1000, [],
    [1, "a", None], {}, {"id": 1, "pk": 1}, {"__all__": 1}, "2024-01-01",
    "null", "[1, 2]", "true",
]  # fmt: skip
# Not "1%20": Django reads "1 " as pk 1.
JUNK_PKS = ["abc", "-1", "0", "99999999999999999999", "1.5", "%00", "%2e%2e", "1e1"]
DANGEROUS_KEYS = [
    "tenant", "brokerage", "brokerage_id", "owner", "owner_id", "assigned_broker",
    "id", "pk", "author__name", "deal__brokerage", "brokerage__name",
    "folder__brokerage", "tenant_field", "roles", "is_staff", "platform",
    "__all__", "", "a\x00b", "fields",
]  # fmt: skip
ENCODINGS = ["json"] * 6 + ["form", "multipart", "json-top", "text", "badjson"]
# The browsable API renders a write's response with the endpoint's forms.
VARIANTS = [""] * 8 + [
    "?format=api",
    "no-slash",
    "?fields=id",
    "?format=json&page_size=1",
]


def _typed(f):
    """Values a client might send for a column of ``f``'s type, valid and not."""
    if f.choices:
        return [c[0] for c in f.choices] + ["bogus"]
    if isinstance(f, models.BooleanField):
        return [True, False, "true", "0", 2]
    if isinstance(f, models.GenericIPAddressField):
        return ["10.0.0.1", "300.1.1.1"]
    if isinstance(f, models.FileField):
        return ["", "f.txt"]
    if isinstance(f, models.BinaryField):
        return ["aGVsbG8=", ""]
    if isinstance(f, models.JSONField):
        return [{}, {"a": 1}, [1, 2], "s", 1, {"cq": "nf"}]
    if isinstance(f, models.UUIDField):
        return [str(uuid.UUID(int=5)), "not-a-uuid"]
    if isinstance(f, models.DurationField):
        return ["1 00:00:00", "P1D", "-1", "999999999 00:00:00"]
    if isinstance(f, models.DateTimeField):
        return ["2024-01-01T00:00:00Z", "2024-02-30T00:00:00", "9999-12-31T23:59:59Z"]
    if isinstance(f, models.DateField):
        return ["2024-01-01", "0001-01-01", "9999-12-31", "2024-13-01"]
    if isinstance(f, models.TimeField):
        return ["12:00", "23:59:59.999999", "25:00"]
    if isinstance(f, models.DecimalField):
        return ["1.50", 1, "-1", "1e5", "12345678901234567890", "NaN", "Infinity"]
    if isinstance(f, models.FloatField):
        return [1.5, -0.0, 1e308, "1e400"]
    if isinstance(f, models.IntegerField):
        return [0, 1, 7, -1, 2**31, 2**63, 10**30, "12", "1.5"]
    if isinstance(f, models.EmailField):
        return ["a@b.co", "nope"]
    if isinstance(f, models.URLField):
        return ["http://x.io", "javascript:alert(1)"]
    return ["nf-canary", "vip-nf", "pub-nf", "", " ", "x" * 300]


def _resolve_key(model, key):
    field = _forward_field(model, key)
    if field is not None:
        return field
    for f in model._meta.concrete_fields:
        if f.attname == key:
            return f
    return None


@lru_cache(maxsize=None)
def _candidate_keys(model, router):
    keys = list(_configured_fields(model, router))
    keys += [n for f in model._meta.concrete_fields for n in (f.name, f.attname)]
    keys += [f.name for f in model._meta.many_to_many] + DANGEROUS_KEYS
    return tuple(dict.fromkeys(keys))


def _valid(f):
    """Values that pass ``f``'s own validation (for bodies that get past it)."""
    if f.choices:
        return [f.choices[0][0]]
    if isinstance(f, models.BooleanField):
        return [True, False]
    if isinstance(f, models.GenericIPAddressField):
        return ["10.0.0.2"]
    if isinstance(f, (models.FileField, models.BinaryField)):
        return [None] if f.null else [""]
    if isinstance(f, models.JSONField):
        return [{}, {"a": 1}]
    if isinstance(f, models.UUIDField):
        return [str(uuid.UUID(int=0xC0000000 + i)) for i in range(3)]
    if isinstance(f, models.DurationField):
        return ["1 00:00:00"]
    if isinstance(f, models.DateTimeField):
        return ["2024-01-01T00:00:00Z"]
    if isinstance(f, models.DateField):
        return ["2024-01-01"]
    if isinstance(f, models.TimeField):
        return ["12:00"]
    if isinstance(f, models.DecimalField):
        return ["1.50"]
    if isinstance(f, models.FloatField):
        return [1.5]
    if isinstance(f, models.IntegerField):
        return [1, 7]
    if isinstance(f, models.EmailField):
        return ["a@b.co"]
    if isinstance(f, models.URLField):
        return ["http://x.io"]
    if isinstance(f, models.SlugField):
        return ["nf-slug"]
    if f.max_length and f.max_length < 7:
        return ["nf"[: f.max_length]]
    return ["nf-canary", "vip-nf", "pub-nf"]


def _pk_strategy(view, target, to_attr=None, only=None):
    pools = view.pools[target]
    cats = [c for c in CATEGORIES if pools.get(c) and (only is None or c in only)]
    if not cats:
        cats = [c for c in CATEGORIES if pools.get(c)]

    def value(pk):
        if to_attr is None or pk == _missing(target):
            return pk
        return WORLD.base[target][pk][to_attr]

    def pick(cat):
        return st.sampled_from(pools[cat]).map(
            lambda pk: PK(target.__name__, cat, value(pk))
        )

    return st.sampled_from(cats).flatmap(pick)


@lru_cache(maxsize=None)
def _value_strategy(router, caller, model, key):
    view = WORLD.views[(router, caller)]
    field = _resolve_key(model, key)
    junk = st.sampled_from(JUNK)
    if field is None:
        return junk
    if field.primary_key and not field.is_relation:
        return st.one_of(
            st.sampled_from(list(WORLD.base[model])[:6] + [_missing(model)]), junk
        )
    if field.is_relation:
        target = field.related_model
        if field.many_to_many:
            return st.one_of(st.lists(_pk_strategy(view, target), max_size=3), junk)
        attname = field.target_field.attname
        to_attr = None if attname == target._meta.pk.attname else attname
        pk = _pk_strategy(view, target, to_attr)
        return st.one_of(pk, pk, pk, junk)
    typed = st.sampled_from(_typed(field))
    return st.one_of(typed, typed, junk)


def _fk_target_attr(field):
    attname = field.target_field.attname
    return None if attname == field.related_model._meta.pk.attname else attname


@lru_cache(maxsize=None)
def _plausible_strategy(router, caller, model):
    """Every configured field with a value that passes its own validation
    (FKs mostly to rows the caller sees), some of them left out: bodies that
    get past DRF to TurboDRF's own write checks."""
    view = WORLD.views[(router, caller)]
    entries = {}
    for name in _configured_bases(model, router):
        f = model._meta.get_field(name)
        if (
            not f.editable
            or getattr(f, "auto_now", False)
            or getattr(f, "auto_now_add", False)
        ):
            continue
        if f.is_relation:
            target = f.related_model
            seen = _pk_strategy(view, target, _fk_target_attr(f), only=("visible",))
            pk = st.one_of(
                seen, seen, seen, _pk_strategy(view, target, _fk_target_attr(f))
            )
            entries[name] = st.lists(pk, max_size=2) if f.many_to_many else pk
        else:
            entries[name] = st.sampled_from(_valid(f))
    return st.one_of(
        st.fixed_dictionaries(entries), st.fixed_dictionaries({}, optional=entries)
    )


@lru_cache(maxsize=None)
def _body_strategy(router, caller, model):
    keys = _candidate_keys(model, router)

    def build(chosen):
        return st.fixed_dictionaries(
            {k: _value_strategy(router, caller, model, k) for k in chosen}
        )

    fuzzed = st.lists(st.sampled_from(keys), unique=True, max_size=6).flatmap(build)
    plausible = _plausible_strategy(router, caller, model)
    mixed = st.tuples(plausible, fuzzed).map(lambda pair: {**pair[0], **pair[1]})
    return st.one_of(plausible, plausible, mixed, mixed, fuzzed)


def _url_pk_strategy(view, model):
    seen = _pk_strategy(view, model, only=("visible",))
    return st.one_of(seen, seen, _pk_strategy(view, model), st.sampled_from(JUNK_PKS))


def draw_case(data):
    router = data.draw(
        st.sampled_from([TENANT, TENANT, TENANT, PLATFORM]), label="router"
    )
    callers = TENANT_CALLERS if router == TENANT else PLATFORM_CALLERS
    # The callers who can write, more often.
    callers = callers + callers[:4] + callers[:2]
    caller = data.draw(st.sampled_from(callers), label="caller")
    view = WORLD.views[(router, caller)]
    endpoints = WORLD.endpoints[router]
    # Models with row rules, more often: they're what a write could escape.
    endpoints = endpoints + [e for e in endpoints if _has_rules(e.model)] * 2
    endpoint = data.draw(st.sampled_from(endpoints), label="endpoint")
    method, kind, detail, url_path = data.draw(
        st.sampled_from(_write_ops(endpoint)), label="operation"
    )
    url_pk = (
        data.draw(_url_pk_strategy(view, endpoint.model), label="url pk")
        if detail
        else None
    )
    encoding = data.draw(st.sampled_from(ENCODINGS), label="encoding")
    body = _body_strategy(router, caller, endpoint.model)
    if encoding == "json-top":
        body = st.one_of(
            body.map(lambda b: [b]), st.sampled_from([[], "x", 1, None, True, 10**30])
        )
    if kind not in ("create", "update"):
        body = st.one_of(st.just(NOBODY), st.just(NOBODY), st.just(NOBODY), body)
    body = data.draw(body, label="body")
    variant = data.draw(st.sampled_from(VARIANTS), label="url variant")
    return Case(
        router,
        caller,
        endpoint,
        method,
        kind,
        detail,
        url_path,
        url_pk,
        encoding,
        body,
        variant,
    )


def _render(value, swaps):
    if isinstance(value, PK):
        return swaps.get(value, value).raw()
    if isinstance(value, dict):
        return {k: _render(v, swaps) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, swaps) for v in value]
    return value


def _flat(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def _encode(case, swaps):
    if case.body is NOBODY:
        return "", "application/json"
    body = _render(case.body, swaps)
    as_json = json.dumps(body, allow_nan=True, default=str)
    if case.encoding in ("json", "json-top"):
        return as_json, "application/json"
    if case.encoding == "text":
        return as_json, "text/plain"
    if case.encoding == "badjson":
        return as_json[:-1] or "{", "application/json"
    if not isinstance(body, dict):
        return _flat(body), (
            MULTIPART_CONTENT
            if case.encoding == "multipart"
            else "application/x-www-form-urlencoded"
        )
    form = {
        k: [_flat(x) for x in v] if isinstance(v, list) else _flat(v)
        for k, v in body.items()
    }
    if case.encoding == "multipart":
        return encode_multipart(BOUNDARY, form), MULTIPART_CONTENT
    return urlencode(form, doseq=True), "application/x-www-form-urlencoded"


def _url(case, swaps):
    url = ROUTERS[case.router][0] + case.endpoint.prefix + "/"
    if case.detail:
        pk = case.url_pk
        url += f"{swaps.get(pk, pk).raw() if isinstance(pk, PK) else pk}/"
    if case.url_path:
        url += f"{case.url_path}/"
    if case.variant == "no-slash":
        return url.rstrip("/")
    return url + case.variant


def _client(view):
    client = APIClient(raise_request_exception=False)
    if view.user is not None:
        client.force_authenticate(view.user)
    return client


def send(case, swaps=None):
    """W (or, with ``swaps``, W'): ``(response, SQL query count)``."""
    swaps = swaps or {}
    payload, content_type = _encode(case, swaps)
    client = _client(case.view)
    with CaptureQueriesContext(connection) as queries:
        response = client.generic(
            case.method, _url(case, swaps), data=payload, content_type=content_type
        )
    return response, len(queries.captured_queries)


def _text(response):
    return response.content.decode("utf-8", "replace")


def _crash(response):
    exc_info = getattr(response, "exc_info", None)
    if exc_info:
        return "".join(traceback.format_exception(*exc_info)[-3:]).strip()
    return _text(response)[:300]


# ---------------------------------------------------------------------------
# The properties
# ---------------------------------------------------------------------------


class _Rollback(Exception):
    pass


def in_savepoint(fn):
    """``fn()``'s result, with everything it wrote rolled back."""
    box = {}
    try:
        with transaction.atomic():
            box["result"] = fn()
            raise _Rollback
    except _Rollback:
        pass
    return box["result"]


def expected_delete(model, pk):
    """What deleting row ``pk`` cascades to, as Django's Collector works it
    out (docs/security.md: cascades change rows the deleter can't see)."""
    obj = model._base_manager.filter(pk=pk).first()
    if obj is None:
        return set()
    collector = Collector(using="default", origin=obj)
    collector.collect([obj])
    out = set()

    def rows(m, objs):
        if m in THROUGHS:
            _h, _f, src, tgt = THROUGHS[m]
            links = (
                objs.values_list(src, tgt)
                if isinstance(objs, QuerySet)
                else [(getattr(o, src), getattr(o, tgt)) for o in objs]
            )
            out.update(("unlink", m, tuple(link)) for link in links)
        else:
            pks = (
                objs.values_list("pk", flat=True)
                if isinstance(objs, QuerySet)
                else [o.pk for o in objs]
            )
            out.update(("delete", m, p) for p in pks)

    for m, objs in collector.data.items():
        rows(m, list(objs))
    for qs in collector.fast_deletes:
        rows(qs.model, qs)
    for (field, value), groups in collector.field_updates.items():
        for objs in groups:
            pks = (
                objs.values_list("pk", flat=True)
                if isinstance(objs, QuerySet)
                else [o.pk for o in objs]
            )
            out.update(("update", field.model, p, field.attname, value) for p in pks)
    return out


class Checker:
    """Runs one case and collects ``(property, message)`` problems."""

    def __init__(self, case):
        self.case = case
        self.view = case.view
        self.problems = []
        self.timing = None  # (W's query count, W''s) when only they differ
        self.documented = None  # the same comparison for other tenants' users

    def fail(self, prop, message):
        self.problems.append((prop, message))

    def run(self):
        cache.clear()
        WORLD.bind()
        with mock.patch("django.utils.timezone.now", return_value=FIXED_NOW):
            status, text, queries = in_savepoint(self.write)
            swaps = self.swaps()
            if swaps:
                response, queries2 = in_savepoint(lambda: send(self.case, swaps))
                _record(event, "hidden pks swapped for missing ones")
                if response.status_code >= 500:
                    self.fail(
                        "P5", f"W' answered {response.status_code}: {_crash(response)}"
                    )
                found = self.compare(status, text, queries, response, queries2, swaps)
                if found and found[0] == "body":
                    self.fail("P4", found[1])
                elif found:
                    self.timing = found[1]
            # docs/security.md: with a property as TURBODRF_TENANT_USER_FIELD
            # an error can tell another tenant's user from no user. Measured,
            # not failed.
            users = self.swaps(users=True)
            if users:
                response, queries2 = in_savepoint(lambda: send(self.case, users))
                self.documented = self.compare(
                    status, text, queries, response, queries2, users
                )
        return self.problems

    # -- W -------------------------------------------------------------------

    def write(self):
        case, before = self.case, WORLD.base
        url_pk = case.url_value()
        expected = None
        if (
            case.kind == "delete"
            and url_pk is not None
            and self.view.sees(case.model, url_pk)
        ):
            expected = expected_delete(case.model, url_pk)
        response, queries = send(case)
        status = response.status_code
        _record(event, f"{case.router} {case.method} {case.kind}: {status}")
        if status >= 500:
            self.fail("P5", f"W answered {status}: {_crash(response)}")
        after = snapshot()
        ok = 200 <= status < 300
        self.changes(before, after, ok, status, expected)
        new = after[case.model].keys() - before[case.model].keys()
        if ok and case.kind == "create":
            self.defaults(after, new)
        if ok and case.kind in ("create", "update"):
            self.references(before, after, new)
            self.landing(new)
        for pair in sorted(cross_pairs(after) - WORLD.base_pairs, key=repr):
            self.fail("P2", f"new cross-tenant reference {pair}")
        leaked = (
            set(TOKEN_RE.findall(_text(response).lower())) & self.view.hidden_tokens
        )
        for token in sorted(leaked):
            model, pk, name = WORLD.tokens[token]
            self.fail(
                "leak", f"W's response shows {model.__name__} {pk}.{name} ({token})"
            )
        return status, _text(response), queries

    def allowed(self, change, new, expected):
        case, view = self.case, self.view
        model, kind = case.model, change[0]
        url_pk = case.url_value()
        parents = model._meta.get_parent_list()
        roles = view.roles
        if case.kind == "create":
            if kind == "insert":
                return change[1] in (model, *parents) and change[2] in new
            if kind == "link":
                host, field, *_ = THROUGHS[change[1]]
                return host is model and change[2][0] in new
            return False
        if url_pk is None or not view.sees(model, url_pk):
            return False
        if case.kind == "update":
            writable = _writable(case.router, roles, model, "update")
            if (
                kind == "update"
                and change[2] == url_pk
                and change[1] in (model, *parents)
            ):
                atts = {
                    model._meta.get_field(n).attname
                    for n in writable
                    if not isinstance(model._meta.get_field(n), models.ManyToManyField)
                }
                atts |= {
                    f.attname
                    for f in change[1]._meta.concrete_fields
                    if getattr(f, "auto_now", False)
                }
                return change[3] in atts
            if kind in ("link", "unlink"):
                host, field, *_ = THROUGHS[change[1]]
                if host is model and field.name in writable and change[2][0] == url_pk:
                    # Links to rows the caller can't see are kept.
                    return kind == "link" or view.sees(
                        field.related_model, change[2][1]
                    )
            return False
        if case.kind == "delete":
            if kind == "update":
                return (
                    "update",
                    change[1],
                    change[2],
                    change[3],
                    change[5],
                ) in expected
            return change in expected
        # A custom action: the developer's code may change its own row.
        return (
            case.detail
            and kind == "update"
            and change[1] is model
            and change[2] == url_pk
        )

    def label(self, change):
        view = self.view
        if change[1] in THROUGHS:
            host, field, *_ = THROUGHS[change[1]]
            host_pk, target_pk = change[2]
            if not view.sees(host, host_pk) or not view.sees(
                field.related_model, target_pk
            ):
                return "a link the caller can't see"
            return "a link"
        if change[0] == "insert":
            return "an unexpected row"
        if not view.sees(change[1], change[2]):
            return "a row the caller can't see"
        if change[0] == "update":
            return "a column the caller can't write"
        return "a row"

    def changes(self, before, after, ok, status, expected):
        changed = diff(before, after)
        if not changed:
            return
        if not ok:
            for change in changed:
                self.fail("P1", f"a {status} response changed data: {_fmt(change)}")
            return
        case = self.case
        new = after[case.model].keys() - before[case.model].keys()
        if case.kind == "create" and len(new) > 1:
            self.fail(
                "P1", f"one create inserted {len(new)} {case.model.__name__} rows"
            )
        for change in changed:
            if not self.allowed(change, new, expected or set()):
                self.fail("P1", f"W changed {self.label(change)}: {_fmt(change)}")

    def defaults(self, after, new):
        """A created row's columns the caller can't write hold their default,
        or what TurboDRF fills in (the tenant, the owner) on the tenant scope."""
        case, view = self.case, self.view
        model = case.model
        writable = _writable(case.router, view.roles, model, "create")
        if case.router == PLATFORM:
            # docs/platform.md: nothing is filled in from the staff member, so
            # a column the body leaves out holds its default too.
            sent = {
                getattr(_resolve_key(model, key), "name", None)
                for key in (case.body if isinstance(case.body, dict) else ())
            }
            writable = writable & sent
        path = TENANT_PATHS.get(model) or ""
        tenant_column = path if path and "__" not in path else None
        owners = _single_owner_columns(model)
        for pk in new:
            row = after[model][pk]
            for f in model._meta.concrete_fields:
                if f.primary_key or f.name in writable or f.model is not model:
                    continue
                if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False):
                    continue
                if (
                    f.has_default()
                    and callable(f.default)
                    and f.default not in (dict, list)
                ):
                    continue
                default = f.get_default()
                allowed = [default.pk if isinstance(default, models.Model) else default]
                if case.router == TENANT and f.name == tenant_column:
                    allowed.append(view.tenant)
                if case.router == TENANT and f.name in owners and view.user is not None:
                    allowed.append(view.user.pk)
                if row[f.attname] not in allowed:
                    self.fail(
                        "P1",
                        f"created {model.__name__} {pk} has {f.name}={row[f.attname]!r}, "
                        f"which the caller can't write",
                    )

    def references(self, before, after, new):
        case = self.case
        model = case.model
        rows = list(new)
        if case.kind == "update" and case.url_value() in after[model]:
            rows = [case.url_value()]
        for pk in rows:
            old = before[model].get(pk)
            row = after[model][pk]
            moved = (
                case.router == PLATFORM
                and old is not None
                and tenant_of(before, model, pk) != tenant_of(after, model, pk)
            )
            for f in FK_FIELDS[model]:
                value = row[f.attname]
                if value is None or (
                    old is not None and old[f.attname] == value and not moved
                ):
                    continue
                self.target(after, model, pk, f, _target_pk(after, f, value))
            for through, (host, f, _src, _tgt) in THROUGHS.items():
                if host is not model:
                    continue
                now = {t for s, t in after[through] if s == pk}
                was = {t for s, t in before[through] if s == pk}
                for t in sorted(now if old is None or moved else now - was, key=str):
                    self.target(after, model, pk, f, t)

    def target(self, snap, model, pk, field, target_pk):
        case, view = self.case, self.view
        target = field.related_model
        where = f"{model.__name__} {pk}.{field.name} = {target.__name__} {target_pk}"
        target_tenant = tenant_of(snap, target, target_pk)
        if case.router == PLATFORM:
            if not _platform_reachable(target):
                self.fail("P2", f"{where}: a model the platform scope can't reach")
            row_tenant = tenant_of(snap, model, pk)
            if None not in (row_tenant, target_tenant) and row_tenant != target_tenant:
                self.fail(
                    "P2", f"{where}: tenant {target_tenant}, the row's is {row_tenant}"
                )
            return
        if target in (User, Brokerage):
            itself = (
                target is User and view.user is not None and target_pk == view.user.pk
            )
            shared_host = not _has_rules(model) and view.tenant is None
            if (
                not itself
                and not shared_host
                and (target_tenant is None or target_tenant != view.tenant)
            ):
                self.fail(
                    "P2",
                    f"{where}: tenant {target_tenant}, the caller's is {view.tenant}",
                )
            return
        if target_tenant is not None and target_tenant != view.tenant:
            self.fail(
                "P2", f"{where}: tenant {target_tenant}, the caller's is {view.tenant}"
            )
        if _has_rules(target) and not view.sees(target, target_pk):
            self.fail("P2", f"{where}: a row the caller can't see")

    def landing(self, new):
        case = self.case
        if case.kind == "create":
            if not new:
                self.fail("P3", "a 2xx create left no row")
                return
            if case.router == TENANT and _create_unchecked(case.model):
                return
            pks = new
        else:
            pks = [case.url_value()]
        base = ROUTERS[case.router][0] + case.endpoint.prefix
        for pk in pks:
            status = _client(self.view).get(f"{base}/{pk}/").status_code
            if status != 200:
                self.fail(
                    "P3", f"after the write, GET {base}/{pk}/ as the caller is {status}"
                )

    # -- W' ------------------------------------------------------------------

    def swaps(self, users=False):
        """``{hidden PK: a missing PK}`` for every hidden pk of the case (of a
        user, with ``users``; of anything else without)."""
        if self.case.encoding == "badjson" or "format=api" in self.case.variant:
            return {}  # a parse error names an offset; a page holds a CSRF token
        found = []

        def walk(value):
            if isinstance(value, PK):
                if value.cat in SWAPPED and (value.model == User.__name__) == users:
                    found.append(value)
            elif isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(self.case.url_pk)
        walk(self.case.body)
        unique = list(dict.fromkeys(found))
        return {
            pk: PK(pk.model, "missing", _replacement(pk.value, i))
            for i, pk in enumerate(unique)
        }

    @staticmethod
    def compare(status, text, queries, response, queries2, swaps):
        """None when W and W' answer the same (each replacement mapped back to
        the pk it replaced, and the random UUIDs of created rows masked);
        ``("body", message)`` when they don't; ``("queries", (n, n'))`` when
        only their SQL query counts differ."""
        known = {str(pk.value) for pk in swaps} | {
            str(pk.value) for pk in swaps.values()
        }
        text2 = _text(response)
        for old, new in swaps.items():
            text2 = re.sub(
                rf"(?<![\w.-]){re.escape(str(new.raw()))}(?![\w-])",
                str(old.raw()),
                text2,
            )

        def masked(match):
            value = match.group(0)
            return value if value in known or _world_uuid(value) else "<uuid>"

        a, b = UUID_RE.sub(masked, text), UUID_RE.sub(masked, text2)
        if status != response.status_code or a != b:
            mapping = {f"{k.model} {k.raw()}": v.raw() for k, v in swaps.items()}
            return (
                "body",
                f"hidden pks {mapping} answer {status} {a[:400]!r}; missing ones "
                f"answer {response.status_code} {b[:400]!r}",
            )
        if queries != queries2:
            return ("queries", (queries, queries2))
        return None


@lru_cache(maxsize=None)
def _world_uuid(text):
    try:
        value = uuid.UUID(text)
    except ValueError:
        return False
    return any(
        value in WORLD.base[m] for m in TABLES if isinstance(_missing(m), uuid.UUID)
    )


def report(case, checker):
    found = []
    if checker.timing:
        _record(event, "query counts differ: hidden vs missing")
        found.append({"kind": "queries", "detail": checker.timing})
    if checker.documented:
        _record(
            event, f"another tenant's user vs no user: {checker.documented[0]} differs"
        )
        found.append(
            {"kind": f"user {checker.documented[0]}", "detail": checker.documented[1]}
        )
    if TIMING and found:
        with open(TIMING, "a") as fh:
            for item in found:
                fh.write(json.dumps({"case": repr(case), **item}, default=str) + "\n")
    if not checker.problems:
        return
    if COLLECT:
        _record(event, "violation (collected)")
        with open(COLLECT, "a") as fh:
            fh.write(
                json.dumps({"case": repr(case), "problems": checker.problems}) + "\n"
            )
        return
    lines = [f"[{prop}] {message}" for prop, message in checker.problems]
    raise AssertionError("\n".join(lines) + f"\n{case!r}")


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def _amount_one(request, roles):
    return Q(amount=1)


def _no_write_rule(data, instance, request):
    return []


def _extra_rules():
    """Predicates the test app has no model for, put on models that have no
    predicates of their own (on the registry and on the router-built viewset,
    which holds its own copy), while the test runs:

    - Portfolio: a Conditional, ``vip-...`` names for the manager role only;
    - BankAccount: an Owner over a path, the deal's broker (manager bypasses);
    - Transaction: Either(an Owner over a path, a Custom showing amount 1),
      whose Owner a write must satisfy on its own."""
    rules = {
        Portfolio: [
            Conditional(when=Q(name__startswith="vip"), require_roles=["manager"])
        ],
        BankAccount: [Owner("deal__assigned_broker", bypass=["manager"])],
        Transaction: [
            Either(
                Owner("bank_account__deal__assigned_broker", bypass=["manager"]),
                Custom(_amount_one, write_validator=_no_write_rule),
            )
        ],
    }
    stack = ExitStack()
    stack.enter_context(mock.patch.dict(registry._model_predicates, rules))
    for endpoint in _endpoints(TENANT):
        if endpoint.model in rules:
            stack.enter_context(
                mock.patch.object(
                    endpoint.viewset, "_predicates", rules[endpoint.model]
                )
            )
    return stack


class NoninterferenceBase(TestCase):
    @classmethod
    def setUpClass(cls):
        validate_permission_strings(ROLES)
        cls._stack = _extra_rules()
        try:
            super().setUpClass()
        except BaseException:
            cls._stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            cls._stack.close()
            _test_user_brokerages.clear()

    @classmethod
    def setUpTestData(cls):
        global WORLD
        WORLD = World()
        for cached in (
            _candidate_keys,
            _value_strategy,
            _plausible_strategy,
            _body_strategy,
            _world_uuid,
        ):
            cached.cache_clear()
        WORLD.build()

    def setUp(self):
        WORLD.bind()

    def check(self, case):
        _record(note, repr(case))
        checker = Checker(case)
        checker.run()
        report(case, checker)
        return checker


@override_settings(**SETTINGS)
class TestWriteNoninterference(NoninterferenceBase):
    def test_the_read_path_agrees_with_the_policy(self):
        """The oracle is the declared policy; each scoped row's detail GET, as
        each caller who can read the model, must show the same rows."""
        self.assertEqual(WORLD.disagreements, [])

    @FUZZ
    @given(data=st.data())
    def test_writes_reach_only_what_the_caller_sees(self, data):
        self.check(draw_case(data))
