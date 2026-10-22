"""Regressions fixed in 0.5.2 (reported from a downstream app on 0.5.1).

1. Traversal scoping dropped rows whose nullable FK is NULL (compiled list,
   filters, search over a nested field).
2. The compiled list left out @property fields for role-scoped users, and
   crashed on properties that read anything outside the .values() row.
3. A nested field ending on an FK rendered the model instance (500 on write).
4. OPTIONS crashed when a model listed an FK and a nested field of it.
5. `read_only: True` also blocked POST custom actions.
6. `permission_classes` declared on a custom action were ignored.
7. `full_clean: True` discarded values normalised by the model's clean().
8. Compiled `?fields=` selecting nothing else rendered EVERY column.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Q
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    Brokerage,
    CompiledSampleModel,
    Gadget,
    Holding,
    HoldingEvent,
    Portfolio,
    RelatedModel,
    Widget,
)

User = get_user_model()

EVENTS_URL = "/api/holdingevents/"


class _TenantFixture(TestCase):
    """Brokerage A: one event with a holding, two with NO holding, and one
    whose holding belongs to brokerage B. Brokerage B: one event."""

    def setUp(self):
        import tests.urls  # noqa: F401  (router registers tenancy)

        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()

        self.brokerage_a = Brokerage.objects.create(name="A")
        self.brokerage_b = Brokerage.objects.create(name="B")
        self.portfolio_a = Portfolio.objects.create(
            name="Growth", brokerage=self.brokerage_a
        )
        self.holding_a = Holding.objects.create(
            name="Acme", brokerage=self.brokerage_a, portfolio=self.portfolio_a
        )
        self.holding_b = Holding.objects.create(name="Bolt", brokerage=self.brokerage_b)

        self.matched = HoldingEvent.objects.create(
            status="matched", brokerage=self.brokerage_a, holding=self.holding_a
        )
        self.review_1 = HoldingEvent.objects.create(
            status="needs_review", brokerage=self.brokerage_a, holding=None
        )
        self.review_2 = HoldingEvent.objects.create(
            status="needs_review", brokerage=self.brokerage_a, holding=None
        )
        # Same tenant, but its FK points at ANOTHER tenant's holding.
        self.cross = HoldingEvent.objects.create(
            status="needs_review", brokerage=self.brokerage_a, holding=self.holding_b
        )
        self.other_tenant = HoldingEvent.objects.create(
            status="needs_review", brokerage=self.brokerage_b, holding=self.holding_b
        )

        self.analyst = self._user("analyst", ["analyst"], self.brokerage_a)
        self.auditor = self._user("auditor", ["auditor"], self.brokerage_a)

    def tearDown(self):
        _test_user_brokerages.clear()

    def _user(self, username, roles, brokerage):
        user = User.objects.create_user(username=username, password="x")
        user._test_roles = roles
        set_test_brokerage(user, brokerage)
        return user

    def _ids(self, resp):
        self.assertEqual(resp.status_code, 200, resp.content)
        return {row["id"] for row in resp.data["data"]}


# ---------------------------------------------------------------------------
# 1. Rows with a NULL FK survive traversal scoping
# ---------------------------------------------------------------------------


class TestNullableFkRowsKept(_TenantFixture):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.analyst)

    def test_compiled_list_keeps_null_fk_rows(self):
        resp = self.client.get(EVENTS_URL)
        self.assertEqual(
            self._ids(resp),
            {self.matched.pk, self.review_1.pk, self.review_2.pk, self.cross.pk},
        )
        self.assertEqual(resp.data["pagination"]["total_items"], 4)
        row = next(r for r in resp.data["data"] if r["id"] == self.review_1.pk)
        self.assertIsNone(row["holding"])
        self.assertIsNone(row["holding_name"])
        self.assertIsNone(row["holding_portfolio"])

    def test_filter_counts_null_fk_rows(self):
        resp = self.client.get(EVENTS_URL, {"status": "needs_review"})
        self.assertEqual(
            self._ids(resp), {self.review_1.pk, self.review_2.pk, self.cross.pk}
        )

    def test_isnull_filter_matches_null_fk_rows(self):
        resp = self.client.get(EVENTS_URL, {"holding__isnull": "true"})
        self.assertEqual(self._ids(resp), {self.review_1.pk, self.review_2.pk})

    def test_search_over_nested_field_keeps_null_fk_rows(self):
        resp = self.client.get(EVENTS_URL, {"search": "needs"})
        self.assertEqual(
            self._ids(resp), {self.review_1.pk, self.review_2.pk, self.cross.pk}
        )

    def test_search_matching_nested_field(self):
        resp = self.client.get(EVENTS_URL, {"search": "acme"})
        self.assertEqual(self._ids(resp), {self.matched.pk})

    def test_ordering_keeps_null_fk_rows(self):
        resp = self.client.get(EVENTS_URL, {"ordering": "holding"})
        self.assertEqual(
            self._ids(resp),
            {self.matched.pk, self.review_1.pk, self.review_2.pk, self.cross.pk},
        )

    def test_non_compiled_path_keeps_null_fk_rows(self):
        from unittest.mock import patch

        from turbodrf.views import TurboDRFViewSet

        with patch.object(
            TurboDRFViewSet, "_should_use_compiled_path", return_value=False
        ):
            resp = self.client.get(EVENTS_URL, {"search": "needs"})
        self.assertEqual(
            self._ids(resp), {self.review_1.pk, self.review_2.pk, self.cross.pk}
        )

    # -- guards: another tenant's target stays hidden ----------------------

    def test_guard_other_tenants_target_renders_null(self):
        # The row is in the caller's tenant, so it's listed; its FK points at
        # brokerage B's holding, so the nested values render as null.
        resp = self.client.get(EVENTS_URL)
        rows = {row["id"]: row for row in resp.data["data"]}
        self.assertNotIn(self.other_tenant.pk, rows)
        self.assertIsNone(rows[self.cross.pk]["holding_name"])
        self.assertIsNone(rows[self.cross.pk]["holding_portfolio"])
        detail = self.client.get(f"{EVENTS_URL}{self.cross.pk}/").data
        self.assertIsNone(detail["holding_name"])

    def test_guard_fk_to_other_tenant_target_hidden_in_filters(self):
        # Filtering or searching on the nested path can't match it.
        for params in (
            {"search": "bolt"},
            {"holding__name": "Bolt"},
        ):
            with self.subTest(params=params):
                ids = self._ids(self.client.get(EVENTS_URL, params))
                self.assertNotIn(self.cross.pk, ids)
                self.assertNotIn(self.other_tenant.pk, ids)


class TestTraversalScopeQ(_TenantFixture):
    """build_traversal_scope_q directly, including a two-hop path whose first
    hop is the NULL one."""

    def _request(self, user):
        from unittest.mock import Mock

        request = Mock()
        request.user = user
        return request

    def _scoped_ids(self, path, user):
        from turbodrf.validation import build_traversal_scope_q

        q = build_traversal_scope_q(HoldingEvent, path, self._request(user))
        self.assertNotEqual(q, Q())
        qs = HoldingEvent.objects.filter(brokerage=self.brokerage_a).filter(q)
        return set(qs.values_list("pk", flat=True))

    def test_one_hop(self):
        self.assertEqual(
            self._scoped_ids("holding__name", self.analyst),
            {self.matched.pk, self.review_1.pk, self.review_2.pk},
        )

    def test_two_hops_null_first_hop(self):
        # review_* have no holding at all, so holding__portfolio is NULL too.
        self.assertEqual(
            self._scoped_ids("holding__portfolio__name", self.analyst),
            {self.matched.pk, self.review_1.pk, self.review_2.pk},
        )

    def test_two_hops_null_second_hop(self):
        self.holding_a.portfolio = None
        self.holding_a.save()
        self.assertEqual(
            self._scoped_ids("holding__portfolio__name", self.analyst),
            {self.matched.pk, self.review_1.pk, self.review_2.pk},
        )

    def test_fail_closed_keeps_only_null_fk_rows(self):
        # No resolvable tenant: every target is out of bounds, but a row with
        # no target reveals nothing.
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(
            self._scoped_ids("holding__name", AnonymousUser()),
            {self.review_1.pk, self.review_2.pk},
        )


# ---------------------------------------------------------------------------
# 2. @property fields on the compiled list
# ---------------------------------------------------------------------------


class TestCompiledPropertyFields(_TenantFixture):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.analyst)

    def _row(self, resp, pk):
        return next(r for r in resp.data["data"] if r["id"] == pk)

    def test_role_user_gets_properties_on_compiled_list(self):
        resp = self.client.get(EVENTS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self._row(resp, self.review_1.pk)["status_label"], "Needs Review"
        )

    def test_property_reading_related_object(self):
        # holding_label reads self.holding — not in the .values() row, so it is
        # computed on the instance instead of raising.
        resp = self.client.get(EVENTS_URL)
        self.assertEqual(self._row(resp, self.matched.pk)["holding_label"], "ACME")
        self.assertIsNone(self._row(resp, self.review_1.pk)["holding_label"])

    def test_list_and_detail_render_same_keys(self):
        list_row = self._row(self.client.get(EVENTS_URL), self.matched.pk)
        detail = self.client.get(f"{EVENTS_URL}{self.matched.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(set(list_row), set(detail.data))
        for key in ("status_label", "holding_label", "holding_portfolio"):
            self.assertEqual(list_row[key], detail.data[key], key)

    def test_property_sees_model_value_types(self):
        # Properties run before Decimal -> str coercion, as on an instance.
        from tests.test_app.models import CompiledSampleModel as M
        from turbodrf.compiler import compile_model

        related = RelatedModel.objects.create(name="r")
        M.objects.create(title="t", price="2.50", related=related)
        plan = compile_model(M)
        plan.property_fields["doubled"] = lambda self: self.price * 2
        qs, active = plan.apply_to_queryset(M.objects.all())
        rows = plan.post_process(list(qs), active)
        self.assertEqual(str(rows[0]["doubled"]), "5.00")
        self.assertEqual(rows[0]["price"], "2.50")


# ---------------------------------------------------------------------------
# 8. ?fields= on the compiled list never renders unselected columns
# ---------------------------------------------------------------------------


class TestCompiledClientFieldsSelection(_TenantFixture):
    def test_property_only_selection_role_user(self):
        # auditor cannot read `holding`; a property-only selection used to
        # run .values() with no arguments and dump every column.
        self.client.force_authenticate(self.auditor)
        resp = self.client.get(EVENTS_URL, {"fields": "status_label"})
        self.assertEqual(resp.status_code, 200)
        for row in resp.data["data"]:
            self.assertEqual(set(row), {"status_label"})

    def test_unreadable_selection_role_user(self):
        self.client.force_authenticate(self.auditor)
        resp = self.client.get(EVENTS_URL, {"fields": "holding"})
        self.assertEqual(resp.status_code, 200)
        for row in resp.data["data"]:
            self.assertNotIn("holding", row)
            self.assertNotIn("holding_id", row)
            self.assertNotIn("status", row)

    def test_property_needing_instance_with_selection(self):
        self.client.force_authenticate(self.analyst)
        resp = self.client.get(EVENTS_URL, {"fields": "holding_label"})
        self.assertEqual(resp.status_code, 200)
        # The cross-tenant row's holding is out of scope: the property, run in
        # the caller's sandbox, sees None.
        labels = {tuple(row.items()) for row in resp.data["data"]}
        self.assertEqual(
            labels, {(("holding_label", "ACME"),), (("holding_label", None),)}
        )
        self.assertNotIn("BOLT", str(resp.data))

    def test_property_only_selection_anonymous(self):
        related = RelatedModel.objects.create(name="r")
        CompiledSampleModel.objects.create(title="t", price="1.00", related=related)
        resp = self.client.get(
            "/api/compiledsamplemodels/", {"fields": "display_title"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["data"], [{"display_title": "T"}])


# ---------------------------------------------------------------------------
# 3. Nested field ending on an FK renders the pk
# ---------------------------------------------------------------------------


class TestFkTerminalNestedField(_TenantFixture):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.analyst)

    def test_detail_renders_pk(self):
        resp = self.client.get(f"{EVENTS_URL}{self.matched.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["holding_portfolio"], self.portfolio_a.pk)

    def test_detail_null_renders_none(self):
        resp = self.client.get(f"{EVENTS_URL}{self.review_1.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["holding_portfolio"])

    def test_create_response_renders_pk(self):
        resp = self.client.post(
            EVENTS_URL, {"status": "new", "holding": self.holding_a.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["holding_portfolio"], self.portfolio_a.pk)

    def test_update_response_renders_pk(self):
        resp = self.client.patch(
            f"{EVENTS_URL}{self.review_1.pk}/",
            {"holding": self.holding_a.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["holding_portfolio"], self.portfolio_a.pk)

    def test_compiled_list_renders_pk(self):
        resp = self.client.get(EVENTS_URL)
        row = next(r for r in resp.data["data"] if r["id"] == self.matched.pk)
        self.assertEqual(row["holding_portfolio"], self.portfolio_a.pk)


# ---------------------------------------------------------------------------
# 4. OPTIONS with an FK and a nested field of it
# ---------------------------------------------------------------------------


class TestOptionsFkAndNestedField(_TenantFixture):
    def test_options_returns_200(self):
        self.client.force_authenticate(self.analyst)
        resp = self.client.options(EVENTS_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        holding = resp.data["model"]["fields"]["holding"]
        self.assertEqual(holding["type"], "ForeignKey")
        self.assertEqual(holding["fields"], ["name", "portfolio"])

    def test_field_order_does_not_matter(self):
        from turbodrf.metadata import TurboDRFMetadata

        meta = TurboDRFMetadata()
        for fields in (
            ["holding", "holding__name"],
            ["holding__name", "holding"],
        ):
            with self.subTest(fields=fields):
                out = meta._get_field_metadata(HoldingEvent, fields, None, None)
                self.assertEqual(out["holding"]["type"], "ForeignKey")
                self.assertEqual(out["holding"]["fields"], ["name"])
                self.assertIn("required", out["holding"])

    def test_nested_only_entry(self):
        from turbodrf.metadata import TurboDRFMetadata

        out = TurboDRFMetadata()._get_field_metadata(
            HoldingEvent, ["holding__name"], None, None
        )
        self.assertEqual(out["holding"], {"type": "nested", "fields": ["name"]})


# ---------------------------------------------------------------------------
# 5 + 6. read_only resources with POST actions; action permission_classes
# ---------------------------------------------------------------------------


class TestReadOnlyActions(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.widget = Widget.objects.create(name="gizmo")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_post_action_routes_on_read_only_resource(self):
        resp = self.client.post(f"/api/widgets/{self.widget.pk}/bump/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["bumped"], "gizmo")

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_guard_crud_writes_still_refused(self):
        detail = f"/api/widgets/{self.widget.pk}/"
        self.assertEqual(
            self.client.post("/api/widgets/", {"name": "x"}, format="json").status_code,
            405,
        )
        self.assertEqual(
            self.client.put(detail, {"name": "x"}, format="json").status_code, 405
        )
        self.assertEqual(
            self.client.patch(detail, {"name": "x"}, format="json").status_code, 405
        )
        self.assertEqual(self.client.delete(detail).status_code, 405)
        self.assertEqual(Widget.objects.get().name, "gizmo")
        self.assertEqual(Widget.objects.count(), 1)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_guard_get_action_rejects_post(self):
        resp = self.client.post(f"/api/widgets/{self.widget.pk}/ping/")
        self.assertEqual(resp.status_code, 405)

    def test_guard_viewset_refuses_crud_write_actions(self):
        # Defence in depth if the viewset is routed without the TurboDRF router.
        from rest_framework.test import APIRequestFactory

        from tests.urls import router

        viewset = next(vs for _, vs, _ in router.registry if vs.model is Widget)
        view = viewset.as_view({"post": "create"})
        with override_settings(TURBODRF_DISABLE_PERMISSIONS=True):
            resp = view(APIRequestFactory().post("/x/", {"name": "x"}, format="json"))
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(Widget.objects.count(), 1)


class TestActionPermissionClasses(_TenantFixture):
    def setUp(self):
        super().setUp()
        self.widget = Widget.objects.create(name="gizmo")

    def test_declared_allow_class_allows(self):
        # Anonymous POST: TurboDRF's role check would refuse; AllowAny allows.
        resp = self.client.post(f"/api/widgets/{self.widget.pk}/bump/")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_declared_deny_class_denies(self):
        # Public GET: TurboDRF's role check would allow; DenyAll denies.
        resp = self.client.get(f"/api/widgets/{self.widget.pk}/locked/")
        self.assertEqual(resp.status_code, 403)

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_declared_classes_apply_with_permissions_disabled(self):
        resp = self.client.get(f"/api/widgets/{self.widget.pk}/locked/")
        self.assertEqual(resp.status_code, 403)

    def test_declared_classes_replace_role_check(self):
        # auditor has no create perm, so the role check would refuse a POST;
        # the action declares IsAuthenticated.
        self.client.force_authenticate(self.auditor)
        resp = self.client.post(f"{EVENTS_URL}{self.review_1.pk}/reprocess/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["reprocessed"], self.review_1.pk)

    def test_declared_classes_still_enforced(self):
        resp = self.client.post(f"{EVENTS_URL}{self.review_1.pk}/reprocess/")
        self.assertIn(resp.status_code, (401, 403))

    def test_get_object_still_tenant_scoped(self):
        self.client.force_authenticate(self.auditor)
        resp = self.client.post(f"{EVENTS_URL}{self.other_tenant.pk}/reprocess/")
        self.assertEqual(resp.status_code, 404)

    def test_crud_routes_keep_role_check(self):
        self.client.force_authenticate(self.auditor)
        resp = self.client.post(EVENTS_URL, {"status": "new"}, format="json")
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# 7. full_clean persists clean()'s normalised values
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
class TestFullCleanNormalisation(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_create_persists_normalised_value(self):
        resp = self.client.post(
            "/api/gadgets/", {"name": "  Wrench ", "qty": 1}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["name"], "wrench")
        self.assertEqual(Gadget.objects.get().name, "wrench")

    def test_patch_persists_normalised_value(self):
        gadget = Gadget.objects.create(name="old", qty=1)
        resp = self.client.patch(
            f"/api/gadgets/{gadget.pk}/", {"name": "HAMMER"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        gadget.refresh_from_db()
        self.assertEqual(gadget.name, "hammer")
        self.assertEqual(gadget.qty, 1)

    def test_failed_validation_leaves_instance_untouched(self):
        from turbodrf.serializers import TurboDRFSerializer

        class GadgetSerializer(TurboDRFSerializer):
            class Meta:
                model = Gadget
                fields = ["name", "qty"]

        gadget = Gadget.objects.create(name="old", qty=1)
        serializer = GadgetSerializer(
            gadget, data={"name": "forbidden", "qty": 9}, partial=True
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(gadget.name, "old")
        self.assertEqual(gadget.qty, 1)
