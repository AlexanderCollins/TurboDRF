"""Security review (0.6.2): escapes from the @property sandbox.

Each test is a finding a review reproduced on 0.6.1, asserted fixed. See
tests/integration/test_property_sandbox.py for the sandbox's baseline tests.
"""

from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models.signals import post_init
from django.test import TestCase, override_settings

from tests.integration.test_regressions_052 import EVENTS_URL, _TenantFixture
from tests.test_app.models import (
    Holding,
    HoldingEvent,
    ParityCurrency,
    ParityTarget,
)
from turbodrf.properties import sandboxed_values

User = get_user_model()

ROLES = {
    **settings.TURBODRF_ROLES,
    # Reads Holding, but has no permission on HoldingEvent at all.
    "holding_only": ["test_app.holding.read"],
    # Reads both parity models; only cur_admin may read ParityCurrency.code.
    "cur_reader": ["test_app.paritycurrency.read", "test_app.paritytarget.read"],
    "cur_admin": ["test_app.paritycurrency.read", "test_app.paritycurrency.code.read"],
}


def _patched_config(model, **extra):
    base = model.turbodrf() if hasattr(model, "turbodrf") else {}
    return mock.patch.object(
        model, "turbodrf", classmethod(lambda cls: {**base, **extra}), create=True
    )


def _values(model, obj, getters, user, **config):
    with _patched_config(model, **config):
        return sandboxed_values(
            model, [obj.pk], getters, SimpleNamespace(user=user)
        ).get(obj.pk, {})


# ---------------------------------------------------------------------------
# 1. A lazy value nested in a property's result ran after the sandbox
# ---------------------------------------------------------------------------


def _lazy_names():
    # A generator function's body runs only when the generator is iterated.
    yield from Holding.objects.values_list("name", flat=True)


