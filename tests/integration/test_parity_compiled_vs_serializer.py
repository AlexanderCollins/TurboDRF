"""Differential parity harness: compiled list rows vs the serializer path.

For each scenario the harness renders, for the same user and query string:

  (a) the compiled list   (``_compiled_list``: .values() + F() + post_process),
  (b) the serializer list (the model dropped from the compiled-plan registry,
      which is what ``"compiled": False`` does),
  (c) the detail response for every row in (b),

decodes the RENDERED JSON (what a client sees, through the active renderer),
and diffs:

  * (a) vs (b): HTTP status, pagination counts, row order/count, the full key
    set of every row, and every value (type-strict: 1 != 1.0, "1" != 1);
  * (a) vs (c): the keys BOTH views are configured to show (for a dict
    ``fields`` config, keys(list) & keys(detail); otherwise the full key set).

Scenarios patch the model's ``turbodrf()`` config and recompile its plan, so
one model covers many field configs. Each scenario runs as an anonymous caller
and as a role user (a divergence must not hide behind a key both paths drop
for one of them). A known divergence can be marked ``xfail(strict=True)`` with
its root cause; none is today.
"""

import datetime
import importlib.util
import json
import sys
import uuid
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    Brokerage,
    Holding,
    HoldingEvent,
    ParityAllTypes,
    ParityChild,
    ParityCodePk,
    ParityCurrency,
    ParityLeaf,
    ParityTag,
    ParityTarget,
    ParityUuidPk,
)
from turbodrf import compiler

User = get_user_model()

ALL_URL = "/api/parityalltypess/"
TARGET_URL = "/api/paritytargets/"
CODE_URL = "/api/paritycodepks/"
UUID_URL = "/api/parityuuidpks/"
CHILD_URL = "/api/paritychilds/"
EVENTS_URL = "/api/holdingevents/"

MISSING = "<missing>"

UTC = datetime.timezone.utc

# Scalar columns that render identically on both paths in every encoder
# (Decimal values chosen away from the known "0E-8" str() case).
SCALAR_FIELDS = [
    "id",
    "char",
    "char_null",
    "text",
    "text_null",
    "integer",
    "integer_null",
    "big",
    "big_null",
    "positive",
    "positive_null",
    "flt",
    "flt_null",
    "flag",
    "flag_null",
    "day",
    "uid",
    "data",
    "data_null",
    "status",
    "status_null",
    "level",
    "email",
    "url",
    "slug",
    "ip",
    "amount",
    "amount_null",
]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _same(a, b):
    """Type-strict structural equality of decoded JSON."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _sorted(value):
    """Order-insensitive view of an M2M array (for tests not about order)."""
    if not isinstance(value, list):
        return value
    return sorted(value, key=lambda v: json.dumps(v, sort_keys=True))


def configured_keys(model, fields):
    """Output keys a ``fields`` list renders (None for ``"__all__"``)."""
    if not isinstance(fields, list):
        return None
    keys = set()
    for name in fields:
        if "__" not in name:
            keys.add(name)
            continue
        base = name.split("__")[0]
        keys.add(base)
        if not model._meta.get_field(base).many_to_many:
            keys.add(name.replace("__", "_"))
    return keys


def configured_m2m_subkeys(model, fields):
    """{m2m_base: {sub-key, ...}} a ``fields`` list renders inside M2M arrays."""
    subkeys = {}
    if not isinstance(fields, list):
        return subkeys
    for name in fields:
        base, _, rest = name.partition("__")
        if rest and model._meta.get_field(base).many_to_many:
            subkeys.setdefault(base, set()).add(rest)
    return subkeys


def _project(value, subkeys):
    """Keep only ``subkeys`` in each object of an M2M array."""
    if not isinstance(value, list):
        return value
    return [
        (
            {k: v for k, v in item.items() if k in subkeys}
            if isinstance(item, dict)
            else item
        )
        for item in value
    ]


@contextmanager
def parity_config(model, overrides):
    """Patch ``model.turbodrf()`` and (re)compile its plan for the duration.

    Yields the compile exception, if any — the router swallows it and serves
    the model from the serializer path, so the harness does the same.
    """
    cfg = dict(model.turbodrf())
    cfg.update(overrides)
    saved = compiler._compiled_plans.pop(model, None)
    with patch.object(model, "turbodrf", classmethod(lambda cls: dict(cfg))):
        error = None
        if cfg.get("compiled", True):
            try:
                plan = compiler.compile_model(model)
                if plan is not None:
                    compiler.register_compiled_plan(model, plan)
            except Exception as exc:  # pragma: no cover - reported
                error = exc
        try:
            yield cfg, error
        finally:
            compiler._compiled_plans.pop(model, None)
            if saved is not None:
                compiler._compiled_plans[model] = saved


@contextmanager
def serializer_path(model):
    """Serve ``model``'s list from the serializer path (``compiled: False``)."""
    plan = compiler._compiled_plans.pop(model, None)
    try:
        yield
    finally:
        if plan is not None:
            compiler._compiled_plans[model] = plan


