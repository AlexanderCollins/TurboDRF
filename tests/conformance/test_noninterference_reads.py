"""Noninterference fuzz of TurboDRF's read side.

THE PROPERTY. A response to caller C mustn't depend on data C may not see.
For a random read request R (a GET list, detail or custom action, or an
OPTIONS) and caller C:

1. send R as C against the base world (world 1);
2. inside a savepoint, add a HIDDEN DELTA, send R again as C (world 2), and
   roll the savepoint back;
3. both responses must be the same: status, headers and body bytes (the
   browsable API's HTML after its CSRF tokens are stripped). A delta canary
   (a unique ``zq...q`` string only the delta writes) in world 2's response
   that world 1's doesn't hold names the leak. A different SQL query count is
   recorded separately (a possible timing oracle), never failed.

WHAT C MAY SEE is recomputed here from the declared configuration and raw
database facts (``Policy``), not read off the code under test: a row's tenant
by its ``tenant_field`` path, ``owner_field`` / ``visibility`` predicates
(Owner, Members, Group, Either, Conditional) on the row's columns, a field's
readability from the role map (a field with a ``.read`` rule on any role, or
in ``sensitive_fields``, needs the caller's grant; any other field needs
model-level read; anonymous callers get model-level read on a
``public_access`` model; the default sensitive names never). On the platform
scope: the models with a ``platform`` block, the tenant model and plain
models, under ``TURBODRF_PLATFORM_ROLES``.

THE BASE WORLD (``World``) is built generically from the app registry: every
test_app model, in FK order, gets rows in tenant A and B (a tenant-scoped
model: by its ``tenant_field``; a plain model: by its FK to the tenant) or
global rows. Values repeat across tenants (``alpha07`` in A and B); owner
columns rotate over the callers, another A user and a B user (a B row owned
by an A caller too); FKs mostly stay in the row's tenant, some cross tenants,
some are NULL; M2Ms link 2-3 rows. So visible rows point at hidden ones
(another owner's ScopeFolder, a B Deal) from the start.

THE HIDDEN DELTA (``Delta``), per caller and scope, may only add data or
change data the caller can't read:

- D1: for every model with row rules (platform: every model the platform
  can't reach), new rows in tenant B and rows in A that a predicate hides
  (owned by another user), canaries in every column, linked to each other
  (a B BankAccount on a new B Deal), owned by the caller across the tenant
  boundary; and hidden rows pointing AT visible rows: one per FK and M2M
  (a B ScopeItem in an A folder, a B ScopeTagged tagged with A tags). A
  candidate the oracle finds visible is deleted again.
- D2: every column of every base row hidden from the caller becomes a
  canary, and its FKs that no row rule reads point at other rows.
- D3: every column the caller can't read on a row it CAN see becomes a
  canary (text: ``zq...q``; numbers 777xx; dates 1977; JSON; booleans flip).
- D4: every visible row's M2M gains links to hidden targets (not a
  ``Members`` field, not to users).
- D5: an FK the caller can't read on a visible row points at another row; an
  M2M it can't read gains a link.
- D6: every other user's username, names and email become canaries.

Never changed: a column or FK the caller can read on a row it can see (an
FK's raw id renders even when its target is hidden: accepted); a field a row
rule reads (``RULE_HOPS``: tenant, owner, members and Conditional paths); the
caller's own user row (the browsable API's navbar shows it); a column a
default manager filters on (docs/security.md, "Default managers aren't a
security layer"); a ``to_field`` target column; MTI parents. For a custom
action the delta leaves visible rows alone (D3, D5): the action's own code
may render any column of the row ``get_object()`` returns. After the delta
the oracle's view (visible rows, their readable columns and FK ids, the
visible targets of readable M2Ms) must be unchanged, or the harness raises
``HarnessError``.

NORMALISED: the browsable API's CSRF tokens (a fresh mask per render), and
``Content-Length`` left out of the header comparison (the body is compared
itself). The canary scan is case-insensitive (a property may upper-case).

THE REQUESTS (``cases``): every endpoint of the tenant router and of the
platform router (``tests/platform_urls.py``), list / detail / OPTIONS /
GET custom actions, with a pk drawn from visible, hidden (B and A), delta and
nonexistent pks; 0-4 filters over the configured paths, their bases, every
model field and one hop past every relation, with the lookups ``exact``,
``iexact``, ``(i)contains``, ``(i)startswith``, ``isnull``, ``gt(e)``,
``lt(e)``, ``in``, ``range``, ``year``, ``month``, ``date`` or none, with the
``_or`` suffix; values from the base world, canaries and canary prefixes,
pks, numbers, dates, booleans; ``ordering``, ``search``, ``page``,
``page_size``, ``fields``, ``format=json|api``. Callers: A users at every
level (``ni_low`` model read only, ``ni_mid`` with half the drawn field
grants, ``ni_top`` with all and every bypass role) and the app's own roles,
a user without a tenant, anonymous; on the platform, staff with each platform
role, a staff member with every rule, a non-staff user, a tenant user,
anonymous. Each example also draws field read / write rules (on the
endpoint's models mostly, or a read rule on every field its paths cross)
added to the role map. Two examples in three send one "probe" aimed at a
canary (``?<path>__icontains=zq``, ``?ordering=<path>``, ``?search=zq``...);
the rest combine random parameters.

TestReadNoninterferenceConditional reruns the fuzz with LedgerInvoice served
through ``register()`` with a Conditional rule (the test app has none).
TestNoninterferenceSweep (``TURBODRF_NONINTERFERENCE_SWEEP=1`` or ``wide``)
sends every probe on every endpoint instead.

KNOWN GAPS: no Custom predicate (no oracle for an arbitrary Q) and no Group
model in the test app (the oracle handles Group); no writes; the swagger /
OpenAPI schemas and the API root aren't requested; no database permission
mode (adding UserRole / RolePermission rows); no ``lookup_field`` model; a
visible row's readable FK is never repointed, so an oracle on an FK id the
caller may read isn't tested.

CI runs ``TURBODRF_NONINTERFERENCE_EXAMPLES`` (30) examples. To fuzz harder:

    TURBODRF_NONINTERFERENCE_EXAMPLES=2000 pytest -n 0 -p no:cacheprovider \\
        tests/conformance/test_noninterference_reads.py

``TURBODRF_NONINTERFERENCE_LOG=<path>`` appends each query-count difference
(as JSON lines) to ``<path>``.
"""

import datetime
import difflib
import json
import os
import re
import sys
import unittest
import uuid
from collections import defaultdict, namedtuple
from contextlib import contextmanager
from decimal import Decimal
from functools import lru_cache
from urllib.parse import quote, urlencode

import pytest

pytest.importorskip("hypothesis", reason="hypothesis not installed (dev/test extra)")

from django.apps import apps  # noqa: E402
from django.conf import settings as django_settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.core.exceptions import FieldDoesNotExist  # noqa: E402
from django.db import connection, models, transaction  # noqa: E402
from django.db.models import Q  # noqa: E402
from django.test import TestCase as DjangoTestCase  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from hypothesis import HealthCheck, event, given, note, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.errors import InvalidArgument  # noqa: E402
from hypothesis.extra.django import TestCase  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from tests.test_app.apps import _test_user_brokerages  # noqa: E402
from tests.test_app.models import Brokerage  # noqa: E402
from turbodrf.mixins import (  # noqa: E402
    _resolve_configured_fields,
    get_searchable_fields,
    is_turbodrf_model,
    turbodrf_config,
)
from turbodrf.predicates import Conditional, Either, Group, Members, Owner  # noqa: E402
from turbodrf.scope import PLATFORM, TENANT, use_scope  # noqa: E402
from turbodrf.settings import TURBODRF_SENSITIVE_FIELDS  # noqa: E402

User = get_user_model()
UTC = datetime.timezone.utc

