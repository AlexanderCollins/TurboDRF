"""Security review (0.6.0): what the docs promise, asserted against the code.

A documentation audit of 0.5.2 found these claims false; each is now true.
"""

from contextlib import contextmanager
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db.models import Q
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from tests.test_app.models import (
    Brokerage,
    Deal,
    Holding,
    HoldingEvent,
    RelatedModel,
    SampleModel,
)

User = get_user_model()

TENANT_SETTINGS = dict(
    TURBODRF_TENANT_MODEL="test_app.Brokerage",
    TURBODRF_REQUIRE_TENANCY=True,
)


@contextmanager
def _preserve_registries():
    """Router init / registry patches mutate module-level state shared by the
    whole worker. Snapshot and restore it."""
    import turbodrf.router as router_mod
    from turbodrf import compiler, predicates

    preds = dict(predicates._model_predicates)
    tenants = dict(predicates._model_tenant_fields)
    plans = dict(compiler._compiled_plans)
    flag = router_mod._bypass_roles_validated
    try:
        yield
    finally:
        predicates._model_predicates.clear()
        predicates._model_predicates.update(preds)
        predicates._model_tenant_fields.clear()
        predicates._model_tenant_fields.update(tenants)
        compiler._compiled_plans.clear()
        compiler._compiled_plans.update(plans)
        router_mod._bypass_roles_validated = flag


def _cfg(config):
    return classmethod(lambda cls: dict(config))


def _rows(response):
    data = response.data
    if isinstance(data, dict):
        return data.get("data") or data.get("results") or []
    return data


class _TwoTenants(TestCase):
    def setUp(self):
        from tests.test_app.apps import _test_user_brokerages

        cache.clear()
        _test_user_brokerages.clear()
        self.b_a = Brokerage.objects.create(name="A")
        self.b_b = Brokerage.objects.create(name="B")
        self.under_a = self._user("under_a", ["underwriter"], self.b_a)
        self.manager_a = self._user("manager_a", ["manager"], self.b_a)
        self.under_b = self._user("under_b", ["underwriter"], self.b_b)
        self.deal_a = Deal.objects.create(
            title="A deal", brokerage=self.b_a, assigned_broker=self.under_a
        )
        self.deal_b = Deal.objects.create(
            title="B deal", brokerage=self.b_b, assigned_broker=self.under_b
        )

    def _user(self, name, roles, brokerage):
        from tests.test_app.apps import set_test_brokerage

        u = User.objects.create_user(username=name, password="x")
        u._test_roles = roles
        set_test_brokerage(u, brokerage)
        return u

    def _client(self, user, **kw):
        c = APIClient(**kw)
        c.force_authenticate(user=user)
        return c