@contextmanager
def select_related_workaround(enabled=True):
    """Drop to-many names from ``get_queryset()``'s ``select_related``.

    ``TurboDRFViewSet.get_queryset`` select_relateds the base of EVERY ``__``
    field, so any M2M / reverse-FK sub-field makes the serializer path raise
    FieldError (500) on detail and uncompiled lists (``.values()`` clears
    select_related, so the compiled list is unaffected). That 500 is asserted
    on its own (TestSelectRelated); the other scenarios run with this workaround
    so the rendering divergences behind it are visible.
    """
    if not enabled:
        yield
        return
    from turbodrf.views import TurboDRFViewSet

    original = TurboDRFViewSet.get_queryset

    def get_queryset(self):
        queryset = original(self)
        selected = queryset.query.select_related
        if isinstance(selected, dict):
            valid = {
                f.name
                for f in self.model._meta.get_fields()
                if f.is_relation and (f.many_to_one or f.one_to_one)
            }
            kept = {k: v for k, v in selected.items() if k in valid}
            queryset.query.select_related = kept or False
        return queryset

    with patch.object(TurboDRFViewSet, "get_queryset", get_queryset):
        yield


_RENDERER_VARIANTS = {}


def _renderer_class(lib):
    """The TurboDRFRenderer variant for ``lib`` (msgspec / orjson / stdlib)."""
    import turbodrf.renderers as live

    if lib == "stdlib":
        return None  # FAST_JSON_AVAILABLE=False -> DRF's stock JSONRenderer
    if lib == live.FAST_JSON_LIB:
        return live.TurboDRFRenderer
    if lib not in _RENDERER_VARIANTS:
        # Re-execute renderers.py once with msgspec hidden so its own orjson
        # branch is what renders (no copy of the renderer code here). Only the
        # hidden keys are touched: patch.dict(sys.modules) would clear and
        # refill the whole module table on exit.
        try:
            importlib.import_module(lib)
        except ImportError:
            pytest.skip(f"{lib} not installed")
        hidden = ("msgspec", "msgspec.json")
        saved = {name: sys.modules.get(name, MISSING) for name in hidden}
        spec = importlib.util.spec_from_file_location(
            f"_parity_renderers_{lib}", live.__file__
        )
        module = importlib.util.module_from_spec(spec)
        try:
            for name in hidden:
                sys.modules[name] = None
            spec.loader.exec_module(module)
        finally:
            for name, value in saved.items():
                if value is MISSING:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
        if module.FAST_JSON_LIB != lib:
            pytest.skip(f"{lib} renderer unavailable")
        _RENDERER_VARIANTS[lib] = module
    return _RENDERER_VARIANTS[lib].TurboDRFRenderer


@contextmanager
def encoder(lib):
    if lib is None:
        yield
        return
    import turbodrf.renderers as live

    cls = _renderer_class(lib)
    if cls is None:
        with patch.object(live, "FAST_JSON_AVAILABLE", False):
            yield
    else:
        with (
            patch.object(live, "TurboDRFRenderer", cls),
            patch.object(live, "FAST_JSON_AVAILABLE", True),
        ):
            yield


class ParityResult:
    def __init__(self, label):
        self.label = label
        self.divergences = []
        self.compiled = None
        self.serializer = None
        self.details = {}
        self.compile_error = None

    def add(self, message):
        self.divergences.append(f"[{self.label}] {message}")

    def __str__(self):
        return "\n".join(self.divergences) or f"[{self.label}] parity"


_UNSET = object()


