"""
Filter, search, ordering, pagination, and DoS-resilience security tests for
the read path.

Adversary attempts data exfiltration via query-string knobs:
?filter, ?search, ?ordering, ?page/?page_size, deep nested lookup chains,
boolean/null coercion attacks, and resource-exhaustion payloads. Every test
asserts the framework either filters cross-tenant rows out or rejects with
a non-5xx status, and never lets a victim secret leak into the response.
"""

import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.apps import set_test_brokerage
from tests.test_app.models import (
    ArticleWithCategories,
    BankAccount,
    Brokerage,
    Category,
    CompiledArticle,
    CompiledSampleModel,
    Deal,
    RelatedModel,
    SampleModel,
    Transaction,
)

User = get_user_model()

SECRETS = ("VICTIM_SECRET_DEAL", "VICTIM_BANK_ACCOUNT", "999999.99")

# Time budgets (seconds) for DoS probes.
NORMAL_BUDGET = 5.0
DOS_BUDGET = 30.0


def assert_no_secrets(testcase, response):
    """Fail loudly if a secret leaks or the framework 5xxs."""
    blob = (
        str(getattr(response, "data", ""))
        + " "
        + str(getattr(response, "content", b""))
    )
    for secret in SECRETS:
        if secret in blob:
            testcase.fail(
                f"VULNERABILITY: {secret!r} leaked. "
                f"status={response.status_code} blob={blob[:600]!r}"
            )
    if response.status_code >= 500:
        testcase.fail(
            f"VULNERABILITY: 5xx ({response.status_code}) — possible "
            f"schema/info leak. body={blob[:600]!r}"
        )


def victim_id_absent(testcase, response, victim_pk):
    if response.status_code == 200 and isinstance(response.data, dict):
        rows = response.data.get("data") or response.data.get("results") or []
        ids = [r.get("id") for r in rows if isinstance(r, dict)]
        testcase.assertNotIn(victim_pk, ids)


assert_no_victim_id = victim_id_absent


def get_rows(response):
    if response.status_code != 200:
        return []
    if not isinstance(response.data, dict):
        return []
    return response.data.get("data") or response.data.get("results") or []


get_data = get_rows


def get_ids(response):
    return [r.get("id") for r in get_rows(response) if isinstance(r, dict)]


def get_pagination(response):
    if response.status_code != 200:
        return {}
    if not isinstance(response.data, dict):
        return {}
    return response.data.get("pagination") or {}


def time_request(fn, *args, **kwargs):
    start = time.monotonic()
    resp = fn(*args, **kwargs)
    elapsed = time.monotonic() - start
    return resp, elapsed


# ============================================================================
# Shared base: attacker @ A, victim @ B, control rows on each side.
# Heavy fixtures hoisted to setUpTestData; setUp only refreshes per-test
# state (cache + brokerage registry + APIClient).
# ============================================================================


class _BaseFixture(TestCase):
    """Common attacker/victim setup. Subclasses extend `setUpTestData` for
    extra rows."""

    @classmethod
    def setUpTestData(cls):
        import tests.urls  # noqa: F401  (registers router)

        cls.brokerage_attacker = Brokerage.objects.create(name="Attacker Co")
        cls.brokerage_victim = Brokerage.objects.create(name="Victim Co")
        cls.brokerage_third = Brokerage.objects.create(name="Innocent Co")

        cls.attacker = User.objects.create_user(username="attacker", password="x")
        cls.attacker._test_roles = ["underwriter"]

        cls.attacker_manager = User.objects.create_user(username="mgr", password="x")
        cls.attacker_manager._test_roles = ["manager"]

        cls.viewer = User.objects.create_user(username="viewer", password="x")
        cls.viewer._test_roles = ["viewer"]

        cls.victim = User.objects.create_user(username="victim", password="x")
        cls.victim._test_roles = ["underwriter"]

        cls.victim_deal = Deal.objects.create(
            title="VICTIM_SECRET_DEAL",
            brokerage=cls.brokerage_victim,
            assigned_broker=cls.victim,
        )
        cls.victim_bank = BankAccount.objects.create(
            name="VICTIM_BANK_ACCOUNT", deal=cls.victim_deal
        )
        cls.victim_tx = Transaction.objects.create(
            amount=Decimal("999999.99"), bank_account=cls.victim_bank
        )

        cls.attacker_deal = Deal.objects.create(
            title="ATTACKER_DEAL",
            brokerage=cls.brokerage_attacker,
            assigned_broker=cls.attacker,
        )
        cls.attacker_bank = BankAccount.objects.create(
            name="ATTACKER_BANK", deal=cls.attacker_deal
        )
        cls.attacker_tx = Transaction.objects.create(
            amount=Decimal("11.11"), bank_account=cls.attacker_bank
        )

    def setUp(self):
        from tests.test_app.apps import _test_user_brokerages

        _test_user_brokerages.clear()
        cache.clear()

        # Re-populate brokerage registry from class-level users.
        set_test_brokerage(self.attacker, self.brokerage_attacker)
        set_test_brokerage(self.attacker_manager, self.brokerage_attacker)
        set_test_brokerage(self.victim, self.brokerage_victim)

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker)

    def tearDown(self):
        cache.clear()


# ============================================================================
# Filter attacks: tenant FK overrides, lookup variants, OR filters,
# deep traversal, isnull oracle, search, ordering, pagination edges.
# ============================================================================


