"""Third audit (0.8.5): a black-box pass through the HTTP API.

Two tenants (A, B) with rows in both, users at every role level and an
anonymous caller, attacking the routes in tests/urls.py and
tests/platform_urls.py with DRF's APIClient.

- ``TestFinding*`` classes send attacks that worked before 0.8.5's fixes and
  assert the secure behaviour.
- ``TestHeld*`` classes pin attack classes that held, so a regression shows.
"""

import re
from unittest import mock
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    CompiledSampleModel,
    Deal,
    Fund,
    Holding,
    HoldingEvent,
    PlatformAccount,
    PlatformJob,
    PlatformTicket,
    Portfolio,
    RbacBoard,
    RelatedModel,
    SampleModel,
    ScopeFolder,
    ScopeItem,
    Transaction,
    Valuation,
)

User = get_user_model()

PLATFORM = dict(
    ROOT_URLCONF="tests.platform_urls",
    TURBODRF_TENANT_MODEL="test_app.Brokerage",
)


class World(TestCase):
    """Tenants A and B, a user per role, and rows on both sides."""

    def setUp(self):
        import tests.urls  # noqa: F401  (the router registers tenancy)

        cache.clear()
        _test_user_brokerages.clear()
        self.addCleanup(_test_user_brokerages.clear)
        self.A = Brokerage.objects.create(name="AlphaCo")
        self.B = Brokerage.objects.create(name="BetaCo")
        self.mgrA = self._user("mgrA", ["manager"], self.A)
        self.uwA = self._user("uwA", ["underwriter"], self.A)
        self.uwA2 = self._user("uwA2", ["underwriter"], self.A)
        self.uwB = self._user("uwB", ["underwriter"], self.B)
        self.anaA = self._user("anaA", ["analyst"], self.A)
        self.audA = self._user("audA", ["auditor"], self.A)
        self.accA = self._user("accA", ["account_admin"], self.A)
        self.dA1 = Deal.objects.create(
            title="A-own-uwA", brokerage=self.A, assigned_broker=self.uwA
        )
        self.dA2 = Deal.objects.create(
            title="A-own-uwA2", brokerage=self.A, assigned_broker=self.uwA2
        )
        self.dB = Deal.objects.create(
            title="B-secret-deal", brokerage=self.B, assigned_broker=self.uwB
        )
        self.baA = BankAccount.objects.create(name="A-acct", deal=self.dA1)
        self.baB = BankAccount.objects.create(name="B-acct", deal=self.dB)
        self.txA = Transaction.objects.create(amount=10, bank_account=self.baA)
        self.txB = Transaction.objects.create(amount=777, bank_account=self.baB)
        self.pA = Portfolio.objects.create(name="A-port", brokerage=self.A)
        self.pB = Portfolio.objects.create(name="B-port", brokerage=self.B)
        self.hA = Holding.objects.create(
            name="A-hold", brokerage=self.A, portfolio=self.pA
        )
        self.hB = Holding.objects.create(
            name="B-hold", brokerage=self.B, portfolio=self.pB
        )
        self.heA = HoldingEvent.objects.create(
            status="newA", brokerage=self.A, holding=self.hA
        )
        self.heB = HoldingEvent.objects.create(
            status="newB", brokerage=self.B, holding=self.hB
        )
        self.fA = Fund.all_objects.create(name="A-fund", brokerage=self.A)
        self.vA = Valuation.objects.create(units=1, brokerage=self.A, fund=self.fA)
        self.paA = PlatformAccount.objects.create(brokerage=self.A, name="A-pacct")
        self.paB = PlatformAccount.objects.create(brokerage=self.B, name="B-pacct")
        self.ptA = PlatformTicket.objects.create(
            brokerage=self.A, subject="A-tk", assignee=self.accA
        )
        self.ptB = PlatformTicket.objects.create(brokerage=self.B, subject="B-tk")
        self.client = APIClient()

    def _user(self, username, roles, brokerage, **extra):
        user = User.objects.create_user(username=username, password="x", **extra)
        user._test_roles = roles
        if brokerage is not None:
            set_test_brokerage(user, brokerage)
        return user

    def _as(self, user):
        """A fresh client (500s come back as responses, not exceptions)."""
        cache.clear()
        self.client = APIClient(raise_request_exception=False)
        if user is not None:
            self.client.force_authenticate(user)