class ParityHarness(TestCase):
    """Fixture data + the parity/diff machinery."""

    def setUp(self):
        import tests.urls  # noqa: F401  (router registers + compiles)

        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()
        self.client.raise_request_exception = False

        self.usd = ParityCurrency.objects.create(code="USD", name="Dollar")
        self.eur = ParityCurrency.objects.create(code="EUR", name="Euro")
        self.leaf1 = ParityLeaf.objects.create(
            name="leaf-one",
            amount=Decimal("3.50"),
            seen_at=datetime.datetime(2024, 1, 2, 3, 4, 5, 600000, tzinfo=UTC),
        )
        self.leaf2 = ParityLeaf.objects.create(name="leaf-two")  # NULL amount
        self.target1 = ParityTarget.objects.create(
            name="alpha",
            amount=Decimal("12.50"),
            seen_at=datetime.datetime(2024, 3, 10, 15, 30, 45, 123456, tzinfo=UTC),
            span=datetime.timedelta(hours=5, microseconds=7),
            uid=uuid.UUID("12345678-1234-5678-1234-567812345678"),
            doc="docs/alpha.txt",
            leaf=self.leaf1,
            partner=self.leaf2,
            currency=self.usd,
        )
        self.target2 = ParityTarget.objects.create(name="beta")  # NULL leaf
        self.target3 = ParityTarget.objects.create(name="gamma", leaf=self.leaf2)

        now = datetime.datetime(2024, 5, 6, 7, 8, 9, 10, tzinfo=UTC)
        self.tag_apple = ParityTag.objects.create(
            name="apple",
            weight=Decimal("1.25"),
            created=now,
            leaf=self.leaf1,
            currency=self.eur,
        )
        self.tag_banana = ParityTag.objects.create(name="banana")
        self.tag_zebra = ParityTag.objects.create(name="zebra", weight=Decimal("9.00"))
        self.tag_deleted = ParityTag.objects.create(name="ghost", is_deleted=True)

        self.row_full = ParityAllTypes.objects.create(
            char="Hello",
            char_null="maybe",
            text="long text",
            text_null="tn",
            integer=42,
            integer_null=7,
            big=9007199254740993,
            big_null=-5,
            positive=5,
            positive_null=6,
            flt=1.5,
            flt_null=2.25,
            flag=True,
            flag_null=False,
            day=datetime.date(2024, 2, 29),
            moment=datetime.datetime(2024, 3, 10, 15, 30, 45, 123456, tzinfo=UTC),
            clock=datetime.time(13, 14, 15, 123),
            span=datetime.timedelta(days=1, hours=2, minutes=3, seconds=4),
            uid=uuid.UUID("87654321-4321-8765-4321-876543218765"),
            data={"a": {"b": [1, 2, {"c": None}]}, "n": 1.5, "s": "x"},
            data_null={"k": "v"},
            status="live",
            status_null="draft",
            level=2,
            email="a@example.com",
            url="https://example.com/x",
            slug="a-slug",
            ip="2001:db8::1",
            attachment="files/report.pdf",
            picture="img/p.png",
            blob=b"\x00\x01binary\xff",
            amount=Decimal("1234.50"),
            amount_null=Decimal("0.10"),
            target=self.target1,
            twin=self.target2,
            currency=self.usd,
        )
        # Through-table order apple -> zebra; ParityTag.Meta.ordering = -name.
        self.row_full.tags.add(self.tag_apple)
        self.row_full.tags.add(self.tag_zebra)

        self.row_nulls = ParityAllTypes.objects.create(char="nulls")
        self.row_mid = ParityAllTypes.objects.create(
            char="mid",
            integer=3,
            moment=datetime.datetime(2024, 7, 1, 23, 59, 59, tzinfo=UTC),
            clock=datetime.time(0, 0),
            target=self.target2,  # target.leaf is NULL (2nd hop)
            twin=self.target3,
            data=[1, "two", None],
        )
        self.row_mid.tags.add(self.tag_banana)
        self.row_leafnull = ParityAllTypes.objects.create(
            char="leafnull", integer=9, target=self.target3  # leaf.amount NULL
        )

        self.reader = self._user("reader", ["parity_reader"])
        self.limited = self._user("limited", ["parity_limited"])

    def tearDown(self):
        _test_user_brokerages.clear()

    def _user(self, username, roles):
        user = User.objects.create_user(username=username, password="x")
        user._test_roles = roles
        return user

    # -- fetching ----------------------------------------------------------

    def _get(self, url, params=None):
        resp = self.client.get(url, params or {})
        exc = getattr(resp, "exc_info", None)
        if exc:
            return resp.status_code, f"{exc[0].__name__}: {exc[1]}"
        try:
            return resp.status_code, json.loads(resp.content)
        except ValueError:
            return resp.status_code, resp.content[:200]

    @staticmethod
    def _rows(body):
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    # -- the parity ---------------------------------------------------------

    def parity(
        self,
        model,
        url,
        label,
        *,
        fields=None,
        config=None,
        params=None,
        user=_UNSET,
        users=None,
        pk_key=None,
        detail=True,
        lib=None,
        unordered=(),
        ignore=(),
        select_related_fix=True,
    ):
        """Render (a) compiled list, (b) serializer list, (c) detail per row and
        return a ParityResult listing every divergence.

        ``users`` runs the parity once per user and merges the divergences
        (one assertion, so an xfail covers every user). ``ignore`` skips value
        comparison for keys whose divergence another test already pins down.
        """
        kwargs = dict(
            fields=fields,
            config=config,
            params=params,
            pk_key=pk_key,
            detail=detail,
            lib=lib,
            unordered=unordered,
            ignore=ignore,
            select_related_fix=select_related_fix,
        )
        if users is None and user is _UNSET:
            # Anonymous callers are gated by field rules too (nested paths need
            # read on every hop's model), so check a role user as well — a
            # divergence must not hide behind a key both paths drop.
            users = [None, self.reader]
        if users is not None:
            merged = ParityResult(label)
            for each in users:
                name = getattr(each, "username", "anonymous")
                for line in self.parity(
                    model, url, label, user=each, **kwargs
                ).divergences:
                    merged.divergences.append(f"({name}) {line}")
            return merged

        overrides = dict(config or {})
        if fields is not None:
            overrides["fields"] = fields
        result = ParityResult(label)
        pk_key = pk_key or model._meta.pk.name
        self._unordered = set(unordered)
        self._ignore = set(ignore)

        self.client.force_authenticate(user)
        cache.clear()
        with (
            parity_config(model, overrides) as (
                cfg,
                compile_error,
            ),
            encoder(lib),
            select_related_workaround(select_related_fix),
        ):
            result.compile_error = compile_error
            if compile_error is not None:
                result.add(f"compile failed (served uncompiled): {compile_error!r}")
            c_status, c_body = self._get(url, params)
            with serializer_path(model):
                s_status, s_body = self._get(url, params)
                s_rows = self._rows(s_body) if s_status == 200 else []
                pks = self._detail_pks(model, s_rows, pk_key, params)
                if detail:
                    # ?fields= selects on detail too.
                    detail_params = (
                        {"fields": params["fields"]}
                        if params and "fields" in params
                        else None
                    )
                    for pk in pks:
                        result.details[pk] = self._get(f"{url}{pk}/", detail_params)
        result.compiled, result.serializer = (c_status, c_body), (s_status, s_body)
        self._project = {}

        if c_status != s_status:
            failed = c_body if c_status != 200 else s_body
            result.add(f"HTTP compiled={c_status} serializer={s_status}: {failed!r}")
            for pk, (d_status, d_body) in result.details.items():
                if d_status != 200:
                    result.add(f"detail {pk}: HTTP {d_status}: {d_body!r}")
            return result
        if c_status != 200:
            result.add(f"HTTP {c_status} on both paths: {c_body!r}")
            return result

        c_rows = self._rows(c_body)
        if isinstance(c_body, dict) and isinstance(s_body, dict):
            cp, sp = c_body.get("pagination"), s_body.get("pagination")
            if not _same(cp, sp):
                result.add(f"pagination compiled={cp} serializer={sp}")
        if len(c_rows) != len(s_rows):
            result.add(
                f"row count compiled={len(c_rows)} serializer={len(s_rows)}; "
                f"compiled rows={c_rows!r}"
            )
            return result

        # (a) vs (b): full key set + values, row by row.
        for i, (c_row, s_row) in enumerate(zip(c_rows, s_rows)):
            self._diff(result, f"list row {i}", c_row, s_row, None, "serializer list")

        # (a) vs (c): keys both views are configured to show.
        fields_cfg = cfg.get("fields", "__all__")
        self._project = {}
        if isinstance(fields_cfg, dict):
            list_cfg = fields_cfg.get("list", "__all__")
            detail_cfg = fields_cfg.get("detail", "__all__")
            keys = configured_keys(model, list_cfg) & configured_keys(model, detail_cfg)
            list_sub = configured_m2m_subkeys(model, list_cfg)
            detail_sub = configured_m2m_subkeys(model, detail_cfg)
            self._project = {
                base: list_sub[base] & detail_sub[base]
                for base in set(list_sub) & set(detail_sub)
            }
        else:
            keys = None  # same config for list and detail: compare every key
        for i, (c_row, pk) in enumerate(zip(c_rows, pks)):
            d_status, d_body = result.details.get(pk, (None, None))
            if d_status is None:
                continue
            if d_status != 200:
                result.add(f"detail {pk}: HTTP {d_status}: {d_body!r}")
                continue
            self._diff(result, f"row {i} (pk={pk})", c_row, d_body, keys, "detail")
        return result

    def _detail_pks(self, model, s_rows, pk_key, params):
        if s_rows and all(pk_key in row for row in s_rows):
            return [row[pk_key] for row in s_rows]
        if params:
            return []  # can't map filtered rows without a pk in the output
        return [str(pk) for pk in model.objects.values_list("pk", flat=True)]

    def _diff(self, result, where, c_row, other, keys, other_name):
        if not isinstance(c_row, dict) or not isinstance(other, dict):
            result.add(f"{where}: non-object rows {c_row!r} / {other!r}")
            return
        if keys is None:
            if set(c_row) != set(other):
                only_c = sorted(set(c_row) - set(other))
                only_o = sorted(set(other) - set(c_row))
                result.add(
                    f"{where}: keys only in compiled={only_c} "
                    f"only in {other_name}={only_o}"
                )
            keys = set(c_row) & set(other)
        for key in sorted(keys - self._ignore):
            cv, ov = c_row.get(key, MISSING), other.get(key, MISSING)
            if other_name == "detail" and key in self._project:
                cv = _project(cv, self._project[key])
                ov = _project(ov, self._project[key])
            if key in self._unordered and isinstance(cv, list):
                cv, ov = _sorted(cv), _sorted(ov)
            if not _same(cv, ov):
                result.add(f"{where} {key!r}: compiled={cv!r} {other_name}={ov!r}")

    def assertParity(self, result):
        if result.divergences:
            self.fail("\n" + str(result))