settings.register_profile(
    "noninterference",
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
FUZZ = settings.get_profile("noninterference")
LOG_PATH = os.environ.get("TURBODRF_NONINTERFERENCE_LOG")
# TURBODRF_NONINTERFERENCE_TRACE=1 prints each request and its delta (-s).
TRACE = int(os.environ.get("TURBODRF_NONINTERFERENCE_TRACE") or 0)

# This module is the URLconf: the tenant router at api/ and the platform
# router at console-api/, built per test class as tests/urls.py and
# tests/platform_urls.py build them (after a class's register() calls), with
# the tenant model the platform scope needs.
URLCONF = dict(ROOT_URLCONF=__name__, TURBODRF_TENANT_MODEL="test_app.Brokerage")
urlpatterns = []

BASE_ROLES = {k: list(v) for k, v in django_settings.TURBODRF_ROLES.items()}
BASE_PLATFORM_ROLES = {
    k: list(v) for k, v in django_settings.TURBODRF_PLATFORM_ROLES.items()
}
SENSITIVE = set(TURBODRF_SENSITIVE_FIELDS) | set(
    getattr(django_settings, "TURBODRF_SENSITIVE_FIELDS", ())
)


class HarnessError(Exception):
    """The delta changed what the oracle says the caller may see: a harness
    bug, not a finding."""


# ---------------------------------------------------------------------------
# The model universe, from the app registry
# ---------------------------------------------------------------------------


def _label(model):
    return f"{model._meta.app_label}.{model._meta.model_name}"


def _forward_fks(model):
    return [f for f in model._meta.concrete_fields if f.is_relation]


def _topo(universe):
    """``universe`` with every model after the models its FKs point at."""
    order, done = [], set()

    def visit(model, path):
        if model in done or model in path:
            return
        for f in _forward_fks(model):
            if f.related_model in universe and f.related_model is not model:
                visit(f.related_model, path | {model})
        done.add(model)
        order.append(model)

    for model in universe:
        visit(model, frozenset())
    return order


# Only the app's own (migrated) models: a test module can declare a model
# under test_app with no table (tests/unit/test_lookup_field.py).
MODELS = _topo(
    [
        m
        for m in apps.get_app_config("test_app").get_models()
        if m.__module__ == "tests.test_app.models"
    ]
)
MI = {m: i for i, m in enumerate(sorted(MODELS, key=_label))}
FI = {m: {f.name: i for i, f in enumerate(m._meta.concrete_fields)} for m in MODELS}
MTI_PARENTS = {p for m in MODELS for p in m._meta.parents}
# Set by _classify() from the registry: TurboDRF models, their row rules, the
# fields those rules read, the field-rule candidates.
TDRF, RULES, RULE_HOPS, ALL_RULES = [], {}, set(), []


def _role_names(value):
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset([value])
    return frozenset(value)


def _pred(p):
    """A visibility predicate as plain data, for the oracle."""
    if isinstance(p, Owner):
        return ("owner", tuple(p.fields), frozenset(p.bypass))
    if isinstance(p, Either):
        return ("either", tuple(_pred(c) for c in p.predicates))
    if isinstance(p, Members):
        return ("members", p.m2m_field)
    if isinstance(p, Group):
        return ("group", f"{p.field}__{p.user_via}")
    if isinstance(p, Conditional):
        return ("cond", p.when, frozenset(p.require_roles))
    return ("unknown", repr(p))


def _rules(model):
    """``(tenant_field, predicates)`` as ``model``'s config declares them, or
    None for a model without row rules (plain, shared or unscoped)."""
    config = turbodrf_config(model) if is_turbodrf_model(model) else None
    if not isinstance(config, dict) or config.get("tenancy") == "shared":
        return None
    tenant_field = config.get("tenant_field")
    if config.get("visibility") is not None:
        preds = tuple(_pred(p) for p in config["visibility"])
    elif config.get("owner_field") is not None:
        owner = config["owner_field"]
        fields = (owner,) if isinstance(owner, str) else tuple(owner)
        bypass = _role_names(config.get("bypass_owner_roles"))
        preds = (("owner", fields, bypass),)
    else:
        preds = ()
    if tenant_field is None and not preds:
        return None
    return tenant_field, preds


def _walk(preds):
    for p in preds:
        yield p
        if p[0] == "either":
            yield from _walk(p[1])


def _pred_paths(model):
    """The paths the oracle reads a row's tenant and owners / members on."""
    rules = RULES.get(model)
    if rules is None:
        return []
    tenant_field, preds = rules
    paths = [tenant_field] if tenant_field else []
    for p in _walk(preds):
        if p[0] == "owner":
            paths.extend(p[1])
        elif p[0] in ("members", "group"):
            paths.append(p[1])
    return list(dict.fromkeys(paths))


def _members_fields(model):
    rules = RULES.get(model)
    if rules is None:
        return set()
    return {
        p[1].split("__")[0] for p in _walk(rules[1]) if p[0] in ("members", "group")
    }


def _unknown_rules(model):
    rules = RULES.get(model)
    return rules is not None and any(p[0] == "unknown" for p in _walk(rules[1]))


def _tenant_hop(model):
    """The FK a row's tenant runs through: the first hop of its
    ``tenant_field``, or a plain model's own FK to the tenant."""
    rules = RULES.get(model)
    if rules and rules[0]:
        return rules[0].split("__")[0]
    if not is_turbodrf_model(model):
        for f in _forward_fks(model):
            if f.related_model is Brokerage:
                return f.name
    return None


def _platform_reachable(model):
    """Rows the platform scope serves: a model with a platform block, or one
    that isn't a TurboDRF model (the tenant model included)."""
    if not is_turbodrf_model(model):
        return True
    config = turbodrf_config(model)
    return isinstance(config, dict) and isinstance(config.get("platform"), dict)


def _manager_filter_columns(model):
    """Columns ``model``'s default manager filters on (soft delete)."""
    from django.db.models.expressions import Col

    names = set()

    def walk(node):
        for child in getattr(node, "children", ()):
            walk(child)
        lhs = getattr(node, "lhs", None)
        if isinstance(lhs, Col):
            names.add(lhs.target.name)

    walk(model._default_manager.get_queryset().query.where)
    return names


def _cond_lookups(model):
    """The ``(lookup, value)`` leaves of ``model``'s Conditional rules."""
    rules = RULES.get(model)
    found = []

    def leaves(q):
        for child in q.children:
            if isinstance(child, tuple):
                found.append(child)
            else:
                leaves(child)

    for p in _walk(rules[1]) if rules else ():
        if p[0] == "cond":
            leaves(p[1])
    return found


def _cond_values(model):
    """Column values that make a row match its Conditional rules' ``when``
    (simple AND-ed leaves only), for a new row only some roles may see."""
    values = {}
    rules = RULES.get(model)
    for p in _walk(rules[1]) if rules else ():
        if p[0] != "cond" or p[1].negated or p[1].connector != "AND":
            continue
        for child in p[1].children:
            if not isinstance(child, tuple):
                continue
            lookup, value = child
            name, _, op = lookup.partition("__")
            if op in ("", "exact"):
                values[name] = value
            elif op in ("startswith", "istartswith"):
                values[name] = f"{value}zq"
            elif op in ("contains", "icontains"):
                values[name] = f"zq{value}"
            elif op == "in":
                values[name] = list(value)[0]
    return values


def _rule_hops():
    """``(model, field name)`` of every field a tenant / owner / members /
    Conditional rule reads: changing one would move rows in or out of
    view."""
    hops = set()
    for model in MODELS:
        paths = _pred_paths(model) + [
            lookup.split("__")[0] if "__" not in lookup else lookup
            for lookup, _value in _cond_lookups(model)
        ]
        for path in paths:
            current = model
            for part in path.split("__"):
                try:
                    field = current._meta.get_field(part)
                except (FieldDoesNotExist, AttributeError):
                    break
                hops.add((current, field.name))
                if field.is_relation and not field.concrete:
                    hops.add((field.related_model, field.remote_field.name))
                current = field.related_model if field.is_relation else None
                if current is None:
                    break
    return hops


def _classify():
    """The registry's view of the models: TurboDRF or not, their row rules.
    Run again after a test class's register() calls."""
    global TDRF, RULES, RULE_HOPS, ALL_RULES
    TDRF = [m for m in MODELS if is_turbodrf_model(m)]
    RULES = {m: _rules(m) for m in MODELS}
    RULE_HOPS = _rule_hops()
    ALL_RULES = sorted(
        f"{_label(m)}.{f.name}"
        for m in TDRF
        for f in list(m._meta.concrete_fields) + list(m._meta.many_to_many)
        if not f.primary_key
    )
    _modifiable.cache_clear()
    _readers.cache_clear()


TO_FIELD_TARGETS = {
    (f.related_model, f.target_field.name)
    for m in MODELS
    for f in _forward_fks(m)
    if not f.target_field.primary_key
}


@lru_cache(maxsize=None)
def _modifiable(model):
    """The columns the delta may rewrite on ``model``'s rows: not a relation
    or the pk, declared on the model itself (an MTI parent's columns are its
    parent's rows too), not filtered on by the default manager (soft delete
    would move rows in and out of the model's own endpoint: the documented
    limitation), not read by a Conditional rule, not the target of a
    ``to_field`` FK (its value is the FK's)."""
    if model in MTI_PARENTS:
        return ()
    skip = _manager_filter_columns(model)
    skip |= {lookup.split("__")[0] for lookup, _value in _cond_lookups(model)}
    return tuple(
        f
        for f in model._meta.concrete_fields
        if not f.is_relation
        and not f.primary_key
        and f.model is model
        and f.name not in skip
        and (model, f.name) not in TO_FIELD_TARGETS
    )


def _through(field):
    through = field.remote_field.through
    src = through._meta.get_field(field.m2m_field_name()).attname
    dst = through._meta.get_field(field.m2m_reverse_field_name()).attname
    return through, src, dst


def _link(field, pk, targets):
    through, src, dst = _through(field)
    through._base_manager.bulk_create(
        [through(**{src: pk, dst: t}) for t in dict.fromkeys(targets)],
        ignore_conflicts=True,
    )


# ---------------------------------------------------------------------------
# Callers
# ---------------------------------------------------------------------------

Caller = namedtuple("Caller", "username roles platform_roles tenant staff")

CALLERS = {
    # Model-level read on every TurboDRF model, no field grants.
    "low": Caller("u_low", ("ni_low",), (), "A", False),
    # Read / create / update everywhere, half the drawn field grants.
    "mid": Caller("u_mid", ("ni_mid", "underwriter"), (), "A", False),
    # Every drawn field grant and every bypass role.
    "top": Caller("u_top", ("ni_top", "manager", "admin"), (), "A", False),
    "underwriter": Caller("u_uw", ("underwriter",), (), "A", False),
    "analyst": Caller("u_ana", ("analyst",), (), "A", False),
    "auditor": Caller("u_aud", ("auditor",), (), "A", False),
    "account_admin": Caller("u_acc", ("account_admin",), (), "A", False),
    "viewer": Caller("u_view", ("viewer",), (), "A", False),
    "parity_limited": Caller("u_par", ("parity_limited",), (), "A", False),
    "no_tenant": Caller("u_none", ("ni_mid",), (), None, False),
    "anonymous": Caller(None, (), (), None, False),
    "staff_admin": Caller("u_padmin", ("ni_low",), ("admin",), "A", True),
    "staff_support": Caller("u_psupport", ("ni_low",), ("support",), "A", True),
    "staff_auditor": Caller("u_pauditor", ("ni_low",), ("auditor",), "B", True),
    "staff_top": Caller("u_ptop", ("ni_low",), ("admin", "ni_ptop"), None, True),
    "not_staff": Caller("u_pnot", ("ni_low",), ("admin",), "A", False),
}
TENANT_CALLERS = [
    "low",
    "mid",
    "top",
    "underwriter",
    "analyst",
    "auditor",
    "account_admin",
    "viewer",
    "parity_limited",
    "no_tenant",
    "anonymous",
]
PLATFORM_CALLERS = [
    "staff_admin",
    "staff_support",
    "staff_auditor",
    "staff_top",
    "not_staff",
    "low",
    "anonymous",
]
# Users that own rows but never call: another A user and a B user.
EXTRA_USERS = {"u_other": "A", "u_b": "B"}
USERNAMES = sorted(
    {c.username for c in CALLERS.values() if c.username} | set(EXTRA_USERS)
)
USER_TENANT = {
    **{c.username: c.tenant for c in CALLERS.values() if c.username},
    **EXTRA_USERS,
}
A_OWNERS = ["u_low", "u_mid", "u_uw", "u_top", "u_other", "u_ana", "u_acc", "u_aud"]
B_OWNERS = ["u_b", "u_low"]  # u_low: an A caller owning a B row
GLOBAL_OWNERS = ["u_low", "u_mid", "u_other", "u_b", "u_top", "u_uw"]

# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

SKIP = object()
WORDS = ("alpha", "beta", "gamma", "delta")
DELTA_ORDINAL = 500


def _fit(text, field):
    max_length = getattr(field, "max_length", None)
    return text[:max_length] if max_length else text


def _pk_value(model, field, ordinal):
    """Explicit pks: the same on every run, and a delta row's pk is known
    before the delta exists (the request strategy draws them)."""
    if isinstance(field, models.UUIDField):
        return uuid.UUID(int=(MI[model] << 32) + ordinal + 1)
    if isinstance(field, models.CharField):
        return f"c{ordinal}"
    if model._meta.parents:
        return 100 + ordinal  # an MTI child shares its parent's pks
    return ordinal + 1 if ordinal < DELTA_ORDINAL else 200 + ordinal


def _pk_field(model):
    return next(
        f for f in model._meta.concrete_fields if f.primary_key and not f.is_relation
    )


def _base_value(model, field, ordinal):
    """A base-world value: repeated across rows and tenants (``alpha07`` in A
    and in B), so filters match visible and hidden rows alike."""
    word = WORDS[ordinal % len(WORDS)]
    if field.choices:
        return list(field.choices)[ordinal % len(field.choices)][0]
    if isinstance(field, models.BooleanField):
        return ordinal % 2 == 0
    if isinstance(field, models.EmailField):
        return f"{word}@example.org"
    if isinstance(field, models.URLField):
        return f"http://example.org/{word}"
    if isinstance(field, models.GenericIPAddressField):
        return f"10.0.0.{ordinal % 4 + 1}"
    if isinstance(field, models.FileField):
        return ""
    if isinstance(field, (models.CharField, models.TextField)):
        if field.unique:
            return _fit(f"{word[0]}{ordinal}", field)
        return _fit(f"{word}{MI[model]:02d}", field)
    if isinstance(field, models.UUIDField):
        return uuid.UUID(int=ordinal % 4 + 1)
    if isinstance(field, models.DecimalField):
        return Decimal(ordinal % 4 * 10 + 1)
    if isinstance(field, models.FloatField):
        return ordinal % 4 + 0.5
    if isinstance(field, models.DurationField):
        return datetime.timedelta(hours=ordinal % 4 + 1)
    if isinstance(field, models.DateTimeField):
        return datetime.datetime(2024, 1, 1 + ordinal % 4, 12, tzinfo=UTC)
    if isinstance(field, models.DateField):
        return datetime.date(2024, 1, 1 + ordinal % 4)
    if isinstance(field, models.TimeField):
        return datetime.time(9 + ordinal % 4, 30)
    if isinstance(field, models.IntegerField):
        return ordinal % 4 * 10 + 1
    if isinstance(field, models.JSONField):
        return {"k": word}
    if isinstance(field, models.BinaryField):
        return word.encode()
    return SKIP


def canary_text(model, field, ordinal):
    return f"zq{MI[model]:02d}{FI[model][field.name]:02d}{ordinal}q"


def canary_value(model, field, ordinal, current=None):
    """``(value, scannable text or None)`` for a delta write, or
    ``(SKIP, None)`` for a column it can't hold."""
    text = canary_text(model, field, ordinal)
    if isinstance(field, models.BooleanField):
        return (True if current is None else not current), None
    if isinstance(field, models.GenericIPAddressField):
        return f"7.7.{ordinal // 200}.{ordinal % 200 + 1}", None
    if isinstance(field, models.JSONField):
        return {"zq": text}, text
    if isinstance(field, models.BinaryField):
        return text.encode(), None
    if isinstance(field, models.UUIDField):
        return uuid.UUID(int=(0x7777 << 96) + (MI[model] << 16) + ordinal), None
    if isinstance(field, models.DecimalField):
        digits = max(field.max_digits - field.decimal_places, 1)
        if digits >= 5:
            return Decimal(77700 + ordinal), None
        return Decimal(7 * 10 ** (digits - 1) + ordinal % 10 ** (digits - 1)), None
    if isinstance(field, models.FloatField):
        return 777.25 + ordinal, None
    if isinstance(field, models.DurationField):
        return datetime.timedelta(days=77, seconds=ordinal), None
    if isinstance(field, models.DateTimeField):
        moment = datetime.datetime(1977, 7, 7, 7, 7, tzinfo=UTC)
        return moment + datetime.timedelta(minutes=ordinal), None
    if isinstance(field, models.DateField):
        return datetime.date(1977, 7, 7) + datetime.timedelta(days=ordinal), None
    if isinstance(field, models.TimeField):
        return datetime.time(7, 7, ordinal % 60), None
    if isinstance(field, models.IntegerField):
        return 77700 + ordinal, None
    if isinstance(field, models.EmailField):
        value = f"{text}@zq.org"
    elif isinstance(field, models.URLField):
        value = f"http://zq.org/{text}"
    elif isinstance(field, models.FileField):
        value = f"zq/{text}.txt"
    elif isinstance(field, (models.CharField, models.TextField)):
        value = text
    else:
        return SKIP, None
    if getattr(field, "max_length", None) and len(value) > field.max_length:
        return SKIP, None
    return value, text


def _make(model, ordinal, pick_fk, value_for, store):
    """Insert one ``model`` row: FK values from ``pick_fk(field)``, the other
    columns from ``value_for(field)``."""
    kwargs = {}
    for f in model._meta.concrete_fields:
        if f.is_relation:
            if f.remote_field.parent_link:
                continue  # an MTI child's parent row is saved with it
            kwargs[f.attname] = pick_fk(f)
        elif f.primary_key:
            kwargs[f.attname] = _pk_value(model, f, ordinal)
        else:
            value = value_for(f)
            if value is not SKIP:
                kwargs[f.attname] = value
    obj = model(**kwargs)
    obj.save(force_insert=True)
    store[(model, obj.pk)] = kwargs
    return obj.pk


# ---------------------------------------------------------------------------
# The base world
# ---------------------------------------------------------------------------


class World:
    def __init__(self):
        self.users = {}  # username -> pk
        self.tenants = {}  # "A" / "B" -> pk
        self.brokerages = {}  # "A" / "B" -> instance
        self.rows = defaultdict(list)  # model -> [pk], creation order
        self.tenant = {}  # (model, pk) -> "A" / "B" / None (global)
        self.ordinal = {}  # (model, pk) -> ordinal
        self.values = {}  # (model, pk) -> the columns it was created with
        self.counter = defaultdict(int)
        self.o2o_used = defaultdict(set)
        self.snapshot = None

    def build(self):
        for name in ("A", "B"):
            brokerage = Brokerage.objects.create(
                pk=len(self.tenants) + 1, name=f"{name}-firm"
            )
            self.tenants[name] = brokerage.pk
            self.brokerages[name] = brokerage
            self.rows[Brokerage].append(brokerage.pk)
            self.tenant[(Brokerage, brokerage.pk)] = name
            self.ordinal[(Brokerage, brokerage.pk)] = len(self.tenants) - 1
        for i, username in enumerate(USERNAMES):
            User.objects.create(pk=i + 1, username=username)
            self.users[username] = i + 1
        for model in MODELS:
            if model is Brokerage:
                continue
            for tenant, count in self._plan(model):
                for _ in range(count):
                    self._add(model, tenant)
        self.snapshot = Snapshot()
        return self

    @staticmethod
    def _plan(model):
        if _tenant_hop(model):
            if is_turbodrf_model(model):
                return [("A", 7), ("B", 3)]
            return [("A", 2), ("B", 2)]
        if RULES.get(model):
            return [(None, 7)]  # predicate-only (RbacBoard)
        return [(None, 4)]

    def _add(self, model, tenant):
        o = self.counter[model]
        self.counter[model] += 1
        pk = _make(
            model,
            o,
            lambda f: self._base_fk(model, f, tenant, o),
            lambda f: _base_value(model, f, o),
            self.values,
        )
        self.rows[model].append(pk)
        self.tenant[(model, pk)] = tenant
        self.ordinal[(model, pk)] = o
        for f in model._meta.many_to_many:
            _link(f, pk, self._base_m2m(model, f, tenant, o))
        return pk

    def fk_value(self, field, target, pk):
        if field.target_field.primary_key:
            return pk
        return self.values[(target, pk)][field.target_field.attname]

    def tenant_pk(self, name):
        return self.tenants.get(name) if name else None

    def rows_in(self, model, tenant):
        """``(same tenant or global, other tenant)`` rows of ``model``."""
        rows = self.rows[model]
        if tenant is None:
            return list(rows), []
        same = [pk for pk in rows if self.tenant[(model, pk)] in (tenant, None)]
        other = [pk for pk in rows if self.tenant[(model, pk)] not in (tenant, None)]
        return same, other

    def _base_fk(self, model, field, tenant, o):
        target = field.related_model
        salt = o + MI[model] + FI[model][field.name]
        if target is Brokerage:
            return self.tenants[tenant or "A"]
        if target is User:
            # Each A caller owns a row (o = 0..3), then another user, then
            # (o = 6) nobody on a nullable column.
            nth = [f for f in _forward_fks(model) if f.related_model is User]
            turn = o + nth.index(field)
            if field.null and o % 7 == 6:
                return None
            owners = {"A": A_OWNERS, "B": B_OWNERS, None: GLOBAL_OWNERS}[tenant]
            return self.users[owners[turn % len(owners)]]
        same, other = self.rows_in(target, tenant)
        if field.one_to_one:
            free = [pk for pk in same if pk not in self.o2o_used[field]]
            if not free or salt % 3 == 2:
                return None
            self.o2o_used[field].add(free[0])
            return self.fk_value(field, target, free[0])
        if field.name != _tenant_hop(model):
            if field.null and salt % 6 == 5:
                return None
            if other and salt % 5 == 4:
                return self.fk_value(field, target, other[salt % len(other)])
        pool = same or other
        if not pool:
            return None
        index = (salt * 3) % len(pool) if tenant is None else salt % len(pool)
        return self.fk_value(field, target, pool[index])

    def _base_m2m(self, model, field, tenant, o):
        target = field.related_model
        salt = o + MI[model]
        if target is User:
            owners = {"A": A_OWNERS, "B": B_OWNERS, None: GLOBAL_OWNERS}[tenant]
            return [
                self.users[owners[salt % len(owners)]],
                self.users[owners[(salt + 2) % len(owners)]],
            ]
        same, other = self.rows_in(target, tenant)
        picks = []
        if same:
            picks += [same[salt % len(same)], same[(salt + 1) % len(same)]]
        if other and salt % 3 == 2:
            picks.append(other[salt % len(other)])
        return picks


WORLD = None  # built by setUpTestData

# ---------------------------------------------------------------------------
# Raw database facts, and the oracle
# ---------------------------------------------------------------------------


class Snapshot:
    """Every row's columns, the values on each row-rule path, M2M links."""

    def __init__(self):
        self.rows, self.paths, self.cond, self.m2m = {}, {}, {}, {}
        for model in MODELS:
            manager = model._base_manager
            pk_att = model._meta.pk.attname
            attnames = [f.attname for f in model._meta.concrete_fields]
            self.rows[model] = {
                row[pk_att]: row for row in manager.order_by("pk").values(*attnames)
            }
            for path in _pred_paths(model):
                found = defaultdict(set)
                for pk, value in manager.values_list("pk", path):
                    if value is not None:
                        found[pk].add(value)
                self.paths[(model, path)] = found
            rules = RULES.get(model)
            for p in _walk(rules[1]) if rules else ():
                if p[0] == "cond":
                    self.cond[(model, repr(p[1]))] = set(
                        manager.filter(p[1]).values_list("pk", flat=True)
                    )
            for f in model._meta.many_to_many:
                through, src, dst = _through(f)
                links = defaultdict(list)
                for s, d in through._base_manager.order_by(src, dst).values_list(
                    src, dst
                ):
                    links[s].append(d)
                self.m2m[(model, f.name)] = links


class Policy:
    """What ``caller`` may see on ``scope`` under ``roles_map``, recomputed
    from the declared config (see the module docstring)."""

    def __init__(self, world, caller, scope, roles_map):
        self.world = world
        self.caller = caller
        self.scope = scope
        self.map = roles_map
        self.user_pk = world.users.get(caller.username)
        self.tenant_pk = world.tenant_pk(caller.tenant)
        self.roles = set(caller.platform_roles if scope == PLATFORM else caller.roles)
        self.perms = set()
        for role in self.roles:
            self.perms.update(roles_map.get(role, ()))
        self._readable = {}

    def visible(self, snap, model, pk):
        if self.scope == PLATFORM:
            return _platform_reachable(model)
        rules = RULES.get(model)
        if rules is None:
            return True
        tenant_field, preds = rules
        if tenant_field:
            if self.user_pk is None or self.tenant_pk is None:
                return False
            if self.tenant_pk not in snap.paths[(model, tenant_field)].get(pk, ()):
                return False
        return all(self._holds(snap, model, pk, p) for p in preds)

    def _holds(self, snap, model, pk, p):
        kind = p[0]
        if kind == "owner":
            if self.roles & p[2]:
                return True
            return self.user_pk is not None and any(
                self.user_pk in snap.paths[(model, f)].get(pk, ()) for f in p[1]
            )
        if kind in ("members", "group"):
            return self.user_pk is not None and self.user_pk in snap.paths[
                (model, p[1])
            ].get(pk, ())
        if kind == "either":
            return any(self._holds(snap, model, pk, c) for c in p[1])
        if kind == "cond":
            return bool(self.roles & p[2]) or pk not in snap.cond[(model, repr(p[1]))]
        raise HarnessError(f"no oracle for {model.__name__}'s predicate {p!r}")

    def readable(self, model, name):
        key = (model, name)
        if key not in self._readable:
            self._readable[key] = self._compute_readable(model, name)
        return self._readable[key]

    def _compute_readable(self, model, name):
        if name in SENSITIVE:
            return False
        label = _label(model)
        ruled = {
            perm.split(".")[2]
            for perms in self.map.values()
            for perm in perms
            if perm.count(".") == 3
            and perm.startswith(label + ".")
            and perm.endswith(".read")
        }
        declared = getattr(model, "sensitive_fields", None) or ()
        ruled |= {declared} if isinstance(declared, str) else set(declared)
        if name in ruled:
            return f"{label}.{name}.read" in self.perms
        if f"{label}.read" in self.perms:
            return True
        config = turbodrf_config(model) if is_turbodrf_model(model) else None
        return (
            self.scope == TENANT
            and self.user_pk is None
            and isinstance(config, dict)
            and bool(config.get("public_access"))
        )

    def view(self, snap):
        """The rows the caller may see, their readable columns (FK ids
        included) and the visible targets of their readable M2Ms."""
        out = {}
        for model in MODELS:
            for pk, row in snap.rows[model].items():
                if not self.visible(snap, model, pk):
                    continue
                cols = {
                    f.attname: row[f.attname]
                    for f in model._meta.concrete_fields
                    if self.readable(model, f.name)
                }
                for f in model._meta.many_to_many:
                    if not self.readable(model, f.name):
                        continue
                    target = f.related_model
                    cols[f.name] = [
                        d
                        for d in snap.m2m[(model, f.name)].get(pk, ())
                        if target is User or self.visible(snap, target, d)
                    ]
                out[(model.__name__, pk)] = cols
        return out


# ---------------------------------------------------------------------------
# The hidden delta
# ---------------------------------------------------------------------------


class Delta:
    def __init__(self, world, policy, columns=True):
        self.world = world
        self.policy = policy
        self.columns = columns  # D3
        self.base = world.snapshot
        self.canaries = {}  # text -> what wrote it
        self.next_ordinal = defaultdict(lambda: DELTA_ORDINAL)
        self.rows = defaultdict(list)  # model -> [(pk, tenant)] still alive
        self.values = {}
        self.created = []  # (model, pk), creation order
        self.counts = defaultdict(int)

    def build(self):
        for model in MODELS:
            if self.hides_rows(model):
                self._add_hidden_rows(model)
        self._drop_visible_candidates()
        self._rewrite_hidden_rows()
        if self.columns:
            self._rewrite_unreadable_columns()
            self._repoint_unreadable_relations()
        self._link_visible_rows_to_hidden_targets()
        self._rewrite_other_users()
        before = self.policy.view(self.base)
        after = self.policy.view(Snapshot())
        if before != after:
            changed = sorted(
                str(k)
                for k in set(before) | set(after)
                if before.get(k) != after.get(k)
            )
            raise HarnessError(f"the delta changed the caller's view: {changed[:10]}")
        return self

    def hides_rows(self, model):
        """Can ``model`` have rows the caller can't see?"""
        if not is_turbodrf_model(model) or _unknown_rules(model):
            return False
        if model._meta.parents:
            # A new MTI child row is a new (visible) parent row too.
            return False
        if self.policy.scope == PLATFORM:
            return not _platform_reachable(model)
        return RULES.get(model) is not None

    def _canary(self, model, field, ordinal, why, current=None):
        value, text = canary_value(model, field, ordinal, current)
        if text is not None and value is not SKIP:
            self.canaries[text] = why
        return value

    def _visible_rows(self, model):
        return [
            pk
            for pk in self.base.rows[model]
            if self.policy.visible(self.base, model, pk)
        ]

    # -- D1: new hidden rows ---------------------------------------------------

    def _add_hidden_rows(self, model):
        hop = _tenant_hop(model)
        scoped = hop is not None
        b, a = ("B", "A") if scoped else (None, None)
        specs = [(b, "b", {}), (b, "b", {}), (a, "hidden", {})]
        fixed = _cond_values(model)
        if fixed:
            # In the caller's tenant, matching a Conditional rule's ``when``.
            specs.append((a, "conditional", fixed))
        for f in _forward_fks(model):
            target = f.related_model
            if f.name == hop or f.one_to_one or target in (User, Brokerage):
                continue
            visible = self._visible_rows(target)
            if visible:
                specs.append((b, "b", {f: visible[0]}))
                specs.append((a, "hidden", {f: visible[-1]}))
        for f in model._meta.many_to_many:
            if f.related_model is not User:
                visible = self._visible_rows(f.related_model)
                if visible:
                    specs.append((b, "b", {f: visible[:2]}))
        for tenant, role, overrides in specs:
            self._create(model, tenant, role, overrides)

    def _create(self, model, tenant, role, overrides):
        o = self.next_ordinal[model]
        self.next_ordinal[model] += 1
        why = f"new hidden {model.__name__} row ({role}, tenant {tenant})"

        def pick_fk(f):
            if f in overrides:
                return self.world.fk_value(f, f.related_model, overrides[f])
            return self._delta_fk(model, f, tenant, role, o)

        def value_for(f):
            if f.name in overrides:
                return overrides[f.name]
            return self._canary(model, f, o, why)

        pk = _make(model, o, pick_fk, value_for, self.values)
        self.rows[model].append((pk, tenant))
        self.created.append((model, pk))
        for f in model._meta.many_to_many:
            targets = overrides.get(f) or self._delta_m2m(f, tenant, role)
            _link(f, pk, targets)

    def _candidates(self, target, tenant):
        new = [pk for pk, t in reversed(self.rows[target]) if t == tenant]
        same, _other = self.world.rows_in(target, tenant)
        return new + same

    def _delta_fk(self, model, field, tenant, role, o):
        target = field.related_model
        if target is Brokerage:
            return self.world.tenants[tenant or "B"]
        if target is User:
            if role == "hidden":
                return self.world.users["u_other"]
            if self.policy.user_pk is not None and o % 2:
                return self.policy.user_pk  # the caller, across the tenant boundary
            return self.world.users["u_b"]
        if field.one_to_one:
            return None
        if field.name != _tenant_hop(model) and field.null and o % 3 == 2:
            return None
        pool = self._candidates(target, tenant)
        if not pool:
            return None
        pk = pool[0] if field.name == _tenant_hop(model) else pool[o % len(pool)]
        return self.world.fk_value(field, target, pk)

    def _delta_m2m(self, field, tenant, role):
        if field.related_model is User:
            if role == "hidden":
                return [self.world.users["u_other"]]
            return [pk for pk in (self.policy.user_pk, self.world.users["u_b"]) if pk]
        return self._candidates(field.related_model, tenant)[:2]

    def _drop_visible_candidates(self):
        snap = Snapshot()
        for model, pk in reversed(self.created):
            if pk not in snap.rows[model]:
                continue  # gone with a row it pointed at
            if self.policy.visible(snap, model, pk):
                model._base_manager.filter(pk=pk).delete()
                self.counts["dropped"] += 1
        alive = Snapshot()
        for model in list(self.rows):
            self.rows[model] = [
                (pk, t) for pk, t in self.rows[model] if pk in alive.rows[model]
            ]
            self.counts["new rows"] += len(self.rows[model])

    # -- D2: hidden base rows --------------------------------------------------

    def _rewrite_hidden_rows(self):
        """Every column of a hidden row becomes a canary, and its FKs that no
        row rule reads point elsewhere (so a hidden row's FK id that leaks
        shows up as a difference too)."""
        for model in MODELS:
            if not self.hides_rows(model):
                continue
            fks = [
                f
                for f in _forward_fks(model)
                if not f.one_to_one and (model, f.name) not in RULE_HOPS
            ]
            for pk, row in self.base.rows[model].items():
                if self.policy.visible(self.base, model, pk):
                    continue
                if _modifiable(model):
                    self._rewrite(model, pk, row, _modifiable(model), "hidden")
                for f in fks:
                    self._repoint(model, pk, row, f)

    def _repoint(self, model, pk, row, f):
        """``f`` of ``model`` row ``pk`` to another row (or NULL)."""
        target = f.related_model
        if target is User:
            choices = sorted(self.world.users.values())
        else:
            choices = [pk for pk, _t in self.rows[target]] + list(
                self.world.rows[target]
            )
        choices = [self.world.fk_value(f, target, c) for c in choices]
        current = row[f.attname]
        others = [c for c in choices if c != current]
        if f.null and current is not None:
            new = None
        elif others:
            new = others[self.world.ordinal[(model, pk)] % len(others)]
        else:
            return
        model._base_manager.filter(pk=pk).update(**{f.attname: new})
        self.counts["repointed"] += 1

    # -- D3: unreadable columns of visible rows --------------------------------

    def _rewrite_unreadable_columns(self):
        for model in MODELS:
            columns = [
                f for f in _modifiable(model) if not self.policy.readable(model, f.name)
            ]
            if not columns:
                continue
            for pk, row in self.base.rows[model].items():
                if self.policy.visible(self.base, model, pk):
                    self._rewrite(model, pk, row, columns, "unreadable column of")

    def _rewrite(self, model, pk, row, columns, what):
        o = self.world.ordinal[(model, pk)]
        why = f"{what} {model.__name__} pk={pk}"
        updates = {}
        for f in columns:
            value = self._canary(model, f, o, f"{why} .{f.name}", row[f.attname])
            if value is not SKIP:
                updates[f.attname] = value
        if updates:
            model._base_manager.filter(pk=pk).update(**updates)
            self.counts["rewritten rows"] += 1

    # -- D5: unreadable FKs and M2Ms of visible rows ---------------------------

    def _repoint_unreadable_relations(self):
        """An FK the caller can't read, on a row it can see, points at another
        row; an M2M it can't read gains a link. Never a relation a row rule
        runs through (RULE_HOPS), a one-to-one or an MTI parent link."""
        for model in MODELS:
            for f in _forward_fks(model):
                if (
                    f.one_to_one
                    or (model, f.name) in RULE_HOPS
                    or self.policy.readable(model, f.name)
                ):
                    continue
                target = f.related_model
                if target is User:
                    choices = sorted(self.world.users.values())
                else:
                    choices = list(self.world.rows[target])
                choices = [self.world.fk_value(f, target, pk) for pk in choices]
                for pk, row in self.base.rows[model].items():
                    if not self.policy.visible(self.base, model, pk):
                        continue
                    current = row[f.attname]
                    others = [c for c in choices if c != current]
                    if f.null and current is not None:
                        new = None
                    elif others:
                        new = others[self.world.ordinal[(model, pk)] % len(others)]
                    else:
                        continue
                    model._base_manager.filter(pk=pk).update(**{f.attname: new})
                    self.counts["repointed"] += 1
            for f in model._meta.many_to_many:
                if (model, f.name) in RULE_HOPS or self.policy.readable(model, f.name):
                    continue
                target = f.related_model
                if target is User:
                    choices = sorted(self.world.users.values())
                else:
                    choices = list(self.world.rows[target])
                for pk in self._visible_rows(model):
                    _link(f, pk, choices[-1:])
                    self.counts["repointed"] += 1

    # -- D6: other users' names ------------------------------------------------

    def _rewrite_other_users(self):
        """Every other user's text columns (username, names, email) become
        canaries: no role reads the user model. The caller's own row is left
        alone (the browsable API's navbar shows its username)."""
        columns = [
            f
            for f in User._meta.concrete_fields
            if isinstance(f, (models.CharField, models.EmailField))
            and not f.primary_key
            and f.name not in SENSITIVE
            and not self.policy.readable(User, f.name)
        ]
        for i, (username, pk) in enumerate(sorted(self.world.users.items())):
            if pk == self.policy.user_pk:
                continue
            updates = {}
            for j, f in enumerate(columns):
                text = f"zq99{j:02d}{i}q"
                value = f"{text}@zq.org" if isinstance(f, models.EmailField) else text
                if f.max_length and len(value) > f.max_length:
                    continue
                updates[f.attname] = value
                self.canaries[text] = f"user {username} .{f.name}"
            User._base_manager.filter(pk=pk).update(**updates)
            self.counts["users"] += 1

    # -- D4: visible rows' M2M links to hidden targets -------------------------

    def _link_visible_rows_to_hidden_targets(self):
        for model in MODELS:
            for f in model._meta.many_to_many:
                target = f.related_model
                if (
                    target is User
                    or f.name in _members_fields(model)
                    or not self.hides_rows(target)
                ):
                    continue
                hidden = [pk for pk, _t in self.rows[target]] + [
                    pk
                    for pk in self.base.rows[target]
                    if not self.policy.visible(self.base, target, pk)
                ]
                if not hidden:
                    continue
                for pk in self._visible_rows(model):
                    _link(f, pk, hidden[:2])
                    self.counts["links"] += 1


# ---------------------------------------------------------------------------
# Endpoints and the request strategy
# ---------------------------------------------------------------------------

Endpoint = namedtuple(
    "Endpoint", "scope base model configured paths outputs search actions relevant"
)
ENDPOINTS = []


def _leaf(model, path):
    """``(model, field)`` a ``__`` path ends on, or ``(None, None)``."""
    current, field = model, None
    for part in path.split("__"):
        if current is None:
            return None, None
        try:
            field = current._meta.pk if part == "pk" else current._meta.get_field(part)
        except FieldDoesNotExist:
            return None, None
        owner = current
        current = field.related_model if field.is_relation else None
    return owner, field


def _param_paths(model, configured):
    """Configured paths, their bases, the pk, every field of the model and one
    hop past each relation (to probe what's refused)."""
    paths = set(configured) | {p.split("__")[0] for p in configured}
    paths |= {"pk", model._meta.pk.name}
    for f in model._meta.get_fields():
        paths.add(f.name)
        target = f.related_model if f.is_relation else None
        if target is not None:
            columns = [g for g in target._meta.concrete_fields if not g.is_relation]
            paths.update(f"{f.name}__{g.name}" for g in columns[:6])
    return sorted(p for p in paths if _leaf(model, p)[1] is not None)


def _relevant_rules(model, paths, scope):
    """``label.field`` rule candidates on the models ``paths`` cross."""
    found = set()
    for path in paths:
        current = model
        for part in path.split("__"):
            if current is None or current is User:
                break
            try:
                field = current._meta.get_field(part)
            except FieldDoesNotExist:
                break
            if field.concrete or field.many_to_many:
                if not getattr(field, "primary_key", False):
                    found.add(f"{_label(current)}.{field.name}")
            current = field.related_model if field.is_relation else None
    return sorted(found)


def _serve():
    """Build the two routers as tests/urls.py and tests/platform_urls.py do,
    serve them from this module, and list their routes in ENDPOINTS."""
    from django.urls import clear_url_caches, include, path

    from tests.platform_urls import IsPlatformStaff
    from turbodrf.router import TurboDRFRouter

    tenant = TurboDRFRouter()
    platform = TurboDRFRouter(scope="platform", gate=[IsPlatformStaff])
    urlpatterns[:] = [
        path("api/", include(tenant.urls)),
        path("console-api/", include(platform.urls)),
    ]
    clear_url_caches()
    ENDPOINTS.clear()
    for scope, prefix, router in (
        (TENANT, "api", tenant),
        (PLATFORM, "console-api", platform),
    ):
        for route, viewset, _basename in router.registry:
            model = viewset.model
            with use_scope(scope):
                configured = _resolve_configured_fields(
                    model, "list"
                ) + _resolve_configured_fields(model, "detail")
                search = [s.lstrip("^=@$") for s in get_searchable_fields(model) or ()]
            configured = list(dict.fromkeys(configured))
            actions = tuple(
                (a.url_path, a.detail)
                for a in viewset.get_extra_actions()
                if "get" in a.mapping
            )
            outputs = sorted(
                {p.replace("__", "_") for p in configured}
                | {p.split("__")[0] for p in configured}
            )
            ENDPOINTS.append(
                Endpoint(
                    scope,
                    f"/{prefix}/{route}/",
                    model,
                    sorted(
                        set(configured + search)
                        | {p.split("__")[0] for p in configured + search}
                    ),
                    _param_paths(model, configured + search),
                    outputs,
                    search,
                    actions,
                    _relevant_rules(model, configured + search, scope),
                )
            )


def _endpoints():
    return ENDPOINTS


def _endpoint(base):
    return next(ep for ep in ENDPOINTS if ep.base == base)


LOOKUPS = (
    "",
    "exact",
    "iexact",
    "icontains",
    "contains",
    "startswith",
    "istartswith",
    "isnull",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "range",
    "year",
    "month",
    "date",
)
NUMBERS = ("0", "1", "11", "21", "31", "77700", "77750", "-1", "2.5", "x")
DATES = ("2024-01-01", "2024-01-03", "1977-07-07", "1977-07-09", "2030-01-01", "x")
DATETIMES = ("2024-01-01T12:00:00Z", "2024-01-02", "1977-07-07T07:07:00Z", "x")
GENERIC = ("alpha", "zq", "a", "")


def _pks(model):
    """Visible, hidden, delta and nonexistent pks of ``model``, as text."""
    world = WORLD
    if model is User:
        return [str(pk) for pk in world.users.values()] + ["9999"]
    if model is Brokerage:
        return ["1", "2", "3"]
    pk_field = _pk_field(model)
    pks = [str(pk) for pk in world.rows.get(model, ())]
    pks += [str(_pk_value(model, pk_field, DELTA_ORDINAL + i)) for i in range(3)]
    return pks + ["99999", "0", "-1", "abc"]


def _strings(model, field):
    world = WORLD
    values = sorted(
        {
            str(row[field.attname])
            for row in world.snapshot.rows.get(model, {}).values()
            if row.get(field.attname) not in (None, "")
        }
    )[:6]
    if model is User:
        values += ["zq99", "zq9900", "u_other", "u_b"]
    if model in MI and field.name in FI[model]:
        prefix = f"zq{MI[model]:02d}{FI[model][field.name]:02d}"
        values += [prefix, prefix + "0q", prefix + "1q", prefix + "4q"]
        values += [prefix + "6q", prefix + f"{DELTA_ORDINAL}q"]
    return values + list(GENERIC)


def _single(model, path):
    owner, field = _leaf(model, path)
    if field is None:
        return st.sampled_from(GENERIC)
    if field.is_relation:
        return st.sampled_from(_pks(field.related_model))
    if isinstance(field, models.BooleanField):
        return st.sampled_from(("true", "false", "1", "x"))
    if isinstance(field, (models.DecimalField, models.FloatField, models.IntegerField)):
        return st.sampled_from(NUMBERS)
    if isinstance(field, models.DateTimeField):
        return st.sampled_from(DATETIMES)
    if isinstance(field, models.DateField):
        return st.sampled_from(DATES)
    if isinstance(field, models.TimeField):
        return st.sampled_from(("09:30", "07:07:05", "x"))
    if isinstance(field, models.DurationField):
        return st.sampled_from(("01:00:00", "77 00:00:05", "x"))
    if field.primary_key and owner is not None:
        return st.sampled_from(_pks(owner))
    return st.sampled_from(_strings(owner, field))


def _value(model, path, lookup):
    if lookup == "isnull":
        return st.sampled_from(("true", "false", "x"))
    if lookup == "year":
        return st.sampled_from(("2024", "1977", "0", "x"))
    if lookup == "month":
        return st.sampled_from(("1", "7", "13"))
    if lookup == "date":
        return st.sampled_from(DATES)
    single = _single(model, path)
    if lookup == "in":
        return st.lists(single, min_size=1, max_size=3).map(",".join)
    if lookup == "range":
        return st.tuples(single, single).map(",".join)
    return single


def _path(ep):
    """A configured path (or its base) half the time, else any path."""
    return st.one_of(st.sampled_from(ep.configured), st.sampled_from(ep.paths))


@st.composite
def _filter(draw, ep):
    path = draw(_path(ep))
    lookup = draw(st.sampled_from(LOOKUPS))
    key = path + (f"__{lookup}" if lookup else "")
    if draw(st.integers(0, 4)) == 4:
        values = draw(st.lists(_value(ep.model, path, lookup), min_size=1, max_size=3))
        return [(key + "_or", v) for v in values]
    return [(key, draw(_value(ep.model, path, lookup)))]


def _sometimes(draw, strategy):
    """``strategy``'s value one time in three; shrinks to None."""
    return draw(strategy) if draw(st.integers(0, 2)) == 2 else None


def _probe_values(model, path):
    """Values that tell a canary from a base value on ``path``: ``zq`` (every
    text canary starts with it, and sorts after every base value), the
    column's canary prefix, numbers and dates past the base world's."""
    owner, field = _leaf(model, path)
    if field is None:
        return ["", "in"], ["zq"]
    if field.is_relation:
        return ["", "in", "isnull"], _pks(field.related_model)
    if isinstance(field, models.BooleanField):
        return ["", "isnull"], ["true", "false"]
    if isinstance(field, (models.DecimalField, models.FloatField, models.IntegerField)):
        return ["gt", "lt", "", "gte"], ["6999", "77000", "0", "11"]
    if isinstance(field, (models.DateField, models.TimeField, models.DurationField)):
        lookups = ["lt", "gt", "year", "date", ""]
        return lookups, ["1980-01-01", "1977", "2024-01-01", "07:07:05"]
    strings = _strings(owner, field)
    return ["icontains", "startswith", "gt", "", "iexact", "in"], [
        "zq",
        *[v for v in strings if v.startswith("zq")][:2],
        *strings[:2],
    ]


@st.composite
def _probe(draw, ep):
    """One parameter aimed at a canary: a filter or an ordering on a
    configured path, or a search."""
    kinds = ["filter", "filter", "ordering", "none"] + ["search"] * (
        2 if ep.search else 1
    )
    kind = draw(st.sampled_from(kinds))
    if kind == "filter":
        path = draw(st.sampled_from(ep.configured))
        lookups, values = _probe_values(ep.model, path)
        lookup = draw(st.sampled_from(lookups))
        key = path + (f"__{lookup}" if lookup else "")
        return [(key, draw(st.sampled_from(values)))]
    if kind == "ordering":
        sign = draw(st.sampled_from(("", "-")))
        return [("ordering", sign + draw(st.sampled_from(ep.configured)))]
    if kind == "search":
        terms = ["zq"]
        for path in ep.search:
            owner, field = _leaf(ep.model, path)
            if field is not None:
                strings = _strings(owner, field)
                terms += [v for v in strings if v.startswith("zq")][:2] + strings[:2]
        return [("search", draw(st.sampled_from(terms)))]
    return []


@st.composite
def _query(draw, ep):
    """A probe two times in three, else 0-4 random filters and the other
    parameters, each one time in three."""
    if draw(st.integers(0, 2)) < 2:
        pairs = draw(_probe(ep))
        if draw(st.integers(0, 3)) == 3:
            pairs.append(("format", "api"))
        return tuple(pairs)
    pairs = []
    count = draw(st.sampled_from((0, 1, 1, 2, 2, 3, 4)))
    for group in draw(st.lists(_filter(ep), min_size=count, max_size=count)):
        pairs.extend(group)
    term = st.tuples(st.sampled_from(("", "-")), _path(ep)).map("".join)
    ordering = _sometimes(draw, st.lists(term, min_size=1, max_size=2).map(",".join))
    if ordering is not None:
        pairs.append(("ordering", ordering))
    search_pool = [
        s
        for path in ep.search
        for s in (
            _strings(*_leaf(ep.model, path))
            if _leaf(ep.model, path)[1] is not None
            else ()
        )
    ] or list(GENERIC)
    search = _sometimes(
        draw, st.lists(st.sampled_from(search_pool), min_size=1, max_size=2)
    )
    if search is not None:
        pairs.append(("search", " ".join(search)))
    fields = _sometimes(
        draw,
        st.lists(
            st.sampled_from(ep.outputs + ["nope"]), min_size=1, max_size=3, unique=True
        ),
    )
    if fields is not None:
        pairs.append(("fields", ",".join(fields)))
    for key, pool in (
        ("page", ("1", "2", "last", "0", "x")),
        ("page_size", ("1", "2", "3", "100", "0")),
        ("format", ("api", "api", "json")),
    ):
        value = _sometimes(draw, st.sampled_from(pool))
        if value is not None:
            pairs.append((key, value))
    return tuple(pairs)


RuleDraw = namedtuple("RuleDraw", "reads mid_reads writes mid_writes")
NO_RULES = RuleDraw((), (), (), ())


@st.composite
def _rule_draws(draw, ep):
    pool = st.sampled_from(ALL_RULES)
    if ep.relevant:
        pool = st.one_of(st.sampled_from(ep.relevant), pool)
    if ep.relevant and draw(st.integers(0, 3)) >= 2:
        # A read rule on every field the endpoint's paths cross: ni_low
        # reads none of them, ni_mid a random half.
        reads = tuple(ep.relevant)
    else:
        reads = tuple(draw(st.lists(pool, max_size=6, unique=True)))
    mid_reads = tuple(r for r in reads if draw(st.booleans()))
    writes = tuple(draw(st.lists(pool, max_size=3, unique=True)))
    mid_writes = tuple(w for w in writes if draw(st.booleans()))
    return RuleDraw(reads, mid_reads, writes, mid_writes)


class Case(namedtuple("Case", "endpoint caller kind pk action query rules")):
    def __repr__(self):
        return (
            f"Case(endpoint={self.endpoint!r}, caller={self.caller!r}, "
            f"kind={self.kind!r}, pk={self.pk!r}, action={self.action!r}, "
            f"query={self.query!r}, rules={self.rules!r})"
        )


@lru_cache(maxsize=None)
def _readers(base):
    """The callers with model-level read on ``base``'s model (and past the
    platform gate), ni_low / ni_mid / ni_top first."""
    ep = _endpoint(base)
    roles, platform = role_maps(NO_RULES, ep.scope)
    label = _label(ep.model)
    config = turbodrf_config(ep.model) or {}
    found = []
    for name in PLATFORM_CALLERS if ep.scope == PLATFORM else TENANT_CALLERS:
        caller = CALLERS[name]
        names = caller.platform_roles if ep.scope == PLATFORM else caller.roles
        perms = {
            perm
            for role in names
            for perm in (platform if ep.scope == PLATFORM else roles).get(role, ())
        }
        if ep.scope == PLATFORM and not caller.staff:
            continue
        if f"{label}.read" in perms or (
            ep.scope == TENANT
            and caller.username is None
            and config.get("public_access")
        ):
            found.append(name)
    return found or list(CALLERS)


def _kinds(ep):
    kinds = ["list", "list", "list", "detail", "detail", "detail", "options"]
    kinds.append("options_detail")
    kinds += ["action"] * len(ep.actions)
    return kinds


@st.composite
def cases(draw, focus=None):
    endpoints = st.sampled_from(ENDPOINTS)
    if focus:
        endpoints = st.one_of(st.just(_endpoint(focus)), endpoints)
    ep = draw(endpoints)
    # A caller with model-level read three times in four.
    everyone = PLATFORM_CALLERS if ep.scope == PLATFORM else TENANT_CALLERS
    readers = st.sampled_from(_readers(ep.base))
    caller = draw(st.one_of(readers, readers, readers, st.sampled_from(everyone)))
    kind = draw(st.sampled_from(_kinds(ep)))
    action = draw(st.sampled_from(ep.actions)) if kind == "action" else None
    needs_pk = kind in ("detail", "options_detail") or (action and action[1])
    pk = draw(st.sampled_from(_pks(ep.model))) if needs_pk else None
    query = draw(_query(ep))
    rules = draw(_rule_draws(ep))
    return Case(ep.base, caller, kind, pk, action, query, rules)


def role_maps(rules, scope):
    """TURBODRF_ROLES / TURBODRF_PLATFORM_ROLES with the harness roles and the
    drawn field rules (``ni_top`` holds every rule, so it exists)."""
    roles = {k: list(v) for k, v in BASE_ROLES.items()}
    platform = {k: list(v) for k, v in BASE_PLATFORM_ROLES.items()}
    reads = [f"{_label(m)}.read" for m in TDRF]
    writes = [f"{_label(m)}.{a}" for m in TDRF for a in ("create", "update")]
    roles["ni_low"] = list(reads)
    roles["ni_mid"] = reads + writes
    roles["ni_top"] = reads + writes + [f"{_label(m)}.delete" for m in TDRF]
    rule = {"read": [], "write": [], "mid_read": [], "mid_write": []}
    rule["read"] = [f"{r}.read" for r in rules.reads]
    rule["write"] = [f"{w}.write" for w in rules.writes]
    rule["mid_read"] = [f"{r}.read" for r in rules.mid_reads]
    rule["mid_write"] = [f"{w}.write" for w in rules.mid_writes]
    if scope == TENANT:
        roles["ni_mid"] += rule["mid_read"] + rule["mid_write"]
        roles["ni_top"] += rule["read"] + rule["write"]
    else:
        served = [m for m in TDRF if _platform_reachable(m)] + [Brokerage]
        platform["ni_ptop"] = (
            [f"{_label(m)}.read" for m in served] + rule["read"] + rule["write"]
        )
        platform["support"] = platform.get("support", []) + rule["mid_read"]
    return roles, platform


# ---------------------------------------------------------------------------
# Sending and comparing
# ---------------------------------------------------------------------------

Response = namedtuple("Response", "status content_type headers body queries")

# The browsable API's CSRF tokens: a fresh mask on every render, in the
# forms' hidden inputs and the page's JS config. Nothing else is normalised.
_TOKENS = [
    re.compile(rb'(name="csrfmiddlewaretoken" value=")[^"]*(")'),
    re.compile(rb'("csrfToken": ")[^"]*(")'),
    re.compile(rb"(csrfToken: ')[^']*(')"),
    re.compile(rb'(csrfToken: ")[^"]*(")'),
]


def _normalised(body, content_type):
    if content_type and "html" in content_type:
        for pattern in _TOKENS:
            body = pattern.sub(rb"\1<token>\2", body)
    return body


def _register_tenants(world):
    for username, tenant in USER_TENANT.items():
        if tenant is None:
            _test_user_brokerages.pop(world.users[username], None)
        else:
            _test_user_brokerages[world.users[username]] = world.brokerages[tenant]


def send(world, caller, method, url):
    cache.clear()  # no permission snapshot carried over between the worlds
    client = APIClient(raise_request_exception=False)
    if caller.username:
        # A fresh instance per send: nothing cached on it crosses worlds.
        user = User.objects.get(pk=world.users[caller.username])
        user._test_roles = list(caller.roles)
        user._test_platform_roles = list(caller.platform_roles)
        if caller.staff:
            user.is_staff = True
        client.force_authenticate(user)
    # The debug query log is a deque of 9000: once it's full,
    # CaptureQueriesContext counts nothing. Empty it first.
    connection.queries_log.clear()
    with CaptureQueriesContext(connection) as queries:
        response = client.generic(method, url)
    # Content-Length is the body's length, and the body is compared itself.
    headers = tuple(
        sorted(
            (k.lower(), v)
            for k, v in response.headers.items()
            if k.lower() != "content-length"
        )
    )
    content_type = response.get("Content-Type", "")
    return Response(
        response.status_code,
        content_type,
        headers,
        _normalised(response.content, content_type),
        len(queries.captured_queries),
    )


def _json_diff(a, b, path="$"):
    if type(a) is not type(b):
        return f"{path}: {a!r:.300} -> {b!r:.300}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b), key=str):
            if key not in a or key not in b:
                world = "world 2" if key in b else "world 1"
                value = b.get(key) if key in b else a.get(key)
                return f"{path}.{key}: only in {world} ({value!r:.200})"
            found = _json_diff(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return (
                f"{path}: {len(a)} -> {len(b)} items\n  1: {a!r:.400}\n  2: {b!r:.400}"
            )
        for i, (x, y) in enumerate(zip(a, b)):
            found = _json_diff(x, y, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if a == b else f"{path}: {a!r:.300} -> {b!r:.300}"


def _body_diff(first, second):
    try:
        return _json_diff(json.loads(first.body), json.loads(second.body))
    except ValueError:
        pass
    a = first.body.decode("utf-8", "replace").splitlines()
    b = second.body.decode("utf-8", "replace").splitlines()
    lines = [
        line
        for line in difflib.unified_diff(a, b, "world 1", "world 2", n=1, lineterm="")
    ]
    return "\n".join(lines[:40])


def _url(case, ep):
    method = "OPTIONS" if case.kind.startswith("options") else "GET"
    path = ep.base
    if case.pk is not None:
        path += quote(str(case.pk), safe="") + "/"
    if case.action:
        path += case.action[0] + "/"
    query = urlencode(list(case.query))
    return method, path + ("?" + query if query else "")


def _record(record, text):
    try:
        record(text)
    except InvalidArgument:  # outside a Hypothesis test
        pass


QUERY_DIFFS = []


def run_case(case):
    """Send ``case`` in both worlds; raise AssertionError on a difference."""
    world = WORLD
    ep = _endpoint(case.endpoint)
    caller = CALLERS[case.caller]
    roles, platform_roles = role_maps(case.rules, ep.scope)
    method, url = _url(case, ep)
    roles_map = platform_roles if ep.scope == PLATFORM else roles
    with override_settings(
        TURBODRF_ROLES=roles, TURBODRF_PLATFORM_ROLES=platform_roles
    ):
        _register_tenants(world)
        first = send(world, caller, method, url)
        policy = Policy(world, caller, ep.scope, roles_map)
        with transaction.atomic():
            # A custom action's own code may render any column of the row
            # get_object() returns (``{"pong": obj.name}``): only row scoping
            # is the library's there (docs/security.md, "Custom code ...").
            # For an action the delta leaves visible rows' columns alone.
            delta = Delta(world, policy, columns=case.kind != "action").build()
            second = send(world, caller, method, url)
            transaction.set_rollback(True)
        if TRACE:
            print(
                f"{first.status} {second.status} q={first.queries}/{second.queries}"
                f" {method} {url} as {case.caller}; delta {dict(delta.counts)},"
                f" {len(delta.canaries)} canaries",
                file=sys.stderr,
            )
            if TRACE > 1:
                print(first.body[:1500].decode("utf-8", "replace"), file=sys.stderr)
        _record(event, f"{case.kind}: status {first.status}")
        if os.environ.get("TURBODRF_NONINTERFERENCE_COVERAGE"):
            ok = "200" if first.status == 200 else "not 200"
            _record(event, f"{ep.base} {ok}")
        _record(event, "format=api" if ("format", "api") in case.query else "json")
        problems = []
        if first.status != second.status:
            problems.append(f"status {first.status} -> {second.status}")
        if first.headers != second.headers:
            problems.append(f"headers {first.headers} -> {second.headers}")
        if first.body != second.body:
            problems.append("body: " + str(_body_diff(first, second)))
        # Case-insensitive: a property may upper-case what it read.
        text1 = first.body.decode("utf-8", "replace").lower()
        text2 = second.body.decode("utf-8", "replace").lower()
        leaked = {
            canary: why
            for canary, why in delta.canaries.items()
            if canary in text2 and canary not in text1 and canary not in url
        }
        if problems or leaked or first.queries != second.queries:
            # World 1 again: a difference it shows too isn't the delta's.
            again = send(world, caller, method, url)
            if problems and (again.status, again.body) != (first.status, first.body):
                raise AssertionError(
                    f"NONDETERMINISTIC response (world 1 twice differs): "
                    f"{method} {url} as {case.caller}"
                )
            if first.queries != second.queries and again.queries != second.queries:
                _record(event, "query count differs")
                QUERY_DIFFS.append(
                    dict(
                        method=method,
                        url=url,
                        caller=case.caller,
                        rules=case.rules._asdict(),
                        world1=first.queries,
                        world2=second.queries,
                        status=first.status,
                    )
                )
        if problems or leaked:
            _record(note, f"delta: {dict(delta.counts)}")
            raise AssertionError(
                f"INTERFERENCE: {method} {url}\n  caller {case.caller} "
                f"{caller.roles if ep.scope == TENANT else caller.platform_roles}"
                f"\n  rules {case.rules}\n  "
                + "\n  ".join(problems)
                + (f"\n  canaries leaked: {leaked}" if leaked else "")
            )


def _write_query_log():
    if LOG_PATH and QUERY_DIFFS:
        with open(LOG_PATH, "a") as handle:
            for row in QUERY_DIFFS:
                handle.write(json.dumps(row, default=str) + "\n")
    QUERY_DIFFS.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_classify()


def _build_world():
    global WORLD
    _classify()
    _serve()
    WORLD = World().build()


def _fuzz_test():
    @FUZZ
    @given(data=st.data())
    def test_reads_do_not_depend_on_hidden_data(self, data):
        run_case(data.draw(cases(self.focus), label="case"))

    return test_reads_do_not_depend_on_hidden_data


@contextmanager
def _registration(model, config):
    """``turbodrf.register(model, config)`` for the block; the registries as
    they were after it (as tests/integration/test_register.py's)."""
    import turbodrf.router as router_mod
    from turbodrf import predicates, register
    from turbodrf.mixins import _registered

    saved = (
        dict(predicates._model_predicates),
        dict(predicates._model_tenant_fields),
        router_mod._bypass_roles_validated,
    )
    try:
        register(model, config)
        yield
    finally:
        _registered.pop(model, None)
        predicates._model_predicates.clear()
        predicates._model_predicates.update(saved[0])
        predicates._model_tenant_fields.clear()
        predicates._model_tenant_fields.update(saved[1])
        router_mod._bypass_roles_validated = saved[2]
        _classify()


@override_settings(**URLCONF)
class TestReadNoninterference(TestCase):
    @classmethod
    def setUpTestData(cls):
        _build_world()

    @classmethod
    def tearDownClass(cls):
        _write_query_log()
        super().tearDownClass()

    focus = None  # an endpoint drawn half the time
    test_reads_do_not_depend_on_hidden_data = _fuzz_test()


# The test app has no Conditional rule: LedgerInvoice (a plain model) is
# served through register() with one, for the class below.
LEDGER_CONFIG = {
    "endpoint": "invoices",
    "tenant_field": "brokerage",
    "visibility": [
        Conditional(when=Q(memo__startswith="beta"), require_roles=["manager"])
    ],
    "fields": ["id", "memo", "brokerage", "ledger", "ledger__name"],
    "searchable_fields": ["memo"],
}


@override_settings(**URLCONF)
class TestReadNoninterferenceConditional(TestReadNoninterference):
    """The same property with a Conditional rule in the world: LedgerInvoice
    rows whose memo starts with "beta" are visible to managers only."""

    focus = "/api/invoices/"
    # Its own function: Hypothesis refuses one test run by two classes.
    test_reads_do_not_depend_on_hidden_data = _fuzz_test()

    @classmethod
    def setUpClass(cls):
        from tests.test_app.models import LedgerInvoice

        cls._registered = _registration(LedgerInvoice, LEDGER_CONFIG)
        cls._registered.__enter__()
        try:
            super().setUpClass()
        except Exception:
            cls._registered.__exit__(None, None, None)
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            cls._registered.__exit__(None, None, None)


@override_settings(**URLCONF)
class TestNoninterferenceCounterexamples(DjangoTestCase):
    """Minimised counterexamples the fuzz found, as plain tests. A leak's test
    is marked expectedFailure until the library is fixed."""

    @classmethod
    def setUpTestData(cls):
        _build_world()

    def test_harness_smoke(self):
        """A list and a detail on a tenant-scoped, owner-scoped model."""
        run_case(Case("/api/deals/", "low", "list", None, None, (), NO_RULES))
        run_case(Case("/api/deals/", "low", "detail", "1", None, (), NO_RULES))


def _sweep_queries(ep, wide=False):
    """Every probe on ``ep`` (see ``_probe``); ``wide``: also ``_or`` and
    ``in`` over relation paths and combined parameters."""
    out = [(), (("format", "api"),)]
    for path in ep.configured:
        lookups, values = _probe_values(ep.model, path)
        for lookup in lookups[:4]:
            for value in values[:3]:
                key = path + (f"__{lookup}" if lookup else "")
                out.append(((key, value),))
        out.append((("ordering", path),))
        out.append((("ordering", "-" + path),))
    terms = ["zq"]
    for path in ep.search:
        owner, field = _leaf(ep.model, path)
        if field is not None:
            strings = _strings(owner, field)
            terms += [v for v in strings if v.startswith("zq")][:2] + strings[:2]
    out += [(("search", t),) for t in dict.fromkeys(terms)]
    if wide:
        # _or and in over relation paths with visible, hidden and delta pks;
        # a filter with an ordering and a search.
        for path in ep.configured:
            owner, field = _leaf(ep.model, path)
            to_one = field is not None and field.is_relation
            if to_one and not (field.many_to_many or field.one_to_many):
                pks = _pks(field.related_model)
                out.append(tuple((path + "_or", pk) for pk in pks[:4]))
                out.append(tuple((path + "_or", pk) for pk in pks[-7:-3]))
                out.append(((path + "__in", ",".join(pks[:6])),))
                out.append(((path + "__in", ",".join(pks[-7:])),))
        for path in ep.configured[:6]:
            out.append(
                (
                    (path + "__icontains", "zq"),
                    ("ordering", "-" + path),
                    ("search", "zq"),
                )
            )
            out.append(
                ((path + "__gt", "a"), ("ordering", path), ("page_size", "2"))
                + (("page", "2"),)
            )
            out.append(
                (("fields", path.replace("__", "_")), ("ordering", path))
                + (("format", "api"),)
            )
    return out


def run_batch(base, caller_name, rules, kinds):
    """``run_case`` for many requests of one caller on one delta: the
    differences, as tuples (``("QUERIES", ...)`` for a query count)."""
    world = WORLD
    ep = _endpoint(base)
    caller = CALLERS[caller_name]
    roles, platform_roles = role_maps(rules, ep.scope)
    roles_map = platform_roles if ep.scope == PLATFORM else roles
    found = []
    with override_settings(
        TURBODRF_ROLES=roles, TURBODRF_PLATFORM_ROLES=platform_roles
    ):
        _register_tenants(world)
        urls = [_url(c, ep) for c in kinds]
        first = [send(world, caller, m, u) for m, u in urls]
        policy = Policy(world, caller, ep.scope, roles_map)
        with transaction.atomic():
            delta = Delta(world, policy).build()
            second = [send(world, caller, m, u) for m, u in urls]
            transaction.set_rollback(True)
        for (m, u), a, b in zip(urls, first, second):
            text1 = a.body.decode("utf-8", "replace").lower()
            text2 = b.body.decode("utf-8", "replace").lower()
            leaked = [
                c
                for c in delta.canaries
                if c in text2 and c not in text1 and c not in u
            ]
            same = (a.status, a.headers, a.body) == (b.status, b.headers, b.body)
            if not same or leaked:
                diff = _body_diff(a, b) if a.body != b.body else None
                found.append((m, u, caller_name, a.status, b.status, diff, leaked[:3]))
            elif a.queries != b.queries:
                found.append(("QUERIES", m, u, caller_name, a.queries, b.queries))
    return found


@override_settings(**URLCONF)
class TestNoninterferenceSweep(TestReadNoninterferenceConditional):
    """A systematic sweep (no Hypothesis): every endpoint, its readers, both
    rule modes, every probe (and with ``TURBODRF_NONINTERFERENCE_SWEEP=wide``
    every reader, ``_or`` / ``in`` over hidden pks and combined parameters),
    one delta per (endpoint, caller, rules). Off unless the variable is set:
    ~22k requests (wide: ~47k), 2-5 minutes."""

    test_reads_do_not_depend_on_hidden_data = None

    @unittest.skipUnless(
        os.environ.get("TURBODRF_NONINTERFERENCE_SWEEP"),
        "set TURBODRF_NONINTERFERENCE_SWEEP=1 (or wide)",
    )
    def test_sweep(self):
        wide = os.environ.get("TURBODRF_NONINTERFERENCE_SWEEP") == "wide"
        total, problems = 0, []
        for ep in ENDPOINTS:
            readers = _readers(ep.base)
            if wide:
                callers = list(readers) + ["no_tenant"]
            else:
                preferred = ("low", "mid", "top")
                preferred += ("staff_support", "staff_auditor", "staff_admin")
                callers = [c for c in preferred if c in readers][:3]
                if "anonymous" in readers:
                    callers.append("anonymous")
            half = tuple(ep.relevant[::2])
            for rules in (NO_RULES, RuleDraw(tuple(ep.relevant), half, (), ())):
                for caller in callers:
                    batch = [
                        Case(ep.base, caller, "list", None, None, q, rules)
                        for q in _sweep_queries(ep, wide)
                    ]
                    pks = _pks(ep.model)
                    batch += [
                        Case(ep.base, caller, "detail", pk, None, q, rules)
                        for j, pk in enumerate(pks)
                        for q in [(("format", "api"),) if j % 2 else ()]
                    ]
                    batch.append(
                        Case(ep.base, caller, "options_detail", pks[0], None, (), rules)
                    )
                    total += len(batch)
                    problems += run_batch(ep.base, caller, rules, batch)
        self.assertEqual(problems, [], f"{len(problems)} of {total} requests differ")