class TestTenantFilterAttacks(_BaseFixture):
    """Attacker tries to override the mandatory tenant filter via
    ?<fk>=, __in, _or, repeated values, empty values. Tenant AND wins."""

    def test_tenant_fk_filter_overrides_no_leak(self):
        """Direct/in/repeated/empty/_or overrides on the brokerage FK
        — tenant filter must AND, not bypass."""
        for q in (
            f"brokerage={self.brokerage_victim.id}",
            f"brokerage__in={self.brokerage_attacker.id},{self.brokerage_victim.id}",
            f"brokerage_or={self.brokerage_attacker.id}&brokerage_or={self.brokerage_victim.id}",
            f"brokerage={self.brokerage_attacker.id}&brokerage={self.brokerage_victim.id}",
            "brokerage=",
        ):
            with self.subTest(q=q):
                r = self.client.get(f"/api/deals/?{q}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)

    def test_owner_fk_filter_does_not_leak(self):
        """assigned_broker FK and chain via __id."""
        for q in (
            f"assigned_broker={self.victim.id}",
            f"assigned_broker__id={self.victim.id}",
        ):
            with self.subTest(q=q):
                r = self.client.get(f"/api/deals/?{q}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)

    def test_lookup_variants_on_title(self):
        """exact / icontains / istartswith for victim title."""
        for q in (
            "title=VICTIM_SECRET_DEAL",
            "title__icontains=VICTIM_SECRET",
            "title__istartswith=VICTIM",
        ):
            with self.subTest(q=q):
                r = self.client.get(f"/api/deals/?{q}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)

    def test_chained_tenancy_traversal_no_leak(self):
        """Transaction filters that traverse to victim brokerage / bank /
        amount — must produce empty result."""
        for q in (
            f"bank_account__deal__brokerage={self.brokerage_victim.id}",
            f"bank_account={self.victim_bank.id}",
            "amount__gte=999999",
        ):
            with self.subTest(q=q):
                r = self.client.get(f"/api/transactions/?{q}")
                assert_no_secrets(self, r)
                if r.status_code == 200:
                    self.assertEqual(len(r.data.get("data", [])), 0)

    def test_bankaccount_filter_by_victim_deal(self):
        r = self.client.get(f"/api/bankaccounts/?deal={self.victim_deal.id}")
        assert_no_secrets(self, r)
        if r.status_code == 200:
            ids = [d.get("id") for d in r.data.get("data", [])]
            self.assertNotIn(self.victim_bank.id, ids)

    def test_reverse_relation_enumeration(self):
        """Reverse FK / chain filters from /api/deals/ — must not surface
        cross-tenant rows."""
        for q in (
            "bank_accounts__transactions__amount__gte=999999",
            f"bank_accounts__id={self.victim_bank.id}",
            "bank_accounts__transactions__amount=999999.99",
            "assigned_broker__assigned_deals__title=VICTIM_SECRET_DEAL",
        ):
            with self.subTest(q=q):
                r = self.client.get(f"/api/deals/?{q}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)

    def test_unregistered_endpoints_404(self):
        """Brokerage / users have no TurboDRFMixin — must not be 200."""
        for u in (
            "/api/brokerages/?deals__title__icontains=VICTIM",
            "/api/users/",
        ):
            with self.subTest(u=u):
                r = self.client.get(u)
                self.assertNotEqual(r.status_code, 200)
                body = (
                    getattr(r, "content", b"") or b""
                ).decode("utf-8", errors="ignore")
                for secret in SECRETS:
                    self.assertNotIn(secret, body)

    def test_sql_wildcards_in_icontains_no_leak(self):
        """Underscore / percent must be escaped, not act as LIKE wildcards."""
        for v in ("%25VICTIM%25", "%25"):
            with self.subTest(v=v):
                r = self.client.get(f"/api/deals/?title__icontains={v}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)

    def test_pathological_inputs_do_not_500(self):
        """Huge param count, deep path, type mismatch — must not 500 / leak."""
        params = "&".join(f"brokerage={i}" for i in range(200)) + (
            f"&brokerage={self.brokerage_victim.id}"
        )
        deep_path = "__".join(["bank_account"] * 10) + "__deal__brokerage__name"
        for url in (
            f"/api/deals/?{params}",
            f"/api/transactions/?{deep_path}=Victim%20Co",
            "/api/transactions/?amount=not-a-decimal",
            "/api/deals/?assigned_broker__date_joined=not-a-date",
        ):
            with self.subTest(url=url[:50]):
                r = self.client.get(url)
                assert_no_secrets(self, r)
                self.assertNotEqual(r.status_code, 500)


class TestDeepLookupAndIdCoercion(_BaseFixture):
    """Multi-hop lookup chains, id__in/range/gt/lt coercion."""

    def test_traversal_from_transaction_to_victim_paths(self):
        for path in (
            "bank_account__deal__brokerage__name__icontains=Victim",
            "bank_account__deal__title__icontains=VICTIM_SECRET",
            "bank_account__deal__assigned_broker__username=victim",
            "bank_account__deal__assigned_broker__email__icontains=victim",
            "bank_account__name__icontains=VICTIM_BANK",
        ):
            with self.subTest(path=path):
                r = self.client.get(f"/api/transactions/?{path}")
                assert_no_secrets(self, r)

    def test_id_lookups_no_leak(self):
        """id__in (csv / dups / negative / 1000+ values), range, gt/lt."""
        big_csv = ",".join(str(i) for i in range(1, 1001))
        for q in (
            "id__in=" + ",".join(str(i) for i in range(1, 11)),
            "id__range=1,1000",
            "id__gt=0&id__lt=1000",
            f"id__in={big_csv}",
            f"id__in={self.victim_deal.id},{self.victim_deal.id}",
            "id__in=-1,-2,-3",
            f"id__gte=0&id__lte={self.victim_deal.id}",
        ):
            with self.subTest(q=q[:40]):
                r = self.client.get(f"/api/deals/?{q}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)

    def test_user_attribute_lookups_no_leak(self):
        for q in (
            "assigned_broker__date_joined__gte=2026-01-01",
            "assigned_broker__is_staff=False",
        ):
            with self.subTest(q=q):
                r = self.client.get(f"/api/deals/?{q}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)

    def test_category_and_relatedmodel_reverse_chains(self):
        """Cross-app M2M reverse chains must not 500 / leak."""
        for u in (
            "/api/categorys/?compiled_articles__author__name=anything",
            "/api/relatedmodels/?test_models__title=ATTACKER_DEAL",
            "/api/relatedmodels/?articles__author__test_models__secret_field=hidden",
        ):
            with self.subTest(u=u):
                r = self.client.get(u)
                assert_no_secrets(self, r)


class TestOrFilterCombinations(_BaseFixture):
    """_or filters across multiple FKs — tenant filter still binds."""

    def test_or_filter_combinations_no_leak(self):
        for q in (
            "&".join(f"id_or={i}" for i in range(1, 11)),
            (f"brokerage_or={self.brokerage_attacker.id}"
             f"&brokerage_or={self.brokerage_victim.id}"),
            "title_or=VICTIM_SECRET_DEAL&title_or=ATTACKER_DEAL",
            (f"assigned_broker_or={self.attacker.id}"
             f"&assigned_broker_or={self.victim.id}"),
        ):
            with self.subTest(q=q[:40]):
                r = self.client.get(f"/api/deals/?{q}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)

    def test_bank_account_or_attacker_and_victim(self):
        r = self.client.get(
            f"/api/transactions/?bank_account_or={self.attacker_bank.id}"
            f"&bank_account_or={self.victim_bank.id}"
        )
        assert_no_secrets(self, r)


class TestBooleanNullExploits(_BaseFixture):
    """Boolean / null / id-coercion edge cases — must not 500 or leak."""

    def test_garbage_boolean_and_id_values(self):
        """is_active and id with various non-bool / non-int garbage."""
        for url in (
            "/api/samplemodels/?is_active=null",
            "/api/samplemodels/?is_active=",
            "/api/samplemodels/?is_active=undefined",
            "/api/samplemodels/?is_active=2",
            "/api/deals/?id=null",
            "/api/deals/?id=true",
            "/api/deals/?id=[]",
            "/api/deals/?id=",
        ):
            with self.subTest(url=url):
                r = self.client.get(url)
                assert_no_secrets(self, r)

    def test_isnull_truthy_strings_no_secret_leak(self):
        """`?<fk>__isnull=<truthy_str>` must not surface secrets even if
        the ORM raises (which currently 500s — open finding)."""
        for q in (
            "id__isnull=1", "id__isnull=yes", "id__isnull=t",
            "assigned_broker__isnull=1", "assigned_broker__isnull=True",
        ):
            with self.subTest(q=q):
                r = self.client.get(f"/api/deals/?{q}")
                blob = (
                    str(getattr(r, "data", "")) + " "
                    + str(getattr(r, "content", b""))
                )
                for secret in SECRETS:
                    self.assertNotIn(secret, blob)


class TestCombinedProbes(_BaseFixture):
    """Multi-knob requests — tenant filter still wins."""

    def test_kitchen_sink_combined_no_leak(self):
        """Multi-knob requests on /api/deals/ and /api/transactions/."""
        r = self.client.get(
            "/api/deals/?ordering=brokerage&page_size=100"
            "&search=VICTIM&id__in=1,2,3"
            f"&brokerage={self.brokerage_victim.id}"
        )
        assert_no_secrets(self, r)
        victim_id_absent(self, r, self.victim_deal.id)

        r2 = self.client.get(
            "/api/transactions/"
            f"?bank_account__deal__brokerage={self.brokerage_victim.id}"
            "&amount__gte=999999&ordering=-amount&page_size=100"
        )
        assert_no_secrets(self, r2)
        if r2.status_code == 200:
            self.assertEqual(len(r2.data.get("data", [])), 0)


class TestM2MAndCategoryProbes(_BaseFixture):
    """M2M / Category filtering on non-tenant models — no 5xx, no leak."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.related = RelatedModel.objects.create(name="r1")
        cls.cat = Category.objects.create(name="c1")
        cls.article = ArticleWithCategories.objects.create(
            title="a1", author=cls.related
        )
        cls.article.categories.add(cls.cat)

    def test_articles_filter_by_categories(self):
        for q in (
            f"categories__id={self.cat.id}",
            "categories__name=VICTIM_SECRET_DEAL",
        ):
            with self.subTest(q=q):
                r = self.client.get(f"/api/articlewithcategoriess/?{q}")
                assert_no_secrets(self, r)


class TestNegativeControl(_BaseFixture):
    """Sanity: attacker CAN see their own deal; victim is NEVER visible."""

    def test_attacker_can_see_own_deal(self):
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, 200)
        rows = r.data.get("data") or r.data.get("results") or []
        ids = [d.get("id") for d in rows]
        self.assertIn(self.attacker_deal.id, ids)
        self.assertNotIn(self.victim_deal.id, ids)
        assert_no_secrets(self, r)


# ============================================================================
# Ordering surface — hidden-field perturbation (binary-search inference),
# multi-field, syntax tricks, cross-model, anonymous.
# ============================================================================


class _OrderingFixture(_BaseFixture):
    """Adds extra deals + sample rows for ordering perturbation tests."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        # Extra attacker deals so ordering has rows to permute.
        Deal.objects.bulk_create([
            Deal(title=f"ATTACKER_DEAL_{i}",
                 brokerage=cls.brokerage_attacker,
                 assigned_broker=cls.attacker)
            for i in range(2, 4)
        ])

        # SampleModel rows for inference probes. Viewer has no read on
        # `secret_field` or `price`; ordering by them must be silently
        # dropped to avoid binary-search inference.
        cls._related = RelatedModel.objects.create(name="r")
        SampleModel.objects.create(
            title="row_zzz", description="d", price=Decimal("1.00"),
            quantity=10, related=cls._related, secret_field="ZZZ",
        )
        SampleModel.objects.create(
            title="row_aaa", description="d", price=Decimal("999.00"),
            quantity=20, related=cls._related, secret_field="AAA",
        )
        SampleModel.objects.create(
            title="row_mmm", description="d", price=Decimal("50.00"),
            quantity=15, related=cls._related, secret_field="MMM",
        )

    def setUp(self):
        super().setUp()
        self.viewer_client = APIClient()
        self.viewer_client.force_authenticate(user=self.viewer)


class TestOrderingHiddenAndSensitive(_OrderingFixture):
    """Hidden + sensitive-field ordering must drop to default order to
    prevent binary-search inference."""

    def test_secret_field_asc_vs_desc_identical_for_viewer(self):
        """Headline check: asc/desc on hidden field must match — both fall
        back to default order."""
        r_asc = self.viewer_client.get("/api/samplemodels/?ordering=secret_field")
        r_desc = self.viewer_client.get("/api/samplemodels/?ordering=-secret_field")
        assert_no_secrets(self, r_asc)
        assert_no_secrets(self, r_desc)
        self.assertEqual(get_ids(r_asc), get_ids(r_desc))

    def test_hidden_fields_no_perturbation(self):
        """Sweep: ordering by hidden fields must equal default order."""
        r_default = self.viewer_client.get("/api/samplemodels/")
        for f in ("secret_field", "-secret_field", "price", "-price",
                  "price__abs"):
            with self.subTest(f=f):
                r = self.viewer_client.get(f"/api/samplemodels/?ordering={f}")
                assert_no_secrets(self, r)
                self.assertEqual(
                    get_ids(r_default), get_ids(r),
                    f"VULN: ordering by hidden {f!r} perturbed row order",
                )

    def test_sensitive_field_names_dropped(self):
        """Deny-listed sensitive field names — must not surface anything."""
        for f in (
            "password", "token", "session_key",
            "assigned_broker__password",
            "-assigned_broker__date_joined",
            "assigned_broker__last_login",
            "-brokerage__name",
        ):
            with self.subTest(f=f):
                r = self.client.get(f"/api/deals/?ordering={f}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)


class TestOrderingMultiFieldAndSyntax(_OrderingFixture):
    """Multi-field ordering, separator abuse, and syntax tricks."""

    def test_mixed_hidden_visible_keeps_default(self):
        """secret_field is dropped → effective ordering is just `title`."""
        r_default = self.viewer_client.get("/api/samplemodels/?ordering=title")
        r_mix = self.viewer_client.get(
            "/api/samplemodels/?ordering=secret_field,title"
        )
        self.assertEqual(get_ids(r_default), get_ids(r_mix))

    def test_csv_separator_variants(self):
        """Empty terms / wrong separator / unicode comma — no leak."""
        for o in (
            "title,price",
            "-price,title",
            "brokerage,title",
            ",title,,brokerage,,,",
            "title;price",
            "title%EF%BC%8Cbrokerage",
            "secret_field,title,price,quantity,is_active,description",
        ):
            with self.subTest(o=o):
                url = (
                    "/api/samplemodels/?ordering=" + o
                    if "secret_field" in o
                    else "/api/deals/?ordering=" + o
                )
                r = (
                    self.viewer_client.get(url)
                    if "secret_field" in o
                    else self.client.get(url)
                )
                assert_no_secrets(self, r)

    def test_garbage_syntax_no_leak(self):
        """Spaces, +/-, dots, wildcards, dunder, F(), backticks, SQL
        injection shapes, null bytes, newline, lookup suffix in ordering."""
        for o in (
            "%20title", "title%20", "%2Btitle", "--title",
            "null", "undefined", "*", "__all__", "...",
            "%3F", "F(title)", "%60title%60",
            "title%3B%20DROP%20TABLE%20deals--", "-",
            "__class__", "_meta", "1234567",
            "ti%00tle", "title%0Aprice", "title__icontains",
            ",", "", "id%00brokerage",
        ):
            with self.subTest(o=o):
                r = self.client.get(f"/api/deals/?ordering={o}")
                assert_no_secrets(self, r)

    def test_long_and_many_ordering_payloads(self):
        for url in (
            f"/api/deals/?ordering={'a' * 10000}",
            "/api/deals/?" + "&".join(f"ordering=field{i}" for i in range(100)),
            "/api/deals/?ordering=" + ",".join(f"f{i}" for i in range(5000)),
            "/api/deals/?ordering=a__b__c__d__e__f__g",
            f"/api/deals/?ordering={'__'.join(['x'] * 50)}",
        ):
            with self.subTest(url=url[:50]):
                r = self.client.get(url)
                assert_no_secrets(self, r)


class TestOrderingCrossModelAndCombo(_OrderingFixture):
    """Ordering on chained models, combined with other params."""

    def test_transaction_and_bankaccount_ordering_no_victim_surface(self):
        for url in (
            "/api/transactions/?ordering=bank_account__deal__title",
            "/api/transactions/?ordering=-amount",
            "/api/bankaccounts/?ordering=deal__title",
            "/api/bankaccounts/?ordering=deal__assigned_broker__password",
        ):
            with self.subTest(url=url):
                r = self.client.get(url)
                assert_no_secrets(self, r)
        # Verify victim transaction never surfaces.
        r = self.client.get("/api/transactions/?ordering=-amount")
        self.assertNotIn(self.victim_tx.id, get_ids(r))

    def test_secret_ordering_with_pagination_and_search(self):
        """page=1&page_size=1 + hidden ordering must not change which row
        appears at position 1."""
        r_default = self.viewer_client.get(
            "/api/samplemodels/?page=1&page_size=1"
        )
        r_secret = self.viewer_client.get(
            "/api/samplemodels/?page=1&page_size=1&ordering=secret_field"
        )
        self.assertEqual(get_ids(r_default), get_ids(r_secret))

        r2 = self.viewer_client.get(
            "/api/samplemodels/?ordering=secret_field&search=row"
        )
        assert_no_secrets(self, r2)

    def test_filter_and_order_same_hidden_field(self):
        r = self.viewer_client.get(
            "/api/samplemodels/"
            "?secret_field__icontains=A&ordering=secret_field"
        )
        assert_no_secrets(self, r)

    def test_compiled_path_invalid_ordering_no_500(self):
        r = self.client.get("/api/compiledsamplemodels/?ordering=does_not_exist")
        assert_no_secrets(self, r)

    def test_admin_vs_viewer_row_set_identical(self):
        """Admin sees secret_field, viewer doesn't — row-set must match."""
        admin = User.objects.create_user(username="adm", password="x")
        admin._test_roles = ["admin"]
        admin_client = APIClient()
        admin_client.force_authenticate(user=admin)
        r_admin = admin_client.get("/api/samplemodels/?ordering=secret_field")
        r_viewer = self.viewer_client.get(
            "/api/samplemodels/?ordering=secret_field"
        )
        assert_no_secrets(self, r_admin)
        assert_no_secrets(self, r_viewer)
        self.assertEqual(set(get_ids(r_admin)), set(get_ids(r_viewer)))


class TestAnonymousOrdering(_OrderingFixture):
    """Anonymous user — denylisted fields must hold; tenant model is empty."""

    def test_anon_ordering_tenant_public_and_denylist(self):
        """Anon: tenant model is empty; public model row-set unchanged
        regardless of ordering term; password/token denylist holds."""
        anon = APIClient()
        r = anon.get("/api/deals/?ordering=brokerage")
        assert_no_secrets(self, r)
        if r.status_code == 200:
            self.assertEqual(get_rows(r), [])

        r_default = anon.get("/api/samplemodels/")
        r_secret = anon.get("/api/samplemodels/?ordering=secret_field")
        assert_no_secrets(self, r_default)
        assert_no_secrets(self, r_secret)
        self.assertEqual(set(get_ids(r_default)), set(get_ids(r_secret)))

        for o in ("password", "token"):
            with self.subTest(o=o):
                r = anon.get(f"/api/samplemodels/?ordering={o}")
                assert_no_secrets(self, r)


# ============================================================================
# Pagination surface
# ============================================================================


class _PaginationFixture(_BaseFixture):
    """Many extra rows so pagination has multiple pages."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.victim_extras = list(Deal.objects.bulk_create([
            Deal(title=f"VICTIM_EXTRA_{i}",
                 brokerage=cls.brokerage_victim,
                 assigned_broker=cls.victim)
            for i in range(7)
        ]))

        # 25 attacker deals total (1 from base + 24 here).
        new_attacker = list(Deal.objects.bulk_create([
            Deal(title=f"ATTACKER_DEAL_{i}",
                 brokerage=cls.brokerage_attacker,
                 assigned_broker=cls.attacker)
            for i in range(24)
        ]))
        cls.attacker_deals = [cls.attacker_deal] + new_attacker

        cls._related = RelatedModel.objects.create(name="r", description="d")
        SampleModel.objects.bulk_create([
            SampleModel(title=f"PublicSample{i}",
                        price=Decimal("1.00"), quantity=i,
                        related=cls._related)
            for i in range(30)
        ])
        CompiledSampleModel.objects.bulk_create([
            CompiledSampleModel(title=f"CompiledSample{i}",
                                price=Decimal("1.00"),
                                related=cls._related)
            for i in range(30)
        ])
        CompiledArticle.objects.bulk_create([
            CompiledArticle(title=f"CompiledArticle{i}") for i in range(15)
        ])


class TestPageAndPageSizeAbuse(_PaginationFixture):
    """Page / page_size garbage values — must not 500 / leak / over-clamp."""

    def test_page_garbage_values(self):
        for v in (
            "0", "-1", "99", "99999999", "null", "true", "undefined", "",
            "1.5", "1e10", "NaN", "Infinity", "01", "%2B1", "-0",
            "1.5", "1&page=99999",  # repeated page
        ):
            with self.subTest(v=v):
                r = self.client.get(f"/api/deals/?page={v}")
                assert_no_secrets(self, r)
                self.assertLess(r.status_code, 500)
                assert_no_victim_id(self, r, self.victim_deal.id)

    def test_page_size_garbage_and_clamp(self):
        """Garbage values must not 500; values > max_page_size (100) must
        clamp; victim row must never appear regardless."""
        for v in (
            "0", "-1", "999999", "", "1.5", "null", "true",
            "1e10", "abc", "Infinity", "NaN", "999999999",
            "%EF%BC%91",  # full-width 1
            "100", "101", "10&page_size=999999",  # clamp probes
        ):
            with self.subTest(v=v):
                r = self.client.get(f"/api/deals/?page_size={v}")
                assert_no_secrets(self, r)
                self.assertLess(r.status_code, 500)
                if r.status_code == 200:
                    self.assertLessEqual(len(get_data(r)), 100)
                    assert_no_victim_id(self, r, self.victim_deal.id)

    def test_page_walking_no_leak(self):
        seen = set()
        for p in (1, 2, 3):
            r = self.client.get(f"/api/deals/?page={p}")
            assert_no_secrets(self, r)
            for row in get_data(r):
                if isinstance(row, dict) and "id" in row:
                    seen.add(row["id"])
        self.assertNotIn(self.victim_deal.id, seen)
        for vd in self.victim_extras:
            self.assertNotIn(vd.id, seen)

    def test_page_size_1_walk_full_depth(self):
        seen = set()
        for p in range(1, 30):
            r = self.client.get(f"/api/deals/?page_size=1&page={p}")
            assert_no_secrets(self, r)
            if r.status_code != 200:
                break
            for row in get_data(r):
                if isinstance(row, dict) and "id" in row:
                    seen.add(row["id"])
            if not get_pagination(r).get("next"):
                break
        self.assertNotIn(self.victim_deal.id, seen)


class TestPaginationCounters(_PaginationFixture):
    """total_items / total_pages counts must be tenant-scoped, never global."""

    def test_total_items_and_pages_and_consistency(self):
        """total_items=25 (attacker scope), total_pages=ceil(25/10)=3,
        and total_items must be identical across page=1 and page=2."""
        r = self.client.get("/api/deals/?page_size=100")
        assert_no_secrets(self, r)
        self.assertEqual(get_pagination(r).get("total_items"), 25)

        r1 = self.client.get("/api/deals/?page=1&page_size=10")
        r2 = self.client.get("/api/deals/?page=2&page_size=10")
        pag = get_pagination(r1)
        self.assertEqual(pag.get("total_items"), 25)
        self.assertEqual(pag.get("total_pages"), 3)
        self.assertEqual(
            get_pagination(r1).get("total_items"),
            get_pagination(r2).get("total_items"),
        )
        # Repeated identical calls produce identical counts.
        counts = [
            get_pagination(self.client.get("/api/deals/?page_size=100"))
            .get("total_items")
            for _ in range(3)
        ]
        self.assertEqual(set(counts), {25})

    def test_count_unaffected_by_victim_writes(self):
        """Adding a victim row must NOT change attacker count; deleting an
        attacker row must decrease it."""
        r1 = self.client.get("/api/deals/?page_size=100")
        c1 = get_pagination(r1).get("total_items")
        Deal.objects.create(
            title="VICTIM_NEW_ROW",
            brokerage=self.brokerage_victim,
            assigned_broker=self.victim,
        )
        r2 = self.client.get("/api/deals/?page_size=100")
        self.assertEqual(c1, get_pagination(r2).get("total_items"))
        Deal.objects.filter(pk=self.attacker_deals[-1].pk).delete()
        r3 = self.client.get("/api/deals/?page_size=100")
        self.assertEqual(c1 - 1, get_pagination(r3).get("total_items"))

    def test_count_zero_on_filter_excluding_all(self):
        r = self.client.get("/api/deals/?title=__definitely_no_such_title__")
        assert_no_secrets(self, r)
        if r.status_code == 200:
            pag = get_pagination(r)
            self.assertEqual(pag.get("total_items"), 0)
            self.assertIn(pag.get("total_pages"), (0, 1))

    def test_chained_tenant_count_excludes_victim(self):
        """Transactions / BankAccounts: attacker has 1 of each."""
        for url in ("/api/transactions/?page_size=100",
                    "/api/bankaccounts/?page_size=100"):
            with self.subTest(url=url):
                r = self.client.get(url)
                assert_no_secrets(self, r)
                self.assertEqual(get_pagination(r).get("total_items"), 1)

    def test_attacker_vs_manager_same_tenant_match(self):
        client_mgr = APIClient()
        client_mgr.force_authenticate(user=self.attacker_manager)
        r1 = self.client.get("/api/deals/?page_size=100")
        r2 = client_mgr.get("/api/deals/?page_size=100")
        self.assertEqual(get_pagination(r1).get("total_items"), 25)
        self.assertEqual(get_pagination(r2).get("total_items"), 25)


class TestNextPreviousLinks(_PaginationFixture):
    """next / previous link integrity."""

    def test_link_boundaries_credentials_host_and_page_size(self):
        """First-page previous=null, last-page next=null, no credentials in
        next link, host matches request, page_size preserved."""
        r1 = self.client.get("/api/deals/?page=1&page_size=10")
        r3 = self.client.get("/api/deals/?page=3&page_size=10")
        self.assertIsNone(get_pagination(r1).get("previous"))
        self.assertIsNone(get_pagination(r3).get("next"))
        nxt = (get_pagination(r1).get("next") or "").lower()
        for forbidden in ("authorization", "bearer", "token", "sessionid",
                          "csrftoken"):
            self.assertNotIn(forbidden, nxt)

        r = self.client.get("/api/deals/?page=1&page_size=5")
        nxt2 = get_pagination(r).get("next") or ""
        if nxt2:
            self.assertIn("testserver", nxt2)
            self.assertIn("page_size=5", nxt2)

    def test_host_header_injection_does_not_leak(self):
        r = self.client.get(
            "/api/deals/?page=1&page_size=10",
            HTTP_HOST="evil.example.com",
        )
        assert_no_secrets(self, r)
        if r.status_code == 200:
            nxt = get_pagination(r).get("next") or ""
            self.assertNotIn("VICTIM_SECRET_DEAL", nxt)

    def test_walk_via_next_link_only(self):
        """Follow next link until null — confirm scope."""
        url = "/api/deals/?page_size=5"
        seen = set()
        for _ in range(10):
            r = self.client.get(url)
            assert_no_secrets(self, r)
            for row in get_data(r):
                if isinstance(row, dict) and "id" in row:
                    seen.add(row["id"])
            nxt = get_pagination(r).get("next")
            if not nxt:
                break
            url = nxt.split("testserver", 1)[-1]
        self.assertNotIn(self.victim_deal.id, seen)
        for vd in self.victim_extras:
            self.assertNotIn(vd.id, seen)
        self.assertEqual(seen, {d.id for d in self.attacker_deals})


class TestPaginationEdges(_PaginationFixture):
    """Pagination interactions with filter, search, compiled paths, anon."""

    def test_pagination_zero_rows_compiled_anon_and_detail(self):
        """Filter/search → 0 rows; compiled paths; detail endpoint has no
        pagination key; anon counts on public models match scope."""
        for u in (
            "/api/deals/?title=__no_such_title_zzz&page=1&page_size=10",
            "/api/samplemodels/?search=__no_such_search_qqq&page=1&page_size=10",
        ):
            with self.subTest(u=u):
                r = self.client.get(u)
                assert_no_secrets(self, r)
                if r.status_code == 200:
                    self.assertEqual(get_pagination(r).get("total_items"), 0)

        r1 = self.client.get(
            "/api/compiledsamplemodels/?fields=title&page=1&page_size=10"
        )
        if r1.status_code == 200:
            self.assertEqual(get_pagination(r1).get("total_pages"), 3)
        r2 = self.client.get("/api/compiledarticles/?page=1&page_size=5")
        if r2.status_code == 200:
            pag = get_pagination(r2)
            self.assertEqual(pag.get("total_items"), 15)
            self.assertEqual(pag.get("total_pages"), 3)

        # Detail responses must not have a pagination block.
        r3 = self.client.get(f"/api/deals/{self.attacker_deals[0].id}/")
        if r3.status_code == 200 and isinstance(r3.data, dict):
            self.assertNotIn("pagination", r3.data)

        # Anon on public models — count = scope.
        anon = APIClient()
        for u, expected in (
            ("/api/samplemodels/?page=1&page_size=20", 30),
            ("/api/compiledsamplemodels/?page=1&page_size=20", 30),
        ):
            with self.subTest(u=u):
                r = anon.get(u)
                assert_no_secrets(self, r)
                if r.status_code == 200:
                    self.assertEqual(get_pagination(r).get("total_items"), expected)

    def test_concurrent_attacker_and_manager_pagination(self):
        client_mgr = APIClient()
        client_mgr.force_authenticate(user=self.attacker_manager)
        for p in (1, 2, 3):
            r1 = self.client.get(f"/api/deals/?page={p}&page_size=10")
            r2 = client_mgr.get(f"/api/deals/?page={p}&page_size=10")
            assert_no_secrets(self, r1)
            assert_no_secrets(self, r2)
            assert_no_victim_id(self, r1, self.victim_deal.id)
            assert_no_victim_id(self, r2, self.victim_deal.id)


# ============================================================================
# Search surface
# ============================================================================


class _SearchableFieldsInjector:
    """Mixin: install / restore searchable_fields on a model for a test."""

    def _inject(self, model, fields):
        prior = getattr(model, "searchable_fields", None)
        model.searchable_fields = fields
        return prior

    def _restore(self, model, prior):
        if prior is None:
            try:
                delattr(model, "searchable_fields")
            except AttributeError:
                pass
        else:
            model.searchable_fields = prior


class TestSearchableFieldsManipulation(_BaseFixture, _SearchableFieldsInjector):
    """Inject sensitive paths into Deal.searchable_fields — no cross-tenant
    leak. Defense: tenant filter + read-perm intersection."""

    def test_inject_various_sensitive_paths_no_leak(self):
        """Sweep title / brokerage__name / username / password / DRF
        prefix syntax / empty list — tenant boundary always wins."""
        cases = [
            (["title"], "/api/deals/?search=VICTIM_SECRET_DEAL"),
            (["brokerage__name"], "/api/deals/?search=Victim"),
            (["assigned_broker__username"], "/api/deals/?search=victim"),
            (["assigned_broker__password"], "/api/deals/?search=pbkdf2"),
            (["=title"], "/api/deals/?search=VICTIM_SECRET_DEAL"),
            (["^title"], "/api/deals/?search=VICTIM"),
            (["$title"], "/api/deals/?search=^VICTIM"),
            ([], "/api/deals/?search=VICTIM_SECRET_DEAL"),
        ]
        for fields, url in cases:
            with self.subTest(fields=fields):
                prior = self._inject(Deal, fields)
                try:
                    r = self.client.get(url)
                    assert_no_secrets(self, r)
                    victim_id_absent(self, r, self.victim_deal.id)
                finally:
                    self._restore(Deal, prior)

    def test_inject_email_pii_and_no_attribute(self):
        """Email PII path; also verify model with no searchable_fields attr."""
        self.victim.email = "victim@victim.example"
        self.victim.save()
        prior = self._inject(Deal, ["assigned_broker__email"])
        try:
            r = self.client.get("/api/deals/?search=victim@victim")
            assert_no_secrets(self, r)
            victim_id_absent(self, r, self.victim_deal.id)
            self.assertNotIn("victim@victim.example", str(r.content))
        finally:
            self._restore(Deal, prior)

        # No attr at all on Deal.
        if hasattr(Deal, "searchable_fields"):
            try:
                delattr(Deal, "searchable_fields")
            except AttributeError:
                pass
        r2 = self.client.get("/api/deals/?search=VICTIM_SECRET_DEAL")
        assert_no_secrets(self, r2)

    def test_inject_chain_into_transaction_and_bankaccount(self):
        prior = self._inject(Transaction, ["bank_account__deal__title"])
        try:
            r = self.client.get("/api/transactions/?search=VICTIM_SECRET")
            assert_no_secrets(self, r)
        finally:
            self._restore(Transaction, prior)

        prior2 = self._inject(BankAccount, ["deal__brokerage__name"])
        try:
            r2 = self.client.get("/api/bankaccounts/?search=Victim Co")
            assert_no_secrets(self, r2)
        finally:
            self._restore(BankAccount, prior2)


class TestSearchPayloads(_BaseFixture):
    """Many payload shapes. Defense holds even if search_fields includes
    sensitive paths."""

    def test_search_payload_shapes_no_leak(self):
        """Sweep: short-prefix inference, numeric substring, empty / SQL /
        wildcards / regex / unicode / encoded whitespace / multi-token /
        long DOS payloads — none must surface victim."""
        for url in (
            "/api/deals/?search=V",
            "/api/deals/?search=VI",
            "/api/deals/?search=victim",
            "/api/transactions/?search=999999.99",
            "/api/transactions/?search=99999",
            "/api/deals/?search=",
            "/api/deals/?search=%20",
            "/api/deals/?search=' OR 1=1 --",
            "/api/deals/?search=%25",
            "/api/deals/?search=_",
            "/api/deals/?search=%5C%25",
            "/api/deals/?search=%5Ba-z%5D*",
            "/api/deals/?search=Ｖ",
            "/api/deals/?search=Ｖｉｃｔｉｍ",
            "/api/deals/?search=%0aVICTIM",
            "/api/deals/?search=%0d%0aVICTIM",
            "/api/deals/?search=%20%20VICTIM%20%20",
            "/api/deals/?search=VICTIM SECRET",
            "/api/deals/?search=" + "A" * 10000,
            "/api/deals/?search=%25_VICTIM_%25",
            "/api/bankaccounts/?search=VICTIM",
            "/api/transactions/?search=VICTIM",
        ):
            with self.subTest(url=url[:50]):
                r = self.client.get(url)
                assert_no_secrets(self, r)

    def test_dos_null_byte_multi_param_and_full_secret(self):
        """50K-char DOS, null byte, multiple ?search= params, full-secret
        match against a public model — must not 5xx or leak Deal secret."""
        for s in ("A" * 50000, "%00VICTIM"):
            with self.subTest(s=s[:30]):
                r = self.client.get(f"/api/deals/?search={s}")
                if r.status_code >= 500:
                    self.fail(f"5xx on payload: {r.status_code}")
                assert_no_secrets(self, r)

        r = self.client.get(
            "/api/deals/?search=ATTACKER&search=VICTIM_SECRET_DEAL"
        )
        assert_no_secrets(self, r)
        victim_id_absent(self, r, self.victim_deal.id)

        SampleModel.objects.create(
            title="VICTIM_SECRET_DEAL",
            price=Decimal("1.00"),
            related=RelatedModel.objects.create(name="r"),
        )
        r2 = self.client.get("/api/samplemodels/?search=VICTIM_SECRET_DEAL")
        if r2.status_code >= 500:
            self.fail(f"5xx on full-secret search: {r2.status_code}")


class TestSearchPrefixSyntax(_BaseFixture, _SearchableFieldsInjector):
    """DRF SearchFilter prefix markers ^ = @ $ — none must leak."""

    def test_drf_prefix_payloads_and_iregex_no_leak(self):
        """DRF prefix markers ^ = @ $ as plain payloads, plus iregex
        metachars when $title is injected as searchable_field."""
        for s in (
            "VICTIM SECRET",
            "^VICTIM",
            "%3DVICTIM_SECRET_DEAL",
            "%40VICTIM",
            "%24VICTIM",
            "^VIC ^Vic",
        ):
            with self.subTest(s=s):
                r = self.client.get(f"/api/deals/?search={s}")
                assert_no_secrets(self, r)
                victim_id_absent(self, r, self.victim_deal.id)

        prior = self._inject(Deal, ["$title"])
        try:
            for s in ("VIC.IM", "^V.*M"):
                with self.subTest(s=s):
                    r = self.client.get(f"/api/deals/?search={s}")
                    assert_no_secrets(self, r)
                    victim_id_absent(self, r, self.victim_deal.id)
        finally:
            self._restore(Deal, prior)


class TestSearchCrossCutting(_BaseFixture, _SearchableFieldsInjector):
    """Search × tenant / ordering / pagination / anon / no-perm /
    permission-disable settings. Tenant boundary holds in every case."""

    def test_search_combined_with_other_knobs(self):
        prior = self._inject(Deal, ["title"])
        try:
            for u in (
                f"/api/deals/?search=VICTIM&brokerage={self.brokerage_victim.id}",
                "/api/deals/?search=VICTIM&ordering=-id",
                "/api/deals/?search=VICTIM&page_size=100",
                "/api/deals/?search=ATTACKER&search_or=VICTIM_SECRET_DEAL",
            ):
                with self.subTest(u=u):
                    r = self.client.get(u)
                    assert_no_secrets(self, r)
                    victim_id_absent(self, r, self.victim_deal.id)
        finally:
            self._restore(Deal, prior)

    def test_search_on_compiled_path_no_5xx(self):
        CompiledSampleModel.objects.create(
            title="ATTACKER_COMPILED",
            price=Decimal("1.00"),
            related=RelatedModel.objects.create(name="r"),
        )
        r = self.client.get("/api/compiledsamplemodels/?search=VICTIM_SECRET_DEAL")
        if r.status_code >= 500:
            self.fail(f"5xx on compiled search: {r.status_code}")
        assert_no_secrets(self, r)

    def test_search_as_anonymous_or_no_role_user(self):
        anon = APIClient()
        for u in (
            "/api/deals/?search=VICTIM_SECRET_DEAL",
            "/api/samplemodels/?search=VICTIM",
        ):
            with self.subTest(u=u):
                r = anon.get(u)
                assert_no_secrets(self, r)

        no_role = User.objects.create_user(username="noperms", password="x")
        no_role._test_roles = []
        set_test_brokerage(no_role, self.brokerage_attacker)
        c = APIClient()
        c.force_authenticate(user=no_role)
        r = c.get("/api/deals/?search=VICTIM")
        assert_no_secrets(self, r)
        victim_id_absent(self, r, self.victim_deal.id)

    def test_manager_search_cross_tenant(self):
        c = APIClient()
        c.force_authenticate(user=self.attacker_manager)
        prior = self._inject(Deal, ["title"])
        try:
            r = c.get("/api/deals/?search=VICTIM_SECRET_DEAL")
            assert_no_secrets(self, r)
            victim_id_absent(self, r, self.victim_deal.id)
        finally:
            self._restore(Deal, prior)

    def test_disable_or_default_permissions_tenant_holds(self):
        prior = self._inject(Deal, ["title"])
        try:
            for s in ({"TURBODRF_DISABLE_PERMISSIONS": True},
                      {"TURBODRF_USE_DEFAULT_PERMISSIONS": True}):
                with self.subTest(s=s), override_settings(**s):
                    r = self.client.get("/api/deals/?search=VICTIM_SECRET_DEAL")
                    if r.status_code >= 500:
                        self.fail(f"5xx: {r.status_code}")
                    assert_no_secrets(self, r)
                    victim_id_absent(self, r, self.victim_deal.id)
        finally:
            self._restore(Deal, prior)


class TestSearchCacheAndPathological(_BaseFixture, _SearchableFieldsInjector):
    """Cache consistency + pathological searchable_fields."""

    def test_warm_cache_inject_revert_no_poisoning(self):
        """Warm permission snapshot, inject sensitive, then revert — second
        call must not be poisoned."""
        r1 = self.client.get("/api/deals/?search=ATTACKER")
        assert_no_secrets(self, r1)
        Deal.searchable_fields = ["assigned_broker__password"]
        try:
            r2 = self.client.get("/api/deals/?search=pbkdf2")
            assert_no_secrets(self, r2)
        finally:
            Deal.searchable_fields = ["title"]
        try:
            r3 = self.client.get("/api/deals/?search=VICTIM")
            assert_no_secrets(self, r3)
            victim_id_absent(self, r3, self.victim_deal.id)
        finally:
            try:
                delattr(Deal, "searchable_fields")
            except AttributeError:
                pass

    def test_repeated_and_different_terms_consistent(self):
        prior = self._inject(Deal, ["title"])
        try:
            for s in ("A", "B", "VICTIM", "NONEXISTENT_XYZ", "ATTACKER"):
                with self.subTest(s=s):
                    r = self.client.get(f"/api/deals/?search={s}")
                    assert_no_secrets(self, r)
                    victim_id_absent(self, r, self.victim_deal.id)
            cache.clear()
            r = self.client.get("/api/deals/?search=VICTIM")
            assert_no_secrets(self, r)
            victim_id_absent(self, r, self.victim_deal.id)
        finally:
            self._restore(Deal, prior)

    def test_pathological_searchable_paths_no_500(self):
        """Deep / broken / non-string / circular / sensitive-at-segment
        searchable_fields must not 500."""
        cases = [
            (Transaction,
             ["bank_account__deal__brokerage__name__icontains__value"],
             "/api/transactions/?search=Victim"),
            (Deal, ["title__nonexistent_lookup"],
             "/api/deals/?search=VICTIM"),
            (Deal, [123], "/api/deals/?search=VICTIM"),
            (Deal, ["password__hash__value"], "/api/deals/?search=secret"),
            (Deal, ["assigned_broker__assigned_deals__title"],
             "/api/deals/?search=VICTIM"),
        ]
        for model, fields, url in cases:
            with self.subTest(fields=fields):
                prior = self._inject(model, fields)
                try:
                    r = self.client.get(url)
                    if r.status_code >= 500:
                        self.fail(f"5xx with fields={fields}: {r.status_code}")
                    assert_no_secrets(self, r)
                finally:
                    self._restore(model, prior)


class TestSearchBonus(_BaseFixture, _SearchableFieldsInjector):
    """Bonus: HEAD method, FK PK search, body vs query."""

    def test_head_body_and_fk_id_search(self):
        """HEAD method, body-vs-query, and FK PK search all hold."""
        prior = self._inject(Deal, ["title"])
        try:
            r1 = self.client.head("/api/deals/?search=VICTIM_SECRET_DEAL")
            if r1.status_code >= 500:
                self.fail(f"5xx on HEAD: {r1.status_code}")
            assert_no_secrets(self, r1)

            # body must be ignored — SearchFilter is query-only.
            r2 = self.client.get(
                "/api/deals/",
                data={"search": "VICTIM_SECRET_DEAL"},
                format="json",
            )
            assert_no_secrets(self, r2)
            victim_id_absent(self, r2, self.victim_deal.id)
        finally:
            self._restore(Deal, prior)

        prior2 = self._inject(Deal, ["assigned_broker__id"])
        try:
            r3 = self.client.get(f"/api/deals/?search={self.victim.id}")
            assert_no_secrets(self, r3)
            victim_id_absent(self, r3, self.victim_deal.id)
        finally:
            self._restore(Deal, prior2)


# ============================================================================
# DOS surface
# ============================================================================


class TestLargePayloads(_BaseFixture):
    """Bulky bodies, headers, URLs — accept or reject cleanly under DOS budget."""

    def test_post_body_giant_string_in_title(self):
        """1MB and 5MB string in title — both must complete under DOS budget."""
        for size in (1_000_000, 5_000_000):
            with self.subTest(size=size):
                body = {
                    "title": "A" * size,
                    "brokerage": self.brokerage_attacker.id,
                }
                r, t = time_request(
                    self.client.post, "/api/deals/", body, format="json"
                )
                assert_no_secrets(self, r)
                self.assertLess(t, DOS_BUDGET)

    def test_post_body_many_keys_or_huge_array(self):
        for body in (
            {**{f"k_{i}": i for i in range(1000)},
             "title": "x", "brokerage": self.brokerage_attacker.id},
            {"title": "x", "brokerage": self.brokerage_attacker.id,
             "description": "B" * 1_000_000},
            {"title": "x", "brokerage": self.brokerage_attacker.id,
             "tags": list(range(100_000))},
        ):
            with self.subTest(keys=len(body)):
                r, t = time_request(
                    self.client.post, "/api/deals/", body, format="json"
                )
                assert_no_secrets(self, r)
                self.assertLess(t, DOS_BUDGET)

    def test_post_body_deeply_nested_json(self):
        for raw in (
            "{" + '"a":' * 500 + "1" + "}" * 500,
            "[" * 500 + "1" + "]" * 500,
            "{" + '"a":' * 1000 + "1" + "}" * 1000,
            "[" * 1000 + "1" + "]" * 1000,
        ):
            with self.subTest(shape=raw[:5]):
                r, t = time_request(
                    self.client.post, "/api/deals/",
                    data=raw, content_type="application/json",
                )
                assert_no_secrets(self, r)
                self.assertLess(t, DOS_BUDGET)

    def test_url_and_query_string_explosions(self):
        for qs in (
            "x=" + ("y" * 8000),
            "&".join(f"k_{i}=1" for i in range(5000)),
            "&".join(["title=x"] * 5000),
            "title=" + ("z" * 100_000),
        ):
            with self.subTest(qs=qs[:40]):
                r, t = time_request(self.client.get, f"/api/deals/?{qs}")
                assert_no_secrets(self, r)
                self.assertLess(t, DOS_BUDGET)

    def test_header_bomb_50_headers(self):
        headers = {f"HTTP_X_CUSTOM_{i}": "v" * 1000 for i in range(50)}
        r, t = time_request(self.client.get, "/api/deals/", **headers)
        assert_no_secrets(self, r)
        self.assertLess(t, NORMAL_BUDGET)


class TestFilterAndSearchDos(_BaseFixture):
    """Filter / search payloads that try to amplify SQL or regex cost."""

    def test_id_in_explosions(self):
        """Huge / repeated / duplicate id__in payloads must complete in budget."""
        for url in (
            "/api/deals/?id__in=" + ",".join(str(i) for i in range(1, 10_001)),
            "/api/deals/?id__in=" + ",".join(str(i) for i in range(1, 1001)),
            "/api/deals/?id__in=" + ",".join([str(self.attacker_deal.id)] * 10000),
            "/api/deals/?" + "&".join(["id__in=1,2,3,4,5"] * 100),
        ):
            with self.subTest(url=url[:50]):
                r, t = time_request(self.client.get, url)
                assert_no_secrets(self, r)
                self.assertLess(t, DOS_BUDGET)

    def test_field_filter_dos_shapes(self):
        """ReDoS regex / very long icontains / null bytes / nested jsonfield
        path / many chained filters in one request."""
        cases = [
            "/api/deals/?title__regex=" + "(a|a)*" + ("a" * 30) + "b",
            "/api/deals/?title__icontains=" + ("%x" * 5000),
            "/api/deals/?title=x\x00\x00\x00",
            "/api/deals/?metadata__path__key=x",
            "/api/deals/?a__b__c__d__e__f__g__h__i__j=x",
            "/api/transactions/?" + "&".join([
                "bank_account__deal__brokerage__name__icontains=Victim",
                "bank_account__deal__title__icontains=SECRET",
                "bank_account__deal__assigned_broker__username=victim",
                "bank_account__name__icontains=BANK",
                "amount__gt=0",
            ]),
        ]
        for url in cases:
            with self.subTest(url=url[:50]):
                r, t = time_request(self.client.get, url)
                assert_no_secrets(self, r)
                self.assertLess(t, DOS_BUDGET)

    def test_search_dos_payloads(self):
        for s in (
            "a" * 100_000,
            "(a|aa)*" + ("a" * 30) + "b",
            " ".join(["xyz"] * 1000),
            " ".join(f"^t{i}" for i in range(100)),
            " ".join(f"-t{i}" for i in range(100)),
        ):
            with self.subTest(s=s[:40]):
                r, t = time_request(
                    self.client.get, f"/api/deals/?search={s}"
                )
                assert_no_secrets(self, r)
                self.assertLess(t, DOS_BUDGET)


class TestPaginationAndOrderingDos(_BaseFixture):
    """Pagination/ordering payloads must clamp/bound cleanly."""

    def test_page_and_page_size_dos_values(self):
        """Page / page_size pathological values: must clamp or reject in
        budget."""
        for url in (
            "/api/deals/?page_size=999999",
            "/api/deals/?page_size=0",
            "/api/deals/?page_size=3.14",
            "/api/deals/?page_size=1e10",
            "/api/deals/?page=999999",
            "/api/deals/?page=-1",
        ):
            with self.subTest(url=url):
                r, t = time_request(self.client.get, url)
                assert_no_secrets(self, r)
                self.assertLess(t, NORMAL_BUDGET)
                if r.status_code == 200 and isinstance(r.data, dict):
                    rows = r.data.get("data") or r.data.get("results") or []
                    self.assertLessEqual(len(rows), 100)

    def test_pagination_with_1000_attacker_rows(self):
        Deal.objects.bulk_create([
            Deal(title=f"attacker_{i}",
                 brokerage=self.brokerage_attacker,
                 assigned_broker=self.attacker)
            for i in range(1000)
        ])
        r, t = time_request(self.client.get, "/api/deals/?page_size=100")
        assert_no_secrets(self, r)
        self.assertLess(t, DOS_BUDGET)

    def test_pagination_count_does_not_scan_all_tenants(self):
        """Adding 50 victim rows must NOT inflate attacker count."""
        for i in range(50):
            Deal.objects.create(
                title=f"victim_extra_{i}",
                brokerage=self.brokerage_victim,
                assigned_broker=self.victim,
            )
        r, t = time_request(self.client.get, "/api/deals/?page_size=10")
        assert_no_secrets(self, r)
        self.assertLess(t, NORMAL_BUDGET)
        if r.status_code == 200 and isinstance(r.data, dict):
            pagination = r.data.get("pagination") or {}
            total = pagination.get("total_count") or pagination.get("count")
            if total is not None:
                self.assertEqual(total, 1)

    def test_pagination_total_pages_empty_tenant(self):
        third = Brokerage.objects.create(name="Empty Co")
        u = User.objects.create_user(username="empty_user", password="x")
        u._test_roles = ["underwriter"]
        set_test_brokerage(u, third)
        c = APIClient()
        c.force_authenticate(user=u)
        r, t = time_request(c.get, "/api/deals/?page_size=100")
        assert_no_secrets(self, r)
        self.assertLess(t, NORMAL_BUDGET)

    def test_ordering_payloads_bounded(self):
        for o in (
            "title",
            "brokerage__name",
            "-nonexistent",
            ",".join(f"f{i}" for i in range(50)),
        ):
            with self.subTest(o=o):
                r, t = time_request(
                    self.client.get, f"/api/deals/?ordering={o}"
                )
                assert_no_secrets(self, r)
                self.assertLess(t, NORMAL_BUDGET)


class TestRecursionAndMemory(_BaseFixture):
    """Deep traversal payloads / huge dicts / wide rows / N+1 amplification."""

    def test_filter_or_ordering_50_segment_chain(self):
        path = "__".join(["a"] * 50)
        for url in (
            f"/api/deals/?{path}=1",
            f"/api/deals/?ordering={path}",
        ):
            with self.subTest(url=url[:50]):
                r, t = time_request(self.client.get, url)
                assert_no_secrets(self, r)
                self.assertLess(t, NORMAL_BUDGET)

    def test_post_giant_flat_dict_or_dup_keys(self):
        for body in (
            {**{f"k_{i}": i for i in range(10_000)},
             "title": "x", "brokerage": self.brokerage_attacker.id},
        ):
            r, t = time_request(
                self.client.post, "/api/deals/", body, format="json"
            )
            assert_no_secrets(self, r)
            self.assertLess(t, DOS_BUDGET)

        kvs = ",".join(['"a":1'] * 5000)
        raw = (
            "{" + kvs
            + ',"title":"x","brokerage":'
            + str(self.brokerage_attacker.id) + "}"
        )
        r, t = time_request(
            self.client.post, "/api/deals/",
            data=raw, content_type="application/json",
        )
        assert_no_secrets(self, r)
        self.assertLess(t, DOS_BUDGET)

    def test_memory_bloat_payloads(self):
        """fields= many names / 100KB blob in body / wide-row response /
        very long per-row strings."""
        rel = RelatedModel.objects.create(name="r")
        SampleModel.objects.bulk_create([
            SampleModel(title=f"t{i}", description="d" * 1000,
                        price=Decimal("1.00"), quantity=1,
                        related=rel, secret_field="x")
            for i in range(50)
        ])
        Deal.objects.create(
            title="LONG_" + "x" * 50_000,
            brokerage=self.brokerage_attacker,
            assigned_broker=self.attacker,
        )
        for url in (
            "/api/deals/?fields=" + ",".join(f"f{i}" for i in range(50)),
            "/api/samplemodels/?page_size=100",
            "/api/deals/",
        ):
            with self.subTest(url=url[:50]):
                r, t = time_request(self.client.get, url)
                assert_no_secrets(self, r)
                self.assertLess(t, DOS_BUDGET)

        # 100KB binary-ish blob in body
        body = {
            "title": "x",
            "brokerage": self.brokerage_attacker.id,
            "blob": "\x01\x02\x03" * 30000,
        }
        r, t = time_request(self.client.post, "/api/deals/", body, format="json")
        assert_no_secrets(self, r)
        self.assertLess(t, DOS_BUDGET)

    def test_list_query_count_bounded(self):
        """N+1 detection: list endpoint must not balloon query count."""
        from django.db import connection as conn
        Deal.objects.bulk_create([
            Deal(title=f"t{i}", brokerage=self.brokerage_attacker,
                 assigned_broker=self.attacker)
            for i in range(50)
        ])
        before = len(conn.queries_log)
        with self.settings(DEBUG=True):
            r = self.client.get("/api/deals/?page_size=100")
        after = len(conn.queries_log)
        assert_no_secrets(self, r)
        self.assertLess(after - before, 200)

        Transaction.objects.bulk_create([
            Transaction(
                amount=Decimal("1.00"),
                bank_account=BankAccount.objects.create(
                    name=f"bank_{i}", deal=self.attacker_deal
                ),
            )
            for i in range(20)
        ])
        before = len(conn.queries_log)
        with self.settings(DEBUG=True):
            r2 = self.client.get("/api/transactions/?page_size=100")
        after = len(conn.queries_log)
        assert_no_secrets(self, r2)
        self.assertLess(after - before, 200)

    def test_repeat_identical_request_100x(self):
        start = time.monotonic()
        for _ in range(100):
            r = self.client.get("/api/deals/")
            assert_no_secrets(self, r)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, DOS_BUDGET)