def xfail(reason):
    return pytest.mark.xfail(strict=True, reason=reason)


# ---------------------------------------------------------------------------
# Field types
# ---------------------------------------------------------------------------


class TestFieldTypes(ParityHarness):
    def test_scalar_types(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "scalars",
                fields=SCALAR_FIELDS,
                users=(None, self.reader),
            )
        )

    def test_datetime_and_time_utc_with_microseconds(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "dt-utc", fields=["id", "moment", "clock"]
            )
        )

    @override_settings(TIME_ZONE="America/New_York")
    def test_datetime_non_utc_time_zone(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "dt-ny", fields=["id", "moment", "day"]
            )
        )

    def test_duration(self):
        self.assertParity(
            self.parity(ParityAllTypes, ALL_URL, "duration", fields=["id", "span"])
        )

    @override_settings(MEDIA_URL="/media/")
    def test_file_and_image(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "files", fields=["id", "attachment", "picture"]
            )
        )

    def test_binary(self):
        self.assertParity(
            self.parity(ParityAllTypes, ALL_URL, "binary", fields=["id", "blob"])
        )

    def test_fk_and_o2o_raw_ids(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "fk-ids",
                fields=["id", "target", "twin", "currency"],
                users=(None, self.reader),
            )
        )


class TestEncoders(ParityHarness):
    """The compiled list hands raw Python values (datetime, timedelta, bytes)
    to the renderer; the serializer path formats them in DRF fields first. So
    the formatting class depends on which renderer is active."""

    TEMPORAL = SCALAR_FIELDS + ["moment", "clock"]

    def _enc(self, lib, fields):
        return self.parity(
            ParityAllTypes, ALL_URL, f"enc-{lib}", fields=fields, lib=lib
        )

    def test_msgspec_scalars_and_temporal(self):
        self.assertParity(self._enc("msgspec", self.TEMPORAL))

    def test_msgspec_binary(self):
        self.assertParity(self._enc("msgspec", ["id", "blob"]))

    def test_orjson_scalars_and_temporal(self):
        self.assertParity(self._enc("orjson", self.TEMPORAL))

    def test_orjson_binary(self):
        self.assertParity(self._enc("orjson", ["id", "blob"]))

    def test_stdlib_scalars_and_temporal(self):
        self.assertParity(self._enc("stdlib", self.TEMPORAL))

    def test_stdlib_binary(self):
        self.assertParity(self._enc("stdlib", ["id", "blob"]))

    def test_duration_every_encoder(self):
        result = ParityResult("duration-all")
        for lib in ("msgspec", "orjson", "stdlib"):
            result.divergences += self._enc(lib, ["id", "span"]).divergences
        self.assertParity(result)


