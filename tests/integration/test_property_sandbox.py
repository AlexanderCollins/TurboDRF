"""@property fields run in the caller's sandbox (turbodrf.properties).

A property sees only the columns the caller may read and the relations the
model declares in ``property_relations``, each hop loaded within the caller's
scope; it can't query. Before 0.6.0 it ran on the full instance: a property
reading ``self.holding.name`` rendered another tenant's holding, and one
reading a restricted column rendered it to roles without the read rule.
"""

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from tests.integration.test_regressions_052 import EVENTS_URL, _TenantFixture
from tests.test_app.models import CompiledSampleModel, Holding, HoldingEvent
from turbodrf.properties import sandboxed_values, validate_property_config

User = get_user_model()


def _patched_config(model, **extra):
    base = model.turbodrf()
    return mock.patch.object(
        model, "turbodrf", classmethod(lambda cls: {**base, **extra})
    )


class _SandboxFixture(_TenantFixture):
    def _row(self, resp, pk):
        self.assertEqual(resp.status_code, 200, resp.content)
        return next(r for r in resp.data["data"] if r["id"] == pk)

    def _values(self, model, obj, getters, user, **config):
        request = SimpleNamespace(user=user)
        if not hasattr(model, "turbodrf"):
            return sandboxed_values(model, [obj.pk], getters, request)[obj.pk]
        with _patched_config(model, **config):
            return sandboxed_values(model, [obj.pk], getters, request)[obj.pk]


# ---------------------------------------------------------------------------
# Row scope: a declared relation is loaded within the caller's scope
# ---------------------------------------------------------------------------


