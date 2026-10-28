"""
The platform scope through the API: ``TurboDRFRouter(scope="platform",
gate=[...])`` serving an operator's own staff across tenants
(tests/platform_urls.py mounts it at console-api/, next to the tenant API at
api/).

- The gate runs before everything else on every route, action, OPTIONS and
  the schema view.
- Rows of every tenant are visible; no tenant boundary, no predicates.
- The tenant router serves a model with a platform block exactly as before.
- Tenant roles grant nothing on the platform router, platform roles nothing on
  the tenant router.
- Field rules, filters, search, ordering, the compiled list and OPTIONS work
  on the platform config.
- Writes name the tenant themselves, FK / M2M values must be reachable rows of
  the same tenant.
- A router subclass can wrap the generated viewsets in a mixin that reads
  ``self.turbodrf_scope``.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    Brokerage,
    Deal,
    PlatformAccount,
    PlatformJob,
    PlatformTicket,
)
from turbodrf.permissions import TurboDRFPermission

User = get_user_model()

PLATFORM = dict(
    ROOT_URLCONF="tests.platform_urls",
    TURBODRF_TENANT_MODEL="test_app.Brokerage",
)
GATE_MESSAGE = "Platform staff only."


def _rows(response):
    body = response.json()
    return body["data"] if isinstance(body, dict) and "data" in body else body


def _error(response):
    """The error payload (turbodrf_exception_handler's envelope)."""
    body = response.json()
    return body.get("error", {}).get("message", body)


@override_settings(**PLATFORM)
class _PlatformBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.acme = Brokerage.objects.create(name="Acme")
        cls.globex = Brokerage.objects.create(name="Globex")
        cls.acme_account = PlatformAccount.objects.create(
            brokerage=cls.acme, name="Acme main", staff_notes="vip customer"
        )
        cls.globex_account = PlatformAccount.objects.create(
            brokerage=cls.globex, name="Globex main", staff_notes="pays late"
        )
        cls.acme_job = PlatformJob.objects.create(
            brokerage=cls.acme,
            account=cls.acme_account,
            kind="sync",
            status="failed",
            payload={"token": "raw", "n": 1},
            last_error="upstream timeout",
        )
        cls.globex_job = PlatformJob.objects.create(
            brokerage=cls.globex,
            account=cls.globex_account,
            kind="email",
            status="done",
            payload={"n": 2},
        )

    def setUp(self):
        cache.clear()

    def user(
        self,
        username,
        platform_roles=(),
        tenant_roles=(),
        brokerage=None,
        is_staff=True,
    ):
        user = User.objects.create_user(
            username=username, password="x", is_staff=is_staff
        )
        user._test_roles = list(tenant_roles)
        user._test_platform_roles = list(platform_roles)
        # The brokerage registry is keyed by pk, and pks are reused after a
        # rollback: drop any stale entry now and ours afterwards.
        _test_user_brokerages.pop(user.pk, None)
        self.addCleanup(_test_user_brokerages.pop, user.pk, None)
        if brokerage is not None:
            set_test_brokerage(user, brokerage)
        return user

    def client_for(self, user=None):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
class TestGate(_PlatformBase):
    def routes(self):
        job, ticket_url = self.acme_job.pk, "/console-api/tickets/"
        return [
            ("get", "/console-api/jobs/", None),
            ("get", f"/console-api/jobs/{job}/", None),
            ("get", "/console-api/jobs/failed/", None),
            ("get", "/console-api/jobs/ping/", None),
            ("post", f"/console-api/jobs/{job}/retry/", {}),
            ("options", "/console-api/jobs/", None),
            ("options", f"/console-api/jobs/{job}/", None),
            ("post", ticket_url, {"subject": "s", "brokerage": self.acme.pk}),
            ("patch", f"/console-api/accounts/{self.acme_account.pk}/", {"plan": "x"}),
            ("put", f"/console-api/accounts/{self.acme_account.pk}/", {"name": "x"}),
            ("delete", f"/console-api/accounts/{self.acme_account.pk}/", None),
            ("get", "/console-audited/jobs/", None),
        ]

    def test_non_staff_with_every_platform_role_is_refused_everywhere(self):
        user = self.user(
            "not_staff", platform_roles=["admin", "support"], is_staff=False
        )
        client = self.client_for(user)
        for method, url, body in self.routes():
            with self.subTest(method=method, url=url):
                response = getattr(client, method)(url, body, format="json")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(_error(response), GATE_MESSAGE)
        self.assertTrue(PlatformAccount.objects.filter(pk=self.acme_account.pk))

    def test_anonymous_is_refused_everywhere(self):
        client = self.client_for()
        for method, url, body in self.routes():
            with self.subTest(method=method, url=url):
                response = getattr(client, method)(url, body, format="json")
                self.assertIn(response.status_code, (401, 403))

    def test_gate_runs_before_the_role_check(self):
        user = self.user("not_staff_2", platform_roles=["admin"], is_staff=False)
        with mock.patch.object(
            TurboDRFPermission, "has_permission", autospec=True, return_value=True
        ) as role_check:
            response = self.client_for(user).get("/console-api/jobs/")
        self.assertEqual(response.status_code, 403)
        role_check.assert_not_called()

    def test_gate_passes_then_roles_decide(self):
        no_roles = self.user("staff_no_roles")
        response = self.client_for(no_roles).get("/console-api/jobs/")
        self.assertEqual(response.status_code, 403)
        self.assertNotEqual(_error(response), GATE_MESSAGE)
        support = self.user("support", platform_roles=["support"])
        self.assertEqual(
            self.client_for(support).get("/console-api/jobs/").status_code, 200
        )

    def test_action_declaring_permission_classes_is_still_gated(self):
        """ping declares permission_classes=[AllowAny]: it replaces the role
        check, never the gate."""
        outsider = self.user("outsider", is_staff=False)
        self.assertEqual(
            self.client_for(outsider).get("/console-api/jobs/ping/").status_code, 403
        )
        staff = self.user("staff_ping")
        response = self.client_for(staff).get("/console-api/jobs/ping/")
        self.assertEqual(response.status_code, 200)

    def test_schema_view_is_gated(self):
        outsider = self.user(
            "outsider_schema", platform_roles=["admin"], is_staff=False
        )
        response = self.client_for(outsider).get("/console-swagger/?format=openapi")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client_for().get("/console-swagger/?format=openapi").status_code, 403
        )
        admin = self.user("admin_schema", platform_roles=["admin"])
        response = self.client_for(admin).get("/console-swagger/?format=openapi")
        self.assertEqual(response.status_code, 200)

    def test_no_index_view(self):
        admin = self.user("admin_index", platform_roles=["admin"])
        self.assertEqual(self.client_for(admin).get("/console-api/").status_code, 404)

    def test_browsable_api_renders_inside_the_platform_scope(self):
        """The HTML renderer calls back into the viewset (forms) after the
        view returns: it must still see platform roles and the gate."""
        admin = self.user("admin_html", platform_roles=["admin"])
        response = self.client_for(admin).get(
            f"/console-api/accounts/{self.globex_account.pk}/", HTTP_ACCEPT="text/html"
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("pays late", content)
        self.assertIn('name="staff_notes"', content)  # the PUT form, admin may write


# ---------------------------------------------------------------------------
# Rows across tenants
# ---------------------------------------------------------------------------
class TestAcrossTenants(_PlatformBase):
    def setUp(self):
        super().setUp()
        # A staff member who also belongs to a tenant: it doesn't narrow
        # anything on the platform scope.
        self.support = self.user(
            "support_rows", platform_roles=["support"], brokerage=self.acme
        )
        self.client = self.client_for(self.support)

    def test_list_has_every_tenants_rows(self):
        rows = _rows(self.client.get("/console-api/jobs/"))
        self.assertEqual(
            {(r["id"], r["brokerage_name"]) for r in rows},
            {(self.acme_job.pk, "Acme"), (self.globex_job.pk, "Globex")},
        )

    def test_detail_of_another_tenants_row(self):
        response = self.client.get(f"/console-api/jobs/{self.globex_job.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["brokerage_name"], "Globex")
        self.assertEqual(response.json()["account_name"], "Globex main")

    def test_filter_by_tenant(self):
        rows = _rows(self.client.get(f"/console-api/jobs/?brokerage={self.globex.pk}"))
        self.assertEqual([r["id"] for r in rows], [self.globex_job.pk])

    def test_action_queryset_spans_tenants(self):
        PlatformJob.objects.create(brokerage=self.globex, kind="x", status="failed")
        response = self.client.get("/console-api/jobs/failed/")
        self.assertEqual(
            response.json()["brokerages"], sorted([self.acme.pk, self.globex.pk])
        )

    def test_serializer_path_lists_every_tenant(self):
        for brokerage in (self.acme, self.globex):
            PlatformTicket.objects.create(brokerage=brokerage, subject=brokerage.name)
        rows = _rows(self.client.get("/console-api/tickets/"))
        self.assertEqual({r["subject"] for r in rows}, {"Acme", "Globex"})


# ---------------------------------------------------------------------------
# The tenant router is unchanged
# ---------------------------------------------------------------------------
class TestTenantRouterUnchanged(_PlatformBase):
    def setUp(self):
        super().setUp()
        self.member = self.user(
            "acme_member",
            tenant_roles=["account_admin"],
            brokerage=self.acme,
            is_staff=False,
        )
        self.client = self.client_for(self.member)

    def test_tenant_list_is_scoped_and_has_tenant_fields_only(self):
        rows = _rows(self.client.get("/api/platform-accounts/"))
        self.assertEqual(
            rows, [{"id": self.acme_account.pk, "name": "Acme main", "plan": "basic"}]
        )
        detail = self.client.get(f"/api/platform-accounts/{self.acme_account.pk}/")
        self.assertEqual(set(detail.json()), {"id", "name", "plan"})
        other = self.client.get(f"/api/platform-accounts/{self.globex_account.pk}/")
        self.assertEqual(other.status_code, 404)

    def test_platform_only_fields_dont_filter_search_or_order(self):
        PlatformAccount.objects.create(brokerage=self.acme, name="Acme b", plan="pro")
        everything = _rows(self.client.get("/api/platform-accounts/"))
        self.assertEqual(len(everything), 2)
        self.assertEqual(
            _rows(self.client.get("/api/platform-accounts/?staff_notes=vip customer")),
            everything,
        )
        self.assertEqual(
            _rows(self.client.get("/api/platform-accounts/?search=vip")), []
        )
        self.assertEqual(
            [
                r["name"]
                for r in _rows(self.client.get("/api/platform-accounts/?search=acme"))
            ],
            ["Acme main", "Acme b"],
        )
        self.assertEqual(
            _rows(self.client.get("/api/platform-accounts/?ordering=-staff_notes")),
            everything,
        )

    def test_tenant_options_show_no_platform_config(self):
        options = self.client.options("/api/platform-accounts/").json()
        self.assertEqual(set(options["model"]["fields"]), {"id", "name", "plan"})
        self.assertEqual(options["capabilities"]["search_fields"], ["name"])
        self.assertNotIn("staff_notes", options["capabilities"]["filterset_fields"])
        self.assertNotIn("staff_notes", json.dumps(options))

    def test_disabled_model_has_no_tenant_route(self):
        self.assertEqual(self.client.get("/api/platformjobs/").status_code, 404)

    def test_tenant_create_still_fills_the_tenant_and_owner(self):
        response = self.client.post(
            "/api/platform-tickets/", {"subject": "from tenant"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        ticket = PlatformTicket.objects.get(pk=response.json()["id"])
        self.assertEqual(ticket.brokerage_id, self.acme.pk)
        self.assertEqual(ticket.assignee_id, self.member.pk)

    def test_tenant_schema_is_the_same_with_a_platform_router_mounted(self):
        admin = self.user(
            "acme_admin", tenant_roles=["account_admin", "admin"], brokerage=self.acme
        )
        with_platform = self.client_for(admin).get("/swagger/?format=openapi").json()
        with override_settings(ROOT_URLCONF="tests.urls"):
            cache.clear()
            without = self.client_for(admin).get("/swagger/?format=openapi").json()
        self.assertEqual(with_platform.get("basePath"), without.get("basePath"))
        self.assertEqual(sorted(with_platform["paths"]), sorted(without["paths"]))
        self.assertIn("/platform-accounts/", with_platform["paths"])
        dumped = json.dumps(with_platform)
        for platform_only in ("staff_notes", "internal_note", "/jobs/", "console"):
            self.assertNotIn(platform_only, dumped)


# ---------------------------------------------------------------------------
# Separate roles
# ---------------------------------------------------------------------------
class TestRolesAreSeparate(_PlatformBase):
    def test_tenant_role_grants_nothing_on_the_platform_router(self):
        user = self.user("tenant_only", tenant_roles=["account_admin", "admin"])
        client = self.client_for(user)
        for url in ("/console-api/accounts/", "/console-api/tickets/"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403)

    def test_platform_role_grants_nothing_on_the_tenant_router(self):
        user = self.user("platform_only", platform_roles=["admin"], brokerage=self.acme)
        client = self.client_for(user)
        self.assertEqual(client.get("/console-api/accounts/").status_code, 200)
        for url in ("/api/platform-accounts/", "/api/platform-tickets/"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403)

    def test_a_role_name_means_what_its_own_map_says(self):
        """account_admin is a tenant role (account and ticket CRUD) and a
        platform role (job read only)."""
        user = self.user("same_name", platform_roles=["account_admin"])
        client = self.client_for(user)
        self.assertEqual(client.get("/console-api/jobs/").status_code, 200)
        self.assertEqual(client.get("/console-api/accounts/").status_code, 403)
        self.assertEqual(client.get("/api/platform-accounts/").status_code, 403)

    @override_settings(TURBODRF_PERMISSION_MODE="database")
    def test_database_roles_grant_nothing_on_the_platform_router(self):
        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        role = TurboDRFRole.objects.create(name="db_accounts")
        RolePermission.objects.create(
            role=role, app_label="test_app", model_name="platformaccount", action="read"
        )
        user = self.user("db_user", brokerage=self.acme)
        UserRole.objects.create(user=user, role=role)
        client = self.client_for(user)
        self.assertEqual(client.get("/api/platform-accounts/").status_code, 200)
        self.assertEqual(client.get("/console-api/accounts/").status_code, 403)
        # The platform scope is static whatever the mode.
        user._test_platform_roles = ["support"]
        cache.clear()
        self.assertEqual(client.get("/console-api/accounts/").status_code, 200)

    @override_settings(
        TURBODRF_PLATFORM_ROLE_GETTER="tests.integration.test_platform_scope.staff_roles"
    )
    def test_role_getter_setting(self):
        user = self.user("getter_user")
        user.staff_profile_roles = ["support"]
        self.assertEqual(
            self.client_for(user).get("/console-api/accounts/").status_code, 200
        )
        user.staff_profile_roles = []
        cache.clear()
        self.assertEqual(
            self.client_for(user).get("/console-api/accounts/").status_code, 403
        )


def staff_roles(user):
    """A TURBODRF_PLATFORM_ROLE_GETTER for test_role_getter_setting."""
    return getattr(user, "staff_profile_roles", [])


# ---------------------------------------------------------------------------
# Field rules by platform role
# ---------------------------------------------------------------------------
class TestFieldRules(_PlatformBase):
    def detail(self, user, url):
        response = self.client_for(user).get(url)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_payload_readable_only_with_its_rule(self):
        url = f"/console-api/jobs/{self.acme_job.pk}/"
        support = self.user("support_fields", platform_roles=["support"])
        self.assertNotIn("payload", self.detail(support, url))
        admin = self.user("admin_fields", platform_roles=["admin"])
        self.assertEqual(self.detail(admin, url)["payload"], {"token": "raw", "n": 1})

    def test_nested_values_need_read_on_the_target(self):
        """auditor reads jobs and payloads, not brokerages or accounts."""
        auditor = self.user("auditor_fields", platform_roles=["auditor"])
        body = self.detail(auditor, f"/console-api/jobs/{self.acme_job.pk}/")
        self.assertIn("payload", body)
        self.assertNotIn("brokerage_name", body)
        self.assertNotIn("account_name", body)
        rows = _rows(self.client_for(auditor).get("/console-api/jobs/"))
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("brokerage_name", row)

    def test_write_rule(self):
        ticket = PlatformTicket.objects.create(
            brokerage=self.globex, subject="s", internal_note="before"
        )
        url = f"/console-api/tickets/{ticket.pk}/"
        support = self.user("support_write", platform_roles=["support"])
        response = self.client_for(support).patch(
            url, {"subject": "renamed", "internal_note": "changed"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        ticket.refresh_from_db()
        self.assertEqual((ticket.subject, ticket.internal_note), ("renamed", "before"))
        admin = self.user("admin_write", platform_roles=["admin"])
        self.client_for(admin).patch(url, {"internal_note": "changed"}, format="json")
        ticket.refresh_from_db()
        self.assertEqual(ticket.internal_note, "changed")

    def test_platform_rules_dont_reach_the_tenant_scope(self):
        """internal_note has platform rules only; on the tenant scope it has
        no rule, so model-level read decides."""
        from turbodrf.backends import build_permission_snapshot
        from turbodrf.scope import PLATFORM as SCOPE
        from turbodrf.scope import use_scope

        user = self.user("both_scopes", tenant_roles=["account_admin"])
        tenant = build_permission_snapshot(user, PlatformTicket, use_cache=False)
        self.assertIn("internal_note", tenant.readable_fields)
        self.assertNotIn("internal_note", tenant.fields_with_read_rules)
        user._test_platform_roles = ["auditor"]
        with use_scope(SCOPE):
            platform = build_permission_snapshot(user, PlatformTicket, use_cache=False)
        self.assertEqual(platform.allowed_actions, set())
        self.assertIn("internal_note", platform.fields_with_read_rules)


# ---------------------------------------------------------------------------
# Filters, search and ordering: platform fields only
# ---------------------------------------------------------------------------
class TestFiltersSearchOrdering(_PlatformBase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(
            self.user("support_q", platform_roles=["support"])
        )

    def ids(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["id"] for row in _rows(response)]

    def test_filter_on_a_platform_field(self):
        self.assertEqual(
            self.ids("/console-api/accounts/?staff_notes__icontains=late"),
            [self.globex_account.pk],
        )
        self.assertEqual(
            self.ids("/console-api/jobs/?brokerage__name=Globex"), [self.globex_job.pk]
        )

    def test_filter_outside_platform_fields_is_dropped(self):
        everything = self.ids("/console-api/jobs/")
        self.assertEqual(self.ids("/console-api/jobs/?account__plan=pro"), everything)
        self.assertEqual(
            self.ids("/console-api/jobs/?account__staff_notes__icontains=late"),
            everything,
        )

    def test_search_over_platform_searchable_fields(self):
        self.assertEqual(
            self.ids("/console-api/accounts/?search=pays"), [self.globex_account.pk]
        )
        self.assertEqual(
            self.ids("/console-api/jobs/?search=timeout"), [self.acme_job.pk]
        )
        # status is a platform field, but not searchable
        self.assertEqual(self.ids("/console-api/jobs/?search=failed"), [])

    def test_ordering_by_platform_fields_only(self):
        self.assertEqual(
            self.ids("/console-api/accounts/?ordering=-staff_notes"),
            [self.acme_account.pk, self.globex_account.pk],
        )
        self.assertEqual(
            self.ids("/console-api/jobs/?ordering=-kind"),
            [self.acme_job.pk, self.globex_job.pk],
        )
        # payload is configured but not orderable for support (no read rule)
        options = self.client.options("/console-api/jobs/").json()
        self.assertNotIn("payload", options["capabilities"]["ordering_fields"])
        self.assertNotIn("payload", options["capabilities"]["filterset_fields"])

    def test_options_describe_the_platform_config(self):
        options = self.client.options("/console-api/accounts/").json()
        self.assertEqual(
            set(options["model"]["fields"]),
            {"id", "name", "plan", "staff_notes", "brokerage"},
        )
        self.assertEqual(
            options["capabilities"]["search_fields"], ["name", "staff_notes"]
        )
        self.assertIn("staff_notes", options["capabilities"]["filterset_fields"])
        self.assertFalse(options["actions"]["create"])  # support: read only
        self.assertTrue(options["actions"]["list"])


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
class TestPlatformWrites(_PlatformBase):
    def setUp(self):
        super().setUp()
        self.admin = self.user(
            "admin_writes", platform_roles=["admin"], brokerage=self.acme
        )
        self.client = self.client_for(self.admin)

    def create(self, **data):
        return self.client.post(
            "/console-api/tickets/", {"subject": "s", **data}, format="json"
        )

    def test_create_names_its_tenant(self):
        response = self.create(brokerage=self.globex.pk, account=self.globex_account.pk)
        self.assertEqual(response.status_code, 201, response.content)
        ticket = PlatformTicket.objects.get(pk=response.json()["id"])
        self.assertEqual(ticket.brokerage_id, self.globex.pk)

    def test_tenant_is_not_filled_from_the_staff_member(self):
        response = self.create()
        self.assertEqual(response.status_code, 400)
        self.assertIn("brokerage", _error(response))
        self.assertFalse(PlatformTicket.objects.exists())

    def test_owner_is_not_filled_from_the_staff_member(self):
        response = self.create(brokerage=self.acme.pk)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(PlatformTicket.objects.get().assignee_id)

    def test_cross_tenant_fk_is_refused(self):
        response = self.create(brokerage=self.acme.pk, account=self.globex_account.pk)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            _error(response),
            {
                "account": [
                    "Invalid account: it belongs to a different tenant than this row."
                ]
            },
        )
        self.assertFalse(PlatformTicket.objects.exists())

    def test_cross_tenant_m2m_is_refused(self):
        response = self.create(
            brokerage=self.acme.pk,
            linked_accounts=[self.acme_account.pk, self.globex_account.pk],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("linked_accounts", _error(response))
        response = self.create(
            brokerage=self.acme.pk, linked_accounts=[self.acme_account.pk]
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_cross_tenant_user_is_refused(self):
        globex_user = self.user("globex_user", brokerage=self.globex, is_staff=False)
        acme_user = self.user("acme_user", brokerage=self.acme, is_staff=False)
        no_tenant = self.user("staff_member_no_tenant")
        response = self.create(brokerage=self.acme.pk, assignee=globex_user.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("assignee", _error(response))
        for assignee in (acme_user, no_tenant):
            with self.subTest(assignee=assignee.username):
                response = self.create(brokerage=self.acme.pk, assignee=assignee.pk)
                self.assertEqual(response.status_code, 201, response.content)

    def test_fk_to_a_model_without_a_platform_block_is_refused(self):
        deal = Deal.objects.create(title="d", brokerage=self.acme)
        response = self.create(brokerage=self.acme.pk, deal=deal.pk)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            _error(response), {"deal": ["Invalid deal: not found or not accessible."]}
        )
        # Its stored id still renders (no JOIN); filtering on it matches
        # nothing.
        ticket = PlatformTicket.objects.create(
            brokerage=self.acme, subject="t", deal=deal
        )
        body = self.client.get(f"/console-api/tickets/{ticket.pk}/").json()
        self.assertEqual(body["deal"], deal.pk)
        filtered = self.client.get(f"/console-api/tickets/?deal={deal.pk}")
        self.assertEqual(_rows(filtered), [])

    def test_moving_a_row_checks_the_relations_it_keeps(self):
        ticket = PlatformTicket.objects.create(
            brokerage=self.acme, account=self.acme_account, subject="t"
        )
        ticket.linked_accounts.set([self.acme_account])
        url = f"/console-api/tickets/{ticket.pk}/"
        response = self.client.patch(url, {"brokerage": self.globex.pk}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(_error(response)), {"account", "linked_accounts"})
        ticket.refresh_from_db()
        self.assertEqual(ticket.brokerage_id, self.acme.pk)
        response = self.client.patch(
            url,
            {
                "brokerage": self.globex.pk,
                "account": self.globex_account.pk,
                "linked_accounts": [],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        ticket.refresh_from_db()
        self.assertEqual(ticket.brokerage_id, self.globex.pk)

    def test_update_another_tenants_row(self):
        ticket = PlatformTicket.objects.create(brokerage=self.globex, subject="t")
        response = self.client.patch(
            f"/console-api/tickets/{ticket.pk}/", {"subject": "u"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        ticket.refresh_from_db()
        self.assertEqual(ticket.subject, "u")

    def test_read_only_resource_routes_its_actions_only(self):
        url = f"/console-api/jobs/{self.acme_job.pk}/"
        # admin has the job's create (for the POST action): a CRUD create is
        # still unrouted. It has no delete: refused before the 405, as on the
        # tenant router.
        self.assertEqual(self.client.post("/console-api/jobs/", {}).status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.assertTrue(PlatformJob.objects.filter(pk=self.acme_job.pk).exists())
        response = self.client.post(f"{url}retry/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"id": self.acme_job.pk, "status": "queued", "scope": "platform"},
        )
        support = self.user("support_retry", platform_roles=["support"])
        self.assertEqual(self.client_for(support).post(f"{url}retry/").status_code, 403)


# ---------------------------------------------------------------------------
# The mixin pattern downstream apps use
# ---------------------------------------------------------------------------
class TestMixinSubclass(_PlatformBase):
    def setUp(self):
        super().setUp()
        from tests import platform_urls

        self.reads = platform_urls.PAYLOAD_READS
        self.reads.clear()

    def test_router_subclass_wraps_viewsets(self):
        from tests.platform_urls import PayloadAudit, audited_router

        self.assertEqual(audited_router.scope, "platform")
        for _prefix, viewset, _basename in audited_router.registry:
            self.assertTrue(issubclass(viewset, PayloadAudit))
            self.assertEqual(viewset.turbodrf_scope, "platform")

    def test_mixin_sees_the_scope_and_audits_payload_reads(self):
        url = f"/console-audited/jobs/{self.globex_job.pk}/"
        support = self.user("support_audit", platform_roles=["support"])
        self.assertEqual(self.client_for(support).get(url).status_code, 200)
        self.assertEqual(self.reads, [])
        admin = self.user("admin_audit", platform_roles=["admin"])
        body = self.client_for(admin).get(url).json()
        self.assertEqual(body["payload"], {"n": 2})
        self.assertEqual(self.reads, [(admin.pk, "PlatformJob", self.globex_job.pk)])

    def test_wrapped_viewsets_keep_the_gate(self):
        outsider = self.user("outsider_audit", platform_roles=["admin"], is_staff=False)
        response = self.client_for(outsider).get(
            f"/console-audited/jobs/{self.globex_job.pk}/"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reads, [])


# ---------------------------------------------------------------------------
# The platform schema
# ---------------------------------------------------------------------------
class TestPlatformSchema(_PlatformBase):
    def schema(self, user):
        response = self.client_for(user).get("/console-swagger/?format=openapi")
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def test_documents_this_routers_platform_routes_only(self):
        schema = self.schema(self.user("admin_doc", platform_roles=["admin"]))
        self.assertEqual(schema["basePath"], "/console-api")
        self.assertEqual(
            sorted(schema["paths"]),
            [
                "/accounts/",
                "/accounts/{id}/",
                "/jobs/",
                "/jobs/failed/",
                "/jobs/ping/",
                "/jobs/{id}/",
                "/jobs/{id}/retry/",
                "/tickets/",
                "/tickets/{id}/",
            ],
        )
        self.assertIn("staff_notes", json.dumps(schema))

    def test_scoped_to_platform_roles(self):
        schema = self.schema(self.user("support_doc", platform_roles=["support"]))
        self.assertNotIn("post", schema["paths"].get("/accounts/", {}))
        self.assertIn("get", schema["paths"]["/accounts/"])
        self.assertNotIn("payload", json.dumps(schema))
        # Tenant roles document nothing here.
        tenant_admin = self.user("tenant_doc", tenant_roles=["account_admin", "admin"])
        self.assertEqual(self.schema(tenant_admin)["paths"], {})

    def test_role_narrowing_uses_platform_roles(self):
        user = self.user("narrow_doc", platform_roles=["admin", "auditor"])
        response = self.client_for(user).get(
            "/console-swagger/?format=openapi&role=auditor"
        )
        paths = response.json()["paths"]
        self.assertEqual(
            sorted(paths), ["/jobs/", "/jobs/failed/", "/jobs/ping/", "/jobs/{id}/"]
        )


# ---------------------------------------------------------------------------
# @property sandbox, sensitive fields, ?fields= and the bypass settings
# ---------------------------------------------------------------------------
class TestPropertiesAndDenyList(_PlatformBase):
    def detail(self, roles, username):
        user = self.user(username, platform_roles=roles)
        response = self.client_for(user).get(f"/console-api/jobs/{self.acme_job.pk}/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_properties_run_in_the_platform_callers_sandbox(self):
        admin = self.detail(["admin"], "admin_props")
        self.assertEqual(admin["summary"], "sync:failed")
        self.assertEqual(admin["account_label"], "ACME MAIN")
        self.assertEqual(admin["payload_keys"], ["n", "token"])
        # support: no payload read, so a property reading it fails closed.
        support = self.detail(["support"], "support_props")
        self.assertEqual(support["account_label"], "ACME MAIN")
        self.assertNotIn("payload_keys", support)
        # auditor: no account read, so the declared relation isn't loaded.
        auditor = self.detail(["auditor"], "auditor_props")
        self.assertEqual(auditor["payload_keys"], ["n", "token"])
        self.assertNotIn("account_label", auditor)

    def test_property_on_the_compiled_list(self):
        user = self.user("support_list_props", platform_roles=["support"])
        rows = _rows(self.client_for(user).get("/console-api/jobs/"))
        self.assertEqual(
            {row["summary"] for row in rows}, {"sync:failed", "email:done"}
        )

    @override_settings(TURBODRF_SENSITIVE_FIELDS=["last_error"])
    def test_sensitive_deny_list_applies(self):
        user = self.user("admin_sensitive", platform_roles=["admin"])
        client = self.client_for(user)
        body = client.get(f"/console-api/jobs/{self.acme_job.pk}/").json()
        self.assertNotIn("last_error", body)
        everything = _rows(client.get("/console-api/jobs/"))
        self.assertEqual(_rows(client.get("/console-api/jobs/?search=timeout")), [])
        self.assertEqual(
            _rows(client.get("/console-api/jobs/?last_error__icontains=timeout")),
            everything,
        )

    def test_client_field_selection(self):
        user = self.user("support_fields_param", platform_roles=["support"])
        client = self.client_for(user)
        rows = _rows(client.get("/console-api/jobs/?fields=id,brokerage.name"))
        self.assertEqual(sorted(rows[0]), ["brokerage", "brokerage_name", "id"])
        detail = client.get(
            f"/console-api/accounts/{self.acme_account.pk}/?fields=staff_notes"
        ).json()
        self.assertEqual(detail, {"staff_notes": "vip customer"})

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_disable_permissions_doesnt_open_the_platform_router(self):
        no_roles = self.user("staff_no_roles_bypass")
        self.assertEqual(
            self.client_for(no_roles).get("/console-api/jobs/").status_code, 403
        )
        support = self.user("support_bypass", platform_roles=["support"])
        body = self.client_for(support).get(f"/console-api/jobs/{self.acme_job.pk}/")
        self.assertNotIn("payload", body.json())

    @override_settings(TURBODRF_USE_DEFAULT_PERMISSIONS=True)
    def test_default_django_permissions_dont_apply_on_the_platform_router(self):
        superuser = User.objects.create_superuser("root", password="x")
        superuser._test_platform_roles = []
        self.assertEqual(
            self.client_for(superuser).get("/console-api/jobs/").status_code, 403
        )