# ---------------------------------------------------------------------------
# Nested FK / O2O chains
# ---------------------------------------------------------------------------

NESTED_CHAIN = [
    "id",
    "target__name",
    "target__leaf__name",  # NULL at hop 1 (row_nulls) and hop 2 (row_mid)
    "target__leaf__amount",  # NULL at hop 3 (row_leafnull)
    "target__leaf__seen_at",
    "target__leaf",  # ends on an FK
    "target__partner",  # ends on a one-to-one
    "target__uid",
    "target__amount",
    "target__seen_at",
    "target__span",
    "target__doc",
    "twin__name",  # through a one-to-one
    "twin__leaf__name",
]


class TestNestedFk(ParityHarness):
    def test_chain_with_null_at_each_hop(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "chain",
                fields=NESTED_CHAIN,
                users=(None, self.reader),
            )
        )

    @override_settings(TIME_ZONE="America/New_York")
    def test_chain_datetime_non_utc(self):
        # Both paths render nested datetimes un-localised (UTC), so they agree
        # with each other -- but not with top-level datetimes on the serializer.
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "chain-ny", fields=["id", "target__seen_at"]
            )
        )

    def test_chain_stdlib_encoder(self):
        import os
        import tempfile

        # Every target gets the (real) file; an empty FieldFile would 500 the
        # serializer path instead ("has no file associated with it").
        ParityTarget.objects.update(doc="docs/alpha.txt")
        with tempfile.TemporaryDirectory() as media_root:
            os.makedirs(os.path.join(media_root, "docs"))
            with open(os.path.join(media_root, "docs", "alpha.txt"), "w") as fh:
                fh.write("file-body-line-1\nfile-body-line-2\n")
            with override_settings(MEDIA_ROOT=media_root):
                self.assertParity(
                    self.parity(
                        ParityAllTypes,
                        ALL_URL,
                        "chain-stdlib",
                        fields=["id", "target__doc"],
                        lib="stdlib",
                    )
                )

    def test_chain_ending_on_to_field_fk(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "to-field", fields=["id", "target__currency"]
            )
        )

    def test_nested_pk_key_collides_with_attname(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "fk-id", fields=["id", "target", "target__id"]
            )
        )

    def test_nested_property(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "nested-prop", fields=["id", "target__label"]
            )
        )

    def test_reverse_one_to_one_nested(self):
        self.assertParity(
            self.parity(
                ParityTarget,
                TARGET_URL,
                "rev-o2o",
                fields=["id", "name", "twin_row__char"],
            )
        )

    def test_reverse_one_to_one_base_is_not_rendered(self):
        # twin_row's pk belongs to the other row, which may be hidden from
        # the caller. Listed bare it's a startup error; as a nested path's
        # base it isn't rendered either, on any path.
        result = self.parity(
            ParityTarget,
            TARGET_URL,
            "rev-o2o-base",
            fields=["id", "name", "twin_row__char"],
            user=self.reader,
        )
        self.assertParity(result)
        rows = []
        for status, body in (result.compiled, result.serializer):
            self.assertEqual(status, 200)
            rows.extend(self._rows(body))
        for status, body in result.details.values():
            self.assertEqual(status, 200)
            rows.append(body)
        self.assertTrue(rows and result.details)
        for row in rows:
            self.assertNotIn("twin_row", row)
            self.assertIn("twin_row_char", row)