class TestPropertyRowScope(_SandboxFixture):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.analyst)

    def test_compiled_list_hides_other_tenants_related_row(self):
        resp = self.client.get(EVENTS_URL)
        self.assertIsNone(self._row(resp, self.cross.pk)["holding_label"])
        self.assertEqual(self._row(resp, self.matched.pk)["holding_label"], "ACME")
        self.assertNotIn("BOLT", str(resp.data))

    def test_detail_hides_other_tenants_related_row(self):
        resp = self.client.get(f"{EVENTS_URL}{self.cross.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["holding_label"])
        resp = self.client.get(f"{EVENTS_URL}{self.matched.pk}/")
        self.assertEqual(resp.data["holding_label"], "ACME")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_bypassed_permissions_still_row_scoped(self):
        resp = self.client.get(f"{EVENTS_URL}{self.cross.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["holding_label"])
        self.assertNotIn("BOLT", str(resp.data))

    def test_deeper_hop_scoped(self):
        # matched -> holding_a (A) -> a portfolio in brokerage B.
        from tests.test_app.models import Portfolio

        foreign = Portfolio.objects.create(name="Secret", brokerage=self.brokerage_b)
        Holding.objects.filter(pk=self.holding_a.pk).update(portfolio=foreign)
        values = self._values(
            HoldingEvent,
            self.matched,
            {"p": lambda s: s.holding.portfolio and s.holding.portfolio.name},
            self.analyst,
            property_relations={"p": ["holding__portfolio"]},
        )
        self.assertIsNone(values["p"])

    def test_reverse_relation_scoped(self):
        # An event in brokerage B pointing at A's holding is not A's to see.
        HoldingEvent.objects.create(
            status="foreign", brokerage=self.brokerage_b, holding=self.holding_a
        )
        values = self._values(
            Holding,
            self.holding_a,
            {"statuses": lambda s: sorted(e.status for e in s.events.all())},
            self.analyst,
            property_relations={"statuses": ["events"]},
        )
        self.assertEqual(values["statuses"], ["matched"])

    def test_declared_relations_load_once_per_page(self):
        for i in range(5):
            HoldingEvent.objects.create(
                status=f"s{i}", brokerage=self.brokerage_a, holding=self.holding_a
            )
        with CaptureQueriesContext(connection) as small:
            self.client.get(EVENTS_URL, {"fields": "id,holding_label"})
        for i in range(10):
            HoldingEvent.objects.create(
                status=f"t{i}", brokerage=self.brokerage_a, holding=self.holding_a
            )
        with CaptureQueriesContext(connection) as large:
            resp = self.client.get(EVENTS_URL, {"fields": "id,holding_label"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(small), len(large))


# ---------------------------------------------------------------------------
# Field permissions: only readable columns and hops
# ---------------------------------------------------------------------------


class TestPropertyFieldRules(_SandboxFixture):
    def test_role_without_relation_read_gets_no_derived_value(self):
        # auditor has no read rule for HoldingEvent.holding.
        self.client.force_authenticate(self.auditor)
        resp = self.client.get(EVENTS_URL)
        row = self._row(resp, self.matched.pk)
        self.assertNotIn("holding_label", row)
        self.assertEqual(row["status_label"], "Matched")
        self.assertNotIn("ACME", str(resp.data))

        resp = self.client.get(f"{EVENTS_URL}{self.matched.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("holding_label", resp.data)
        self.assertNotIn("ACME", str(resp.data))

    def test_unreadable_column_is_not_loaded(self):
        values = self._values(
            HoldingEvent,
            self.matched,
            {"fk": lambda s: s.holding_id, "status": lambda s: s.status},
            self.auditor,
        )
        self.assertEqual(values, {"status": "matched"})

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_sensitive_columns_never_loaded(self):
        values = self._values(
            User,
            self.analyst,
            {"pw": lambda s: s.password, "name": lambda s: s.username},
            self.analyst,
        )
        self.assertEqual(values, {"name": "analyst"})

    def test_anonymous_public_model(self):
        from tests.test_app.models import RelatedModel

        related = RelatedModel.objects.create(name="r")
        CompiledSampleModel.objects.create(title="t", price="1.00", related=related)
        self.client.force_authenticate(None)
        resp = self.client.get("/api/compiledsamplemodels/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["display_title"] for r in resp.data["data"]], ["T"])


# ---------------------------------------------------------------------------
# No queries: undeclared relations, own queries, lazy results
# ---------------------------------------------------------------------------


class TestPropertyQueriesBlocked(_SandboxFixture):
    def test_undeclared_relation(self):
        values = self._values(
            HoldingEvent,
            self.matched,
            {"p": lambda s: s.holding.portfolio.name},
            self.analyst,
            property_relations={"p": ["holding"]},
        )
        self.assertEqual(values, {})
        values = self._values(
            HoldingEvent,
            self.matched,
            {"p": lambda s: s.holding.portfolio.name},
            self.analyst,
            property_relations={"p": ["holding__portfolio"]},
        )
        self.assertEqual(values, {"p": "Growth"})

    def test_own_query(self):
        values = self._values(
            HoldingEvent,
            self.matched,
            {"n": lambda s: HoldingEvent.objects.count()},
            self.analyst,
        )
        self.assertEqual(values, {})

    def test_lazy_queryset_evaluated_in_sandbox(self):
        HoldingEvent.objects.create(
            status="foreign", brokerage=self.brokerage_b, holding=self.holding_a
        )
        values = self._values(
            Holding,
            self.holding_a,
            {
                "all": lambda s: (e.status for e in s.events.all()),
                "lazy": lambda s: s.events.values_list("status", flat=True),
                "everything": lambda s: HoldingEvent.objects.all(),
            },
            self.analyst,
            property_relations={"all": ["events"], "lazy": ["events"]},
        )
        self.assertEqual(values["all"], ["matched"])
        self.assertNotIn("lazy", values)
        self.assertNotIn("everything", values)


# ---------------------------------------------------------------------------
# unscoped_properties: the explicit opt-out
# ---------------------------------------------------------------------------


class TestUnscopedProperties(_SandboxFixture):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.analyst)

    def test_opt_out_runs_on_full_instance(self):
        base = HoldingEvent.turbodrf()
        config = {
            **base,
            "property_relations": {},
            "unscoped_properties": ["holding_label"],
        }
        with mock.patch.object(
            HoldingEvent, "turbodrf", classmethod(lambda cls: config)
        ):
            listed = self._row(self.client.get(EVENTS_URL), self.cross.pk)
            detail = self.client.get(f"{EVENTS_URL}{self.cross.pk}/").data
        self.assertEqual(listed["holding_label"], "BOLT")
        self.assertEqual(detail["holding_label"], "BOLT")


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


class TestPropertyConfigValidation(_SandboxFixture):
    def _check(self, **config):
        with _patched_config(HoldingEvent, **config):
            validate_property_config(HoldingEvent)

    def test_valid(self):
        self._check(
            property_relations={
                "holding_label": ["holding", "holding__portfolio__name"]
            },
            unscoped_properties=["status_label"],
        )

    def test_invalid(self):
        bad = [
            {"property_relations": ["holding"]},
            {"property_relations": {"holding_label": "holding"}},
            {"property_relations": {"nope": ["holding"]}},
            {"property_relations": {"status": ["holding"]}},
            {"property_relations": {"holding_label": ["status__x"]}},
            {"property_relations": {"holding_label": ["holding__nope"]}},
            {"property_relations": {"holding_label": ["status"]}},
            {"unscoped_properties": "holding_label"},
            {"unscoped_properties": ["status"]},
            {
                "property_relations": {"holding_label": ["holding"]},
                "unscoped_properties": ["holding_label"],
            },
        ]
        for config in bad:
            with self.subTest(config=str(config)):
                with self.assertRaises(ImproperlyConfigured):
                    self._check(**config)