# ===========================================================================
# Finding 1 (low): the browsable API's write-form dropdowns list the rows in
# the target's Meta.ordering, on a column the caller can't read. Lists,
# ?ordering=, to-many arrays and property hops all fall back to pk order
# there (0.8.4 / 0.8.5); the dropdown queryset (serializers.get_fields ->
# _referenceable) keeps the target's default order.
# ===========================================================================


def _roles_with_holding_name_rule():
    roles = dict(settings.TURBODRF_ROLES)
    # A read rule on Holding.name, granted to managers only: analysts can't
    # read the column.
    roles["manager"] = list(roles["manager"]) + ["test_app.holding.name.read"]
    return roles


@override_settings(TURBODRF_ROLES=_roles_with_holding_name_rule())
class TestFindingFormDropdownOrder(World):
    def setUp(self):
        super().setUp()
        Holding.objects.all().delete()
        names = ["zzz-top", "aaa-low", "mmm-mid", "ccc", "yyy"]
        self.ids = [
            Holding.objects.create(name=n, brokerage=self.A, portfolio=self.pA).pk
            for n in names
        ]
        self.by_name = [
            str(pk) for _n, pk in sorted(zip(names, self.ids), key=lambda x: x[0])
        ]
        self.heA = HoldingEvent.objects.create(status="x", brokerage=self.A)
        patcher = mock.patch.object(Holding._meta, "ordering", ["name"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self._as(self.anaA)

    def _dropdown(self, url):
        html = self.client.get(url, HTTP_ACCEPT="text/html").content.decode()
        select = re.search(r'<select[^>]*name="holding".*?</select>', html, re.S)
        self.assertIsNotNone(select, "no holding dropdown rendered")
        return re.findall(r'<option value="(\d+)"', select.group(0))

    def test_precondition_the_column_is_hidden_and_not_orderable(self):
        row = self.client.get(f"/api/holdings/{self.ids[0]}/").json()
        self.assertNotIn("name", row)
        listed = self.client.get("/api/holdings/?ordering=name").json()["data"]
        self.assertEqual([r["id"] for r in listed], sorted(self.ids))

    def test_create_form_dropdown_does_not_follow_hidden_column(self):
        options = self._dropdown("/api/holdingevents/?format=api")
        self.assertEqual(sorted(options, key=int), [str(pk) for pk in self.ids])
        self.assertNotEqual(
            options,
            self.by_name,
            "the dropdown lists holdings in the order of Holding.name, a "
            "column the analyst can't read",
        )

    def test_update_form_dropdown_does_not_follow_hidden_column(self):
        options = self._dropdown(f"/api/holdingevents/{self.heA.pk}/?format=api")
        self.assertNotEqual(options, self.by_name)


# ===========================================================================
# Finding 2 (low, any database): a `__year` filter outside 1..9999 is a 500.
# django-filter's NumberFilter (views._filter_lookups offers `year` on date
# fields) and ORFilterBackend (_filter_value parses it as an IntegerField)
# both accept 0, -1, 10000 or 0.5; Django builds the year's bounds as Python
# datetimes when it compiles the SQL and raises ValueError("year 0 is out of
# range"), after every guard. Any caller who can read a date column.
# ===========================================================================


class TestFindingYearFilterOutOfRange(World):
    def setUp(self):
        super().setUp()
        SampleModel.objects.create(
            title="s", price=1, related=RelatedModel.objects.create(name="r")
        )
        self._as(self._user("viewer1", ["viewer"], self.A))

    def test_django_filter_year_is_400(self):
        for value in ("0", "-1", "10000", "0.5"):
            resp = self.client.get(f"/api/samplemodels/?created_at__year={value}")
            self.assertEqual(resp.status_code, 400, (value, resp.content[:200]))

    def test_or_backend_year_lookup_is_400(self):
        for query in ("created_at__year__gt=0", "created_at__year_or=0"):
            resp = self.client.get(f"/api/samplemodels/?{query}")
            self.assertEqual(resp.status_code, 400, (query, resp.content[:200]))

    def test_date_field_year_is_400(self):
        resp = self.client.get("/api/samplemodels/?published_date__year=0")
        self.assertEqual(resp.status_code, 400, resp.content[:200])

    def test_tenant_scoped_model_year_is_400(self):
        self._as(self.anaA)
        resp = self.client.get("/api/valuations/?valued_at__year=0")
        self.assertEqual(resp.status_code, 400, resp.content[:200])


# ===========================================================================
# Finding 3 (low, PostgreSQL 500; precondition shown on SQLite): a NUL byte in
# a filter value reaches the database through ORFilterBackend. django-filter's
# CharField and DRF's search refuse it ("Null characters are not allowed",
# 400); _parse_value returns text lookups' raw values and model
# CharField.to_python doesn't check, so `?title_or=%00`,
# `?title__contains=%00`, `?title__in=a,%00` ... bind '\x00' as a SQL
# parameter. psycopg refuses a NUL in a string literal (psycopg2 ValueError,
# psycopg 3 DataError): a 500, anonymous included on a public model.
# ===========================================================================


class TestFindingNulInFilters(World):
    QUERIES = (
        "title_or=%00",
        "title__contains=%00",
        "title__startswith=a%00",
        "title__in=a,%00",
        "title__iexact=%00",
    )

    def _nul_bound(self, url):
        bound = []

        def spy(execute, sql, params, many, context):
            bound.extend(str(p) for p in (params or ()))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(spy):
            resp = self.client.get(url)
        return resp, any("\x00" in p for p in bound)

    def test_reference_django_filter_refuses_nul(self):
        self._as(self.mgrA)
        self.assertEqual(self.client.get("/api/deals/?title=%00").status_code, 400)

    def test_or_and_lookup_filters_refuse_nul(self):
        self._as(self.mgrA)
        for query in self.QUERIES:
            resp, nul = self._nul_bound(f"/api/deals/?{query}")
            self.assertFalse(nul, f"{query}: NUL bound as a SQL parameter")
            self.assertEqual(resp.status_code, 400, query)

    def test_anonymous_on_a_public_model(self):
        CompiledSampleModel.objects.create(
            title="c", price=1, related=RelatedModel.objects.create(name="r")
        )
        self._as(None)
        resp, nul = self._nul_bound("/api/compiledsamplemodels/?title_or=%00")
        self.assertFalse(nul, "anonymous NUL bound as a SQL parameter")
        self.assertEqual(resp.status_code, 400)


# ===========================================================================
# Finding 4 (low, SQLite / Oracle only): `?<jsonfield>__contains=` is a 500.
# ORFilterBackend accepts `contains` on a readable JSONField; Django raises
# NotSupportedError when it compiles the SQL on backends without JSON
# containment.
# ===========================================================================


@override_settings(**PLATFORM)
class TestFindingJsonContains(World):
    def test_json_contains_is_not_a_500(self):
        PlatformJob.objects.create(brokerage=self.A, kind="sync", payload={"n": 1})
        staff = self._user("padmin", [], None, is_staff=True)
        staff._test_platform_roles = ["admin"]  # holds platformjob.payload.read
        self._as(staff)
        for value in ('{"n":1}', "x"):
            resp = self.client.get(
                f"/console-api/jobs/?payload__contains={quote(value)}"
            )
            self.assertNotEqual(resp.status_code, 500, (value, resp.content[:200]))


# ===========================================================================
# Held: attack classes that didn't get through.
# ===========================================================================


class TestHeldTenantRowsLookMissing(World):
    """Another tenant's row (or an owner-hidden one) answers exactly like a
    pk that doesn't exist, on every method."""

    CASES = (
        ("deals", "dB", "mgrA"),
        ("deals", "dA2", "uwA"),  # owner-hidden within the tenant
        ("bankaccounts", "baB", "mgrA"),
        ("transactions", "txB", "mgrA"),
        ("holdingevents", "heB", "anaA"),
        ("platform-tickets", "ptB", "accA"),
    )

    def test_every_method(self):
        for endpoint, row, user in self.CASES:
            self._as(getattr(self, user))
            pk = getattr(self, row).pk
            for method in ("get", "head", "options", "put", "patch", "delete"):
                send = getattr(self.client, method)
                args = ({},) if method in ("put", "patch") else ()
                hidden = send(f"/api/{endpoint}/{pk}/", *args, format="json")
                missing = send(f"/api/{endpoint}/987654/", *args, format="json")
                self.assertEqual(
                    (hidden.status_code, hidden.content),
                    (missing.status_code, missing.content),
                    (endpoint, row, method),
                )

    def test_custom_action(self):
        self._as(self._user("norole", [], self.A))
        hidden = self.client.post(f"/api/holdingevents/{self.heB.pk}/reprocess/")
        missing = self.client.post("/api/holdingevents/987654/reprocess/")
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.content, missing.content)


class TestHeldWrites(World):
    def test_cross_tenant_and_hidden_targets_get_one_message(self):
        self._as(self.uwA)
        hidden = self.client.post(
            "/api/bankaccounts/", {"name": "x", "deal": self.dB.pk}, format="json"
        )
        owner_hidden = self.client.post(
            "/api/bankaccounts/", {"name": "x", "deal": self.dA2.pk}, format="json"
        )
        missing = self.client.post(
            "/api/bankaccounts/", {"name": "x", "deal": 987654}, format="json"
        )
        self.assertEqual(hidden.content, missing.content)
        self.assertEqual(owner_hidden.content, missing.content)

    def test_tenant_and_owner_columns(self):
        self._as(self.uwA)
        url = f"/api/deals/{self.dA1.pk}/"
        for body in (
            {"brokerage": self.B.pk},
            {"assigned_broker": self.uwA2.pk},
            {"assigned_broker": self.uwB.pk},
            {"assigned_broker": None},
        ):
            self.assertEqual(
                self.client.patch(url, body, format="json").status_code, 400
            )
        form = "title=f&brokerage=%d&brokerage=%d" % (self.A.pk, self.B.pk)
        resp = self.client.patch(
            url, form, content_type="application/x-www-form-urlencoded"
        )
        self.assertEqual(resp.status_code, 400)
        self.dA1.refresh_from_db()
        self.assertEqual(
            (self.dA1.brokerage_id, self.dA1.assigned_broker_id),
            (self.A.pk, self.uwA.pk),
        )

    def test_platform_only_and_unconfigured_keys_are_ignored(self):
        self._as(self.accA)
        resp = self.client.post(
            "/api/platform-tickets/",
            {
                "subject": "s",
                "internal_note": "x",
                "brokerage": self.B.pk,
                "deal": self.dB.pk,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        row = PlatformTicket.objects.get(pk=resp.json()["id"])
        self.assertEqual(
            (row.brokerage_id, row.deal_id, row.internal_note), (self.A.pk, None, "")
        )

    @override_settings(
        TURBODRF_ROLES={
            **settings.TURBODRF_ROLES,
            "worker": [
                "test_app.rbacboard.read",
                "test_app.rbacboard.update",
                "test_app.deal.read",
            ],
        }
    )
    def test_hidden_m2m_links_survive_a_write(self):
        worker = self._user("worker", ["worker"], self.A)
        board = RbacBoard.objects.create(title="b", owner=worker)
        board.deals.add(self.dA2, self.dB)
        self._as(worker)
        resp = self.client.patch(
            f"/api/rbacboards/{board.pk}/", {"deals": []}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(board.deals.values_list("pk", flat=True)), {self.dA2.pk, self.dB.pk}
        )


@override_settings(
    TURBODRF_ROLES={
        **settings.TURBODRF_ROLES,
        "worker": ["test_app.scopeitem.read", "test_app.scopefolder.read"],
    }
)
class TestHeldNestedFilters(World):
    """Every lookup on a nested path is scoped to the caller's targets."""

    def test_lookup_variants_dont_match_a_hidden_folder(self):
        worker = self._user("worker", ["worker"], self.A)
        other = self._user("other", ["worker"], self.A)
        hidden = ScopeFolder.objects.create(
            name="other-folder-secret", brokerage=self.A, owner=other
        )
        mine = ScopeFolder.objects.create(name="mine", brokerage=self.A, owner=worker)
        item = ScopeItem.objects.create(label="i", brokerage=self.A, folder=hidden)
        own = ScopeItem.objects.create(label="j", brokerage=self.A, folder=mine)
        self._as(worker)
        # The filter applies (it isn't dropped): it matches the caller's folder.
        rows = self.client.get("/api/scopeitems/?folder__name=mine").json()["data"]
        self.assertEqual([r["id"] for r in rows], [own.pk])
        for query in (
            "folder__name=other-folder-secret",
            "folder__name__icontains=secret",
            "folder__name__gt=n",
            "folder__name__gte=other",
            "folder__name__range=o,p",
            "folder__name_or=other-folder-secret",
            "folder__name__in=other-folder-secret",
            f"folder={hidden.pk}",
        ):
            rows = self.client.get(f"/api/scopeitems/?{query}").json()["data"]
            self.assertNotIn(item.pk, [r["id"] for r in rows], query)
        row = self.client.get(f"/api/scopeitems/{item.pk}/").json()
        self.assertIsNone(row["folder_name"])


class TestHeldFieldRules(World):
    """HoldingEvent.holding has a read rule auditors lack: no route reads it."""

    def test_auditor_cant_reach_holding(self):
        self._as(self.audA)
        base = self.client.get("/api/holdingevents/").json()["data"]
        for query in (
            "search=A-hold",
            f"holding={self.hA.pk}",
            "holding__name=A-hold",
            "holding__name=nope",
            "holding__in=987654",
            "ordering=-holding",
        ):
            rows = self.client.get(f"/api/holdingevents/?{query}").json()["data"]
            if query.startswith("search"):
                self.assertEqual(rows, [], query)
            else:
                self.assertEqual(rows, base, query)
        detail = self.client.get(
            f"/api/holdingevents/{self.heA.pk}/?fields=holding_label,holding"
        ).json()
        self.assertEqual(detail, {})


@override_settings(**PLATFORM)
class TestHeldPlatformGate(World):
    URLS = (
        "/console-api/jobs/",
        "/console-api/jobs/1/",
        "/console-api/jobs/ping/",
        "/console-api/jobs/ping.json",
        "/console-api/jobs/1/retry/",
        "/console-api/accounts/",
        "/console-api/tickets/",
        "/console-swagger/?format=openapi",
        "/console-openapi/",
    )

    def test_non_staff_get_nothing(self):
        holder = self._user("np", ["manager"], self.A)
        holder._test_platform_roles = ["admin"]  # platform roles, not staff
        for who in (None, self.mgrA, holder):
            self._as(who)
            for url in self.URLS:
                for method in ("get", "options", "post"):
                    resp = getattr(self.client, method)(url)
                    self.assertIn(resp.status_code, (401, 403), (who, method, url))


class TestHeldNoServerErrors(World):
    """Odd pks and query parameters across every tenant route."""

    def test_odd_pks_and_params(self):
        import tests.urls as urls

        god = self._user("god", list(settings.TURBODRF_ROLES), self.A)
        self._as(god)
        endpoints = sorted(
            {
                str(p.pattern).strip("^$").split("/")[0]
                for p in urls.router.urls
                if str(p.pattern).strip("^$") and "format" not in str(p.pattern)
            }
        )
        pks = ["-1", "99999999999999999999999", "%EF%BC%91", "%00", "1%20OR%201=1"]
        params = [
            "page=99999999999999999999",
            "page_size=abc",
            "ordering=--id",
            "id__in=a,b",
            "id__range=1",
            "id__isnull=maybe",
            "id__gt=1e400",
            "id=-9223372036854775809",
            "foo__bar__baz__qux=1",
            "format=xml",
            "ordering=" + ",".join(["id"] * 200),
        ]
        for endpoint in endpoints:
            for pk in pks:
                for method in ("get", "patch", "delete"):
                    resp = getattr(self.client, method)(f"/api/{endpoint}/{pk}/")
                    self.assertLess(resp.status_code, 500, (endpoint, pk, method))
            for query in params:
                resp = self.client.get(f"/api/{endpoint}/?{query}")
                self.assertLess(resp.status_code, 500, (endpoint, query))