# ---------------------------------------------------------------------------
# M2M
# ---------------------------------------------------------------------------


class TestM2M(ParityHarness):
    SUBFIELDS = [
        "id",
        "char",
        "tags__name",
        "tags__weight",
        "tags__created",
        "tags__leaf",
    ]

    def test_scalar_subfields_including_empty(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "m2m",
                fields=self.SUBFIELDS,
                users=(None, self.reader),
                unordered={"tags"},
            )
        )

    def test_id_subfield(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "m2m-id",
                fields=["id", "tags__id", "tags__name"],
                unordered={"tags"},
            )
        )

    def test_array_order(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "m2m-order", fields=["id", "tags__name"]
            )
        )

    def test_soft_deleted_target(self):
        self.row_mid.tags.add(self.tag_deleted)
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "m2m-softdel",
                fields=["id", "tags__name"],
                unordered={"tags"},
            )
        )

    def test_nested_subfield(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "m2m-nested", fields=["id", "tags__leaf__name"]
            )
        )

    def test_to_field_fk_subfield(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "m2m-to-field", fields=["id", "tags__currency"]
            )
        )

    def test_property_subfield(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "m2m-prop", fields=["id", "tags__label"]
            )
        )

    def test_bare_m2m_field(self):
        self.assertParity(
            self.parity(
                ParityAllTypes, ALL_URL, "m2m-bare", fields=["id", "char", "tags"]
            )
        )

    def test_bare_m2m_plus_subfield(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "m2m-bare-sub",
                fields=["id", "char", "tags", "tags__name"],
                unordered={"tags"},
            )
        )


# ---------------------------------------------------------------------------
# get_queryset() select_related on to-many bases (serializer path only)
# ---------------------------------------------------------------------------


class TestSelectRelated(ParityHarness):
    """Without the harness workaround: any to-many sub-field 500s the
    serializer path (detail + uncompiled list) while the compiled list
    renders."""

    def test_m2m_subfield(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "m2m-select-related",
                fields=["id", "tags__name"],
                unordered={"tags"},
                select_related_fix=False,
            )
        )

    def test_existing_compiled_article(self):
        from tests.test_app.models import Category, CompiledArticle

        article = CompiledArticle.objects.create(title="t")
        article.categories.add(Category.objects.create(name="c"))
        self.assertParity(
            self.parity(
                CompiledArticle,
                "/api/compiledarticles/",
                "compiledarticle",
                pk_key="id",
                select_related_fix=False,
                users=(None,),  # public; the parity roles have no grant on it
            )
        )


# ---------------------------------------------------------------------------
# Reverse relations listed in fields
# ---------------------------------------------------------------------------


class TestReverseRelations(ParityHarness):
    def setUp(self):
        super().setUp()
        # target1 now has two rows pointing at it.
        ParityAllTypes.objects.create(char="second", target=self.target1)

    def test_reverse_fk(self):
        self.assertParity(
            self.parity(
                ParityTarget, TARGET_URL, "rev-fk", fields=["id", "name", "rows"]
            )
        )

    def test_reverse_fk_subfield(self):
        self.assertParity(
            self.parity(
                ParityTarget,
                TARGET_URL,
                "rev-fk-sub",
                fields=["id", "name", "rows__char"],
            )
        )


# ---------------------------------------------------------------------------
# @property fields
# ---------------------------------------------------------------------------


class TestProperties(ParityHarness):
    def test_row_computable_and_related_reading(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "props",
                fields=["id", "char", "char_upper", "target_label"],
                users=(None, self.reader),
            )
        )

    def test_property_with_getattr_default(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "prop-getattr",
                fields=["id", "target_label_safe"],
            )
        )

    def test_property_type_introspection(self):
        self.assertParity(
            self.parity(ParityAllTypes, ALL_URL, "prop-type", fields=["id", "kind"])
        )


# ---------------------------------------------------------------------------
# "__all__" and dict configs
# ---------------------------------------------------------------------------