class TestNestedLazyValues(_TenantFixture):
    """A queryset or generator nested in the dict / list a property returns
    (or a top-level map over one) was evaluated by the JSON encoder, after the
    sandbox: unscoped and with every column."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.analyst)

    def _detail(self, fget):
        fields = list(HoldingEvent.turbodrf()["fields"]) + ["evil"]
        with mock.patch.object(HoldingEvent, "evil", property(fget), create=True):
            with _patched_config(HoldingEvent, fields=fields):
                resp = self.client.get(f"{EVENTS_URL}{self.matched.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp

    def test_lazy_values_are_left_out(self):
        for case, fget in enumerate(
            (
                lambda s: {"names": Holding.objects.values_list("name", flat=True)},
                lambda s: [HoldingEvent.objects.values_list("status", "brokerage_id")],
                lambda s: map(list, [Holding.objects.values_list("name", flat=True)]),
                lambda s: {"g": _lazy_names()},
            )
        ):
            with self.subTest(case=case):
                resp = self._detail(fget)
                self.assertNotIn(b"Bolt", resp.content)
                self.assertNotIn("evil", resp.data)

    def test_sandbox_returns_plain_values(self):
        """sandboxed_values is what both read paths render (the compiled list
        calls it too)."""
        values = _values(
            HoldingEvent,
            self.matched,
            {
                "lazy": lambda s: {"g": _lazy_names()},
                "plain": lambda s: {
                    "status": s.status,
                    "parts": (p for p in s.status.split("_")),
                    "pair": ("a", 1),
                    "set": {"x"},
                },
            },
            self.analyst,
        )
        self.assertEqual(
            values,
            {
                "plain": {
                    "status": "matched",
                    "parts": ["matched"],
                    "pair": ["a", 1],
                    "set": ["x"],
                }
            },
        )

    def test_declared_relation_values_list(self):
        """The realistic shape: a dict holding a values_list over a declared
        relation. values_list is a new query, so it can't run in the sandbox
        (it used to run after it, unscoped: another tenant's event status)."""
        HoldingEvent.objects.create(
            status="tenant-B-event", brokerage=self.brokerage_b, holding=self.holding_a
        )
        base = Holding.turbodrf()
        config = {
            **base,
            "fields": list(base["fields"]) + ["summary"],
            "property_relations": {"summary": ["events"]},
        }
        prop = property(
            lambda s: {"n": 1, "statuses": s.events.values_list("status", flat=True)}
        )
        with mock.patch.object(Holding, "summary", prop, create=True):
            with mock.patch.object(Holding, "turbodrf", classmethod(lambda c: config)):
                resp = self.client.get(f"/api/holdings/{self.holding_a.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn(b"tenant-B-event", resp.content)
        self.assertNotIn("summary", resp.data)

    def test_plain_nested_values_still_render(self):
        resp = self._detail(lambda s: {"status": s.status, "parts": iter(["a"])})
        self.assertEqual(resp.data["evil"], {"status": "matched", "parts": ["a"]})


# ---------------------------------------------------------------------------
# 9. A property returning a model instance made the whole response 500
# ---------------------------------------------------------------------------


class TestModelInstanceResult(_TenantFixture):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.analyst)

    def test_instance_is_left_out(self):
        fields = list(HoldingEvent.turbodrf()["fields"]) + ["obj"]
        relations = {"holding_label": ["holding__name"], "obj": ["holding"]}
        for case, fget in enumerate(
            (
                lambda s: s.holding,
                lambda s: {"holding": s.holding},
                lambda s: s.holding.events if s.holding else None,
            )
        ):
            with self.subTest(case=case):
                with mock.patch.object(
                    HoldingEvent, "obj", property(fget), create=True
                ):
                    with _patched_config(
                        HoldingEvent, fields=fields, property_relations=relations
                    ):
                        resp = self.client.get(f"{EVENTS_URL}{self.matched.pk}/")
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertNotIn("obj", resp.data)
                self.assertEqual(resp.data["status_label"], "Matched")

    def test_unscoped_instance_is_left_out(self):
        fields = list(HoldingEvent.turbodrf()["fields"]) + ["obj"]
        with mock.patch.object(
            HoldingEvent, "obj", property(lambda s: s.holding), create=True
        ):
            with _patched_config(
                HoldingEvent, fields=fields, unscoped_properties=["obj"]
            ):
                resp = self.client.get(f"{EVENTS_URL}{self.matched.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn("obj", resp.data)


# ---------------------------------------------------------------------------
# 5. Columns loaded before the sandbox (post_init, to_field joins) leaked
# ---------------------------------------------------------------------------


def _remember_holding_id(sender, instance, **kwargs):
    # The common "remember the original value" pattern: it loads the deferred
    # column while the sandbox copy is built.
    instance._original_holding = instance.holding_id


def _remember_holding(sender, instance, **kwargs):
    # Touches the related object: loads it, unscoped, into the field cache.
    if "holding_id" in instance.__dict__:
        instance._original = instance.holding


class TestColumnsLoadedBeforeSandbox(_TenantFixture):
    def _with_handler(self, handler, func):
        post_init.connect(handler, sender=HoldingEvent)
        try:
            return func()
        finally:
            post_init.disconnect(handler, sender=HoldingEvent)

    def test_post_init_loaded_column_is_dropped(self):
        # auditor has no read rule for HoldingEvent.holding.
        values = self._with_handler(
            _remember_holding_id,
            lambda: _values(
                HoldingEvent,
                self.matched,
                {"fk": lambda s: s.holding_id, "status": lambda s: s.status},
                self.auditor,
            ),
        )
        self.assertEqual(values, {"status": "matched"})

    def test_post_init_cached_relation_is_rescoped(self):
        # `cross` points at brokerage B's holding: out of the analyst's scope,
        # so the declared relation reads as None. The handler cached the
        # unscoped row before the scoped prefetch ran.
        values = self._with_handler(
            _remember_holding,
            lambda: _values(
                HoldingEvent,
                self.cross,
                {"name": lambda s: s.holding and s.holding.name},
                self.analyst,
                property_relations={"name": ["holding"]},
            ),
        )
        self.assertEqual(values, {"name": None})


@override_settings(TURBODRF_ROLES=ROLES)
class TestToFieldJoinColumn(TestCase):
    """A reverse relation into a to_field FK joins on a column of the source
    row. Prefetching it loaded that column even when the caller can't read
    it."""

    def setUp(self):
        cache.clear()
        self.currency = ParityCurrency.objects.create(code="SEC", name="Secret")
        ParityTarget.objects.create(name="t", currency=self.currency)
        self.reader = User.objects.create_user(username="reader")
        self.reader._test_roles = ["cur_reader"]
        self.admin = User.objects.create_user(username="cadmin")
        self.admin._test_roles = ["cur_admin", "cur_reader"]

    def _run(self, user):
        getters = {
            "code": lambda s: s.code,
            "targets": lambda s: [t.name for t in s.paritytarget_set.all()],
        }
        relations = {"code": ["paritytarget_set"], "targets": ["paritytarget_set"]}
        return _values(
            ParityCurrency,
            self.currency,
            getters,
            user,
            property_relations=relations,
        )

    def test_unreadable_join_column_skips_the_hop(self):
        self.assertEqual(self._run(self.reader), {})

    def test_readable_join_column_loads_the_hop(self):
        self.assertEqual(self._run(self.admin), {"code": "SEC", "targets": ["t"]})


# ---------------------------------------------------------------------------
# 10. A hop's target model needed no read permission
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=ROLES)
class TestHopTargetModelRead(_TenantFixture):
    def test_target_without_model_read_is_not_loaded(self):
        user = self._user("ho", ["holding_only"], self.brokerage_a)
        values = _values(
            Holding,
            self.holding_a,
            {"n": lambda s: [e.pk for e in s.events.all()]},
            user,
            property_relations={"n": ["events"]},
        )
        self.assertEqual(values, {})

    def test_target_with_model_read_is_loaded(self):
        values = _values(
            Holding,
            self.holding_a,
            {"n": lambda s: [e.pk for e in s.events.all()]},
            self.analyst,
            property_relations={"n": ["events"]},
        )
        self.assertEqual(values, {"n": [self.matched.pk]})

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_bypassed_permissions_load_the_hop(self):
        user = self._user("ho", ["holding_only"], self.brokerage_a)
        values = _values(
            Holding,
            self.holding_a,
            {"n": lambda s: [e.pk for e in s.events.all()]},
            user,
            property_relations={"n": ["events"]},
        )
        self.assertEqual(values, {"n": [self.matched.pk]})
