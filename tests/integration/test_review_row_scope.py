"""Security review (0.6.0): row-level access control and the query surface.

Each class is a finding a review reproduced on 0.5.2, asserted fixed.

Fixture (brokerage = tenant):
  * a1, a2 — tenant A, role ``scoper`` (read on every Scope* model). a1 owns
    ``folder_priv`` (ScopeFolder is owner-only: a2 cannot see it).
  * mgr_a — tenant A, roles ``manager`` + ``scoper`` (``manager`` is the
    Owner bypass role on ScopeBoard / ScopeLoose).
  * b1 — tenant B.
"""

import datetime
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.fixtures import router_apps
from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    Brokerage,
    Holding,
    HoldingEvent,
    ScopeBoard,
    ScopeEntry,
    ScopeFolder,
    ScopeItem,
    ScopeLedger,
    ScopeLoose,
    ScopeMultiOwner,
    ScopeTag,
    ScopeTagged,
)

User = get_user_model()

_SCOPE_PERMS = [
    "test_app.scopefolder.read",
    "test_app.scopeitem.read",
    "test_app.scopetag.read",
    "test_app.scopetagged.read",
    "test_app.scopeboard.read",
    "test_app.scopeloose.read",
    "test_app.scopeentry.read",
    "test_app.scopeentry.create",
    "test_app.scopeentry.update",
    "test_app.scopemultiowner.read",
    "test_app.scopemultiowner.create",
    "test_app.scopemultiowner.update",
]

SCOPE_ROLES = {
    **settings.TURBODRF_ROLES,
    "scoper": list(_SCOPE_PERMS),
    # Can read ScopeItem but has NO permission on ScopeFolder at all.
    "item_only": ["test_app.scopeitem.read"],
    "manager": list(settings.TURBODRF_ROLES["manager"]) + _SCOPE_PERMS,
}


@override_settings(TURBODRF_ROLES=SCOPE_ROLES)
class _Fixture(TestCase):
    def setUp(self):
        import tests.urls  # noqa: F401  (router registers tenancy/predicates)

        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()

        self.A = Brokerage.objects.create(name="A")
        self.B = Brokerage.objects.create(name="B")
        self.a1 = self._user("a1", ["scoper"], self.A)
        self.a2 = self._user("a2", ["scoper"], self.A)
        self.mgr_a = self._user("mgr_a", ["manager", "scoper"], self.A)
        self.b1 = self._user("b1", ["scoper"], self.B)

        self.folder_priv = ScopeFolder.objects.create(
            name="A1-private-plan",
            date=datetime.date(2024, 3, 15),
            brokerage=self.A,
            owner=self.a1,
        )

    def tearDown(self):
        _test_user_brokerages.clear()

    def _user(self, username, roles, brokerage):
        user = User.objects.create_user(username=username, password="x")
        user._test_roles = roles
        set_test_brokerage(user, brokerage)
        return user

    def _as(self, user):
        cache.clear()
        self.client.force_authenticate(user)

    def _rows(self, resp):
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        return resp.data["data"]