class TestFieldConfigs(ParityHarness):
    def test_all_no_m2m(self):
        # Same key set on both paths; doc / span values are pinned by
        # test_file_and_image / test_duration.
        self.assertParity(
            self.parity(
                ParityTarget,
                TARGET_URL,
                "all-target",
                fields="__all__",
                users=(None, self.reader),
                ignore={"doc", "span"},
            )
        )

    def test_all_with_m2m(self):
        ParityCodePk.objects.create(code="b", name="B").tags.add(
            self.tag_apple, self.tag_zebra
        )
        ParityCodePk.objects.create(code="a", name="A")
        self.assertParity(
            self.parity(
                ParityCodePk,
                CODE_URL,
                "all-code",
                fields="__all__",
                users=(None, self.reader),
            )
        )

    def test_dict_config(self):
        fields = {
            "list": [
                "id",
                "char",
                "integer",
                "target__name",
                "tags__name",
                "char_upper",
            ],
            "detail": [
                "id",
                "char",
                "text",
                "integer",
                "target__name",
                "target__amount",
                "tags__name",
                "tags__weight",
                "char_upper",
            ],
        }
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "dict",
                fields=fields,
                users=(None, self.reader, self.limited),
                unordered={"tags"},
            )
        )


# ---------------------------------------------------------------------------
# Primary keys
# ---------------------------------------------------------------------------


class TestPrimaryKeys(ParityHarness):
    def setUp(self):
        super().setUp()
        self.u1 = ParityUuidPk.objects.create(name="one", rank=1)
        self.u2 = ParityUuidPk.objects.create(name="two", rank=2)
        self.u1.tags.add(self.tag_apple)
        self.c1 = ParityCodePk.objects.create(code="x-1", name="X", target=self.target1)
        self.c2 = ParityCodePk.objects.create(code="a-2", name="A")
        self.c1.tags.add(self.tag_banana)
        self.child = ParityChild.objects.create(name="kid", extra="e")

    def test_pk_not_configured(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "no-pk",
                fields=["char", "integer"],
                users=(None, self.reader),
            )
        )

    def test_pk_not_configured_with_m2m(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "no-pk-m2m",
                fields=["char", "tags__name"],
                unordered={"tags"},
            )
        )

    def test_uuid_pk_not_configured(self):
        self.assertParity(self.parity(ParityUuidPk, UUID_URL, "uuid-no-pk"))

    def test_uuid_pk_with_m2m(self):
        self.assertParity(
            self.parity(
                ParityUuidPk, UUID_URL, "uuid-m2m", fields=["id", "name", "tags__name"]
            )
        )

    def test_char_pk(self):
        self.assertParity(
            self.parity(ParityCodePk, CODE_URL, "char-pk", users=(None, self.reader))
        )

    def test_multi_table_child(self):
        self.assertParity(self.parity(ParityChild, CHILD_URL, "mti", pk_key="id"))


# ---------------------------------------------------------------------------
# Role-scoped field permissions
# ---------------------------------------------------------------------------

ROLE_FIELDS = [
    "id",
    "char",
    "amount",  # explicit rule: parity_reader only
    "twin",  # explicit rule: parity_reader only
    "twin__name",
    "target",
    "target__name",
    "target__amount",  # explicit rule on ParityTarget.amount
    "tags__name",
    "tags__weight",  # explicit rule on ParityTag.weight
    "char_upper",
    "target_label",
]


class TestRoles(ParityHarness):
    def test_reader_and_limited(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "roles",
                fields=ROLE_FIELDS,
                users=(self.reader, self.limited),
                unordered={"tags"},
            )
        )

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_permissions_disabled_role_user(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "perm-disabled",
                fields=ROLE_FIELDS,
                user=self.limited,
                unordered={"tags"},
            )
        )


# ---------------------------------------------------------------------------
# ?fields=, pagination, ordering, filtering, search
# ---------------------------------------------------------------------------

QUERY_FIELDS = [
    "id",
    "char",
    "char_null",
    "char_upper",
    "integer",
    "moment",
    "target",
    "target__name",
]


class TestQueryParams(ParityHarness):
    def setUp(self):
        super().setUp()
        for i in range(5):
            ParityAllTypes.objects.create(
                char=f"bulk{i}", integer=i % 3, target=self.target1
            )

    def test_pagination_ordering_filter_search(self):
        cases = [
            {"page_size": 2},
            {"page_size": 2, "page": 3},
            {"ordering": "-integer"},
            {"ordering": "char"},
            {"ordering": "moment"},
            {"search": "alpha"},
            {"search": "l"},
            {"integer__gte": 2},
            {"target__isnull": "true"},
            {"target": self.target1.pk},
            {"tags": self.tag_apple.pk},
            {"status": "live"},
            {"char__icontains": "BULK", "page_size": 3, "page": 2},
        ]
        for user in (None, self.reader, self.limited):
            for params in cases:
                with self.subTest(user=getattr(user, "username", None), params=params):
                    self.assertParity(
                        self.parity(
                            ParityAllTypes,
                            ALL_URL,
                            f"qp {params}",
                            fields=QUERY_FIELDS,
                            params=params,
                            user=user,
                        )
                    )

    def test_ordering_ties(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "qp ordering=-target",
                fields=QUERY_FIELDS,
                params={"ordering": "-target"},
                users=(None, self.reader),
            )
        )

    def test_ordering_by_m2m(self):
        # An M2M isn't orderable (the JOIN repeated rows): both paths ignore
        # the term identically.
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "qp ordering=tags",
                fields=QUERY_FIELDS,
                params={"ordering": "tags"},
                user=self.reader,
            )
        )

    def test_search_over_m2m(self):
        self.row_mid.tags.add(self.tag_apple)
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "qp search m2m",
                config={"searchable_fields": ["char", "tags__name"]},
                fields=QUERY_FIELDS,
                params={"search": "a"},
            )
        )

    def test_fields_param_on_both_paths(self):
        self.assertParity(
            self.parity(
                ParityAllTypes,
                ALL_URL,
                "?fields=id,target.name",
                fields=QUERY_FIELDS,
                params={"fields": "id,target.name"},
            )
        )

    def test_fields_param_prefix_overmatch(self):
        with parity_config(ParityAllTypes, {"fields": QUERY_FIELDS}):
            status, body = self._get(ALL_URL, {"fields": "char"})
        self.assertEqual(status, 200)
        self.assertEqual({k for row in body["data"] for k in row}, {"char"})