# ---------------------------------------------------------------------------
# 1. Owner auto-fill runs on UPDATE: a bypass-role PATCH steals ownership.
#    Docs: tenancy.md "Auto-fills owner FK on create".
# ---------------------------------------------------------------------------
class TestOwnerAutofillOnUpdate(_TwoTenants):
    def test_manager_patch_does_not_reassign_owner(self):
        r = self._client(self.manager_a).patch(
            f"/api/deals/{self.deal_a.pk}/", {"title": "renamed"}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.deal_a.refresh_from_db()
        self.assertEqual(self.deal_a.title, "renamed")
        # Docs: owner is only auto-filled on create.
        self.assertEqual(self.deal_a.assigned_broker_id, self.under_a.pk)


# ---------------------------------------------------------------------------
# 2. Database permission mode ignores field rules defined on OTHER roles.
#    Docs: permissions.md "If ANY role defines an explicit field rule ...
#    that field requires explicit permission for ALL roles".
# ---------------------------------------------------------------------------
class TestDatabaseModeFieldRuleScope(TestCase):
    def setUp(self):
        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        cache.clear()
        admin = TurboDRFRole.objects.create(name="dbadmin")
        viewer = TurboDRFRole.objects.create(name="dbviewer")
        for role in (admin, viewer):
            RolePermission.objects.create(
                role=role,
                app_label="test_app",
                model_name="samplemodel",
                action="read",
            )
        RolePermission.objects.create(
            role=admin,
            app_label="test_app",
            model_name="samplemodel",
            field_name="secret_field",
            permission_type="read",
        )
        self.viewer = User.objects.create_user(username="dbv")
        UserRole.objects.create(user=self.viewer, role=viewer)
        self.item = SampleModel.objects.create(
            title="t",
            price=Decimal("1.00"),
            related=RelatedModel.objects.create(name="r"),
            secret_field="TOP-SECRET",
        )

    @override_settings(
        TURBODRF_ROLES={
            "dbadmin": [
                "test_app.samplemodel.read",
                "test_app.samplemodel.secret_field.read",
            ],
            "dbviewer": ["test_app.samplemodel.read"],
        }
    )
    def test_static_mode_control(self):
        """Control: static mode hides secret_field from dbviewer."""
        from turbodrf.backends import build_permission_snapshot

        self.viewer._test_roles = ["dbviewer"]
        snap = build_permission_snapshot(self.viewer, SampleModel, use_cache=False)
        self.assertNotIn("secret_field", snap.readable_fields)

    @override_settings(TURBODRF_PERMISSION_MODE="database")
    def test_database_mode_hides_field_ruled_by_other_role(self):
        r = self._client().get(f"/api/samplemodels/{self.item.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("secret_field", r.data)

    def _client(self):
        c = APIClient()
        c.force_authenticate(user=self.viewer)
        return c


# ---------------------------------------------------------------------------
# 3. TURBODRF_REQUIRE_TENANCY hard-fail is satisfied by owner_field alone or
#    by ANY 'tenancy' value. Docs: only tenant_field / visibility /
#    'tenancy': 'shared' count.
# ---------------------------------------------------------------------------
class TestRequireTenancyBypass(_TwoTenants):
    FIELDS = ["id", "title", "brokerage", "assigned_broker"]

    def _router_with_deal_config(self, config):
        from django.apps import apps

        from turbodrf.router import TurboDRFRouter

        with mock.patch.object(apps, "get_models", return_value=[Deal]):
            return TurboDRFRouter()

    def _list_as(self, router, user):
        viewset = dict((p, v) for p, v, _ in router.registry)["deals"]
        req = APIRequestFactory().get("/api/deals/")
        force_authenticate(req, user=user)
        resp = viewset.as_view({"get": "list"})(req)
        return {row["id"] for row in _rows(resp)}

    @override_settings(**TENANT_SETTINGS)
    def test_owner_field_only_is_refused(self):
        cfg = {
            "owner_field": "assigned_broker",
            "bypass_owner_roles": ["manager"],
            "fields": self.FIELDS,
            "compiled": False,
        }
        with _preserve_registries(), mock.patch.object(Deal, "turbodrf", _cfg(cfg)):
            try:
                router = self._router_with_deal_config(cfg)
            except ImproperlyConfigured:
                return  # documented behaviour
            # Not refused -> show the consequence: manager at A sees B's row.
            seen = self._list_as(router, self.manager_a)
            self.fail(
                f"router accepted owner_field-only model; manager_a sees "
                f"deal_b={self.deal_b.pk in seen} (ids {sorted(seen)})"
            )

    @override_settings(**TENANT_SETTINGS)
    def test_tenancy_typo_is_refused(self):
        cfg = {"tenancy": "shard", "fields": self.FIELDS, "compiled": False}
        with _preserve_registries(), mock.patch.object(Deal, "turbodrf", _cfg(cfg)):
            try:
                router = self._router_with_deal_config(cfg)
            except ImproperlyConfigured:
                return
            seen = self._list_as(router, self.under_a)
            self.fail(
                f"'tenancy': 'shard' accepted; under_a sees deal_b="
                f"{self.deal_b.pk in seen}"
            )

    @override_settings(
        TURBODRF_TENANT_MODEL="test_app.Brokerage", TURBODRF_REQUIRE_TENANCY=False
    )
    def test_turbodrf_check_matches_router_tenancy(self):
        """turbodrf_check must not report a tenant boundary the router won't
        apply (router default TURBODRF_AUTODETECT_TENANT=False)."""
        from io import StringIO

        cfg = {
            "owner_field": "assigned_broker",
            "bypass_owner_roles": ["manager"],
            "fields": self.FIELDS,
            "compiled": False,
        }
        out = StringIO()
        with _preserve_registries(), mock.patch.object(Deal, "turbodrf", _cfg(cfg)):
            router = self._router_with_deal_config(cfg)
            viewset = dict((p, v) for p, v, _ in router.registry)["deals"]
            self.assertIsNone(viewset._tenant_field)  # router: no tenant wall
            call_command("turbodrf_check", "--model", "Deal", stdout=out)
        self.assertNotIn("Tenant field: brokerage", out.getvalue(), out.getvalue())


# ---------------------------------------------------------------------------
# 4. Sensitive deny-list is not applied when no permission snapshot applies
#    and fields='__all__' (the default) — and search ignores it for anon.
#    Docs: security.md "never exposed ... regardless of permissions";
#    settings_reference "ALWAYS stripped from responses, search, ... OPTIONS".
#    'description' stands in for e.g. 'api_key'.
# ---------------------------------------------------------------------------
SENSITIVE = ["password", "token", "api_key", "description"]
ALL_CFG = {
    "public_access": True,
    "fields": "__all__",
    "searchable_fields": ["name", "description"],
}


@override_settings(TURBODRF_SENSITIVE_FIELDS=SENSITIVE)
class TestSensitiveFieldsWithoutSnapshot(TestCase):
    def setUp(self):
        cache.clear()
        self.obj = RelatedModel.objects.create(name="n1", description="sk-live-XYZ")
        RelatedModel.objects.create(name="n2", description="other")

    def test_anonymous_detail_strips_sensitive(self):
        with mock.patch.object(RelatedModel, "turbodrf", _cfg(ALL_CFG)):
            r = APIClient().get(f"/api/relatedmodels/{self.obj.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("description", r.data)

    @override_settings(TURBODRF_USE_DEFAULT_PERMISSIONS=True)
    def test_default_permissions_detail_strips_sensitive(self):
        u = User.objects.create_user(username="dj")
        u.user_permissions.add(Permission.objects.get(codename="view_relatedmodel"))
        u = User.objects.get(pk=u.pk)
        c = APIClient()
        c.force_authenticate(user=u)
        with mock.patch.object(RelatedModel, "turbodrf", _cfg(ALL_CFG)):
            r = c.get(f"/api/relatedmodels/{self.obj.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("description", r.data)

    def test_anonymous_cannot_search_sensitive(self):
        with mock.patch.object(RelatedModel, "turbodrf", _cfg(ALL_CFG)):
            r = APIClient().get("/api/relatedmodels/?search=sk-live")
            miss = APIClient().get("/api/relatedmodels/?search=zz-no-match")
        self.assertEqual(r.status_code, 200)
        # No oracle: the result doesn't depend on whether the term matches.
        self.assertEqual(_rows(r), _rows(miss), "substring oracle on a sensitive field")

    def test_anonymous_options_hides_sensitive(self):
        with mock.patch.object(RelatedModel, "turbodrf", _cfg(ALL_CFG)):
            r = APIClient().options("/api/relatedmodels/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("description", r.data["model"]["fields"])

    @override_settings(TURBODRF_SENSITIVE_FIELDS=["ssn"])
    def test_override_adds_to_defaults(self):
        """settings_reference: 'Override to add app-specific deny entries'."""
        from turbodrf.validation import is_field_path_sensitive

        self.assertTrue(is_field_path_sensitive("ssn"))
        self.assertTrue(is_field_path_sensitive("password"))


# ---------------------------------------------------------------------------
# 5. public_access without a 'guest' role ignores field-level read rules.
#    SampleModel.secret_field / price carry explicit read rules (admin only).
#    Docs: settings_reference 'public_access' "subject to field-level perms
#    via the guest role"; permissions.md "requires explicit permission for
#    ALL roles".
# ---------------------------------------------------------------------------
class TestPublicAccessWithoutGuest(TestCase):
    def setUp(self):
        cache.clear()
        self.item = SampleModel.objects.create(
            title="t",
            price=Decimal("123.45"),
            related=RelatedModel.objects.create(name="r"),
            secret_field="TOP-SECRET",
        )

    def test_anonymous_detail_hides_role_restricted_fields(self):
        r = APIClient().get(f"/api/samplemodels/{self.item.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("secret_field", r.data)
        self.assertNotIn("price", r.data)

    def test_anonymous_cannot_filter_on_role_restricted_field(self):
        hit = APIClient().get("/api/samplemodels/?secret_field__istartswith=TOP")
        miss = APIClient().get("/api/samplemodels/?secret_field__istartswith=NOPE")
        self.assertEqual(len(_rows(hit)), len(_rows(miss)), "filter oracle")


# ---------------------------------------------------------------------------
# 6. AGENTS.md API that doesn't exist.
# ---------------------------------------------------------------------------
class TestAgentsMdClaims(TestCase):
    @override_settings(TURBODRF_ROLES={"admin": ["*"]})
    def test_wildcard_role_is_rejected_at_boot(self):
        """There is no wildcard permission: the boot gate rejects it (and
        AGENTS.md no longer documents one)."""
        from turbodrf.predicates import validate_permission_strings

        with self.assertRaises(ImproperlyConfigured):
            validate_permission_strings()

    def test_invalidate_user_permissions_exists(self):
        from turbodrf.backends import invalidate_user_permissions  # noqa: F401

    def test_model_sensitive_fields_attribute(self):
        """AGENTS.md: sensitive_fields = ['price']  # never serialized to
        anyone w/o read perm. underwriter has deal.read but no title.read."""
        from tests.test_app.apps import set_test_brokerage

        cache.clear()
        b = Brokerage.objects.create(name="A")
        u = User.objects.create_user(username="u")
        u._test_roles = ["underwriter"]
        set_test_brokerage(u, b)
        d = Deal.objects.create(title="T", brokerage=b, assigned_broker=u)
        c = APIClient()
        c.force_authenticate(user=u)
        with mock.patch.object(Deal, "sensitive_fields", ["title"], create=True):
            r = c.get(f"/api/deals/{d.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("title", r.data)


# ---------------------------------------------------------------------------
# 7. Members / Group / Conditional raise NotImplementedError on writes -> 500.
#    AGENTS.md recommends them (incl. inside Either) with no caveat.
# ---------------------------------------------------------------------------
class TestReadOnlyPredicatesOnWritableModel(_TwoTenants):
    def _post(self, predicates, body):
        from turbodrf.predicates import register_predicates

        with _preserve_registries():
            register_predicates(Deal, predicates)
            c = self._client(self.under_a, raise_request_exception=False)
            return c.post("/api/deals/", body, format="json")

    def test_conditional_write_is_not_500(self):
        from turbodrf.predicates import Conditional, Owner

        r = self._post(
            [
                Owner("assigned_broker", bypass=["manager"]),
                Conditional(when=Q(title="vip"), require_roles=["manager"]),
            ],
            {"title": "ordinary"},
        )
        self.assertEqual(r.status_code, 201, r.content)

    def test_either_owner_members_non_owner_write_is_not_500(self):
        """Either runs every child's write check: the Members branch no
        longer overrides Owner's "can't assign the row to another user"."""
        from turbodrf.predicates import Either, Members, Owner

        r = self._post(
            [Either(Owner("assigned_broker"), Members("assigned_broker"))],
            {"title": "x", "assigned_broker": self.manager_a.pk},
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(Deal.objects.filter(title="x").exists())


# ---------------------------------------------------------------------------
# 8. Multi-owner sugar (owner_field=[a, b]) does not stop assigning rows to
#    other users. Docs: "non-bypass roles cannot assign rows to other users".
# ---------------------------------------------------------------------------
class TestMultiOwnerWriteCheck(TestCase):
    def test_multi_owner_rejects_foreign_owner(self):
        from turbodrf.predicates import parse_config

        me = User.objects.create_user(username="me")
        me._test_roles = ["underwriter"]
        victim = User.objects.create_user(username="victim")
        _, preds = parse_config({"owner_field": ["author", "editor"]})
        req = mock.Mock(user=me)
        errors = []
        for p in preds:
            errors += p.validate_write({"author": victim}, None, req)
        self.assertTrue(errors, "author=<other user> accepted")


# ---------------------------------------------------------------------------
# 9. FK-injection defense only runs when the HOST model is scoped. A
#    'tenancy': 'shared' (or unscoped) host can point an FK at another
#    tenant's row. Simulated by clearing HoldingEvent's registry entries.
#    Docs: security.md "every FK in the request body must resolve to a row
#    visible under the related model's predicate stack".
# ---------------------------------------------------------------------------
class TestFkInjectionOnUnscopedHost(_TwoTenants):
    def test_shared_host_rejects_cross_tenant_fk(self):
        from turbodrf.predicates import register_predicates, register_tenant_field

        analyst = self._user("analyst_a", ["analyst"], self.b_a)
        foreign = Holding.objects.create(name="B holding", brokerage=self.b_b)
        with _preserve_registries():
            register_tenant_field(HoldingEvent, None)
            register_predicates(HoldingEvent, [])
            r = self._client(analyst).post(
                "/api/holdingevents/",
                {"status": "new", "holding": foreign.pk},
                format="json",
            )
        self.assertEqual(r.status_code, 400, getattr(r, "data", r))


# ---------------------------------------------------------------------------
# 10. TURBODRF_MAX_NESTING_DEPTH is not enforced on the compiled plan (the
#     router only logs a warning). Docs: configuration.md "Maximum nesting
#     depth is 3"; settings_reference "Maximum __ depth for nested fields".
# ---------------------------------------------------------------------------
class TestNestingDepthCompiled(TestCase):
    @override_settings(TURBODRF_MAX_NESTING_DEPTH=0)
    def test_compiled_plan_drops_over_depth_fields(self):
        from tests.test_app.models import CompiledSampleModel
        from turbodrf.compiler import compile_model

        plan = compile_model(CompiledSampleModel)
        self.assertNotIn("related_name", plan.fk_annotations)


# ---------------------------------------------------------------------------
# 11. ?fields= is honoured only on the compiled LIST path; non-compiled models
#     (and detail) ignore it. Docs: configuration.md "Client field selection".
# ---------------------------------------------------------------------------
class TestAllauthMappingPassthrough(TestCase):
    """integrations.md: groups are 'mapped via TURBODRF_ALLAUTH_ROLE_MAPPING'.
    Unmapped groups pass through under their raw name (Keycloak's strict
    default exists to stop exactly this: a group called 'admin')."""

    @override_settings(TURBODRF_ALLAUTH_ROLE_MAPPING={"Editors": "editor"})
    def test_unmapped_group_is_not_a_role(self):
        from django.contrib.auth.models import Group

        from turbodrf.integrations.allauth import get_user_roles_from_groups

        u = User.objects.create_user(username="g")
        u.groups.add(Group.objects.create(name="admin"))
        self.assertEqual(get_user_roles_from_groups(u), [])


class TestClientFieldSelection(_TwoTenants):
    def test_fields_param_on_non_compiled_list(self):
        r = self._client(self.under_a).get("/api/deals/?fields=title")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(_rows(r)[0].keys()), {"title"})