class TestNestedFkValueScopedOnSerializerPath(_Fixture):
    """A nested FK value from a row the caller can't see through the target's
    own endpoint renders as null — on detail and uncompiled lists too."""

    def setUp(self):
        super().setUp()
        self.item = ScopeItem.objects.create(
            label="shared item", brokerage=self.A, folder=self.folder_priv
        )

    def test_a2_cannot_see_folder_via_its_own_endpoint(self):
        self._as(self.a2)
        r = self.client.get(f"/api/scopefolders/{self.folder_priv.pk}/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._rows(self.client.get("/api/scopefolders/")), [])

    def test_uncompiled_list_nulls_target_value(self):
        self._as(self.a2)
        rows = self._rows(self.client.get("/api/scopeitems/"))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["folder_name"])

    def test_detail_nulls_target_value(self):
        self._as(self.a2)
        r = self.client.get(f"/api/scopeitems/{self.item.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data["folder_name"])

    def test_owner_still_sees_it(self):
        self._as(self.a1)
        r = self.client.get(f"/api/scopeitems/{self.item.pk}/")
        self.assertEqual(r.data["folder_name"], "A1-private-plan")


class TestCrossTenantNestedValueOnEveryPath(TestCase):
    """A tenant-A HoldingEvent whose FK points at tenant B's Holding: the row
    is A's, so it's listed; B's value is null on every path."""

    def setUp(self):
        import tests.urls  # noqa: F401

        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()
        self.A = Brokerage.objects.create(name="A")
        self.B = Brokerage.objects.create(name="B")
        holding_b = Holding.objects.create(name="Bolt-B-secret", brokerage=self.B)
        self.cross = HoldingEvent.objects.create(
            status="x", brokerage=self.A, holding=holding_b
        )
        user = User.objects.create_user(username="an", password="x")
        user._test_roles = ["analyst"]
        set_test_brokerage(user, self.A)
        self.client.force_authenticate(user)

    def tearDown(self):
        _test_user_brokerages.clear()

    def test_compiled_list_keeps_row_and_nulls_value(self):
        r = self.client.get("/api/holdingevents/")
        self.assertEqual([row["id"] for row in r.data["data"]], [self.cross.pk])
        self.assertIsNone(r.data["data"][0]["holding_name"])

    def test_detail_nulls_value(self):
        r = self.client.get(f"/api/holdingevents/{self.cross.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data["holding_name"])

    def test_browsable_list_does_not_render_value(self):
        r = self.client.get("/api/holdingevents/?format=api")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Bolt-B-secret", r.content)


class TestLookupNamedRelatedFieldIsScoped(_Fixture):
    """``?folder__date=`` — a related field named like a lookup — goes through
    the same scoping and permission checks as any nested filter. (A nested
    filter needs its path configured: ScopeItem lists ``folder__date`` here.)"""

    def setUp(self):
        super().setUp()
        ScopeItem.objects.create(label="i", brokerage=self.A, folder=self.folder_priv)
        config = dict(ScopeItem.turbodrf())
        config["fields"] = [*config["fields"], "folder__date"]
        patcher = patch.object(
            ScopeItem, "turbodrf", classmethod(lambda cls: dict(config))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_scoped_baselines(self):
        self._as(self.a2)
        for query in (
            "folder__name=A1-private-plan",
            "folder__date__gte=2000-01-01",
        ):
            self.assertEqual(
                self._rows(self.client.get(f"/api/scopeitems/?{query}")), []
            )

    def test_exact_date_filter_is_no_oracle(self):
        self._as(self.a2)
        for query in (
            "folder__date=2024-03-15",
            "folder__date=2024-03-16",
            "folder__date_or=2024-03-15",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    self._rows(self.client.get(f"/api/scopeitems/?{query}")), []
                )

    def test_needs_permission_on_target(self):
        # item_only has NO permission on ScopeFolder: the filter is dropped, so
        # the result can't depend on the hidden value.
        self._as(self._user("io", ["item_only"], self.A))
        hit = self._rows(self.client.get("/api/scopeitems/?folder__date=2024-03-15"))
        miss = self._rows(self.client.get("/api/scopeitems/?folder__date=2024-03-16"))
        self.assertEqual(hit, miss)


class TestM2MSubFieldAcrossFurtherHop(_Fixture):
    """``tags__folder__name``: the second hop (ScopeFolder, owner-only) is
    scoped too — refused at boot when compiled, null at request time."""

    def setUp(self):
        super().setUp()
        tag = ScopeTag.objects.create(
            name="t", brokerage=self.A, folder=self.folder_priv
        )
        self.tagged = ScopeTagged.objects.create(title="x", brokerage=self.A)
        self.tagged.tags.add(tag)

    def test_serializer_path_nulls_hidden_hop(self):
        self._as(self.a2)
        rows = self._rows(self.client.get("/api/scopetaggeds/"))
        self.assertEqual(rows[0]["tags"], [{"name": "t", "folder__name": None}])

    def test_compiled_path_nulls_hidden_hop(self):
        from turbodrf import compiler

        config = {**ScopeTagged.turbodrf(), "compiled": True}
        with patch.object(ScopeTagged, "turbodrf", classmethod(lambda cls: config)):
            plan = compiler.compile_model(ScopeTagged)
        saved = compiler._compiled_plans.pop(ScopeTagged, None)
        compiler.register_compiled_plan(ScopeTagged, plan)
        try:
            self._as(self.a2)
            rows = self._rows(self.client.get("/api/scopetaggeds/"))
            self._as(self.a1)
            owner_rows = self._rows(self.client.get("/api/scopetaggeds/"))
        finally:
            compiler._compiled_plans.pop(ScopeTagged, None)
            if saved is not None:
                compiler._compiled_plans[ScopeTagged] = saved
        self.assertEqual(rows[0]["tags"], [{"name": "t", "folder__name": None}])
        self.assertEqual(owner_rows[0]["tags"][0]["folder__name"], "A1-private-plan")

    def test_boot_gate_refuses_compiled_config(self):
        from turbodrf import compiler

        config = {**ScopeTagged.turbodrf(), "compiled": True}
        with patch.object(ScopeTagged, "turbodrf", classmethod(lambda cls: config)):
            plan = compiler.compile_model(ScopeTagged)
        saved = compiler._compiled_plans.pop(ScopeTagged, None)
        compiler.register_compiled_plan(ScopeTagged, plan)
        try:
            with self.assertRaises(ImproperlyConfigured) as ctx:
                compiler.validate_compiled_path_safety(ScopeTagged)
            self.assertIn("ScopeFolder", str(ctx.exception))
        finally:
            compiler._compiled_plans.pop(ScopeTagged, None)
            if saved is not None:
                compiler._compiled_plans[ScopeTagged] = saved


class TestEitherComposition(_Fixture):
    """Either(Owner(bypass), Members): the bypass role sees every tenant row;
    an M2M predicate doesn't duplicate rows or 500 detail."""

    def setUp(self):
        super().setUp()
        self.board_watched = ScopeBoard.objects.create(
            title="watched", brokerage=self.A, owner=self.a1
        )
        self.board_watched.watchers.add(self.a2, self.b1)
        self.board_plain = ScopeBoard.objects.create(
            title="plain", brokerage=self.A, owner=self.a1
        )

    def test_bypass_child_makes_either_unrestricted(self):
        from turbodrf.predicates import Either, Owner

        class Req:
            user = None

        req = Req()
        req.user = self.mgr_a
        q = Either(Owner("owner", bypass=["manager"]), Owner("watchers")).q(
            req, {"manager"}
        )
        self.assertEqual(q, Q())

    def test_bypass_role_sees_all_tenant_rows(self):
        self._as(self.mgr_a)
        ids = [r["id"] for r in self._rows(self.client.get("/api/scopeboards/"))]
        self.assertEqual(sorted(ids), [self.board_watched.pk, self.board_plain.pk])

    def test_owner_sees_each_row_once(self):
        self._as(self.a1)
        resp = self.client.get("/api/scopeboards/")
        ids = [r["id"] for r in self._rows(resp)]
        self.assertEqual(sorted(ids), [self.board_watched.pk, self.board_plain.pk])
        self.assertEqual(resp.data["pagination"]["total_items"], 2)

    def test_owner_detail(self):
        self._as(self.a1)
        r = self.client.get(f"/api/scopeboards/{self.board_watched.pk}/")
        self.assertEqual(r.status_code, 200)

    def test_watcher_sees_watched_board_only(self):
        self._as(self.a2)
        ids = [r["id"] for r in self._rows(self.client.get("/api/scopeboards/"))]
        self.assertEqual(ids, [self.board_watched.pk])


@override_settings(
    TURBODRF_ROLES=SCOPE_ROLES,
    TURBODRF_TENANT_MODEL="test_app.Brokerage",
    TURBODRF_REQUIRE_TENANCY=True,
)
class TestTenancyGate(_Fixture):
    """TURBODRF_REQUIRE_TENANCY accepts only a tenant_field, a non-empty
    visibility or tenancy: 'shared'."""

    def _build(self, cfg):
        """Build a fresh router that only sees ScopeLoose with config `cfg`;
        restore the global registries afterwards."""
        from turbodrf import compiler, predicates
        from turbodrf.router import TurboDRFRouter

        saved = (
            dict(predicates._model_predicates),
            dict(predicates._model_tenant_fields),
            dict(compiler._compiled_plans),
        )
        self.addCleanup(self._restore, saved)
        p1 = patch("turbodrf.router.apps", router_apps([ScopeLoose]))
        p2 = patch("turbodrf.validation.logger")  # quiet
        p3 = patch.object(ScopeLoose, "turbodrf", classmethod(lambda cls: cfg))
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)
        router = TurboDRFRouter()
        return router.registry[0][1]

    @staticmethod
    def _restore(saved):
        from turbodrf import compiler, predicates

        predicates._model_predicates.clear()
        predicates._model_predicates.update(saved[0])
        predicates._model_tenant_fields.clear()
        predicates._model_tenant_fields.update(saved[1])
        compiler._compiled_plans.clear()
        compiler._compiled_plans.update(saved[2])

    def test_no_declaration_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            self._build({"fields": ["id", "title"], "compiled": False})

    def test_any_other_tenancy_value_raises(self):
        for value in ("isolated", "tenant", "required", "shared "):
            with self.subTest(tenancy=value), self.assertRaises(ImproperlyConfigured):
                self._build({"tenancy": value, "fields": ["id"], "compiled": False})

    def test_shared_with_scoping_keys_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            self._build(
                {"tenancy": "shared", "tenant_field": "brokerage", "fields": ["id"]}
            )

    def test_empty_visibility_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            self._build({"visibility": [], "fields": ["id"], "compiled": False})

    def test_bare_owner_field_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            self._build(
                {
                    "owner_field": "owner",
                    "bypass_owner_roles": ["manager"],
                    "fields": ["id", "title"],
                    "compiled": False,
                }
            )

    def test_tenant_field_with_owner_is_scoped(self):
        vs = self._build(
            {
                "tenant_field": "brokerage",
                "owner_field": "owner",
                "bypass_owner_roles": ["manager"],
                "fields": ["id", "title"],
                "compiled": False,
            }
        )
        self.assertEqual(vs._tenant_field, "brokerage")


class TestChainedTenantWrites(_Fixture):
    """tenant_field='ledger__firm' through a plain model: a write can't
    attach the row to another tenant's ledger."""

    def setUp(self):
        super().setUp()
        self.ledger_a = ScopeLedger.objects.create(name="LA", firm=self.A)
        self.ledger_b = ScopeLedger.objects.create(name="LB", firm=self.B)

    def test_create_into_own_tenant(self):
        self._as(self.a2)
        r = self.client.post(
            "/api/scopeentrys/",
            {"memo": "mine", "ledger": self.ledger_a.pk},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)

    def test_create_into_other_tenant_rejected(self):
        self._as(self.a2)
        r = self.client.post(
            "/api/scopeentrys/",
            {"memo": "injected-by-A", "ledger": self.ledger_b.pk},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(ScopeEntry.objects.filter(memo="injected-by-A").exists())

    def test_patch_cannot_move_row_to_other_tenant(self):
        entry = ScopeEntry.objects.create(memo="mine", ledger=self.ledger_a)
        self._as(self.a2)
        r = self.client.patch(
            f"/api/scopeentrys/{entry.pk}/", {"ledger": self.ledger_b.pk}, format="json"
        )
        self.assertEqual(r.status_code, 400, r.content)
        entry.refresh_from_db()
        self.assertEqual(entry.ledger_id, self.ledger_a.pk)


class TestSearchOverMultiValuedPath(_Fixture):
    """Search scopes a reverse-relation path inside its own JOIN: a visible
    sibling can't let the term match a hidden related row."""

    def setUp(self):
        super().setUp()
        self.folder = ScopeFolder.objects.create(
            name="Ops", brokerage=self.A, owner=self.a1
        )
        # A tenant-B row linked to a tenant-A folder.
        ScopeItem.objects.create(
            label="bsecret-zq", brokerage=self.B, folder=self.folder
        )

    def test_no_match_without_a_visible_sibling(self):
        self._as(self.a1)
        self.assertEqual(
            self._rows(self.client.get("/api/scopefolders/?search=bsecret")), []
        )

    def test_no_match_with_a_visible_sibling(self):
        ScopeItem.objects.create(label="visible", brokerage=self.A, folder=self.folder)
        self._as(self.a1)
        self.assertEqual(
            self._rows(self.client.get("/api/scopefolders/?search=bsecret")), []
        )
        rows = self._rows(self.client.get("/api/scopefolders/?search=visible"))
        self.assertEqual([r["id"] for r in rows], [self.folder.pk])


class TestOrderingByFk(_Fixture):
    """?ordering=<fk> orders by the stored id, not the hidden target's
    Meta.ordering."""

    def test_order_does_not_follow_hidden_target_values(self):
        zzz = ScopeFolder.objects.create(
            name="zzz-hidden", brokerage=self.A, owner=self.a1
        )
        aaa = ScopeFolder.objects.create(
            name="aaa-hidden", brokerage=self.A, owner=self.a1
        )
        i1 = ScopeItem.objects.create(label="i1", brokerage=self.A, folder=zzz)
        i2 = ScopeItem.objects.create(label="i2", brokerage=self.A, folder=aaa)
        self._as(self.a2)

        def ordered():
            resp = self.client.get("/api/scopeitems/?ordering=folder")
            return [r["id"] for r in self._rows(resp)]

        self.assertEqual(ordered(), [i1.pk, i2.pk])  # by folder id
        ScopeFolder.objects.filter(pk=zzz.pk).update(name="000-hidden")
        self.assertEqual(ordered(), [i1.pk, i2.pk])


class TestFkFilterIsNoExistenceOracle(_Fixture):
    def test_other_tenant_pk_and_nonexistent_pk_look_the_same(self):
        folder_b = ScopeFolder.objects.create(name="B", brokerage=self.B, owner=self.b1)
        self._as(self.a2)
        exists = self.client.get(f"/api/scopeitems/?folder={folder_b.pk}")
        missing = self.client.get("/api/scopeitems/?folder=987654")
        self.assertEqual(exists.status_code, 200)
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(exists.data["data"], missing.data["data"])


class TestMultiOwnerWrites(_Fixture):
    """owner_field=[owner, editor]: a write can't set either column to another
    user unless the caller holds a bypass role."""

    def test_create_assigning_other_user_as_owner_rejected(self):
        self._as(self.a2)
        r = self.client.post(
            "/api/scopemultiowners/",
            {"title": "planted", "owner": self.a1.pk},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(ScopeMultiOwner.objects.exists())

    def test_patch_reassigning_owner_rejected(self):
        row = ScopeMultiOwner.objects.create(
            title="mine", brokerage=self.A, owner=self.a2, editor=self.a2
        )
        self._as(self.a2)
        r = self.client.patch(
            f"/api/scopemultiowners/{row.pk}/", {"owner": self.a1.pk}, format="json"
        )
        self.assertEqual(r.status_code, 400, r.content)
        row.refresh_from_db()
        self.assertEqual(row.owner_id, self.a2.pk)

    def test_own_columns_accepted(self):
        self._as(self.a2)
        r = self.client.post(
            "/api/scopemultiowners/",
            {"title": "ok", "owner": self.a2.pk, "editor": self.a2.pk},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