# ---------------------------------------------------------------------------
# Tenant-scoped FK whose target belongs to another tenant
# ---------------------------------------------------------------------------


class TestCrossTenantFkTarget(ParityHarness):
    def setUp(self):
        super().setUp()
        self.brokerage_a = Brokerage.objects.create(name="A")
        self.brokerage_b = Brokerage.objects.create(name="B")
        self.holding_a = Holding.objects.create(name="Acme", brokerage=self.brokerage_a)
        self.holding_b = Holding.objects.create(name="Bolt", brokerage=self.brokerage_b)
        HoldingEvent.objects.create(
            status="ok", brokerage=self.brokerage_a, holding=self.holding_a
        )
        HoldingEvent.objects.create(
            status="null", brokerage=self.brokerage_a, holding=None
        )
        # Same tenant, FK points at ANOTHER tenant's holding.
        self.cross = HoldingEvent.objects.create(
            status="cross", brokerage=self.brokerage_a, holding=self.holding_b
        )
        self.analyst = self._user("analyst", ["analyst"])
        set_test_brokerage(self.analyst, self.brokerage_a)

    def test_cross_tenant_fk_row(self):
        self.assertParity(
            self.parity(HoldingEvent, EVENTS_URL, "cross", user=self.analyst)
        )

    def test_other_tenants_nested_value_is_null_on_every_path(self):
        # The row is the caller's (same tenant), so it's listed; the nested
        # value comes from brokerage B's holding, so it renders as null.
        self.client.force_authenticate(self.analyst)
        status, body = self._get(f"{EVENTS_URL}{self.cross.pk}/")
        self.assertEqual(status, 200)
        self.assertIsNone(body["holding_name"])
        for path in ("compiled", "serializer"):
            if path == "serializer":
                with serializer_path(HoldingEvent):
                    status, body = self._get(EVENTS_URL)
            else:
                status, body = self._get(EVENTS_URL)
            rows = {row["id"]: row for row in body["data"]}
            self.assertIn(self.cross.pk, rows, path)
            self.assertIsNone(rows[self.cross.pk]["holding_name"], path)


# ---------------------------------------------------------------------------
# DRF output settings the compiled path doesn't consult
# ---------------------------------------------------------------------------


def _rest_framework(**extra):
    return {**settings.REST_FRAMEWORK, **extra}


class TestDrfOutputSettings(ParityHarness):
    def test_coerce_decimal_to_string_false_fast_renderer(self):
        # Parity, but only because msgspec renders the serializer's Decimal as
        # a string too: the setting is silently ignored on both paths.
        with override_settings(
            REST_FRAMEWORK=_rest_framework(COERCE_DECIMAL_TO_STRING=False)
        ):
            self.assertParity(
                self.parity(
                    ParityAllTypes,
                    ALL_URL,
                    "decimal-num",
                    fields=["id", "amount", "amount_null"],
                )
            )

    def test_coerce_decimal_to_string_false_stdlib(self):
        with override_settings(
            REST_FRAMEWORK=_rest_framework(COERCE_DECIMAL_TO_STRING=False)
        ):
            self.assertParity(
                self.parity(
                    ParityAllTypes,
                    ALL_URL,
                    "decimal-num-stdlib",
                    fields=["id", "amount", "amount_null"],
                    lib="stdlib",
                )
            )

    def test_custom_datetime_format(self):
        with override_settings(
            REST_FRAMEWORK=_rest_framework(
                DATETIME_FORMAT="%Y-%m-%d %H:%M",
                DATE_FORMAT="%d/%m/%Y",
                TIME_FORMAT="%H:%M",
            )
        ):
            self.assertParity(
                self.parity(
                    ParityAllTypes,
                    ALL_URL,
                    "dt-format",
                    fields=["id", "moment", "day", "clock"],
                )
            )

    @override_settings(MEDIA_URL="/media/")
    def test_uploaded_files_use_url_false(self):
        with override_settings(
            REST_FRAMEWORK=_rest_framework(UPLOADED_FILES_USE_URL=False)
        ):
            self.assertParity(
                self.parity(
                    ParityAllTypes, ALL_URL, "files-name", fields=["id", "attachment"]
                )
            )
